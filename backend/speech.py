"""Speech: voice notes in, voice replies out (design decision D9).

Sarvam does the speech work: Saaras v4 in `codemix` mode keeps Hinglish readable (English words in
Latin letters inside Hindi), Bulbul speaks. If Sarvam fails, transcription falls back to Gemini
(audio understanding); if speech synthesis fails the merchant simply gets the text, which is always
sent first. Synthesised audio is cached by text, so repeated messages never spend credits twice.
"""

import base64
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from backend.net import ipv4_client

SARVAM_STT = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS = "https://api.sarvam.ai/text-to-speech"
LANG_CODES = {
    "hi": "hi-IN", "en": "en-IN", "ta": "ta-IN", "te": "te-IN", "kn": "kn-IN", "ml": "ml-IN",
    "mr": "mr-IN", "gu": "gu-IN", "bn": "bn-IN", "pa": "pa-IN", "od": "od-IN",
}  # fmt: skip
MAX_TTS_CHARS = 450  # Sarvam's per-request limit is 500


class SpeechError(Exception):
    pass


@dataclass
class Transcript:
    text: str
    language: str | None  # ISO 639-1, or None when unknown
    provider: str


class Speech(Protocol):
    async def transcribe(self, audio: bytes, mime: str = "audio/wav") -> Transcript: ...

    async def synthesize(self, text: str, lang: str) -> bytes: ...


def speakable(text: str) -> str:
    """The part of a message worth reading aloud: no links, within the engine's length limit."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_TTS_CHARS:
        return text
    cut = text[:MAX_TTS_CHARS]
    end = max(cut.rfind("."), cut.rfind("?"), cut.rfind("!"))
    return cut[: end + 1] if end > 100 else cut


@dataclass
class SpeechStats:
    transcriptions: int = 0
    syntheses: int = 0
    cache_hits: int = 0
    characters_spoken: int = 0
    fallbacks: int = 0
    errors: list[str] = field(default_factory=list)


class SarvamSpeech:
    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        *,
        stt_model: str = "saaras:v4",
        tts_model: str = "bulbul:v4-flash",
        speaker: str = "",
        cache_dir: str | Path | None = None,
        fallback_transcriber: Any = None,
    ):
        self.api_key, self.stt_model, self.tts_model, self.speaker = (
            api_key,
            stt_model,
            tts_model,
            speaker,
        )
        self.client = client or ipv4_client(60)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.memory: dict[str, bytes] = {}
        self.fallback = fallback_transcriber
        self.stats = SpeechStats()

    @property
    def headers(self) -> dict[str, str]:
        return {"api-subscription-key": self.api_key}

    async def aclose(self) -> None:
        await self.client.aclose()

    # ----- speech to text -----

    async def transcribe(self, audio: bytes, mime: str = "audio/wav") -> Transcript:
        ext = {"audio/wav": "wav", "audio/ogg": "ogg", "audio/mpeg": "mp3"}.get(
            mime.split(";")[0], "wav"
        )
        try:
            resp = await self.client.post(
                SARVAM_STT,
                headers=self.headers,
                files={"file": (f"voice.{ext}", audio, mime.split(";")[0])},
                data={"model": self.stt_model, "mode": "codemix"},
            )
            if resp.is_error:
                raise SpeechError(f"Sarvam STT HTTP {resp.status_code}: {resp.text[:150]}")
            body = resp.json()
            text = (body.get("transcript") or "").strip()
            if not text:
                raise SpeechError("Sarvam STT returned an empty transcript")
            self.stats.transcriptions += 1
            code = (body.get("language_code") or "")[:2].lower() or None
            return Transcript(text, code, "sarvam")
        except (httpx.HTTPError, SpeechError, ValueError) as e:
            self.stats.errors.append(f"stt: {type(e).__name__}: {str(e)[:120]}")
            if self.fallback is None:
                raise SpeechError(str(e)) from e
            self.stats.fallbacks += 1
            return await self.fallback.transcribe(audio, mime)

    # ----- text to speech -----

    def _key(self, text: str, lang: str) -> str:
        raw = f"{self.tts_model}|{self.speaker}|{lang}|{text}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    async def synthesize(self, text: str, lang: str) -> bytes:
        text = speakable(text)
        if not text:
            raise SpeechError("nothing to say")
        key = self._key(text, lang)
        if key in self.memory:
            self.stats.cache_hits += 1
            return self.memory[key]
        if self.cache_dir and (path := self.cache_dir / f"{key}.wav").exists():
            self.stats.cache_hits += 1
            self.memory[key] = path.read_bytes()
            return self.memory[key]

        payload: dict[str, Any] = {
            "text": text,
            "target_language_code": LANG_CODES.get(lang, "hi-IN"),
            "model": self.tts_model,
        }
        if self.speaker:
            payload["speaker"] = self.speaker
        try:
            resp = await self.client.post(SARVAM_TTS, headers=self.headers, json=payload)
            if resp.is_error:
                raise SpeechError(f"Sarvam TTS HTTP {resp.status_code}: {resp.text[:150]}")
            audio = base64.b64decode(resp.json()["audios"][0])
        except (httpx.HTTPError, SpeechError, KeyError, IndexError, ValueError) as e:
            self.stats.errors.append(f"tts: {type(e).__name__}: {str(e)[:120]}")
            raise SpeechError(str(e)) from e
        self.stats.syntheses += 1
        self.stats.characters_spoken += len(text)
        self.memory[key] = audio
        if self.cache_dir:
            (self.cache_dir / f"{key}.wav").write_bytes(audio)
        return audio


class GeminiTranscriber:
    """Fallback: Gemini hears the audio directly (WAV, MP3, Ogg/Opus)."""

    PROMPT = (
        "Transcribe this voice note exactly as spoken. It is from an Indian small-business "
        "merchant and may mix Hindi or another Indian language with English. Write Hindi in "
        "Devanagari and keep English words in Latin letters. Reply with the transcript only."
    )

    def __init__(self, cascade: Any):
        self.cascade = cascade

    async def transcribe(self, audio: bytes, mime: str = "audio/wav") -> Transcript:
        body = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": self.PROMPT},
                    {"inlineData": {"mimeType": mime.split(";")[0],
                                    "data": base64.b64encode(audio).decode()}},
                ],
            }],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
        }  # fmt: skip
        try:
            text = (await self.cascade.generate(body)).text.strip()
        except Exception as e:
            raise SpeechError(f"Gemini transcription failed: {type(e).__name__}") from e
        if not text:
            raise SpeechError("Gemini returned an empty transcript")
        return Transcript(text, None, "gemini")
