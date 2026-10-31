"""WhatsApp conversations end to end: webhook payload in, verified fix and replies out."""

import hashlib
import hmac
import json
import re

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.agent.runtime import build_agent
from backend.api import ApiContext, create_app
from backend.audit.log import AuditLog
from backend.channels.whatsapp import OutboxWhatsApp, WhatsAppError, make_payload
from backend.channels.whatsapp_channel import LinkStore, WhatsAppChannel, said
from backend.pii.masker import PiiMasker
from backend.service import CaseService
from backend.speech import SpeechError
from backend.tools.client import PaytmTools
from eval.fake_llm import FakeLLM
from mock_paytm import clock
from mock_paytm.db import make_engine
from tests.test_speech_whatsapp_client import sine_wav

PHONE = "919800000001"


class FakeSpeech:
    def __init__(self, text="Mera settlement 2 din se nahi aaya", fail_stt=False, fail_tts=False):
        self.text, self.fail_stt, self.fail_tts = text, fail_stt, fail_tts
        self.spoken: list[tuple[str, str]] = []

    async def transcribe(self, audio, mime="audio/wav"):
        if self.fail_stt:
            raise SpeechError("stt down")
        assert audio[:4] == b"RIFF"  # the channel hands STT clean WAV, not raw Ogg/Opus
        from backend.speech import Transcript

        return Transcript(self.text, "hi", "fake")

    async def synthesize(self, text, lang):
        if self.fail_tts:
            raise SpeechError("tts down")
        self.spoken.append((text, lang))
        return sine_wav()


@pytest.fixture
async def rig(engine, tmp_path):
    from mock_paytm.app import app as mock_app

    mock = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock")
    tools = PaytmTools(mock)
    audit = AuditLog(make_engine(f"sqlite:///{tmp_path / 'audit.db'}"))
    ticks = {"t": 0.0}

    async def sleep(s):
        clock.advance(s)
        ticks["t"] += s

    llm = FakeLLM()
    graph, deps = build_agent(tools, llm, audit, InMemorySaver(), masker=PiiMasker(use_presidio=False),
                              sleep=sleep, monotonic=lambda: ticks["t"])  # fmt: skip
    service = CaseService(graph, audit, tools)

    class Rig:
        pass

    r = Rig()
    r.mock, r.tools, r.audit, r.service, r.deps = mock, tools, audit, service, deps
    r.box, r.speech, r.now = OutboxWhatsApp(), FakeSpeech(), {"t": 1_000_000.0}
    r.llm = llm
    r.links = LinkStore(tmp_path / "links.json")
    r.links.link(PHONE, "M1001")

    def build_channel(**kw):
        ch = WhatsAppChannel(
            r.box, service, r.links, kw.pop("speech", r.speech), clock=lambda: r.now["t"], **kw
        )
        deps.on_message = ch.deliver
        r.channel = ch  # the helpers below always use the channel most recently built
        return ch

    r.channel = build_channel()
    r.build_channel = build_channel

    async def send(text=None, sender=PHONE, **message):
        message = message or {"type": "text", "text": {"body": text}}
        return await r.channel.handle_payload(make_payload(sender, **message))

    async def settle(phone=PHONE):
        return await service.wait(r.channel.conversations[phone].thread_id)

    async def tap_link():
        """The merchant taps the KYC link in their browser."""
        link = next(m["body"] for m in r.box.sent if "/sim/kyc/" in m.get("body", ""))
        url = re.search(r"/sim/kyc/\w+/complete", link).group(0)
        assert (await mock.get(url)).status_code == 200

    r.send, r.settle, r.tap_link = send, settle, tap_link
    r.texts = lambda: [m["body"] for m in r.box.sent if m["type"] in ("text", "buttons")]
    yield r
    await service.aclose()
    await mock.aclose()


# ---------- the whole conversation ----------


