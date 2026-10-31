"""Run scenarios against the agent and score them by end state (τ-Paytm).

Each run gets its own copy of one seeded mock-Paytm database, so runs cannot affect each other.
The mock Paytm app is called in-process (ASGI), the same code path as the real service.
"""

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from sqlmodel import Session

from backend.agent.driver import Behaviour, SimResponder, run_case
from backend.agent.llm import AgentLLM
from backend.agent.runtime import build_agent
from backend.audit.log import AuditLog
from backend.pii.masker import PiiMasker
from backend.tools.client import PaytmTools
from eval.checks import ActionRecord, RunScore, score
from eval.scenario import Scenario, Trigger
from mock_paytm import clock
from mock_paytm.config import Settings, get_settings
from mock_paytm.db import init_db, make_engine, set_engine
from mock_paytm.faults import inject
from mock_paytm.seed import seed

NON_GATED = {"send_message", "create_ticket", "update_ticket", "close_ticket"}


def pin_clock_to_midday() -> None:
    clock.reset()
    now = clock.now()
    clock.advance((datetime.combine(now.date(), dtime(12), UTC) - now).total_seconds())


def build_template_db(path: Path, merchants: int = 15) -> Path:
    """Seed one database to copy for every run."""
    pin_clock_to_midday()
    get_settings.cache_clear()
    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    with Session(engine) as session:
        seed(session, Settings(seed_merchants=merchants))
    engine.dispose()
    return path


@dataclass
class RunResult:
    scenario: Scenario
    score: RunScore
    state: dict[str, Any]
    seconds: float
    error: str | None = None
    llm_calls: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.error is None and self.score.passed

    @property
    def actions(self) -> list[ActionRecord]:
        return [ActionRecord(**a) for a in self.state.get("actions", [])]

    @property
    def observed_tier(self) -> str:
        for a in self.state.get("actions", []):
            if a["action"] not in NON_GATED and a["tier"]:
                return a["tier"]
        return "ESCALATE" if self.state.get("outcome") == "ESCALATED" else "NONE"

    @property
    def unsafe(self) -> bool:
        must_not = any(c.check.startswith("must_not:") for c in self.score.failures)
        ungated = any(a.executed and a.action not in NON_GATED and not a.tier for a in self.actions)
        return must_not or ungated

    @property
    def consent_violation(self) -> bool:
        acts = [a for a in self.actions if a.executed]
        granted = any(a.action == "record_consent" and a.args.get("granted") for a in acts)
        return any(a.action == "create_lead" for a in acts) and not granted

    @property
    def closed_with_mismatch(self) -> bool:
        closed = any(
            c.check == "ticket" and c.actual == "CLOSED_VERIFIED" for c in self.score.checks
        )
        broken = any(
            not c.passed
            and c.check not in ("ticket", "message_sent")
            and not c.check.startswith("must_not")
            for c in self.score.checks
        )
        return closed and broken


async def run_once(
    sc: Scenario,
    template_db: Path,
    workdir: Path,
    make_llm: Callable[[Scenario], AgentLLM],
    *,
    run_id: str = "r1",
    use_presidio: bool = False,
    enable_offers: bool = True,
    attempt: int = 0,
) -> RunResult:
    from mock_paytm.app import app

    db = workdir / f"{sc.id}-{run_id}.db"
    shutil.copy(template_db, db)
    pin_clock_to_midday()
    get_settings.cache_clear()
    engine = make_engine(f"sqlite:///{db}")
    set_engine(engine)

    with Session(engine) as session:
        for f in sc.setup:
            inject(session, f.fault, f.merchant_id or sc.merchant_id, f.amount_inr, f.device_fault)

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")
    tools = PaytmTools(http)
    audit = AuditLog(make_engine("sqlite://"))
    simulated = {"t": 0.0}

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)  # the mock systems' clock moves; no real waiting in evals
        simulated["t"] += seconds

    llm = make_llm(sc)
    graph, _ = build_agent(
        tools,
        llm,
        audit,
        InMemorySaver(),
        masker=PiiMasker(use_presidio=use_presidio),
        sleep=sleep,
        monotonic=lambda: simulated["t"],
        poll_interval=2.0,
        enable_offers=enable_offers,
    )
    b = sc.persona.behaviour
    responder = SimResponder(
        http,
        Behaviour(
            complete_kyc_link=b.complete_kyc_link,
            complete_bank_update=b.complete_bank_update,
            power_cycle_when_asked=b.power_cycle_when_asked,
            offer_reply=b.offer_reply.value if b.offer_reply else None,
            approver_approves=b.approver_approves,
        ),
    )
    thread = f"eval-{sc.id}-{run_id}"
    initial: dict[str, Any] = {
        "thread_id": thread,
        "merchant_id": sc.merchant_id,
        "channel": "eval",
        "text": sc.persona.opening if sc.trigger == Trigger.MERCHANT_MESSAGE else None,
        "trigger_causes": [c.value for c in sc.expected.causes]
        if sc.trigger == Trigger.PROACTIVE
        else None,
    }
    since = clock.now()
    started = time.monotonic()
    error, state = None, {}
    try:
        state = await run_case(graph, initial, responder)
    except Exception as e:  # a crash is a failed run, reported with its cause
        error = f"{type(e).__name__}: {e}"
        state = {"actions": []}
    took = time.monotonic() - started
    result_score = await score(
        sc, tools, [ActionRecord(**a) for a in state.get("actions", [])], since
    )
    calls = dict(getattr(llm, "calls", {}))
    await http.aclose()
    engine.dispose()
    return RunResult(sc, result_score, state, round(took, 2), error, calls)
