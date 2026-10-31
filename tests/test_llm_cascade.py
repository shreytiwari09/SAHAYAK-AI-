"""The cascade is tested with simulated outages (httpx.MockTransport): no network, no key."""

import httpx
import pytest

from backend.llm.cascade import (
    COOLDOWN_TRANSIENT,
    COOLDOWN_UNAVAILABLE,
    CascadeExhausted,
    GeminiCascade,
    LLMRequestError,
    simple_request,
)

MODELS = ["best", "mid", "lite"]


class Fake:
    """Scripted Gemini: per-model behaviour, a call log, and a controllable clock."""

    def __init__(self, behaviour: dict[str, object]):
        self.behaviour = behaviour  # model -> status int | Exception | "ok"
        self.calls: list[str] = []
        self.now = 1000.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/models/")[1].split(":")[0]
        self.calls.append(model)
        b = self.behaviour.get(model, "ok")
        if isinstance(b, Exception):
            raise b
        if b == "ok":
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": f"hello from {model}"}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 3,
                        "totalTokenCount": 8,
                    },
                },
            )
        return httpx.Response(
            int(b), json={"error": {"message": "x"}}, headers=getattr(self, "headers", {})
        )

    def cascade(self, **kw) -> GeminiCascade:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

        async def no_sleep(_seconds):
            self.slept = getattr(self, "slept", 0) + 1

        kw.setdefault("passes", 1)
        kw.setdefault("sleep", no_sleep)
        return GeminiCascade("key", MODELS, client=client, clock=lambda: self.now, **kw)


REQ = simple_request("hi")


async def test_uses_the_best_model_when_all_are_up():
    f = Fake({})
    r = await f.cascade().generate(REQ)
    assert r.model == "best" and r.text == "hello from best" and not r.fell_back
    assert f.calls == ["best"]
    assert r.usage == {"prompt_tokens": 5, "output_tokens": 3, "total_tokens": 8}


async def test_falls_down_the_list_on_503_and_timeout():
    f = Fake({"best": 503, "mid": httpx.ReadTimeout("slow")})
    r = await f.cascade().generate(REQ)
    assert r.model == "lite" and r.fell_back
    assert [(a.model, a.outcome) for a in r.attempts] == [
        ("best", "HTTP 503"), ("mid", "timeout"), ("lite", "ok"),
    ]  # fmt: skip


async def test_network_error_also_falls_back():
    f = Fake({"best": httpx.ConnectError("no route")})
    r = await f.cascade().generate(REQ)
    assert r.model == "mid" and "network error" in r.attempts[0].outcome


async def test_rate_limit_falls_back_and_honours_retry_after():
    f = Fake({"best": 429})
    f.headers = {"retry-after": "120"}
    c = f.cascade()
    assert (await c.generate(REQ)).model == "mid"
    assert c.health()["best"]["retry_in_seconds"] == 120.0


async def test_failed_model_is_skipped_on_later_calls():
    f = Fake({"best": 503})
    c = f.cascade()
    await c.generate(REQ)
    f.calls.clear()
    r = await c.generate(REQ)
    assert f.calls == ["mid"]  # best is cooling down: no wasted attempt
    assert r.model == "mid" and not r.fell_back
    assert c.available() == ["mid", "lite"]


async def test_climbs_back_up_when_the_cooldown_ends_and_model_recovers():
    f = Fake({"best": 503})
    c = f.cascade()
    await c.generate(REQ)
    f.behaviour["best"] = "ok"  # the outage ends
    f.now += COOLDOWN_TRANSIENT + 1
    f.calls.clear()
    r = await c.generate(REQ)
    assert r.model == "best" and f.calls == ["best"]
    assert c.health()["best"]["up"] is True


async def test_still_down_after_cooldown_drops_again_without_failing_the_call():
    f = Fake({"best": 503})
    c = f.cascade()
    await c.generate(REQ)
    f.now += COOLDOWN_TRANSIENT + 1
    f.calls.clear()
    r = await c.generate(REQ)
    assert f.calls == ["best", "mid"] and r.model == "mid"


