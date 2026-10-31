"""WhatsApp Cloud API: sending, media, and reading webhook payloads.

`WhatsAppAPI` talks to Meta's Graph API. `OutboxWhatsApp` has the same interface but only records
what would be sent, so the whole channel can be exercised (and demoed) without Meta credentials.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from backend.net import ipv4_client

GRAPH = "https://graph.facebook.com"
WINDOW_ERROR_CODE = 131047  # re-engagement: more than 24 h since the user's last message


class WhatsAppError(Exception):
    def __init__(self, code: int | None, message: str, status: int):
        super().__init__(f"WhatsApp {status} (code {code}): {message}")
        self.code, self.message, self.status = code, message, status

    @property
    def window_closed(self) -> bool:
        return self.code == WINDOW_ERROR_CODE


class Transport(Protocol):
    async def send_text(self, to: str, body: str) -> str: ...
    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str: ...
    async def send_audio(self, to: str, ogg: bytes) -> str: ...
    async def download_media(self, media_id: str) -> tuple[bytes, str]: ...
    async def mark_read(self, message_id: str) -> None: ...


class WhatsAppAPI:
    def __init__(
        self,
        token: str,
        phone_id: str,
        http: httpx.AsyncClient | None = None,
        version: str = "v23.0",
    ):
        self.token, self.phone_id = token, phone_id
        self.http = http or ipv4_client(30)
        self.base = f"{GRAPH}/{version}"

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        resp = await self.http.request(method, url, headers=self._auth, **kw)
        if resp.is_error:
            try:
                err = resp.json()["error"]
                raise WhatsAppError(err.get("code"), err.get("message", ""), resp.status_code)
            except (ValueError, KeyError, TypeError):
                raise WhatsAppError(None, resp.text[:200], resp.status_code) from None
        return resp

    async def _send(self, payload: dict[str, Any]) -> str:
        body = {"messaging_product": "whatsapp", **payload}
        resp = await self._request("POST", f"{self.base}/{self.phone_id}/messages", json=body)
        return resp.json()["messages"][0]["id"]

    async def send_text(self, to: str, body: str) -> str:
        return await self._send({"to": to, "type": "text", "text": {"body": body[:4096]}})

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        """Up to 3 reply buttons; titles are cut to WhatsApp's 20-character limit."""
        if not 1 <= len(buttons) <= 3:
            raise ValueError("WhatsApp allows 1 to 3 reply buttons")
        action = {
            "buttons": [
                {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                for bid, title in buttons
            ]
        }
        interactive = {"type": "button", "body": {"text": body[:1024]}, "action": action}
        return await self._send({"to": to, "type": "interactive", "interactive": interactive})

    async def send_audio(self, to: str, ogg: bytes) -> str:
        upload = await self._request(
            "POST",
            f"{self.base}/{self.phone_id}/media",
            files={"file": ("voice.ogg", ogg, "audio/ogg")},
            data={"messaging_product": "whatsapp", "type": "audio/ogg"},
        )
        return await self._send({"to": to, "type": "audio", "audio": {"id": upload.json()["id"]}})

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        meta = (await self._request("GET", f"{self.base}/{media_id}")).json()
        blob = await self._request("GET", meta["url"])
        return blob.content, meta.get("mime_type", "audio/ogg")

    async def mark_read(self, message_id: str) -> None:
        await self._send({"status": "read", "message_id": message_id})


@dataclass
class OutboxWhatsApp:
    """Records instead of sending. Used when no Meta credentials are configured and in tests."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    media: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    audio: dict[str, bytes] = field(
        default_factory=dict
    )  # sent voice notes, so a web chat can play them
    fail_with: WhatsAppError | None = None  # tests: make the next sends fail

    def _record(self, kind: str, to: str, **kw: Any) -> str:
        if self.fail_with:
            raise self.fail_with
        mid = f"wamid.OUT{len(self.sent) + 1}"
        self.sent.append({"id": mid, "type": kind, "to": to, "at": time.time(), **kw})
        return mid

    async def send_text(self, to: str, body: str) -> str:
        return self._record("text", to, body=body)

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        return self._record(
            "buttons", to, body=body, buttons=[{"id": i, "title": t[:20]} for i, t in buttons]
        )

    async def send_audio(self, to: str, ogg: bytes) -> str:
        message_id = self._record("audio", to, bytes=len(ogg), magic=ogg[:4].decode("latin-1"))
        self.audio[message_id] = ogg
        return message_id

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        if media_id not in self.media:
            raise WhatsAppError(100, f"unknown media {media_id}", 404)
        return self.media[media_id]

    async def mark_read(self, message_id: str) -> None:
        return None

    def add_media(self, data: bytes, mime: str) -> str:
        media_id = f"media-{len(self.media) + 1}"
        self.media[media_id] = (data, mime)
        return media_id


SIM_MESSAGE_PREFIX = "wamid.IN"  # ids make_payload gives simulated inbound messages
SIM_PREFIX = "919000"  # the console Chat page's stand-in phones (see console/src/screens/Chat.jsx)


class RoutingWhatsApp:
    """Real phones go to WhatsApp; the console's stand-in phones go to an in-memory outbox.

    So the Chat page keeps working for anyone with the access key while the real number is live,
    and nothing it sends ever reaches a real phone.
    """

    def __init__(self, real: Transport, sim: OutboxWhatsApp | None = None):
        self.real, self.sim = real, sim or OutboxWhatsApp()

    def _for(self, to: str) -> Transport:
        return self.sim if to.startswith(SIM_PREFIX) else self.real

    async def send_text(self, to: str, body: str) -> str:
        return await self._for(to).send_text(to, body)

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        return await self._for(to).send_buttons(to, body, buttons)

    async def send_audio(self, to: str, ogg: bytes) -> str:
        return await self._for(to).send_audio(to, ogg)

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        sim = media_id in self.sim.media
        return await (self.sim if sim else self.real).download_media(media_id)

    async def mark_read(self, message_id: str) -> None:
        if not message_id.startswith(SIM_MESSAGE_PREFIX):  # a stand-in message is not on WhatsApp
            await self.real.mark_read(message_id)


# ---------- webhook ----------


@dataclass
class Inbound:
    id: str
    sender: str  # wa_id: digits only, country code included
    kind: str  # text | audio | button | unsupported
    text: str | None = None
    media_id: str | None = None
    mime: str | None = None
    button_id: str | None = None
    name: str | None = None
    timestamp: int | None = None


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Meta signs each webhook body: X-Hub-Signature-256: sha256=<hex HMAC with the app secret>."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict[str, Any]) -> list[Inbound]:
    """Messages in a webhook body. Delivery/read statuses and other fields are ignored."""
    out: list[Inbound] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                sender = msg.get("from", "")
                base = {
                    "id": msg.get("id", ""),
                    "sender": sender,
                    "name": names.get(sender),
                    "timestamp": int(msg["timestamp"])
                    if str(msg.get("timestamp", "")).isdigit()
                    else None,
                }
                kind = msg.get("type")
                if kind == "text":
                    out.append(
                        Inbound(kind="text", text=msg.get("text", {}).get("body", ""), **base)
                    )
                elif kind == "audio":
                    a = msg.get("audio", {})
                    out.append(
                        Inbound(kind="audio", media_id=a.get("id"), mime=a.get("mime_type"), **base)
                    )
                elif kind == "interactive":
                    reply = msg.get("interactive", {})
                    r = reply.get("button_reply") or reply.get("list_reply") or {}
                    out.append(
                        Inbound(kind="button", button_id=r.get("id"), text=r.get("title"), **base)
                    )
                elif kind == "button":
                    b = msg.get("button", {})
                    out.append(
                        Inbound(
                            kind="button", button_id=b.get("payload"), text=b.get("text"), **base
                        )
                    )
                else:
                    out.append(Inbound(kind="unsupported", **base))
    return out


def make_payload(sender: str, **message: Any) -> dict[str, Any]:
    """A Meta-shaped webhook body for one message (used by the simulator and tests)."""
    msg = {
        "from": sender,
        "id": f"{SIM_MESSAGE_PREFIX}{time.time_ns()}",
        "timestamp": str(int(time.time())),
        **message,
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "0", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": sender, "profile": {"name": "Demo merchant"}}],
            "messages": [msg],
        }}]}],
    }  # fmt: skip
