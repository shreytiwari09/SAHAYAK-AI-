"""Chat with Sahayak in the terminal, playing the merchant, and watch it work.

    uv run python -m backend.cli                    # interactive, in-process mock, real Gemini
    uv run python -m backend.cli --auto             # also plays the merchant and approver
    uv run python -m backend.cli --fault kyc_hold --merchant M1013 -m "settlement nahi aaya"
    uv run python -m backend.cli --fake --auto -m "Mera settlement 2 din se nahi aaya"   # offline

By default the mock Paytm systems run in-process on a fresh seeded database (try merchant M1001,
Ramesh Kumar). `--base-url http://localhost:8001` uses a running mock service instead.
"""

import argparse
import asyncio
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx
from langgraph.types import Command
from sqlmodel import Session

from backend.agent.driver import Behaviour, SimResponder
from backend.agent.runtime import build_agent, open_audit, open_checkpointer
from backend.audit.log import AuditEvent
from backend.config import get_settings
from backend.net import ipv4_client
from backend.pii.masker import PiiMasker
from backend.tools.client import PaytmTools

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
COLORS = {"AUTO": "\033[32m", "APPROVE": "\033[33m", "ESCALATE": "\033[31m"}
GREEN, RED, CYAN, YELLOW = "\033[32m", "\033[31m", "\033[36m", "\033[33m"


