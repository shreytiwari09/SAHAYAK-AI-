"""Gemini with a model cascade: always use the best model that is available right now.

Each call walks the list from best to worst and returns the first model that answers. A model
that fails (503, 429, timeout, network error) goes on a short cooldown, so the next calls skip
straight past it instead of waiting on it again; when the cooldown ends it is tried first again,
so the cascade climbs back up on its own. Models the account cannot use (404/403) get a long
cooldown. Errors that would fail on every model (a bad request, a bad key) are raised at once.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.config import get_settings
from backend.net import ipv4_client

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# Best first. Names that this account cannot use are skipped automatically (404 -> long cooldown).
DEFAULT_CASCADE = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    # Same key, separate free quota. Slow (10-20 s) and its answers are weaker, so it is the last
    # resort: better than escalating every case when every Gemini model is overloaded.
    "gemma-4-26b-a4b-it",
]

# Models that need longer than the default per-attempt timeout.
SLOW_MODELS = {"gemma-4-26b-a4b-it": 40.0}

COOLDOWN_TRANSIENT = 30.0  # 5xx, timeouts, network errors
COOLDOWN_RATE_LIMIT = 60.0  # 429 (a Retry-After header overrides this)
COOLDOWN_UNAVAILABLE = 3600.0  # 404 / 403: this account cannot use the model
MAX_COOLDOWN = 3600.0


class LLMError(Exception):
    """Base class for cascade failures."""


class LLMRequestError(LLMError):
    """The request itself is bad (or the key is): trying another model would not help."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


class CascadeExhausted(LLMError):
    """Every model failed or the deadline passed. `attempts` says what happened to each."""

    def __init__(self, attempts: list["Attempt"]):
        trail = "; ".join(f"{a.model}: {a.outcome}" for a in attempts) or "no model tried"
        super().__init__(f"no Gemini model answered ({trail})")
        self.attempts = attempts


@dataclass
class Attempt:
    model: str
    outcome: str  # "ok", "HTTP 503", "timeout", "network error", ...
    seconds: float


