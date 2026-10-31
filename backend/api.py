"""HTTP API for the console and channels (PRD §5.4).

POST /chat                     start a case from a merchant message
GET  /cases, /cases/{id}       list / detail (includes the pending question if paused)
POST /cases/{id}/resume        answer a pause (approval, merchant action, offer reply)
POST /cases/{id}/simulate      play the merchant: do the pending action, then resume
GET  /approvals                cases waiting for a human click;  POST /approvals/{id}
GET  /escalations              cases handed to a human, with the full handoff
GET  /events/{id}, /events     live audit trail (server-sent events)
GET  /audit, /audit/verify     audit log; hash-chain integrity check
GET  /metrics                  live numbers + latest evaluation + model health
POST /monitor/scan             start proactive cases for merchants with a proven problem
POST /faults/{fault}           break a merchant on purpose (demo control)
GET  /policy, POST /policy/kill-switch
"""

import asyncio
import base64
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

from backend.access import enforce
from backend.audit.log import AuditEvent, AuditLog
from backend.channels.whatsapp import OutboxWhatsApp, make_payload, verify_signature
from backend.channels.whatsapp_channel import normalise_phone
from backend.gate.engine import Gate
from backend.service import CaseService
from backend.tools.client import PaytmTools, ToolCallError

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"


@dataclass
class ApiContext:
    service: CaseService
    gate: Gate
    audit: AuditLog
    tools: PaytmTools
    cascade: Any = None  # GeminiCascade, for model health
    whatsapp: Any = None  # WhatsAppChannel
    meta_verify_token: str = "sahayak-verify"
    meta_app_secret: str = ""
    speech: Any = None
    access_key: str = (
        ""  # when set, requests from outside this machine need it (see backend/access.py)
    )


class ChatIn(BaseModel):
    merchant_id: str
    text: str
    channel: str = "api"


class ResumeIn(BaseModel):
    answer: dict[str, Any]


class ApprovalIn(BaseModel):
    approved: bool
    approver: str = "console"


class SimulateIn(BaseModel):
    done: bool = True
    reply: str | None = None  # for offer questions: Haan / Nahi
    approved: bool = True  # for approvals, if simulating the approver too


class ScanIn(BaseModel):
    merchant_ids: list[str] | None = None
    limit: int = 20


class FaultIn(BaseModel):
    merchant_id: str
    amount_inr: float | None = None
    device_fault: str = "POWER"


class LinkIn(BaseModel):
    phone: str
    merchant_id: str


class SimulateInboundIn(BaseModel):
    sender: str = "919800000001"
    text: str | None = None
    button_id: str | None = None
    audio_base64: str | None = None  # a voice note, base64 (any format ffmpeg reads)
    audio_mime: str = "audio/ogg"


class KillSwitchIn(BaseModel):
    on: bool
    actor: str = "console"


def event_dict(e: AuditEvent) -> dict[str, Any]:
    return {
        "seq": e.seq, "ts": e.ts.isoformat(), "thread_id": e.thread_id,
        "merchant_id": e.merchant_id, "node": e.node, "event": e.event, "action": e.action,
        "gate": e.gate, "confidence": e.confidence, "actor": e.actor, "result": e.result,
        "verified": e.verified, "evidence_ids": e.evidence_ids,
    }  # fmt: skip