def paint(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else text


def render(e: AuditEvent) -> str | None:
    """One audit event as one or two readable lines. None means: do not show."""
    r = e.result or {}
    if e.event == "UNDERSTOOD":
        c = r.get("classification", {})
        return (
            f"{paint('understood', BOLD)}  {c.get('language')}/{c.get('script')} · {c.get('sentiment')} · "
            f"intent={c.get('intent')}\n"
            f"             system data proves: {r.get('findings') or 'no problem'}"
            + (f"   signals: {r['signals']}" if r.get("signals") else "")
        )
    if e.event == "DIAGNOSED":
        c = e.confidence or {}
        return (
            f"{paint('diagnosed', BOLD)}   {r.get('cause')} → {r.get('action')}   "
            f"confidence {c.get('total', 0):.2f}  {paint(_explain(c), DIM)}"
        )
    if e.event == "GATE_DECIDED":
        g = e.gate or {}
        tier = g.get("tier", "?")
        return f"{paint('gate', BOLD)}        {e.action} → {paint(tier, COLORS.get(tier, ''))}  ({g.get('reason')})"
    if e.event == "EXECUTED":
        return f"{paint('executed', BOLD)}    {e.action}  {paint(_short(r), DIM)}"
    if e.event == "EXECUTION_REFUSED":
        return f"{paint('refused', RED)}     {e.action}: {r.get('code')}"
    if e.event in ("APPROVED", "REJECTED"):
        color = GREEN if e.event == "APPROVED" else RED
        return f"{paint(e.event.lower(), color)}    {e.action} by {e.actor}"
    if e.event == "MERCHANT_RESPONDED":
        return f"{paint('merchant', BOLD)}    {r.get('waiting_for')} → {'done' if r.get('done') else 'not done'}"
    if e.event == "VERIFIED":
        mark = paint("✅ verified", GREEN) if e.verified else paint("✗ not verified", RED)
        return f"{mark}    {_short(r.get('proof') or {}, 140)}"
    if e.event == "CONSENT_RECORDED":
        return f"{paint('consent', BOLD)}     recorded"
    if e.event == "LEAD_CREATED":
        return f"{paint('lead', BOLD)}        created after consent"
    if e.event in ("ESCALATED", "WAITING_MERCHANT", "REFUSED", "NOTHING_TO_DO", "INFO_ONLY"):
        extra = f": {r.get('reason')}" if e.event == "ESCALATED" else ""
        return f"{paint(e.event.lower().replace('_', ' '), YELLOW)}{extra}"
    if e.event == "MESSAGE_FALLBACK":
        return paint(f"  (message fell back to template: {r.get('why')})", DIM)
    if e.event == "CLASSIFY_FALLBACK":
        return paint("  (classifier unavailable; using safe defaults)", DIM)
    return None


def _explain(c: dict) -> str:
    keys = ("evidence_rule_match", "self_consistency", "verifier_pass", "schema_valid")
    return " ".join(f"{k.split('_')[0]}={c.get(k, 0):.1f}" for k in keys)


def _short(obj: Any, n: int = 110) -> str:
    text = str(obj)
    return text if len(text) <= n else text[: n - 1] + "…"


def bubble(text: str, who: str) -> str:
    head = paint(f"📱 {who}", CYAN)
    body = "\n".join(f"   {line}" for line in text.splitlines())
    return f"{head}\n{body}"


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


class InteractiveResponder:
    """You play the merchant and the approver."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def __call__(self, pause: dict[str, Any]) -> dict[str, Any]:
        kind = pause["type"]
        if kind == "approval":
            print(paint("\n⏸  Needs one human click (you are the approver)", YELLOW))
            print(f"   {pause['summary']}  ·  ₹{pause['amount_inr']:,.0f}  ·  {pause['reason']}")
            print(paint(f"   confidence {pause['confidence']}", DIM))
            ok = (await ask("   Approve? [y/N] ")).lower().startswith("y")
            return {"approved": ok, "approver": "you"}
        if kind == "offer_reply":
            print(paint("\n⏸  The merchant is asked about the offer", YELLOW))
            reply = (await ask("   Reply Haan / Nahi / (Enter to ignore): ")).strip()
            return {"reply": reply or None}
        what, ref = pause["waiting_for"], pause["ref"]
        print(paint(f"\n⏸  Waiting for the merchant: {what}", YELLOW))
        done = (
            not (await ask("   Press Enter when you have done it (or type n): "))
            .lower()
            .startswith("n")
        )
        if done:
            if what == "kyc_link":
                await self.http.post(f"/sim/kyc/{ref}/complete")
            elif what == "bank_update":
                await self.http.post(f"/sim/bank-update/{ref}/complete")
            else:
                await self.http.post(f"/sim/devices/{pause['device_id']}/power-cycle")
        return {"done": done}


async def tail(audit, thread: str, stop: asyncio.Event) -> None:
    shown = 0
    while True:
        events = audit.events(thread_id=thread)
        for e in events[shown:]:
            if line := render(e):
                print(f"  {line}")
            if e.event == "MESSAGE_SENT":
                print(bubble(e.result["text"], "Sahayak → merchant"))
        shown = len(events)
        if stop.is_set() and len(audit.events(thread_id=thread)) == shown:
            return
        await asyncio.sleep(0.15)


async def converse(graph, audit, initial: dict[str, Any], responder) -> dict[str, Any]:
    config = {"configurable": {"thread_id": initial["thread_id"]}}
    stop = asyncio.Event()
    printer = asyncio.create_task(tail(audit, initial["thread_id"], stop))
    try:
        result = await graph.ainvoke(initial, config)
        while result.get("__interrupt__"):
            await asyncio.sleep(0.3)  # let the trace print before asking
            answer = await responder(result["__interrupt__"][0].value)
            result = await graph.ainvoke(Command(resume=answer), config)
    finally:
        stop.set()
        await printer
    return result


async def start_inprocess_mock(faults: list[str], merchant: str, merchants: int):
    """Fresh seeded mock Paytm, called in-process. Returns (http client, cleanup)."""
    from mock_paytm.app import app
    from mock_paytm.config import Settings
    from mock_paytm.db import init_db, make_engine, set_engine
    from mock_paytm.faults import Fault, inject
    from mock_paytm.seed import seed

    workdir = Path(tempfile.mkdtemp(prefix="sahayak-mock-"))
    engine = make_engine(f"sqlite:///{workdir / 'mock.db'}")
    set_engine(engine)
    init_db(engine)
    with Session(engine) as session:
        seed(session, Settings(seed_merchants=merchants))
        for name in faults:
            inject(session, Fault(name), merchant)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")

    async def cleanup():
        await http.aclose()
        engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)

    return http, cleanup


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.base_url:
        http, cleanup = ipv4_client(10), (lambda: asyncio.sleep(0))
        http.base_url = httpx.URL(args.base_url)
    else:
        http, cleanup = await start_inprocess_mock(args.fault, args.merchant, args.merchants)
    tools = PaytmTools(http)

    cascade = None
    if args.fake:
        from eval.fake_llm import FakeLLM

        llm = FakeLLM()
        print(paint("Using the offline stand-in model (no Gemini).", DIM))
    else:
        if not settings.gemini_api_key:
            print("GEMINI_API_KEY is not set (see .env). Use --fake for an offline run.")
            return 2
        from backend.agent.llm import GeminiAgentLLM
        from backend.llm.cascade import GeminiCascade

        cascade = GeminiCascade()
        llm = GeminiAgentLLM(cascade)
        print(paint("Checking which Gemini models are available…", DIM))
        health = await cascade.warm_up()
        up = [m for m, h in health.items() if h["up"]]
        print(paint(f"Models up: {', '.join(up) or 'none'}  (best first)", DIM))
        if not up:
            print("No Gemini model is answering right now. Try --fake, or retry shortly.")
            return 2

    audit = open_audit(None if not args.fake else "sqlite://")
    responder = (
        SimResponder(http, Behaviour(offer_reply=args.offer_reply))
        if args.auto
        else InteractiveResponder(http)
    )

    async def fast_sleep(seconds: float) -> None:
        from mock_paytm import clock

        clock.advance(seconds)  # skip the wait for the mock systems' async work

    kw: dict[str, Any] = {"sleep": fast_sleep} if args.fast and not args.base_url else {}
    try:
        async with open_checkpointer(":memory:" if args.fake else None) as saver:
            graph, _ = build_agent(
                tools,
                llm,
                audit,
                saver,
                masker=PiiMasker(),
                public_url=str(http.base_url).rstrip("/"),
                **kw,
            )
            profile = await tools.get_merchant_profile(args.merchant)
            print(
                paint(
                    f"\nMerchant {profile.id}: {profile.owner_name}, {profile.business_name}, "
                    f"{profile.city} ({profile.language})",
                    BOLD,
                )
            )
            message = args.message
            while True:
                if message is None:
                    message = await ask(paint("\nmerchant> ", BOLD))
                if not message or message.lower() in ("quit", "exit", "q"):
                    break
                print(bubble(message, f"{profile.owner_name.split()[0]} → Sahayak"))
                initial = {
                    "thread_id": f"cli-{uuid.uuid4().hex[:8]}",
                    "merchant_id": args.merchant,
                    "channel": "cli",
                    "text": message,
                    "trigger_causes": None,
                }
                state = await converse(graph, audit, initial, responder)
                print(paint(f"\n── outcome: {state.get('outcome')} ──", BOLD))
                if args.message is not None:
                    break
                message = None
    finally:
        if cascade:
            print(
                paint(
                    f"\nGemini model health: { {m: (h['ok'], h['failed']) for m, h in cascade.health().items()} }",
                    DIM,
                )
            )
            await cascade.aclose()
        await cleanup()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Chat with Sahayak as a merchant.")
    p.add_argument("--merchant", default="M1001", help="merchant id (default M1001, Ramesh Kumar)")
    p.add_argument("-m", "--message", help="send one message and exit")
    p.add_argument("--fault", action="append", default=[], help="inject a fault first (repeatable)")
    p.add_argument(
        "--auto", action="store_true", help="play the merchant and approver automatically"
    )
    p.add_argument(
        "--offer-reply", default=None, help="with --auto: answer the loan offer (Haan/Nahi)"
    )
    p.add_argument("--fake", action="store_true", help="offline stand-in model instead of Gemini")
    p.add_argument("--fast", action="store_true", help="skip waits by advancing the mock clock")
    p.add_argument("--base-url", help="use a running mock service instead of the in-process one")
    p.add_argument("--merchants", type=int, default=50, help="merchants to seed (in-process)")
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