async def test_whatsapp_hero_flow_from_message_to_consented_offer(rig):
    assert await rig.send("Mera settlement 2 din se nahi aaya") == ["started"]
    case = await rig.settle()
    assert case.pause["waiting_for"] == "kyc_link"
    first = rig.box.sent[0]
    assert (
        first["type"] == "text" and "AI assistant" in first["body"] and "/sim/kyc/" in first["body"]
    )

    await rig.tap_link()  # no chat reply needed: the systems show the link was completed
    assert await rig.service.poll_pauses() == [case.thread_id]
    await rig.settle()
    offer = rig.box.sent[-1]
    assert offer["type"] == "buttons" and [b["id"] for b in offer["buttons"]] == ["HAAN", "NAHI"]
    assert any("UTR" in t for t in rig.texts())  # proof of the verified credit reached the phone

    assert await rig.send(type="interactive", interactive={"type": "button_reply",
                          "button_reply": {"id": "HAAN", "title": "Haan"}}) == ["offer_haan"]  # fmt: skip
    case = await rig.settle()
    assert case.summary["outcome"] == "RESOLVED_VERIFIED"
    assert any(a["action"] == "create_lead" and a["executed"] for a in case.summary["actions"])
    assert "[offer_thanks]" in rig.texts()[-1]  # written by the model, with a template as fallback


async def test_typing_done_without_doing_it_does_not_fake_the_fix(rig):
    await rig.send("settlement nahi aaya")
    await rig.settle()
    assert await rig.send("ho gaya") == ["merchant_done"]
    case = await rig.settle()
    assert case.summary["outcome"] == "WAITING_MERCHANT"  # the systems say it is not done


# ---------- voice ----------


async def test_voice_note_in_voice_reply_out(rig):
    media = rig.box.add_media(sine_wav(), "audio/ogg")
    handled = await rig.send(
        type="audio", audio={"id": media, "mime_type": "audio/ogg", "voice": True}
    )
    assert handled == ["started"]
    await rig.settle()
    kinds = [m["type"] for m in rig.box.sent]
    assert kinds[:2] == ["text", "audio"]  # the text always goes first, then the voice note
    assert rig.box.sent[1]["magic"] == "OggS"
    assert rig.speech.spoken and rig.speech.spoken[0][1] == "hi"
    events = [e.event for e in rig.audit.events(merchant_id="M1001")]
    assert "VOICE_TRANSCRIBED" in events


async def test_text_in_text_out_no_voice(rig):
    await rig.send("settlement nahi aaya")
    await rig.settle()
    assert [m["type"] for m in rig.box.sent] == ["text"] and rig.speech.spoken == []


async def test_a_failed_transcription_asks_for_a_resend(rig):
    ch = rig.build_channel(speech=FakeSpeech(fail_stt=True))
    media = rig.box.add_media(sine_wav(), "audio/ogg")
    handled = await ch.handle_payload(make_payload(PHONE, type="audio", audio={"id": media}))
    assert handled == ["unreadable"] and not rig.service.cases
    assert "voice note" in rig.box.sent[0]["body"]
    assert "VOICE_FAILED" in [e.event for e in rig.audit.events(merchant_id="M1001")]


async def test_a_failed_voice_reply_still_leaves_the_text_delivered(rig):
    ch = rig.build_channel(speech=FakeSpeech(fail_tts=True))
    media = rig.box.add_media(sine_wav(), "audio/ogg")
    await ch.handle_payload(make_payload(PHONE, type="audio", audio={"id": media}))
    await rig.settle()
    assert [m["type"] for m in rig.box.sent] == ["text"]
    assert "VOICE_REPLY_FAILED" in [e.event for e in rig.audit.events(merchant_id="M1001")]


async def test_no_speech_engine_means_a_polite_text_only_reply(rig):
    ch = rig.build_channel(speech=None)
    ch.speech = None
    media = rig.box.add_media(sine_wav(), "audio/ogg")
    assert await ch.handle_payload(make_payload(PHONE, type="audio", audio={"id": media})) == [
        "unreadable"
    ]
    assert "type" in rig.box.sent[0]["body"].lower()


