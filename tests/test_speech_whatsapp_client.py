"""Speech and the WhatsApp client, against faked HTTP (no network, no credentials)."""

import base64
import io
import json
import math
import struct
import wave

import httpx
import pytest

from backend.audio import ffmpeg_available, to_ogg_opus, to_wav16k
from backend.channels.whatsapp import (
    OutboxWhatsApp,
    WhatsAppAPI,
    WhatsAppError,
    make_payload,
    parse_webhook,
    verify_signature,
)
from backend.speech import (
    GeminiTranscriber,
    SarvamSpeech,
    SpeechError,
    speakable,
)


def sine_wav(seconds: float = 0.5, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(n)
            )
        )
    return buf.getvalue()


# ---------- audio ----------


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
async def test_audio_round_trip_wav_to_opus_and_back():
    ogg = await to_ogg_opus(sine_wav())
    assert ogg[:4] == b"OggS"
    wav = await to_wav16k(ogg)
    assert wav[:4] == b"RIFF"
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)


# ---------- Sarvam ----------


def sarvam(handler, **kw):
    return SarvamSpeech("key", httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


async def test_transcribe_reads_codemix_text_and_language():
    seen = {}

    def handler(request):
        seen["key"] = request.headers["api-subscription-key"]
        seen["body"] = request.content
        return httpx.Response(
            200, json={"transcript": "मेरा settlement नहीं आया", "language_code": "hi-IN"}
        )

    t = await sarvam(handler).transcribe(sine_wav(), "audio/wav")
    assert t.text == "मेरा settlement नहीं आया" and t.language == "hi" and t.provider == "sarvam"
    assert seen["key"] == "key" and b"codemix" in seen["body"] and b"saaras:v4" in seen["body"]


async def test_transcribe_falls_back_to_gemini_when_sarvam_fails():
    class FakeCascade:
        async def generate(self, body):
            assert body["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "audio/wav"

            class R:
                text = "mera settlement nahi aaya"

            return R()

    s = sarvam(
        lambda r: httpx.Response(500, text="down"),
        fallback_transcriber=GeminiTranscriber(FakeCascade()),
    )
    t = await s.transcribe(sine_wav(), "audio/wav")
    assert t.provider == "gemini" and t.text == "mera settlement nahi aaya"
    assert s.stats.fallbacks == 1 and "stt" in s.stats.errors[0]


async def test_transcribe_without_a_fallback_raises_a_clear_error():
    with pytest.raises(SpeechError):
        await sarvam(lambda r: httpx.Response(429, text="slow down")).transcribe(sine_wav())
    with pytest.raises(SpeechError):  # an empty transcript is a failure too
        await sarvam(lambda r: httpx.Response(200, json={"transcript": "  "})).transcribe(
            sine_wav()
        )


async def test_synthesize_sends_language_and_model_and_decodes_audio():
    sent = {}
    audio = sine_wav()

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"audios": [base64.b64encode(audio).decode()]})

    out = await sarvam(handler, speaker="anushka").synthesize("Aapka settlement aa gaya hai.", "ta")
    assert out == audio
    assert sent["target_language_code"] == "ta-IN" and sent["model"] == "bulbul:v4-flash"
    assert sent["speaker"] == "anushka"


async def test_synthesis_is_cached_in_memory_and_on_disk_so_credits_are_spent_once(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"audios": [base64.b64encode(b"RIFFfake").decode()]})

    s = sarvam(handler, cache_dir=tmp_path)
    await s.synthesize("Namaste ji", "hi")
    await s.synthesize("Namaste ji", "hi")
    assert (
        len(calls) == 1
        and s.stats.cache_hits == 1
        and s.stats.characters_spoken == len("Namaste ji")
    )
    again = sarvam(handler, cache_dir=tmp_path)  # a new process still finds the file
    assert await again.synthesize("Namaste ji", "hi") == b"RIFFfake" and len(calls) == 1
    await s.synthesize("Namaste ji", "ta")  # a different language is a different clip
    assert len(calls) == 2


async def test_synthesis_failure_raises_for_the_caller_to_fall_back_to_text():
    with pytest.raises(SpeechError):
        await sarvam(lambda r: httpx.Response(500, text="x")).synthesize("hello", "hi")


def test_speakable_drops_links_and_respects_the_engine_limit():
    assert (
        speakable("Complete KYC here: https://x.io/a/b?c=1 thanks") == "Complete KYC here: thanks"
    )
    long = ("Yeh ek lamba vakya hai. " * 60).strip()
    out = speakable(long)
    assert len(out) <= 450 and out.endswith(".")
    assert speakable("   ") == ""


# ---------- WhatsApp client ----------