async def test_model_the_account_cannot_use_gets_a_long_cooldown():
    f = Fake({"best": 404})
    c = f.cascade()
    await c.generate(REQ)
    f.now += COOLDOWN_TRANSIENT * 10
    assert "best" not in c.available()
    f.now += COOLDOWN_UNAVAILABLE
    assert "best" in c.available()


async def test_bad_request_is_raised_not_retried_on_other_models():
    f = Fake({"best": 400})
    with pytest.raises(LLMRequestError) as e:
        await f.cascade().generate(REQ)
    assert e.value.status == 400 and f.calls == ["best"]


async def test_all_down_raises_with_the_full_trail():
    f = Fake({"best": 503, "mid": httpx.ReadTimeout("t"), "lite": 429})
    with pytest.raises(CascadeExhausted) as e:
        await f.cascade().generate(REQ)
    assert [a.model for a in e.value.attempts] == MODELS
    assert "best: HTTP 503" in str(e.value) and "mid: timeout" in str(e.value)


async def test_when_everything_is_cooling_down_it_still_tries_rather_than_refusing():
    f = Fake({"best": 503, "mid": 503, "lite": 503})
    c = f.cascade()
    with pytest.raises(CascadeExhausted):
        await c.generate(REQ)
    f.behaviour["mid"] = "ok"  # recovers while every model is still on cooldown
    f.calls.clear()
    r = await c.generate(REQ)
    assert r.model == "mid" and f.calls == ["best", "mid"]


async def test_deadline_stops_a_long_walk():
    f = Fake({"best": 503, "mid": 503})
    c = f.cascade(deadline=5)
    real_handler = f.handler

    def slow(request):
        f.now += 6  # each attempt burns the whole budget
        return real_handler(request)

    c.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    with pytest.raises(CascadeExhausted) as e:
        await c.generate(REQ)
    assert f.calls == ["best"] and e.value.attempts[-1].outcome == "deadline reached"


async def test_stats_and_health_report():
    f = Fake({"best": 503})
    c = f.cascade()
    await c.generate(REQ)
    await c.generate(REQ)
    h = c.health()
    assert h["best"]["failed"] == 1 and h["best"]["up"] is False
    assert h["mid"]["ok"] == 2 and h["mid"]["up"] is True
    assert h["lite"]["ok"] == 0


async def test_function_calls_are_exposed():
    def handler(request):
        part = {"functionCall": {"name": "send_kyc_link", "args": {"merchant_id": "M1001"}}}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [part]}}]})

    c = GeminiCascade(
        "key", MODELS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    r = await c.generate(REQ)
    assert r.function_calls == [{"name": "send_kyc_link", "args": {"merchant_id": "M1001"}}]
    assert r.text == ""


async def test_api_key_goes_in_a_header_not_the_url():
    seen = {}

    def handler(request):
        seen["url"], seen["key"] = str(request.url), request.headers.get("x-goog-api-key")
        return httpx.Response(200, json={"candidates": []})

    c = GeminiCascade(
        "secret-key", MODELS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await c.generate(REQ)
    assert seen["key"] == "secret-key" and "secret-key" not in seen["url"]


async def test_warm_up_marks_failing_models_so_the_first_call_is_fast():
    f = Fake({"best": 503, "mid": httpx.ReadTimeout("slow")})
    c = f.cascade()
    health = await c.warm_up()
    assert [m for m, h in health.items() if h["up"]] == ["lite"]
    f.calls.clear()
    r = await c.generate(REQ)
    assert r.model == "lite" and f.calls == ["lite"] and not r.fell_back


async def test_warm_up_with_everything_healthy_changes_nothing():
    f = Fake({})
    c = f.cascade()
    health = await c.warm_up()
    assert all(h["up"] for h in health.values())
    assert (await c.generate(REQ)).model == "best"


async def test_a_second_pass_recovers_from_a_blip_that_took_every_model_down():
    f = Fake({"best": 503, "mid": 503, "lite": 503})
    c = f.cascade(passes=2)
    real = f.handler
    seen = []

    def flaky(request):
        seen.append(1)
        if len(seen) > 3:  # the outage clears after the first pass
            f.behaviour = {}
        return real(request)

    c.client = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    r = await c.generate(REQ)
    assert r.model == "best" and len(r.attempts) == 4 and f.slept == 1


async def test_second_pass_still_fails_cleanly_when_everything_stays_down():
    f = Fake({"best": 503, "mid": 503, "lite": 503})
    with pytest.raises(CascadeExhausted) as e:
        await f.cascade(passes=2).generate(REQ)
    assert len(e.value.attempts) == 6


async def test_concurrent_requests_are_capped():
    import asyncio

    active, peak = 0, 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"candidates": []})

    c = GeminiCascade(
        "key",
        MODELS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_concurrency=2,
    )
    await asyncio.gather(*[c.generate(REQ) for _ in range(8)])
    assert peak <= 2