# ---------- routing and robustness ----------


async def test_unlinked_numbers_are_told_so_and_a_default_merchant_can_be_set(rig):
    assert await rig.send("hi", sender="919811111111") == ["unlinked"] and not rig.service.cases
    ch = rig.build_channel(default_merchant="M1006")
    assert await ch.handle_payload(
        make_payload("919811111111", type="text", text={"body": "parso ka paisa nahi aaya"})
    ) == ["started"]
    assert next(iter(rig.service.cases.values())).merchant_id == "M1006"


async def test_meta_retries_do_not_start_a_second_case(rig):
    payload = make_payload(PHONE, type="text", text={"body": "settlement nahi aaya"})
    assert await rig.channel.handle_payload(payload) == ["started"]
    assert await rig.channel.handle_payload(payload) == ["duplicate"]
    assert len(rig.service.cases) == 1


async def test_unsupported_message_types_get_a_hint(rig):
    assert await rig.send(type="image", image={"id": "I1"}) == ["unreadable"]
    assert "text" in rig.box.sent[0]["body"]


async def test_asking_for_a_human_starts_a_fresh_escalating_case_even_mid_conversation(rig):
    await rig.send("settlement nahi aaya")
    await rig.settle()
    assert await rig.send("HUMAN") == ["started"]
    case = rig.service.get(rig.channel.conversations[PHONE].thread_id)
    await rig.service.wait(case.thread_id)
    assert case.summary["outcome"] == "ESCALATED" and len(rig.service.cases) == 2


# ---------- the 24-hour window ----------


async def test_after_24_hours_proactive_messages_are_held_back_not_failed(rig):
    await rig.send("settlement nahi aaya")
    await rig.settle()
    rig.now["t"] += 25 * 3600
    await rig.tools.http.post(
        "/faults/heartbeat_loss", json={"merchant_id": "M1001", "device_fault": "POWER"}
    )
    sent_before = len(rig.box.sent)
    started = await rig.service.scan_and_start(["M1001"])
    assert started and started[0]["causes"] == ["DEVICE_OFFLINE"]
    await rig.service.wait(started[0]["thread_id"])
    assert len(rig.box.sent) == sent_before
    skipped = [
        e
        for e in rig.audit.events(thread_id=started[0]["thread_id"])
        if e.event == "DELIVERY_SKIPPED"
    ]
    assert skipped and "24-hour" in skipped[0].result["why"]


async def test_proactive_messages_reach_a_merchant_who_messaged_recently(rig):
    await rig.send("namaste")  # opens the window (no problem to fix, just a reply)
    await rig.service.wait(rig.channel.conversations[PHONE].thread_id)
    await rig.tools.http.post(
        "/faults/heartbeat_loss", json={"merchant_id": "M1001", "device_fault": "POWER"}
    )
    started = await rig.service.scan_and_start(["M1001"])
    await rig.service.wait(started[0]["thread_id"])
    assert any("[power_cycle]" in t for t in rig.texts())  # the stand-in model's wording of the ask


async def test_a_window_error_from_meta_is_recorded_not_raised(rig):
    rig.box.fail_with = WhatsAppError(131047, "Re-engagement message", 400)
    await rig.send("settlement nahi aaya")
    await rig.settle()  # the case still progresses
    failed = [e for e in rig.audit.events(merchant_id="M1001") if e.event == "DELIVERY_FAILED"]
    assert failed and failed[0].result["window_closed"] is True


# ---------- pause watcher ----------


async def test_the_watcher_resumes_a_device_case_when_the_device_comes_back(rig):
    rig.links.link("919800000002", "M1002")
    await rig.send("Soundbox band hai", sender="919800000002")
    case = await rig.settle("919800000002")
    assert case.pause["waiting_for"] == "power_cycle"
    assert await rig.service.poll_pauses() == []  # not yet
    await rig.mock.post(f"/sim/devices/{case.pause['device_id']}/power-cycle")
    assert await rig.service.poll_pauses() == [case.thread_id]
    done = await rig.settle("919800000002")
    assert done.summary["outcome"] == "RESOLVED_VERIFIED"