@dataclass
class LLMResult:
    model: str  # the model that actually answered
    raw: dict[str, Any]
    seconds: float
    attempts: list[Attempt] = field(default_factory=list)  # includes the failures before it

    @property
    def fell_back(self) -> bool:
        return len(self.attempts) > 1

    @property
    def text(self) -> str:
        try:
            parts = self.raw["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return ""
        return "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")
        )

    @property
    def function_calls(self) -> list[dict[str, Any]]:
        try:
            parts = self.raw["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return []
        return [
            p["functionCall"]
            for p in parts
            if isinstance(p, dict) and "functionCall" in p and not p.get("thought")
        ]

    @property
    def usage(self) -> dict[str, int]:
        u = self.raw.get("usageMetadata", {})
        return {
            "prompt_tokens": u.get("promptTokenCount", 0),
            "output_tokens": u.get("candidatesTokenCount", 0),
            "total_tokens": u.get("totalTokenCount", 0),
        }


@dataclass
class ModelStats:
    ok: int = 0
    failed: int = 0
    last_outcome: str = ""


class GeminiCascade:
    def __init__(
        self,
        api_key: str | None = None,
        models: list[str] | None = None,
        *,
        attempt_timeout: float | None = None,
        deadline: float | None = None,
        passes: int = 2,
        retry_pause: float = 2.0,
        max_concurrency: int = 3,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        configured = [m.strip() for m in settings.model_cascade.split(",") if m.strip()]
        self.models = models or configured or list(DEFAULT_CASCADE)
        self.attempt_timeout = attempt_timeout or settings.llm_attempt_timeout_seconds
        self.deadline = deadline or settings.llm_deadline_seconds
        self.client = client or ipv4_client(self.attempt_timeout)
        self.cache: dict[str, dict[str, Any]] = {}  # temperature-0 answers, to save free quota
        self.clock = clock
        self.sleep = sleep
        self.passes = passes
        self.retry_pause = retry_pause
        self.gate = asyncio.Semaphore(max_concurrency)  # free tiers punish bursts with 429s
        self.down_until: dict[str, float] = {}
        self.stats: dict[str, ModelStats] = {m: ModelStats() for m in self.models}

    @staticmethod
    def _cache_key(body: dict[str, Any]) -> str | None:
        """Only deterministic (temperature 0) requests are safe to reuse."""
        if (body.get("generationConfig") or {}).get("temperature") != 0:
            return None
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    async def aclose(self) -> None:
        await self.client.aclose()

    # ----- health -----

    def available(self) -> list[str]:
        """Models not on cooldown, best first."""
        now = self.clock()
        return [m for m in self.models if self.down_until.get(m, 0.0) <= now]

    def health(self) -> dict[str, dict[str, Any]]:
        now = self.clock()
        return {
            m: {
                "up": self.down_until.get(m, 0.0) <= now,
                "retry_in_seconds": max(0.0, round(self.down_until.get(m, 0.0) - now, 1)),
                **vars(self.stats[m]),
            }
            for m in self.models
        }

    def _mark_down(self, model: str, seconds: float, outcome: str) -> None:
        self.down_until[model] = self.clock() + min(seconds, MAX_COOLDOWN)
        self.stats[model].failed += 1
        self.stats[model].last_outcome = outcome

    async def warm_up(self) -> dict[str, dict[str, Any]]:
        """Probe every model at once with a tiny request and mark the failing ones as down.

        Run this at startup so the first real call goes straight to a model that is answering,
        instead of discovering outages one timeout at a time.
        """
        probe = simple_request("Reply with one word: ok", maxOutputTokens=8)

        async def check(model: str) -> None:
            try:
                outcome, raw, cooldown = await self._try(model, probe)
            except LLMRequestError as e:
                outcome, raw, cooldown = f"HTTP {e.status}", None, COOLDOWN_UNAVAILABLE
            if raw is None:
                self._mark_down(model, cooldown, outcome)
            else:
                self.stats[model].last_outcome = "ok (warm-up)"
                self.down_until.pop(model, None)

        await asyncio.gather(*(check(m) for m in self.models))
        return self.health()

    # ----- calling -----

    async def generate(self, body: dict[str, Any]) -> LLMResult:
        """`body` is a Gemini generateContent request: contents, systemInstruction, tools, ..."""
        start = self.clock()
        attempts: list[Attempt] = []
        cache_key = self._cache_key(body)
        if cache_key and cache_key in self.cache:
            return LLMResult("cache", self.cache[cache_key], 0.0, [Attempt("cache", "ok", 0.0)])
        for pass_number in range(self.passes):
            candidates = self.available()
            if pass_number or not candidates:
                # A second pass, or everything cooling down: better to retry than to refuse.
                candidates = list(self.models)
                if pass_number:
                    await self.sleep(self.retry_pause)
            for model in candidates:
                if self.clock() - start >= self.deadline:
                    attempts.append(Attempt(model, "deadline reached", 0.0))
                    raise CascadeExhausted(attempts)
                t0 = self.clock()
                async with self.gate:
                    outcome, raw, cooldown = await self._try(model, body)
                took = self.clock() - t0
                attempts.append(Attempt(model, outcome, round(took, 2)))
                if raw is not None:
                    self.stats[model].ok += 1
                    self.stats[model].last_outcome = "ok"
                    self.down_until.pop(model, None)
                    if cache_key:
                        self.cache[cache_key] = raw
                    return LLMResult(model, raw, round(self.clock() - start, 2), attempts)
                self._mark_down(model, cooldown, outcome)
        raise CascadeExhausted(attempts)

    async def _try(
        self, model: str, body: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None, float]:
        """One attempt. Returns (outcome, raw response or None, cooldown if it failed)."""
        try:
            resp = await self.client.post(
                f"{API_ROOT}/{model}:generateContent",
                json=body,
                headers={"x-goog-api-key": self.api_key},
                timeout=SLOW_MODELS.get(model, self.attempt_timeout),
            )
        except httpx.TimeoutException:
            return "timeout", None, COOLDOWN_TRANSIENT
        except httpx.TransportError as e:
            return f"network error ({type(e).__name__})", None, COOLDOWN_TRANSIENT

        status = resp.status_code
        if status == 200:
            try:
                return "ok", resp.json(), 0.0
            except ValueError:
                return "bad JSON in 200 response", None, COOLDOWN_TRANSIENT
        if status == 429:
            retry_after = resp.headers.get("retry-after", "")
            wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else None
            return "HTTP 429", None, wait or COOLDOWN_RATE_LIMIT
        if status in (403, 404):
            return (
                f"HTTP {status} (model not available to this account)",
                None,
                COOLDOWN_UNAVAILABLE,
            )
        if status >= 500:
            return f"HTTP {status}", None, COOLDOWN_TRANSIENT
        raise LLMRequestError(status, resp.text)  # 400, 401, ...: same on every model


def simple_request(prompt: str, system: str | None = None, **generation: Any) -> dict[str, Any]:
    """Convenience for a plain text prompt."""
    body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if generation:
        body["generationConfig"] = generation
    return body
