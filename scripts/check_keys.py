"""Phase 0 key check: confirms the Gemini and Sarvam keys work before any agent code relies on them.

Usage:  uv run python scripts/check_keys.py

- Gemini: lists the models this key can use and confirms MODEL_MAIN / MODEL_FAST exist.
- Sarvam: synthesises a short Hindi phrase (TTS), then transcribes that audio back (STT).
  Model names are read from SARVAM_TTS_MODEL / SARVAM_STT_MODEL because they were not
  verified against Sarvam's live docs when this script was written.
"""

import base64
import os
import sys
from pathlib import Path

import httpx

GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
TEST_PHRASE = "Namaste, aapka settlement aaj shaam tak aapke bank mein aa jayega."

# This machine's network has no working IPv6 route: Python tries the IPv6 address first and
# waits out the whole timeout before falling back, which looks like a hang. Binding the local
# address to 0.0.0.0 makes httpx connect over IPv4 only.
HTTP = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"), timeout=30)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def check_gemini() -> bool:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("[gemini] GEMINI_API_KEY is not set")
        return False

    names: list[str] = []
    params = {"key": key, "pageSize": 1000}
    while True:
        resp = HTTP.get(GEMINI_MODELS_URL, params=params)
        if resp.status_code != 200:
            print(f"[gemini] list models failed: HTTP {resp.status_code} {resp.text[:300]}")
            return False
        body = resp.json()
        names += [m["name"].removeprefix("models/") for m in body.get("models", [])]
        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    print(f"[gemini] key OK, {len(names)} models visible")
    ok = True
    defaults = {"MODEL_MAIN": "gemini-3.8-flash", "MODEL_FAST": "gemini-3.5-flash-lite"}
    for var, default in defaults.items():
        wanted = os.environ.get(var) or default
        if wanted in names:
            print(f"[gemini] {var}={wanted} is available")
        else:
            ok = False
            close = [n for n in names if "flash" in n]
            print(f"[gemini] {var}={wanted!r} NOT found. Flash models visible: {close}")
    print("[gemini] free-tier limits are per project: check https://aistudio.google.com/rate-limit")
    return ok


def check_sarvam() -> bool:
    key = os.environ.get("SARVAM_API_KEY")
    if not key:
        print("[sarvam] SARVAM_API_KEY is not set")
        return False
    headers = {"api-subscription-key": key}

    tts_model = os.environ.get("SARVAM_TTS_MODEL") or "bulbul:v4-flash"
    resp = HTTP.post(
        SARVAM_TTS_URL,
        headers=headers,
        json={"text": TEST_PHRASE, "target_language_code": "hi-IN", "model": tts_model},
    )
    if resp.status_code != 200:
        print(f"[sarvam] TTS ({tts_model}) failed: HTTP {resp.status_code} {resp.text[:300]}")
        return False
    audios = resp.json().get("audios") or []
    if not audios:
        print(f"[sarvam] TTS returned no audio: {resp.text[:300]}")
        return False
    audio = base64.b64decode(audios[0])
    print(f"[sarvam] TTS OK ({tts_model}), {len(audio)} bytes of audio")

    stt_model = os.environ.get("SARVAM_STT_MODEL") or "saaras:v4"
    resp = HTTP.post(
        SARVAM_STT_URL,
        headers=headers,
        files={"file": ("check.wav", audio, "audio/wav")},
        data={"model": stt_model, "mode": "codemix"},
    )
    if resp.status_code != 200:
        print(f"[sarvam] STT ({stt_model}) failed: HTTP {resp.status_code} {resp.text[:300]}")
        return False
    print(f"[sarvam] STT OK ({stt_model}): {resp.json().get('transcript')!r}")
    print("[sarvam] check remaining credits and their unit on the Sarvam dashboard")
    return True


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    results = {"gemini": check_gemini(), "sarvam": check_sarvam()}
    print("\nSummary:", ", ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