def test_yes_no_matching_handles_hinglish_and_punctuation():
    assert (
        said("Haan!", {"haan"})
        and said("ji haan, kar do", {"ji haan"})
        and said(" HO GAYA. ", {"ho gaya"})
    )
    assert not said("nahi haan", {"haan"})  # a mixed message is not a clear yes
    assert not said("", {"haan"}) and not said(None, {"haan"})


# ---------- HTTP routes ----------


@pytest.fixture
async def http(rig):
    ctx = ApiContext(rig.service, rig.deps.gate, rig.audit, rig.tools, whatsapp=rig.channel,
                     meta_verify_token="tok123", meta_app_secret="")  # fmt: skip
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(lambda: ctx)), base_url="http://api"
    )
    client.ctx = ctx
    yield client
    await client.aclose()


async def test_webhook_verification_handshake(http):
    ok = await http.get("/channels/whatsapp/webhook",
                        params={"hub.mode": "subscribe", "hub.verify_token": "tok123", "hub.challenge": "42"})  # fmt: skip
    assert ok.status_code == 200 and ok.text == "42"
    bad = await http.get("/channels/whatsapp/webhook",
                         params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})  # fmt: skip
    assert bad.status_code == 403


async def test_webhook_accepts_a_message_and_answers_meta_immediately(http, rig):
    payload = make_payload(PHONE, type="text", text={"body": "settlement nahi aaya"})
    r = await http.post("/channels/whatsapp/webhook", content=json.dumps(payload))
    assert r.status_code == 200 and r.json() == {"status": "received"}
    for _ in range(50):  # the conversation runs in the background
        if rig.channel.conversations.get(PHONE) and rig.channel.conversations[PHONE].thread_id:
            break
        import asyncio

        await asyncio.sleep(0.05)
    await rig.settle()
    assert rig.box.sent and "/sim/kyc/" in rig.box.sent[0]["body"]


async def test_webhook_signatures_are_enforced_when_a_secret_is_set(http, rig):
    http.ctx.meta_app_secret = "s3cret"
    body = json.dumps(make_payload(PHONE, type="text", text={"body": "hi"})).encode()
    assert (await http.post("/channels/whatsapp/webhook", content=body)).status_code == 403
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    ok = await http.post(
        "/channels/whatsapp/webhook", content=body, headers={"x-hub-signature-256": good}
    )
    assert ok.status_code == 200
    bad = await http.post(
        "/channels/whatsapp/webhook", content=body + b" ", headers={"x-hub-signature-256": good}
    )
    assert bad.status_code == 403
    assert (await http.post("/channels/whatsapp/webhook", content=b"not json",
                            headers={"x-hub-signature-256": "sha256=" + hmac.new(b"s3cret", b"not json", hashlib.sha256).hexdigest()})).status_code == 400  # fmt: skip


async def test_link_simulate_and_outbox_routes_work_without_meta(http, rig):
    assert (
        await http.post(
            "/channels/whatsapp/link", json={"phone": "+91 98000 00005", "merchant_id": "M1006"}
        )
    ).json() == {"linked": {"919800000005": "M1006"}}
    assert (
        await http.post("/channels/whatsapp/link", json={"phone": "1", "merchant_id": "M9999"})
    ).status_code == 404
    assert (await http.get("/channels/whatsapp/links")).json()["919800000005"] == "M1006"

    r = await http.post(
        "/channels/whatsapp/simulate",
        json={"sender": "919800000005", "text": "parso ka paisa nahi aaya"},
    )
    assert r.json() == {"handled": ["started"]}
    await rig.settle("919800000005")
    out = (await http.get("/channels/whatsapp/outbox")).json()
    assert out and out[0]["to"] == "919800000005"
    assert (await http.get("/channels/whatsapp/outbox", params={"after": len(out)})).json() == []