def latest_eval() -> dict[str, Any] | None:
    files = sorted(EVAL_DIR.glob("results*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            return {"file": path.name, **json.loads(path.read_text())["summary"]}
        except (ValueError, KeyError):
            continue
    return None


class _Lazy:
    """Reads the context at request time, so it can be built asynchronously at startup."""

    def __init__(self, provider: Callable[[], ApiContext]):
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


def create_app(provider: Callable[[], ApiContext], lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="Sahayak API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )
    ctx = _Lazy(provider)

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        try:
            key, signed = ctx.access_key, bool(ctx.meta_app_secret)
        except AttributeError:  # still starting up
            return JSONResponse({"detail": "starting up"}, status_code=503)
        return await enforce(request, call_next, key, signed)

    def case_or_404(thread_id: str):
        try:
            return ctx.service.get(thread_id)
        except KeyError:
            raise HTTPException(404, f"no case {thread_id}") from None

    @app.get("/health")
    async def health():
        return {"status": "ok", "cases": len(ctx.service.cases)}

    # ----- cases -----

    @app.post("/chat", status_code=202)
    async def chat(body: ChatIn):
        try:
            await ctx.tools.get_merchant_profile(body.merchant_id)
        except ToolCallError as e:
            raise HTTPException(404 if e.status == 404 else 502, e.message) from None
        return ctx.service.start(body.merchant_id, body.text, body.channel).view()

    @app.get("/agents")
    async def agents():
        return ctx.service.agents_view()

    @app.get("/cases")
    async def cases(
        status: str | None = None, merchant_id: str | None = None, agent_id: str | None = None
    ):
        rows = [
            c
            for c in ctx.service.cases.values()
            if (not status or c.status == status)
            and (not merchant_id or c.merchant_id == merchant_id)
            and (not agent_id or c.agent_id == agent_id)
        ]
        rows.sort(key=lambda c: c.started_at, reverse=True)
        return [c.view() for c in rows]

    @app.get("/cases/{thread_id}")
    async def case(thread_id: str):
        rec = case_or_404(thread_id)
        if rec.status != "running":
            await ctx.service.refresh(thread_id)
        return rec.view()

    @app.post("/cases/{thread_id}/resume", status_code=202)
    async def resume(thread_id: str, body: ResumeIn):
        case_or_404(thread_id)
        try:
            return ctx.service.resume(thread_id, body.answer).view()
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/cases/{thread_id}/simulate", status_code=202)
    async def simulate(thread_id: str, body: SimulateIn):
        """Do what the merchant (or approver) would do for the pending question, then resume."""
        rec = case_or_404(thread_id)
        pause = rec.pause
        if rec.status != "paused" or not pause:
            raise HTTPException(409, f"case is {rec.status}, nothing to simulate")
        http = ctx.tools.http
        if pause["type"] == "approval":
            answer = {"approved": body.approved, "approver": "simulated"}
        elif pause["type"] == "offer_reply":
            answer = {"reply": body.reply}
        else:
            what, ref = pause["waiting_for"], pause["ref"]
            if body.done:
                if what == "kyc_link":
                    await http.post(f"/sim/kyc/{ref}/complete")
                elif what == "bank_update":
                    await http.post(f"/sim/bank-update/{ref}/complete")
                else:
                    await http.post(f"/sim/devices/{pause['device_id']}/power-cycle")
            answer = {"done": body.done}
        return ctx.service.resume(thread_id, answer).view()

    # ----- approvals and escalations -----

    @app.get("/approvals")
    async def approvals():
        return [
            {**c.view(), "request": c.pause}
            for c in ctx.service.cases.values()
            if c.status == "paused" and c.pause and c.pause.get("type") == "approval"
        ]

    @app.post("/approvals/{thread_id}", status_code=202)
    async def decide(thread_id: str, body: ApprovalIn):
        rec = case_or_404(thread_id)
        if not (rec.status == "paused" and rec.pause and rec.pause.get("type") == "approval"):
            raise HTTPException(409, "this case is not waiting for an approval")
        return ctx.service.resume(
            thread_id, {"approved": body.approved, "approver": body.approver}
        ).view()

    @app.get("/escalations")
    async def escalations():
        return [
            c.view() for c in ctx.service.cases.values() if c.summary.get("outcome") == "ESCALATED"
        ]

    # ----- live trail -----

    async def stream(request: Request, thread_id: str | None, after: int):
        seen = after
        while True:
            events = ctx.audit.events(thread_id=thread_id, after_seq=seen, limit=200)
            for e in events:
                yield f"id: {e.seq}\nevent: audit\ndata: {json.dumps(event_dict(e), default=str)}\n\n"
                seen = e.seq
            if thread_id:
                rec = ctx.service.cases.get(thread_id)
                if rec and rec.status in ("done", "error") and rec.settled.is_set() and not events:
                    yield f"event: end\ndata: {json.dumps({'status': rec.status, 'outcome': rec.summary.get('outcome')})}\n\n"
                    return
            if await request.is_disconnected():
                return
            if not events:
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

    @app.get("/events/{thread_id}")
    async def events_for(request: Request, thread_id: str, after: int = 0):
        case_or_404(thread_id)
        return StreamingResponse(stream(request, thread_id, after), media_type="text/event-stream")

    @app.get("/events")
    async def events_all(request: Request, after: int = 0):
        return StreamingResponse(stream(request, None, after), media_type="text/event-stream")

    # ----- audit -----

    @app.get("/audit")
    async def audit(
        thread_id: str | None = None,
        merchant_id: str | None = None,
        after: int = 0,
        limit: int = 500,
    ):
        return [event_dict(e) for e in ctx.audit.events(thread_id, merchant_id, limit, after)]

    @app.get("/audit/verify")
    async def audit_verify():
        ok, bad_seq = ctx.audit.verify_chain()
        return {"intact": ok, "first_bad_seq": bad_seq}

    # ----- metrics, monitor, faults, policy -----

    @app.get("/metrics")
    async def metrics():
        return {
            "live": ctx.service.metrics(),
            "evaluation": latest_eval(),
            "models": ctx.cascade.health() if ctx.cascade else None,
        }

    @app.post("/monitor/scan", status_code=202)
    async def monitor_scan(body: ScanIn | None = None):
        body = body or ScanIn()
        return {"started": await ctx.service.scan_and_start(body.merchant_ids, body.limit)}

    @app.get("/merchants")
    async def merchants():
        return await ctx.tools.list_merchants()

    @app.post("/faults/{fault}")
    async def inject_fault(fault: str, body: FaultIn):
        resp = await ctx.tools.http.post(
            f"/faults/{fault}", json=body.model_dump(exclude_none=True)
        )
        if resp.is_error:
            raise HTTPException(resp.status_code, resp.json())
        return resp.json()

    @app.get("/policy")
    async def policy():
        return ctx.gate.policy.model_dump()

    @app.post("/policy/kill-switch")
    async def kill_switch(body: KillSwitchIn):
        ctx.gate.policy.kill_switch = body.on
        ctx.audit.append(thread_id="policy", node="api", event="KILL_SWITCH", actor=body.actor,
                         result={"on": body.on})  # fmt: skip
        return {"kill_switch": ctx.gate.policy.kill_switch}

    # ----- WhatsApp -----

    def whatsapp_or_404():
        if ctx.whatsapp is None:
            raise HTTPException(404, "the WhatsApp channel is not enabled")
        return ctx.whatsapp

    @app.get("/channels/whatsapp/webhook")
    async def whatsapp_verify(
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ):
        """Meta calls this once when you register the webhook URL."""
        if mode == "subscribe" and hmac.compare_digest(token, ctx.meta_verify_token):
            return PlainTextResponse(challenge)
        raise HTTPException(403, "verify token mismatch")

    @app.post("/channels/whatsapp/webhook")
    async def whatsapp_webhook(request: Request):
        channel = whatsapp_or_404()
        raw = await request.body()
        if ctx.meta_app_secret and not verify_signature(
            ctx.meta_app_secret, raw, request.headers.get("x-hub-signature-256")
        ):
            raise HTTPException(403, "bad signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "not JSON") from None
        # Answer Meta at once (it retries slow webhooks); the conversation continues in the background.
        task = asyncio.create_task(channel.handle_payload(payload))
        ctx.service._tasks.add(task)
        task.add_done_callback(ctx.service._tasks.discard)
        return {"status": "received"}

    @app.post("/channels/whatsapp/link")
    async def whatsapp_link(body: LinkIn):
        channel = whatsapp_or_404()
        try:
            await ctx.tools.get_merchant_profile(body.merchant_id)
        except ToolCallError as e:
            raise HTTPException(404 if e.status == 404 else 502, e.message) from None
        channel.links.link(body.phone, body.merchant_id)
        return {"linked": {normalise_phone(body.phone): body.merchant_id}}

    @app.get("/channels/whatsapp/links")
    async def whatsapp_links():
        return whatsapp_or_404().links.links

    @app.post("/channels/whatsapp/simulate")
    async def whatsapp_simulate(body: SimulateInboundIn):
        """Drive the real WhatsApp code path without Meta: the same handler, replies go to the outbox."""
        channel = whatsapp_or_404()
        if not isinstance(channel.api, OutboxWhatsApp):
            raise HTTPException(
                400, "simulation is only available without live WhatsApp credentials"
            )
        message: dict[str, Any]
        if body.audio_base64:
            media_id = channel.api.add_media(base64.b64decode(body.audio_base64), body.audio_mime)
            message = {
                "type": "audio",
                "audio": {"id": media_id, "mime_type": body.audio_mime, "voice": True},
            }
        elif body.button_id:
            message = {"type": "interactive", "interactive": {"type": "button_reply",
                       "button_reply": {"id": body.button_id, "title": body.button_id.title()}}}  # fmt: skip
        else:
            message = {"type": "text", "text": {"body": body.text or ""}}
        handled = await channel.handle_payload(
            make_payload(normalise_phone(body.sender), **message)
        )
        return {"handled": handled}

    @app.get("/channels/whatsapp/outbox")
    async def whatsapp_outbox(after: int = 0):
        channel = whatsapp_or_404()
        if not isinstance(channel.api, OutboxWhatsApp):
            raise HTTPException(
                400, "outbox is only kept when WhatsApp credentials are not configured"
            )
        return channel.api.sent[after:]

    @app.get("/channels/whatsapp/outbox/{message_id}/audio")
    async def whatsapp_outbox_audio(message_id: str):
        channel = whatsapp_or_404()
        if not isinstance(channel.api, OutboxWhatsApp) or message_id not in channel.api.audio:
            raise HTTPException(404, "no such voice note")
        return Response(channel.api.audio[message_id], media_type="audio/ogg")

    @app.get("/channels/speech/stats")
    async def speech_stats():
        return vars(ctx.speech.stats) if ctx.speech else None

    return app