def api(handler):
    return WhatsAppAPI("tok", "123", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_send_text_builds_the_graph_api_request():
    seen = {}

    def handler(request):
        seen.update(
            url=str(request.url),
            auth=request.headers["authorization"],
            body=json.loads(request.content),
        )
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    assert await api(handler).send_text("919800000001", "Namaste") == "wamid.X"
    assert (
        seen["url"] == "https://graph.facebook.com/v23.0/123/messages"
        and seen["auth"] == "Bearer tok"
    )
    assert seen["body"] == {"messaging_product": "whatsapp", "to": "919800000001", "type": "text",
                            "text": {"body": "Namaste"}}  # fmt: skip


async def test_buttons_respect_whatsapp_limits():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "w"}]})

    a = api(handler)
    await a.send_buttons(
        "9198", "Offer?", [("HAAN", "Haan"), ("NAHI", "A very long button title indeed")]
    )
    buttons = seen["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["HAAN", "NAHI"]
    assert all(len(b["reply"]["title"]) <= 20 for b in buttons)
    with pytest.raises(ValueError):
        await a.send_buttons("9198", "x", [(str(i), "b") for i in range(4)])


async def test_send_audio_uploads_then_sends_by_media_id():
    steps = []

    def handler(request):
        steps.append((request.method, request.url.path))
        if request.url.path.endswith("/media"):
            assert b"audio/ogg" in request.content and b"OggS" in request.content
            return httpx.Response(200, json={"id": "MEDIA9"})
        body = json.loads(request.content)
        assert body["type"] == "audio" and body["audio"] == {"id": "MEDIA9"}
        return httpx.Response(200, json={"messages": [{"id": "wamid.A"}]})

    assert await api(handler).send_audio("9198", b"OggS-fake") == "wamid.A"
    assert steps == [("POST", "/v23.0/123/media"), ("POST", "/v23.0/123/messages")]


async def test_download_media_resolves_the_url_then_fetches_with_auth():
    def handler(request):
        if request.url.path == "/v23.0/MID":
            return httpx.Response(
                200, json={"url": "https://lookaside.example/blob", "mime_type": "audio/ogg"}
            )
        assert (
            request.url.host == "lookaside.example"
            and request.headers["authorization"] == "Bearer tok"
        )
        return httpx.Response(200, content=b"voicebytes")

    assert await api(handler).download_media("MID") == (b"voicebytes", "audio/ogg")


async def test_errors_carry_meta_codes_including_the_24_hour_window():
    def handler(request):
        return httpx.Response(
            400, json={"error": {"code": 131047, "message": "Re-engagement message"}}
        )

    with pytest.raises(WhatsAppError) as e:
        await api(handler).send_text("9198", "hi")
    assert e.value.code == 131047 and e.value.window_closed


# ---------- webhook parsing ----------


def test_parse_text_audio_button_and_ignore_statuses():
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "919800000001", "profile": {"name": "Ramesh"}}],
        "messages": [
            {"from": "919800000001", "id": "m1", "timestamp": "1700000000", "type": "text", "text": {"body": "hi"}},
            {"from": "919800000001", "id": "m2", "type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg; codecs=opus", "voice": True}},
            {"from": "919800000001", "id": "m3", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "HAAN", "title": "Haan"}}},
            {"from": "919800000001", "id": "m4", "type": "image", "image": {"id": "I1"}},
        ],
    }}, {"value": {"statuses": [{"id": "x", "status": "delivered"}]}}]}]}  # fmt: skip
    msgs = parse_webhook(payload)
    assert [(m.kind, m.id) for m in msgs] == [
        ("text", "m1"),
        ("audio", "m2"),
        ("button", "m3"),
        ("unsupported", "m4"),
    ]
    assert msgs[0].name == "Ramesh" and msgs[0].timestamp == 1700000000
    assert msgs[1].media_id == "A1" and msgs[1].mime.startswith("audio/ogg")
    assert msgs[2].button_id == "HAAN"
    assert (
        parse_webhook({}) == []
        and parse_webhook({"entry": [{"changes": [{"value": {"statuses": []}}]}]}) == []
    )


def test_make_payload_round_trips_through_the_parser():
    [m] = parse_webhook(make_payload("919800000001", type="text", text={"body": "namaste"}))
    assert (m.sender, m.kind, m.text) == ("919800000001", "text", "namaste")


def test_signature_check():
    import hashlib
    import hmac

    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, good)
    assert not verify_signature("secret", body, good[:-1] + "0")
    assert not verify_signature("secret", body, None) and not verify_signature(
        "secret", body, "md5=abc"
    )
    assert not verify_signature("other", body, good)


async def test_outbox_records_what_would_be_sent():
    box = OutboxWhatsApp()
    await box.send_text("9198", "hello")
    await box.send_buttons("9198", "offer", [("HAAN", "Haan")])
    await box.send_audio("9198", b"OggSxx")
    assert [m["type"] for m in box.sent] == ["text", "buttons", "audio"] and box.sent[2][
        "magic"
    ] == "OggS"
    media = box.add_media(b"abc", "audio/ogg")
    assert await box.download_media(media) == (b"abc", "audio/ogg")