async def test_reasoning_parts_are_not_the_answer():
    def handler(request):
        parts = [{"text": "thinking about {braces}", "thought": True}, {"text": '{"ok": true}'}]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}}]})

    c = GeminiCascade("k", MODELS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await c.generate(REQ)).text == '{"ok": true}'


async def test_temperature_zero_requests_are_answered_once_and_creative_ones_are_not():
    f = Fake({})
    c = f.cascade()
    exact = simple_request("classify this", temperature=0)
    first, second = await c.generate(exact), await c.generate(exact)
    assert f.calls == ["best"] and second.model == "cache" and second.text == first.text
    varied = simple_request("write this", temperature=0.7)
    await c.generate(varied)
    await c.generate(varied)
    assert f.calls == ["best", "best", "best"]


async def test_default_cascade_ends_with_gemma_and_gives_it_a_longer_timeout():
    from backend.llm.cascade import DEFAULT_CASCADE, SLOW_MODELS

    assert DEFAULT_CASCADE[-1] == "gemma-4-26b-a4b-it" and SLOW_MODELS["gemma-4-26b-a4b-it"] > 8


# ---------- several keys ----------


def test_settings_gather_every_key_without_blanks_or_repeats():
    from backend.config import Settings

    s = Settings(gemini_api_key="a, b,,a", gemini_api_key_old="c", _env_file=None)
    assert s.gemini_keys == ["a", "b", "c"]
    assert Settings(_env_file=None).gemini_keys == []


async def test_pool_moves_to_the_next_key_when_one_runs_out_of_quota():
    from backend.llm.cascade import GeminiPool

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-goog-api-key"]
        seen.append(key)
        if key == "spent":
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
        )

    async def no_sleep(_):
        return None

    pool = GeminiPool(
        ["spent", "fresh"], models=["m1", "m2"], passes=1, sleep=no_sleep,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )  # fmt: skip
    assert (await pool.generate(simple_request("hi"))).text == "hello"
    assert seen == ["spent", "spent", "fresh"]  # the spent key fails on both models first

    seen.clear()
    await pool.generate(simple_request("again"))
    assert seen == ["fresh"]  # the spent key is on cooldown, so it is not asked again
    health = pool.health()
    assert health["m1"]["up"] and health["m1"]["failed"] == 1 and health["m1"]["ok"] == 2


async def test_pool_raises_when_every_key_is_exhausted():
    from backend.llm.cascade import GeminiPool

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "quota"}})

    async def no_sleep(_):
        return None

    pool = GeminiPool(
        ["a", "b"], models=["m1"], passes=1, sleep=no_sleep,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )  # fmt: skip
    with pytest.raises(CascadeExhausted):
        await pool.generate(simple_request("hi"))