async def test_simulated_voice_note_travels_the_real_voice_path(http, rig):
    import base64

    audio = base64.b64encode(sine_wav()).decode()
    r = await http.post(
        "/channels/whatsapp/simulate",
        json={"sender": PHONE, "audio_base64": audio, "audio_mime": "audio/wav"},
    )
    assert r.json() == {"handled": ["started"]}
    await rig.settle()
    assert "audio" in [m["type"] for m in rig.box.sent]


async def test_routes_are_404_when_the_channel_is_off(rig):
    ctx = ApiContext(rig.service, rig.deps.gate, rig.audit, rig.tools)  # no whatsapp
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(lambda: ctx)), base_url="http://api"
    )
    assert (await c.post("/channels/whatsapp/webhook", content=b"{}")).status_code == 404
    assert (await c.get("/channels/speech/stats")).json() is None
    await c.aclose()


async def test_voice_replies_can_be_played_back_from_the_outbox(http, rig):
    media = rig.box.add_media(sine_wav(), "audio/ogg")
    await rig.send(type="audio", audio={"id": media, "mime_type": "audio/ogg", "voice": True})
    await rig.settle()
    voice = next(m for m in rig.box.sent if m["type"] == "audio")
    r = await http.get(f"/channels/whatsapp/outbox/{voice['id']}/audio")
    assert (
        r.status_code == 200
        and r.headers["content-type"] == "audio/ogg"
        and r.content[:4] == b"OggS"
    )
    assert (await http.get("/channels/whatsapp/outbox/nope/audio")).status_code == 404


# ---------- it is a conversation, not a menu ----------


async def test_a_question_while_waiting_is_answered_and_the_waiting_case_stays_resumable(rig):
    await rig.send("settlement nahi aaya")
    waiting = await rig.settle()
    assert waiting.pause["waiting_for"] == "kyc_link"

    assert await rig.send("link kaam nahi kar raha, kya karun?") == ["started"]
    answer = rig.service.get(rig.channel.conversations[PHONE].thread_id)
    await rig.service.wait(answer.thread_id)
    assert answer.summary["outcome"] == "INFO_ONLY"
    assert not [
        a
        for a in answer.summary["actions"]
        if a["executed"] and a["action"] not in ("create_ticket",)
    ]
    assert "[answer]" in rig.texts()[-1] and "link kaam nahi kar raha" in rig.texts()[-1]

    assert waiting.status == "paused"  # the original case is untouched...
    await rig.tap_link()
    assert await rig.service.poll_pauses() == [waiting.thread_id]  # ...and still moves on


async def test_unclear_replies_to_an_offer_get_a_real_answer_and_haan_or_nahi_still_work(rig):
    await rig.send("settlement nahi aaya")
    await rig.settle()
    await rig.tap_link()
    await rig.service.poll_pauses()
    offer_case = await rig.settle()
    assert offer_case.pause["type"] == "offer_reply"

    assert await rig.send("interest kitna hai?") == ["started"]  # a question, not a Haan or Nahi
    await rig.service.wait(rig.channel.conversations[PHONE].thread_id)
    assert offer_case.status == "paused"

    assert await rig.send("nahi") == ["offer_nahi"]
    await rig.service.wait(offer_case.thread_id)
    assert offer_case.summary["outcome"] == "RESOLVED_VERIFIED"
    assert not any(a["action"] == "create_lead" for a in offer_case.summary["actions"])


async def test_a_message_while_an_approval_is_pending_is_answered_not_nagged(rig):
    rig.links.link("919800000009", "M1009")
    await rig.send("customer ka paisa kat gaya", sender="919800000009")
    waiting = await rig.settle("919800000009")
    assert waiting.pause["type"] == "approval"
    assert await rig.send("kya hua?", sender="919800000009") == ["started"]
    await rig.service.wait(rig.channel.conversations["919800000009"].thread_id)
    assert waiting.status == "paused" and waiting.pause["type"] == "approval"  # still approvable


