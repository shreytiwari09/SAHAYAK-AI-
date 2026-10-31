"""Runs conversations and tracks them for the API (and later the WhatsApp webhook and console).

A case is one thread of the agent graph. It is `running`, `paused` (waiting for an approver, the
merchant or an offer reply), `done` or `error`. All state lives in the graph's checkpoint and the
audit log, so `rehydrate()` rebuilds the picture after a restart, including pending approvals.
"""

import asyncio
import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import Command

from backend import monitor
from backend.agents import AgentRoster
from backend.audit.log import AuditLog
from backend.tools.client import PaytmTools, ToolCallError

# Actions that change no merchant system, so they are logged but not gated.
NON_GATED = {"send_message", "create_ticket", "update_ticket", "close_ticket"}


@dataclass
class CaseRecord:
    thread_id: str
    merchant_id: str
    channel: str
    trigger: list[str] | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    status: str = "running"  # running | paused | done | error
    pause: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    settled: asyncio.Event = field(default_factory=asyncio.Event)

    def view(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "merchant_id": self.merchant_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "channel": self.channel,
            "proactive": bool(self.trigger),
            "status": self.status,
            "pause": self.pause,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            **self.summary,
        }


def summarise(values: dict[str, Any]) -> dict[str, Any]:
    """The parts of the graph state a console or API caller cares about."""
    actions = values.get("actions", [])
    gated = [a for a in actions if a["tier"] and a["action"] not in NON_GATED]
    diag = values.get("diagnosis") or {}
    cls = values.get("cls") or {}
    return {
        "outcome": values.get("outcome"),
        "verified": values.get("verified"),
        "ticket_id": values.get("ticket_id"),
        "language": cls.get("language"),
        "sentiment": cls.get("sentiment"),
        "intent": cls.get("intent"),
        "signals": values.get("signals", []),
        "diagnosis": {
            "cause": diag.get("cause"),
            "action": diag.get("action"),
            "confidence": diag.get("confidence"),
            "explain": diag.get("explain"),
        }  # fmt: skip
        if diag
        else None,
        "messages": values.get("messages", []),
        "actions": [
            {"action": a["action"], "executed": a["executed"], "tier": a["tier"]}
            for a in actions
            if a["action"] not in ("send_message",)
        ],
        "approvals_needed": sum(1 for a in gated if a["tier"] == "APPROVE"),
        "proof": values.get("proof_text") or None,
        "escalation_reason": values.get("escalation_reason"),
        "handoff": values.get("handoff"),
    }


