"""WhatsApp conversations: which merchant is this, which case does the message belong to, and how
do the agent's messages get back to the merchant's phone (text, buttons, voice).

Routing of an inbound message:
  - a hard signal (HUMAN, legal threat, fraud, prompt injection) always starts a fresh case;
  - a case waiting for the merchant's offer answer takes Haan / Nahi (text, voice or button);
  - a case waiting for the merchant to do something takes "done" / "nahi" (the systems are also
    polled, so tapping the link is enough);
  - anything else starts a new case.
Delivery respects WhatsApp's 24-hour window: after 24 h of silence only templates may be sent, so
proactive messages to that merchant are skipped (and logged) until they message first.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.agent.messages import disclosure
from backend.agent.signals import rule_signals
from backend.audio import AudioError, to_ogg_opus, to_wav16k
from backend.channels.whatsapp import Inbound, Transport, WhatsAppError, parse_webhook
from backend.gate.engine import Policy
from backend.service import CaseService
from backend.speech import Speech, SpeechError

WINDOW_SECONDS = 24 * 3600
HISTORY_TURNS = 10
WAITING_FOR = {
    "kyc_link": "tap the KYC link Sahayak sent",
    "bank_update": "correct their bank details using the link Sahayak sent",
    "power_cycle": "switch the device off and on again",
}
VOICE_KINDS = {
    "resolved", "escalated", "explain", "refuse", "offer", "stalled", "power_cycle",
    "kyc_link", "bank_update", "approval_pending", "offer_thanks", "offer_declined", "no_offer",
}  # fmt: skip

YES = {
    "haan",
    "han",
    "ha",
    "haa",
    "yes",
    "y",
    "ok",
    "okay",
    "ji",
    "ji haan",
    "theek hai",
    "thik hai",
}
DONE = YES | {"done", "ho gaya", "hogaya", "ho gya", "kar diya", "kardiya", "kar liya", "complete",
              "completed", "finished"}  # fmt: skip
NO = {"nahi", "nahin", "nhi", "no", "n", "nope", "abhi nahi", "later", "baad mein"}


def normalise_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def said(text: str | None, words: set[str]) -> bool:
    t = re.sub(r"[^\w\s]", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t in words or any(f" {w} " in f" {t} " for w in words if " " in w)


class LinkStore:
    """Which phone belongs to which merchant. Kept in a small JSON file so it survives restarts."""

    def __init__(self, path: str | Path | None = None, initial: str = ""):
        self.path = Path(path) if path else None
        self.links: dict[str, str] = {}
        if self.path and self.path.exists():
            self.links = json.loads(self.path.read_text())
        for pair in filter(None, (p.strip() for p in initial.split(","))):
            phone, _, merchant = pair.partition(":")
            if merchant:
                self.links[normalise_phone(phone)] = merchant.strip()

    def link(self, phone: str, merchant_id: str) -> None:
        self.links[normalise_phone(phone)] = merchant_id
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.links, indent=2))

    def merchant_for(self, phone: str) -> str | None:
        return self.links.get(normalise_phone(phone))

    def phone_for(self, merchant_id: str) -> str | None:
        return next((p for p, m in self.links.items() if m == merchant_id), None)


@dataclass
class Conversation:
    merchant_id: str
    last_inbound: float = 0.0
    voice: bool = False  # the merchant's latest message was a voice note, so reply in voice too
    threads: list[str] = field(default_factory=list)  # cases started in this conversation
    history: list[dict] = field(default_factory=list)  # recent turns, oldest first
    disclosed: bool = False  # the AI disclosure goes out once per conversation

    @property
    def thread_id(self) -> str | None:
        return self.threads[-1] if self.threads else None


class WhatsAppChannel:
    def __init__(
        self,
        transport: Transport,
        service: CaseService,
        links: LinkStore,
        speech: Speech | None = None,
        *,
        default_merchant: str = "",
        voice_replies: bool = True,
        clock=time.time,
    ):
        self.api, self.service, self.links, self.speech = transport, service, links, speech
        self.default_merchant = default_merchant
        self.voice_replies = voice_replies
        self.clock = clock
        self.conversations: dict[str, Conversation] = {}
        self.thread_phone: dict[str, str] = {}
        self.seen: set[str] = set()
        self.hard = set(Policy.load().always_escalate_signals)

    # ----- inbound -----

    async def handle_payload(self, payload: dict[str, Any]) -> list[str]:
        results = []
        for msg in parse_webhook(payload):
            results.append(await self.handle(msg))
        return results

    async def handle(self, m: Inbound) -> str:
        if m.id in self.seen:  # Meta retries a webhook until it gets a 200
            return "duplicate"
        self.seen.add(m.id)
        phone = normalise_phone(m.sender)
        merchant = self.links.merchant_for(phone) or self.default_merchant
        if not merchant:
            await self._text(phone, "Yeh number kisi Paytm merchant account se juda nahi hai. "
                             "This number is not linked to a Paytm merchant account.")  # fmt: skip
            return "unlinked"
        conv = self.conversations.setdefault(phone, Conversation(merchant))
        conv.merchant_id, conv.last_inbound = merchant, self.clock()
        try:
            await self.api.mark_read(m.id)
        except Exception:
            pass

        text = await self._text_of(m, conv, phone)
        if text is None:
            return "unreadable"

        self._remember(conv, "merchant", text)
        hard = bool(rule_signals(text) & self.hard)
        waiting = None if hard else self._waiting(conv)
        if waiting:
            pause = waiting.pause or {}
            if pause.get("type") == "offer_reply" and (said(text, YES) or said(text, NO)):
                reply = "HAAN" if said(text, YES) else "NAHI"
                self.service.resume(waiting.thread_id, {"reply": reply})
                return f"offer_{reply.lower()}"
            if pause.get("type") == "merchant_action" and (said(text, DONE) or said(text, NO)):
                done = said(text, DONE)
                self.service.resume(waiting.thread_id, {"done": done})
                return "merchant_done" if done else "merchant_declined"

        # Anything else is a conversation turn. It carries what was said so far, and when
        # something is already open it answers without starting a second fix.
        context: dict = {"history": conv.history[:-1], "disclosed": conv.disclosed}
        if waiting:
            context.update(chat_only=True, open_note=self._open_note(waiting))
        started = self.service.start(merchant, text, channel="whatsapp", **context)
        conv.threads.append(started.thread_id)
        self.thread_phone[started.thread_id] = phone
        return "started"

    def _remember(self, conv: Conversation, role: str, text: str) -> None:
        conv.history.append({"role": role, "text": text})
        del conv.history[:-HISTORY_TURNS]

    def _waiting(self, conv: Conversation):
        """The newest case in this conversation that is paused, waiting for someone."""
        for thread_id in reversed(conv.threads):
            rec = self.service.cases.get(thread_id)
            if rec and rec.status == "paused":
                return rec
        return None

    @staticmethod
    def _open_note(rec) -> str:
        pause = rec.pause or {}
        kind = pause.get("type")
        if kind == "offer_reply":
            return "A loan offer was sent and the merchant has not answered Haan or Nahi yet."
        if kind == "approval":
            return "The merchant's request is with a Paytm team member for approval."
        return f"Waiting for the merchant to {WAITING_FOR.get(pause.get('waiting_for'), 'finish a step')}."

    async def _text_of(self, m: Inbound, conv: Conversation, phone: str) -> str | None:
        """The words in a message, transcribing voice notes. None when it cannot be read."""
        if m.kind == "text":
            conv.voice = False
            return (m.text or "").strip() or None
        if m.kind == "button":
            conv.voice = False
            return m.button_id or m.text
        if m.kind == "audio" and m.media_id:
            if self.speech is None:
                await self._text(phone, "Abhi voice note nahi sun sakta, kripya likh kar bhejein. "
                                 "I cannot play voice notes right now; please type.")  # fmt: skip
                return None
            try:
                raw, _mime = await self.api.download_media(m.media_id)
                transcript = await self.speech.transcribe(await to_wav16k(raw), "audio/wav")
            except (WhatsAppError, AudioError, SpeechError) as e:
                self.service.audit.append(thread_id=conv.thread_id or f"wa-{phone}", merchant_id=conv.merchant_id,
                                          node="whatsapp", event="VOICE_FAILED", result={"error": str(e)[:200]})  # fmt: skip
                await self._text(phone, "Aapka voice note samajh nahi aaya, kripya dobara bhejein ya likh kar bataein. "
                                 "I could not understand the voice note; please resend or type.")  # fmt: skip
                return None
            conv.voice = True
            self.service.audit.append(thread_id=conv.thread_id or f"wa-{phone}", merchant_id=conv.merchant_id,
                                      node="whatsapp", event="VOICE_TRANSCRIBED",
                                      result={"provider": transcript.provider, "language": transcript.language,
                                              "chars": len(transcript.text)})  # fmt: skip
            return transcript.text
        await self._text(phone, "Main abhi sirf text aur voice note samajh sakta hoon. "
                         "I can read text and voice notes.")  # fmt: skip
        return None

    # ----- outbound -----

    async def _text(self, phone: str, body: str) -> None:
        try:
            await self.api.send_text(phone, body)
        except WhatsAppError:
            pass  # nothing more we can do; the audit trail already has the case

    def window_open(self, phone: str) -> bool:
        conv = self.conversations.get(phone)
        return bool(conv and self.clock() - conv.last_inbound < WINDOW_SECONDS)

    async def deliver(
        self, thread_id: str, merchant_id: str, channel: str, text: str, kind: str, lang: str
    ) -> None:
        """Agent hook: send a message the agent just composed to the merchant's phone."""
        if channel not in ("whatsapp", "proactive"):
            return
        phone = self.thread_phone.get(thread_id) or self.links.phone_for(merchant_id)
        if not phone:
            return
        self.thread_phone.setdefault(thread_id, phone)
        if not self.window_open(phone):
            self._audit(
                thread_id, merchant_id, "DELIVERY_SKIPPED", {"why": "24-hour window closed"}
            )
            return
        try:
            if kind == "offer":
                await self.api.send_buttons(phone, text, [("HAAN", "Haan"), ("NAHI", "Nahi")])
            else:
                await self.api.send_text(phone, text)
        except WhatsAppError as e:
            self._audit(thread_id, merchant_id, "DELIVERY_FAILED",
                        {"code": e.code, "window_closed": e.window_closed, "error": e.message[:150]})  # fmt: skip
            return
        conv = self.conversations[phone]
        conv.disclosed = True
        self._remember(
            conv,
            "sahayak",
            text.removeprefix(disclosure(lang, self._agent_name(merchant_id)) + "\n"),
        )
        if self.voice_replies and self.speech and conv.voice and kind in VOICE_KINDS:
            await self._voice(phone, thread_id, merchant_id, text, lang)

    def _agent_name(self, merchant_id: str) -> str | None:
        roster = getattr(self.service, "roster", None)
        return roster.for_merchant(merchant_id).name if roster else None

    async def _voice(
        self, phone: str, thread_id: str, merchant_id: str, text: str, lang: str
    ) -> None:
        try:
            wav = await self.speech.synthesize(text, lang)  # type: ignore[union-attr]
            await self.api.send_audio(phone, await to_ogg_opus(wav))
        except (SpeechError, AudioError, WhatsAppError) as e:
            # The text already went out, so the merchant is not left without the message.
            self._audit(thread_id, merchant_id, "VOICE_REPLY_FAILED", {"error": str(e)[:150]})

    def _audit(self, thread_id: str, merchant_id: str, event: str, result: dict[str, Any]) -> None:
        self.service.audit.append(thread_id=thread_id, merchant_id=merchant_id, node="whatsapp",
                                  event=event, result=result)  # fmt: skip