async def test_the_ai_disclosure_goes_out_once_per_conversation_not_once_per_message(rig):
    await rig.send("settlement nahi aaya")
    await rig.settle()
    await rig.send("aur ek sawal hai")
    await rig.service.wait(rig.channel.conversations[PHONE].thread_id)
    assert sum("AI assistant" in t or "AI assistant hoon" in t for t in rig.texts()) == 1


async def test_each_turn_knows_what_was_said_before(rig):
    await rig.send("settlement nahi aaya")
    await rig.settle()
    await rig.send(
        "ye link kis liye hai?"
    )  # a question ("ok" would count as done and resume the case)
    await rig.service.wait(rig.channel.conversations[PHONE].thread_id)
    seen = rig.llm.history_seen
    assert {"role": "merchant", "text": "settlement nahi aaya"} in seen
    assert any(h["role"] == "sahayak" and "[kyc_link]" in h["text"] for h in seen)
    assert not any(
        "AI assistant hoon" in h["text"] for h in seen
    )  # the disclosure is not "conversation"


async def test_a_vague_message_with_nothing_open_is_answered_not_treated_as_a_problem(rig):
    rig.links.link("919800000010", "M1010")  # a healthy merchant
    await rig.send("mughe aur doubts hai", sender="919800000010")
    case = await rig.settle("919800000010")
    assert case.summary["outcome"] == "INFO_ONLY"
    assert [a["action"] for a in case.summary["actions"] if a["executed"]] == []
    assert "[answer]" in rig.texts()[-1]


async def test_history_is_trimmed_and_masked_before_the_model_sees_it(rig):
    from backend.channels.whatsapp_channel import HISTORY_TURNS

    await rig.send("mera PAN ABCPK1234Z hai, settlement nahi aaya")
    await rig.settle()
    for i in range(HISTORY_TURNS + 3):
        rig.channel._remember(rig.channel.conversations[PHONE], "merchant", f"turn {i}")
    assert len(rig.channel.conversations[PHONE].history) == HISTORY_TURNS
    await rig.send("kuch aur")
    await rig.service.wait(rig.channel.conversations[PHONE].thread_id)
    assert "ABCPK1234Z" not in str(rig.llm.history_seen)


# ---------- the console chat while WhatsApp is live ----------

LIVE_PHONE, STAND_IN = "917000000001", "919000001001"


async def test_routing_sends_real_phones_to_whatsapp_and_stand_ins_to_the_outbox():
    from backend.channels.whatsapp import RoutingWhatsApp

    real = OutboxWhatsApp()  # stands in for the Graph API
    routing = RoutingWhatsApp(real)
    await routing.send_text(LIVE_PHONE, "to a real phone")
    await routing.send_buttons(STAND_IN, "to the console", [("HAAN", "Haan")])
    await routing.mark_read("wamid.IN123")  # a stand-in message: never reported to WhatsApp
    assert [m["to"] for m in real.sent] == [LIVE_PHONE]
    assert [m["to"] for m in routing.sim.sent] == [STAND_IN]


async def test_console_chat_works_with_live_whatsapp_and_never_reaches_a_real_phone(http, rig):
    from backend.channels.whatsapp import RoutingWhatsApp

    real = OutboxWhatsApp()
    rig.channel.api = RoutingWhatsApp(real)
    rig.links.link(STAND_IN, "M1001")

    refused = await http.post(
        "/channels/whatsapp/simulate", json={"sender": LIVE_PHONE, "text": "hello"}
    )
    assert refused.status_code == 400 and not real.sent  # cannot impersonate a real number

    ok = await http.post(
        "/channels/whatsapp/simulate", json={"sender": STAND_IN, "text": "settlement nahi aaya"}
    )
    assert ok.status_code == 200
    await rig.settle(STAND_IN)
    outbox = (await http.get("/channels/whatsapp/outbox")).json()
    assert outbox and all(m["to"] == STAND_IN for m in outbox)
    assert not real.sent