class CaseService:
    def __init__(
        self, graph, audit: AuditLog, tools: PaytmTools, roster: AgentRoster | None = None
    ):
        self.graph, self.audit, self.tools = graph, audit, tools
        self.roster = roster or AgentRoster.default()
        self.cases: dict[str, CaseRecord] = {}
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    # ----- running cases -----

    def start(
        self,
        merchant_id: str,
        text: str | None,
        channel: str = "api",
        trigger_causes: list[str] | None = None,
        **context: Any,
    ) -> CaseRecord:
        """`context` carries conversation state into the case: history, disclosed, chat_only, open_note."""
        thread_id = f"{channel}-{uuid.uuid4().hex[:10]}"
        rec = self._assign(CaseRecord(thread_id, merchant_id, channel, trigger_causes))
        self.cases[thread_id] = rec
        initial = {
            "agent_name": rec.agent_name,
            "thread_id": thread_id,
            "merchant_id": merchant_id,
            "channel": channel,
            "text": text,
            "trigger_causes": trigger_causes,
            **context,
        }
        self._spawn(thread_id, initial)
        return rec

    def _assign(self, rec: CaseRecord) -> CaseRecord:
        agent = self.roster.for_merchant(rec.merchant_id)
        rec.agent_id, rec.agent_name = agent.id, agent.name
        return rec

    def resume(self, thread_id: str, answer: dict[str, Any]) -> CaseRecord:
        rec = self.get(thread_id)
        if rec.status != "paused":
            raise ValueError(f"case is {rec.status}, not waiting for an answer")
        rec.status, rec.pause = "running", None
        rec.settled.clear()
        self._spawn(thread_id, Command(resume=answer))
        return rec

    def _spawn(self, thread_id: str, payload: Any) -> None:
        task = asyncio.create_task(self._run(thread_id, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, thread_id: str, payload: Any) -> None:
        rec = self.cases[thread_id]
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        # One agent runs its merchants' cases concurrently, up to its own limit.
        async with lock, self.roster.slot(rec.agent_id or ""):
            config = {"configurable": {"thread_id": thread_id}}
            try:
                await self.graph.ainvoke(payload, config)
                await self.refresh(thread_id)
            except Exception as e:  # keep the service alive; the case shows what went wrong
                rec.status, rec.error = "error", f"{type(e).__name__}: {e}"
                rec.finished_at = time.time()
                self.audit.append(thread_id=thread_id, merchant_id=rec.merchant_id, node="service",
                                  event="CASE_ERROR", result={"error": rec.error})  # fmt: skip
            finally:
                rec.settled.set()

    async def refresh(self, thread_id: str) -> CaseRecord:
        """Read the case's state from the checkpoint."""
        rec = self.cases[thread_id]
        snap = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        rec.summary = summarise(snap.values or {})
        pauses = [i.value for t in snap.tasks for i in t.interrupts]
        if pauses:
            rec.status, rec.pause = "paused", pauses[0]
        elif snap.next:
            rec.status, rec.pause = "running", None
        else:
            rec.status, rec.pause = "done", None
            rec.finished_at = rec.finished_at or time.time()
        return rec

    async def wait(self, thread_id: str, timeout: float = 120.0) -> CaseRecord:
        """Until the case next pauses, finishes or fails."""
        rec = self.get(thread_id)
        await asyncio.wait_for(rec.settled.wait(), timeout)
        return rec

    def get(self, thread_id: str) -> CaseRecord:
        if thread_id not in self.cases:
            raise KeyError(thread_id)
        return self.cases[thread_id]

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ----- after a restart -----

    async def rehydrate(self) -> int:
        """Rebuild the case list from the audit log and checkpoints, including pending approvals."""
        seen: dict[str, str] = {}
        for e in self.audit.events(limit=1_000_000):
            if e.merchant_id and e.thread_id not in seen:
                seen[e.thread_id] = e.merchant_id
        restored = 0
        for thread_id, merchant_id in seen.items():
            if thread_id in self.cases:
                continue
            snap = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
            if not snap.values:
                continue
            rec = self._assign(CaseRecord(thread_id, merchant_id, snap.values.get("channel", "api"),
                                          snap.values.get("trigger_causes")))  # fmt: skip
            self.cases[thread_id] = rec
            await self.refresh(thread_id)
            rec.settled.set()
            restored += 1
        return restored

    def agents_view(self) -> list[dict[str, Any]]:
        """Each rep with its merchants and what it is doing right now."""
        out = []
        for a in self.roster.agents:
            mine = [c for c in self.cases.values() if c.agent_id == a.id]
            out.append({
                "id": a.id, "name": a.name, "merchants": list(a.merchants),
                "concurrency": self.roster.concurrency,
                "working": sum(c.status == "running" for c in mine),
                "waiting": sum(c.status == "paused" for c in mine),
                "finished": sum(c.status == "done" for c in mine),
                "handed_to_person": sum(bool(c.summary.get("escalation_reason")) for c in mine),
                "active_merchants": sorted({c.merchant_id for c in mine if c.status in ("running", "paused")}),
            })  # fmt: skip
        return out

    # ----- proactive monitor -----

    async def scan_and_start(
        self, merchant_ids: list[str] | None = None, limit: int = 20
    ) -> list[dict]:
        busy = {
            (c.merchant_id, topic)
            for c in self.cases.values()
            if c.status in ("running", "paused")
            for topic in self._topics(c)
        }
        triggers = await monitor.scan(self.tools, merchant_ids, busy)
        started = []
        for t in triggers[:limit]:
            rec = self.start(t.merchant_id, None, "proactive", t.causes)
            started.append({"thread_id": rec.thread_id, "merchant_id": t.merchant_id,
                            "topic": t.topic, "causes": t.causes})  # fmt: skip
        return started

    @staticmethod
    def _topics(c: CaseRecord) -> set[str]:
        from backend.agent.playbook import TOPIC_OF
        from backend.gate.evidence import Cause

        return {TOPIC_OF[Cause(x)] for x in (c.trigger or [])}

    # ----- steps the merchant takes outside the chat -----

    async def poll_pauses(self) -> list[str]:
        """Resume cases whose awaited step has happened in the systems (the merchant tapped the
        KYC link, corrected the bank details, power-cycled the device). Returns resumed ids."""
        resumed = []
        for rec in list(self.cases.values()):
            pause = rec.pause or {}
            if rec.status != "paused" or pause.get("type") != "merchant_action":
                continue
            if await self._step_done(rec.merchant_id, pause):
                try:
                    self.resume(rec.thread_id, {"done": True})
                    resumed.append(rec.thread_id)
                except ValueError:
                    pass  # answered by someone else in the meantime
        return resumed

    async def _step_done(self, merchant_id: str, pause: dict[str, Any]) -> bool:
        what = pause["waiting_for"]
        try:
            if what == "kyc_link":
                return (
                    pause["ref"] not in (await self.tools.get_kyc_status(merchant_id)).pending_links
                )
            if what == "bank_update":
                return (await self.tools.get_merchant_profile(merchant_id)).bank_status == "ACTIVE"
            if what == "power_cycle":
                return (await self.tools.get_device(pause["device_id"])).online
        except ToolCallError:
            return False
        return False

    def start_watcher(self, interval: float) -> asyncio.Task:
        async def loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.poll_pauses()
                except Exception:  # never let the watcher die
                    pass

        task = asyncio.create_task(loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ----- numbers for the metrics page -----

    def metrics(self) -> dict[str, Any]:
        cases = list(self.cases.values())
        done = [c for c in cases if c.status == "done"]
        by_outcome: dict[str, int] = {}
        for c in done:
            by_outcome[str(c.summary.get("outcome"))] = (
                by_outcome.get(str(c.summary.get("outcome")), 0) + 1
            )
        resolved = [c for c in done if c.summary.get("outcome") == "RESOLVED_VERIFIED"]
        auto = [c for c in resolved if c.summary.get("approvals_needed", 0) == 0]
        seconds = [c.finished_at - c.started_at for c in resolved if c.finished_at]
        total_closed = len(done)
        return {
            "cases_total": len(cases),
            "running": sum(c.status == "running" for c in cases),
            "waiting": sum(c.status == "paused" for c in cases),
            "pending_approvals": sum(
                bool(c.pause and c.pause.get("type") == "approval") for c in cases
            ),
            "errors": sum(c.status == "error" for c in cases),
            "finished": total_closed,
            "by_outcome": by_outcome,
            "verified_resolutions": len(resolved),
            "auto_resolved": len(auto),
            "auto_resolved_rate": round(len(auto) / total_closed, 3) if total_closed else None,
            "escalation_rate": round(by_outcome.get("ESCALATED", 0) / total_closed, 3)
            if total_closed
            else None,
            "proactive_cases": sum(bool(c.trigger) for c in cases),
            "median_seconds_to_verified": round(statistics.median(seconds), 1) if seconds else None,
        }
