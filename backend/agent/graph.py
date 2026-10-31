"""The Sahayak agent: a LangGraph state machine (PRD §5.2, design decision D5).

    ingest ─► diagnose ─► plan ─► gate ─┬─► execute ─► plan …
        │                  │            ├─► approval_notice ─► approval (interrupt) ─► execute
        │                  │            └─► escalate
        │                  ├─► ask_merchant ─► await_merchant (interrupt) ─► plan
        │                  ├─► stall
        │                  └─► verify ─► resolve ─► offer_check ─► offer_ask ─► offer_wait ─► offer_record
        ├─► escalate / refuse / respond / idle

Every step that changes a merchant's systems passes through the deterministic gate. Interrupts sit
at the top of their own nodes, before any side effect, so a resumed node never repeats an action.
Messages and ticket notes are logged but not gated (they change no merchant state).
"""

import asyncio
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.agent import playbook
from backend.agent.llm import AgentLLM
from backend.agent.messages import (
    disclosure,
    inr,
    scrub,
    template,
    ungrounded_numbers,
)
from backend.agent.schemas import Classification
from backend.agent.signals import MESSAGE_SIGNALS, rule_signals
from backend.agent.sop import procedures
from backend.audit.log import AuditLog, hash_text
from backend.gate.confidence import score, self_consistency
from backend.gate.engine import Gate, ProposedAction, Tier
from backend.gate.evidence import (
    CAUSE_ACTIONS,
    Finding,
    contact_count,
    detect,
    evidence_rule_match,
    signals,
)
from backend.labels import humanize
from backend.pii.masker import PiiMasker, PiiVault
from backend.tools.client import PaytmTools, ToolCallError
from backend.tools.context import MerchantContext, fetch_context
from mock_paytm import schemas as s

DEVANAGARI = re.compile("[\u0900-\u097f]")
MAX_STEPS = 14
MAX_VERIFY_ROUNDS = 3


class AgentState(TypedDict, total=False):
    # input
    thread_id: str
    merchant_id: str
    channel: str
    text: str | None
    trigger_causes: list[str] | None
    # conversation (set by a channel such as WhatsApp)
    history: list[dict]  # earlier turns: {"role": "merchant" | "sahayak", "text": ...}
    chat_only: bool  # answer and explain, but start no new fixes (something else is already open)
    open_note: str | None  # what the open case is waiting for, in plain words
    # understanding
    vault: dict
    masked: str
    cls: dict
    ctx: dict
    ctx_fresh: bool
    scope_topics: list[str]
    category: str | None
    signals: list[str]
    contact_count: int
    gate_case: dict
    case_escalate: bool
    gate: dict
    diagnosis: dict
    # working
    case: dict
    next: dict | None
    pending: dict | None
    waiting: dict | None
    approved: bool
    exec_failed: bool
    steps: int
    verify_rounds: int
    ticket_id: str | None
    consents: list[str]
    offer: dict | None
    offer_reply: str | None
    # output
    actions: list[dict]
    messages: list[str]
    disclosed: bool
    agent_name: str
    verified: bool
    proof: dict
    proof_text: str
    escalation_reason: str | None
    handoff: dict | None
    outcome: str | None


@dataclass
class Deps:
    tools: PaytmTools
    llm: AgentLLM
    gate: Gate
    audit: AuditLog
    masker: PiiMasker
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    # Called after each message is sent: (thread_id, merchant_id, channel, text, kind, language).
    # A channel (WhatsApp) uses it to deliver the message to the merchant's phone.
    on_message: Callable[..., Awaitable[None]] | None = None
    verify_timeout: float = 45.0
    poll_interval: float = 2.0
    samples: int = 3
    enable_offers: bool = True
    public_url: str = "http://localhost:8001"
    notes: dict[str, Any] = field(default_factory=dict)


def _dump(model: Any) -> Any:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model


def build_graph(deps: Deps, checkpointer: Any = None):
    tools, llm, gate, audit, masker = deps.tools, deps.llm, deps.gate, deps.audit, deps.masker

    # ---------- helpers ----------

    def log(state: AgentState, node: str, event: str, **kw: Any):
        return audit.append(
            thread_id=state["thread_id"],
            merchant_id=state["merchant_id"],
            node=node,
            event=event,
            **kw,
        )

    def rec(state: AgentState, action: str, args: dict, executed: bool, tier: str | None = None):
        entry = {
            "action": action,
            "merchant_id": state["merchant_id"],
            "args": args,
            "executed": executed,
            "tier": tier,
        }
        return [*state.get("actions", []), entry]

    def scope_of(state: AgentState, ctx: MerchantContext) -> list[Finding]:
        return playbook.scope_findings(detect(ctx), set(state.get("scope_topics", [])))

    async def say(
        state: AgentState, kind: str, facts: dict[str, Any], use_llm: bool = True
    ) -> dict:
        """Compose, check, send and record one message. Returns the state updates."""
        cls = state.get("cls", {})
        profile = state["ctx"]["profile"]
        lang = cls.get("language") or profile["language"]
        script = cls.get("script", "latin")
        address = profile["owner_name"].split()[0] + " ji"
        text, fallback_reason = "", None
        if use_llm:
            try:
                shown = {**facts, "first_message": not state.get("disclosed")}
                text = scrub(await llm.compose(kind, shown, lang, script, address))
                bad = ungrounded_numbers(text, facts)
                if script == "latin" and DEVANAGARI.search(text):
                    fallback_reason = "mixed scripts (Devanagari in a Latin-script message)"
                elif bad:
                    fallback_reason = f"numbers not in facts: {sorted(bad)}"
                elif not text:
                    fallback_reason = "empty message"
            except Exception as e:  # LLM down or unusable: never block the merchant's update
                fallback_reason = f"{type(e).__name__}: {str(e)[:120]}"
        else:
            fallback_reason = "template"
        if fallback_reason:
            text = template(kind, facts, lang)
            if use_llm:
                log(
                    state,
                    "message",
                    "MESSAGE_FALLBACK",
                    result={"kind": kind, "why": fallback_reason},
                )
        if not state.get("disclosed"):
            text = f"{disclosure(lang, state.get('agent_name'))}\n{text}"
        await tools.send_message(state["thread_id"], state["merchant_id"], text)
        log(
            state,
            "message",
            "MESSAGE_SENT",
            action="send_message",
            result={"kind": kind, "text": text},
        )
        if deps.on_message:
            try:
                await deps.on_message(
                    state["thread_id"],
                    state["merchant_id"],
                    state.get("channel", ""),
                    text,
                    kind,
                    lang,
                )
            except Exception as e:  # a delivery problem must never break the fix itself
                log(
                    state,
                    "message",
                    "DELIVERY_FAILED",
                    result={"error": f"{type(e).__name__}: {e}"[:200]},
                )
        return {
            "messages": [*state.get("messages", []), text],
            "actions": rec(state, "send_message", {"text": text, "kind": kind}, True),
            "disclosed": True,
        }

    async def run_action(state: AgentState, action: str, args: dict, case: dict) -> dict:
        thread, mid = state["thread_id"], state["merchant_id"]
        case.setdefault("tracking", {})
        match action:
            case "send_kyc_link":
                r = await tools.send_kyc_link(thread, mid)
                case["links"][r.token] = r.url
            case "raise_bank_update":
                r = await tools.raise_bank_update(thread, mid, args["reason"])
                case["bank_token"] = r.token
                case["links"][r.token] = r.url
            case "retrigger_settlement":
                r = await tools.retrigger_settlement(thread, args["settlement_id"])
                case["retriggered"].append(args["settlement_id"])
            case "remote_device_reset":
                r = await tools.remote_device_reset(thread, args["device_id"])
                case["reset"][args["device_id"]] = r.result
            case "book_device_replacement":
                r = await tools.book_device_replacement(thread, args["device_id"])
                case["replacements"].append(args["device_id"])
                case["tracking"][args["device_id"]] = r.tracking_id
            case "stop_rental":
                r = await tools.stop_rental(thread, args["plan_id"])
                case["stopped"].append(args["plan_id"])
            case "reverse_rental_charge":
                r = await tools.reverse_rental_charge(thread, args["deduction_id"])
                case["reversed"].append(args["deduction_id"])
            case "create_refund_request":
                r = await tools.create_refund_request(
                    thread, mid, args["amount_inr"], args["reason"], args["transaction_id"]
                )
                case["refunded"].append(args["transaction_id"])
                case["refund_ids"].append(r.id)
            case _:
                raise ToolCallError("UNKNOWN_ACTION", action, 400)
        return _dump(r)

    # ---------- nodes ----------

    async def ingest(state: AgentState) -> dict:
        mid, text = state["merchant_id"], state.get("text") or ""
        vault = PiiVault.from_dict(state["vault"]) if state.get("vault") else PiiVault()
        masked = masker.mask(text, vault) if text else ""
        history = [
            {"role": h["role"], "text": masker.mask(h["text"], vault)}
            for h in state.get("history", [])
        ]

        async def classify() -> Classification:
            if not text:
                return Classification(intent="report_problem")
            try:
                return await llm.classify(masked, history)
            except Exception as e:
                log(state, "ingest", "CLASSIFY_FALLBACK", result={"error": str(e)[:200]})
                return Classification()  # conservative defaults; rule signals still apply

        ctx, cls = await asyncio.gather(fetch_context(tools, mid), classify())
        findings = detect(ctx)
        scope = playbook.pick_scope(findings, cls.intent, cls.topic, state.get("trigger_causes"))
        topics = sorted({playbook.TOPIC_OF[f.cause] for f in scope})
        category = playbook.CATEGORY_OF_TOPIC.get(topics[0]) if topics else None

        sigs = rule_signals(text) | (set(cls.signals) & MESSAGE_SIGNALS) | signals(scope)
        count = contact_count(ctx, category, deps.now()) if category else 1
        decision = gate.decide(
            ProposedAction(
                action="create_ticket",
                confidence=1.0,
                sentiment=cls.sentiment,
                signals=sigs,
                contact_count=count,
            )
        )
        case_escalate = decision.tier == Tier.ESCALATE
        log(
            state,
            "ingest",
            "UNDERSTOOD",
            input_hash=hash_text(masked),
            evidence_ids=[e for f in scope for e in f.evidence],
            gate=decision.to_dict(),
            result={
                "classification": cls.model_dump(),
                "findings": [f.cause.value for f in findings],
                "scope": [f.cause.value for f in scope],
                "signals": sorted(sigs),
                "contact_count": count,
            },
        )
        return {
            "vault": vault.to_dict(),
            "history": history,
            "masked": masked,
            "cls": cls.model_dump(),
            "ctx": _dump(ctx),
            "ctx_fresh": True,
            "scope_topics": topics,
            "category": category,
            "signals": sorted(sigs),
            "contact_count": count,
            "gate_case": decision.to_dict(),
            "case_escalate": case_escalate,
            "case": state.get("case") or playbook.new_case(),
            "actions": state.get("actions", []),
            "messages": state.get("messages", []),
            "steps": 0,
            "verify_rounds": 0,
            "consents": state.get("consents", []),
        }

    def route_ingest(state: AgentState) -> str:
        cls = state["cls"]
        if cls["intent"] == "other_merchant_data":
            return "refuse"
        if state["case_escalate"]:
            return "escalate"
        if state.get("chat_only"):
            return "respond"  # something is already open: answer, but start no second fix
        if state["scope_topics"]:
            return "diagnose"
        if state.get("trigger_causes"):
            return "idle"
        if cls["intent"] == "loan_request":
            return "offer_check"
        return "respond"

    async def diagnose(state: AgentState) -> dict:
        ctx = MerchantContext.model_validate(state["ctx"])
        scope = scope_of(state, ctx)
        fjson = [f.model_dump(mode="json") for f in scope]
        allowed = {f.cause.value: list(CAUSE_ACTIONS[f.cause]) for f in scope}
        sop = procedures([f.cause for f in scope])
        masked = state.get("masked", "")

        subject = (masked or f"Proactive: {scope[0].detail}")[:80]
        ticket = await tools.create_ticket(
            state["thread_id"],
            state["merchant_id"],
            state["category"] or "OTHER",
            subject,
            note=f"Opened by Sahayak ({state.get('channel', 'chat')})",
        )

        async def sample():
            try:
                return await llm.diagnose(masked, fjson, allowed, sop)
            except Exception:
                return None

        samples = await asyncio.gather(*[sample() for _ in range(deps.samples)])
        votes: list[Any] = [
            (d.cause, d.action) if d else (f"__failed_{i}__", None) for i, d in enumerate(samples)
        ]
        valid = [v for v in votes if not str(v[0]).startswith("__failed_")]
        cause, action = Counter(valid).most_common(1)[0][0] if valid else (None, None)
        consistency = self_consistency(votes) if valid else 0.0

        schema_ok = bool(valid) and (
            (cause is None and action is None)
            or (cause in allowed and (action is None or action in allowed[cause]))
        )
        evidence = evidence_rule_match(scope, cause or "", action) if cause else 0.0
        verifier = False
        if cause:
            try:
                verifier = (await llm.verify_plan(fjson, cause, action, sop)).passed
            except Exception:
                verifier = False
        breakdown = score(
            evidence_rule_match=evidence,
            self_consistency=consistency,
            verifier_pass=verifier,
            schema_valid=schema_ok,
            weights=gate.policy.confidence_weights,
        )
        diagnosis = {
            "cause": cause,
            "action": action,
            "confidence": breakdown.model_dump(),
            "explain": breakdown.explain(),
            "samples": [(d.cause, d.action) if d else None for d in samples],
        }
        log(
            state,
            "diagnose",
            "DIAGNOSED",
            evidence_ids=[e for f in scope for e in f.evidence],
            confidence=breakdown.model_dump(),
            result={"cause": cause, "action": action, "samples": diagnosis["samples"]},
        )
        return {
            "ticket_id": ticket.id,
            "diagnosis": diagnosis,
            "actions": rec(state, "create_ticket", {"category": state["category"]}, True),
        }

    async def plan(state: AgentState) -> dict:
        if state.get("steps", 0) >= MAX_STEPS:
            return {
                "next": {"type": "escalate", "reason": "too many steps without a verified fix"},
                "ctx_fresh": False,
            }
        if state.get("ctx_fresh"):
            ctx = MerchantContext.model_validate(state["ctx"])
        else:
            ctx = await fetch_context(tools, state["merchant_id"])
        scope = scope_of(state, ctx)
        nxt = playbook.next_step(scope, ctx, state["case"])
        log(
            state,
            "plan",
            "PLANNED",
            evidence_ids=[e for f in scope for e in f.evidence],
            result={"next": nxt, "open_causes": [f.cause.value for f in scope]},
        )
        return {"ctx": _dump(ctx), "next": nxt or {"type": "none"}, "ctx_fresh": False}

    def route_plan(state: AgentState) -> str:
        return {
            "step": "gate",
            "wait": "ask_merchant",
            "escalate": "escalate",
            "stalled": "stall",
            "none": "verify",
        }[state["next"]["type"]]

    async def gate_node(state: AgentState) -> dict:
        nxt, diag = state["next"], state["diagnosis"]
        proposal = ProposedAction(
            action=nxt["action"],
            args=nxt["args"],
            amount_inr=nxt["amount_inr"],
            confidence=diag["confidence"]["total"],
            sentiment=state["cls"]["sentiment"],
            signals=set(state["signals"]),
            contact_count=state["contact_count"],
            consents=set(state.get("consents", [])),
        )
        decision = gate.decide(proposal)
        log(
            state,
            "gate",
            "GATE_DECIDED",
            action=nxt["action"],
            args=nxt["args"],
            gate=decision.to_dict(),
            confidence=diag["confidence"],
        )
        update: dict = {
            "pending": {"step": nxt, "tier": decision.tier.name, "decision": decision.to_dict()},
            "gate": decision.to_dict(),
        }
        if decision.tier == Tier.ESCALATE:
            update["actions"] = rec(state, nxt["action"], nxt["args"], False, "ESCALATE")
            update["escalation_reason"] = decision.reason
        return update

    def route_gate(state: AgentState) -> str:
        return {"AUTO": "execute", "APPROVE": "approval_notice", "ESCALATE": "escalate"}[
            state["pending"]["tier"]
        ]

    async def approval_notice(state: AgentState) -> dict:
        return await say(state, "approval_pending", {})

    async def approval(state: AgentState) -> dict:
        pend = state["pending"]
        step = pend["step"]
        resp = interrupt(
            {
                "type": "approval",
                "action": step["action"],
                "args": step["args"],
                "amount_inr": step["amount_inr"],
                "summary": step["summary"],
                "reason": pend["decision"]["reason"],
                "rules": [r["rule"] for r in pend["decision"]["rules"]],
                "confidence": state["diagnosis"]["explain"],
            }
        )
        resp = resp or {}
        approved = bool(resp.get("approved"))
        approver = resp.get("approver", "human")
        log(
            state,
            "approval",
            "APPROVED" if approved else "REJECTED",
            action=step["action"],
            actor=approver,
            args=step["args"],
        )
        update: dict = {"approved": approved}
        if not approved:
            update["actions"] = rec(state, step["action"], step["args"], False, "APPROVE")
            update["escalation_reason"] = f"approver rejected: {step['summary']}"
        return update

    def route_approval(state: AgentState) -> str:
        return "execute" if state["approved"] else "escalate"

    async def execute(state: AgentState) -> dict:
        pend = state["pending"]
        step = pend["step"]
        case = {**state["case"]}
        for k in ("links", "reset", "tracking"):
            case[k] = dict(case.get(k, {}))
        for k in ("retriggered", "waited", "declined", "stopped", "reversed", "refunded",
                  "refund_ids", "replacements"):  # fmt: skip
            case[k] = list(case.get(k, []))
        try:
            result = await run_action(state, step["action"], step["args"], case)
        except ToolCallError as e:
            log(state, "execute", "EXECUTION_REFUSED", action=step["action"], args=step["args"],
                result={"code": e.code, "message": e.message})  # fmt: skip
            return {
                "actions": rec(state, step["action"], step["args"], False, pend["tier"]),
                "escalation_reason": f"{step['action']} refused by Paytm systems: {e.code}",
                "exec_failed": True,
            }
        log(state, "execute", "EXECUTED", action=step["action"], args=step["args"],
            actor="sahayak" if pend["tier"] == "AUTO" else "sahayak+approver", result=result)  # fmt: skip
        return {
            "case": case,
            "actions": rec(state, step["action"], step["args"], True, pend["tier"]),
            "steps": state.get("steps", 0) + 1,
            "pending": None,
            "exec_failed": False,
            "ctx_fresh": False,
        }

    def route_execute(state: AgentState) -> str:
        return "escalate" if state.get("exec_failed") else "plan"

    async def ask_merchant(state: AgentState) -> dict:
        w = state["next"]
        ctx = MerchantContext.model_validate(state["ctx"])
        case = state["case"]
        facts: dict[str, Any] = {}
        if w["kind"] in ("kyc_link", "bank_update"):
            url = case["links"].get(w["ref"]) or (
                f"{deps.public_url}/sim/{'kyc' if w['kind'] == 'kyc_link' else 'bank-update'}"
                f"/{w['ref']}/complete"
            )
            facts["url"] = url
            held = sum(f.amount_inr for f in scope_of(state, ctx))
            if held:
                facts["held_amount"] = inr(held)  # pre-formatted: the model copies it verbatim
        else:
            dev = next((d for d in ctx.devices if d.id == w.get("device_id")), None)
            facts["device"] = f"{dev.type.value.title()} ({dev.model})" if dev else "device"
        upd = await say(state, w["kind"], facts)
        return {**upd, "waiting": {**w}}

    async def await_merchant(state: AgentState) -> dict:
        w = state["waiting"]
        resp = interrupt(
            {
                "type": "merchant_action",
                "waiting_for": w["kind"],
                "ref": w["ref"],
                "device_id": w.get("device_id"),
            }
        )
        done = bool((resp or {}).get("done", True))
        case = {**state["case"], "waited": [*state["case"]["waited"], w["ref"]]}
        case["declined"] = list(state["case"]["declined"]) + ([] if done else [w["ref"]])
        log(state, "await_merchant", "MERCHANT_RESPONDED", result={"waiting_for": w["kind"], "done": done},
            actor="merchant")  # fmt: skip
        return {"case": case, "waiting": None, "ctx_fresh": False}

    async def verify(state: AgentState) -> dict:
        case = state["case"]
        started = deps.monotonic()
        while True:
            refunds = [await tools.get_refund(r) for r in case["refund_ids"]]
            ctx = await fetch_context(tools, state["merchant_id"])
            inflight = any(x.status == "PROCESSING" for x in ctx.settlements) or any(
                r.status != "POSTED" for r in refunds
            )
            if not inflight or deps.monotonic() - started >= deps.verify_timeout:
                break
            await deps.sleep(deps.poll_interval)
        remaining = scope_of(state, ctx)
        verified = not remaining and not inflight

        retriggered = [x for x in ctx.settlements if x.id in case["retriggered"]]
        reversed_ = [d for p in ctx.billing.plans for d in p.deductions if d.id in case["reversed"]]
        proof: dict[str, Any] = {}
        if retriggered:
            proof["settlements"] = [
                {"amount_inr": x.amount_inr, "utr": x.utr, "status": x.status.value}
                for x in retriggered
            ]
        if reversed_:
            proof["rent_reversed"] = [
                {"amount_inr": d.amount_inr, "reversal_ref": d.reversal_ref} for d in reversed_
            ]
        if refunds:
            proof["credit_adjustments"] = [
                {"amount_inr": r.amount_inr, "status": r.status.value} for r in refunds
            ]
        for did in case["reset"]:
            dev = next((d for d in ctx.devices if d.id == did), None)
            if dev and dev.online:
                proof.setdefault("devices_online", []).append(f"{dev.type.value} ({dev.model})")
        for tracking in case.get("tracking", {}).values():
            proof.setdefault("replacements", []).append({"tracking_id": tracking})
        if "send_kyc_link" in [a["action"] for a in state.get("actions", []) if a["executed"]]:
            proof["kyc_status"] = ctx.kyc.kyc_status.value

        parts = []
        for x in proof.get("settlements", []):
            parts.append(f"{inr(x['amount_inr'])} credited (UTR {x['utr']})")
        for x in proof.get("rent_reversed", []):
            parts.append(f"{inr(x['amount_inr'])} rent reversed (ref {x['reversal_ref']})")
        for x in proof.get("credit_adjustments", []):
            parts.append(f"{inr(x['amount_inr'])} credited back ({x['status'].lower()})")
        for x in proof.get("devices_online", []):
            parts.append(f"{x} is back online")
        for x in proof.get("replacements", []):
            parts.append(f"replacement booked (tracking {x['tracking_id']})")
        proof_text = "; ".join(parts)
        log(state, "verify", "VERIFIED", verified=verified, result={"proof": proof, "inflight": inflight,
            "open_causes": [f.cause.value for f in remaining]})  # fmt: skip
        update: dict = {
            "ctx": _dump(ctx),
            "ctx_fresh": True,
            "verified": verified,
            "proof": proof,
            "proof_text": proof_text,
            "verify_rounds": state.get("verify_rounds", 0) + 1,
        }
        if not verified and inflight:
            update["escalation_reason"] = "fix started but could not be confirmed in time"
        elif not verified and update["verify_rounds"] >= MAX_VERIFY_ROUNDS:
            update["escalation_reason"] = "fix not confirmed after repeated checks"
        return update

    def route_verify(state: AgentState) -> str:
        if state["verified"]:
            return "resolve"
        if state.get("escalation_reason"):
            return "escalate"
        return "plan"

    async def resolve(state: AgentState) -> dict:
        ticket_id = state.get("ticket_id")
        actions = state.get("actions", [])
        if ticket_id:
            note = f"Verified by Sahayak. {state.get('proof_text', '')}".strip()
            await tools.close_ticket(state["thread_id"], ticket_id, note, True)
            actions = rec(state, "close_ticket", {"verified": True}, True)
        state = {**state, "actions": actions}
        facts = {"proof": state.get("proof_text", ""), **state.get("proof", {})}
        upd = await say(state, "resolved", facts)
        log(state, "resolve", "RESOLVED", verified=True, result={"proof": state.get("proof")})
        return {**upd, "outcome": "RESOLVED_VERIFIED", "pending": None}

    def route_resolve(state: AgentState) -> str:
        if (
            deps.enable_offers
            and not state.get("trigger_causes")
            and state["cls"]["sentiment"] != "angry"
            and not state.get("signals")
        ):
            return "offer_check"
        return END

    async def offer_check(state: AgentState) -> dict:
        el = await tools.check_loan_eligibility(state["merchant_id"])
        if not el.eligible:
            update: dict = {"offer": None}
            if state["cls"]["intent"] == "loan_request" and not state.get("outcome"):
                update.update(await say(state, "no_offer", {}))
                update["outcome"] = "INFO_ONLY"
            return update
        k = el.kfs
        return {
            "offer": {
                "offer_id": el.offer.id,
                "notice_version": k.notice_version,
                "kfs": k.model_dump(mode="json"),
            }
        }

    def route_offer_check(state: AgentState) -> str:
        return "offer_ask" if state.get("offer") else END

    async def offer_ask(state: AgentState) -> dict:
        k = state["offer"]["kfs"]
        facts = {
            **k,
            "amount": inr(k["loan_amount_inr"]),
            "tenure": k["tenure_months"],
            "apr": k["apr_percent"],
            "days": k["cooling_off_days"],
        }
        return await say(state, "offer", facts)

    async def offer_wait(state: AgentState) -> dict:
        resp = interrupt({"type": "offer_reply", "offer_id": state["offer"]["offer_id"],
                          "kfs": state["offer"]["kfs"]})  # fmt: skip
        reply = (resp or {}).get("reply")
        log(state, "offer", "OFFER_REPLY", actor="merchant", result={"reply": reply})
        return {"offer_reply": reply}

    async def offer_record(state: AgentState) -> dict:
        reply = (state.get("offer_reply") or "").strip()
        if not reply:
            return {}
        granted = reply.upper().startswith(("HAAN", "HAA", "YES", "Y"))
        offer = state["offer"]
        thread, mid = state["thread_id"], state["merchant_id"]
        base = dict(
            confidence=1.0, sentiment=state["cls"]["sentiment"], signals=set(), contact_count=1
        )

        d1 = gate.decide(ProposedAction(action="record_consent", **base))
        log(state, "offer", "GATE_DECIDED", action="record_consent", gate=d1.to_dict())
        consent = await tools.record_consent(
            thread,
            mid,
            s.ConsentIn(
                purpose="LOAN_OFFER",
                notice_version=offer["notice_version"],
                granted=granted,
                channel=state.get("channel", "chat"),
                offer_id=offer["offer_id"],
                button_payload="HAAN" if granted else "NAHI",
            ),
        )
        actions = rec(
            state, "record_consent", {"granted": granted, "purpose": "LOAN_OFFER"}, True, "AUTO"
        )
        log(state, "offer", "CONSENT_RECORDED", action="record_consent", result=_dump(consent))
        state = {**state, "actions": actions}
        if not granted:
            upd = await say(state, "offer_declined", {})
            return {
                **upd,
                "consents": state.get("consents", []),
                "outcome": state.get("outcome") or "OFFER_DECLINED",
            }

        consents = [*state.get("consents", []), "LOAN_OFFER"]
        d2 = gate.decide(ProposedAction(action="create_lead", consents=set(consents), **base))
        log(state, "offer", "GATE_DECIDED", action="create_lead", gate=d2.to_dict())
        if d2.tier != Tier.AUTO:
            return {"actions": actions, "consents": consents}
        lead = await tools.create_lead(thread, mid, offer["offer_id"], consent.id)
        actions = rec(state, "create_lead", {"offer_id": offer["offer_id"]}, True, "AUTO")
        log(state, "offer", "LEAD_CREATED", action="create_lead", result=_dump(lead))
        state = {**state, "actions": actions}
        upd = await say(state, "offer_thanks", {"days": offer["kfs"]["cooling_off_days"]})
        return {**upd, "consents": consents, "outcome": state.get("outcome") or "OFFER_ACCEPTED"}

    async def respond(state: AgentState) -> dict:
        """Answer any question from the merchant's own account, the procedures and the conversation."""
        ctx = MerchantContext.model_validate(state["ctx"])
        findings = detect(ctx)
        account: dict[str, Any] = {
            "kyc_status": humanize(ctx.kyc.kyc_status.value),
            "kyc_expires_in_days": ctx.kyc.days_to_expiry,
            "recent_settlements": [
                {
                    "date": x.business_date.strftime("%d %b"),
                    "amount": inr(x.amount_inr),
                    **(
                        {"rent_deducted": inr(x.rental_deducted_inr)}
                        if x.rental_deducted_inr
                        else {}
                    ),
                    "status": humanize(x.status.value),
                    **({"utr": x.utr} if x.utr else {}),
                }
                for x in ctx.settlements[:3]
            ],
            "devices": [
                {"type": d.type.value.title(), "model": d.model, "online": d.online}
                for d in ctx.devices
            ],
            "billing": [
                {
                    "plan": p.plan_name,
                    "monthly_rent": inr(p.monthly_rent_inr),
                    "rent_active": p.rental_active,
                }
                for p in ctx.billing.plans
            ],
            "problems_found": [humanize(f.cause.value) for f in findings] or "none",
        }
        facts: dict[str, Any] = {
            "question": state.get("masked") or "",
            "conversation": state.get("history", [])[-8:],
            "account": account,
            "procedures": procedures(list(playbook.TOPIC_OF))[:6000],
        }
        if state.get("open_note"):
            facts["already_open"] = state["open_note"]
            if "loan offer" in state["open_note"].lower():
                el = await tools.check_loan_eligibility(state["merchant_id"])
                if el.eligible:
                    k = el.kfs
                    facts["loan_offer"] = {
                        "lender": k.lender,
                        "amount": inr(k.loan_amount_inr),
                        "tenure_months": k.tenure_months,
                        "interest_per_year_percent": k.apr_percent,
                        "monthly_instalment": inr(k.emi_inr),
                        "processing_fee": inr(k.processing_fee_inr),
                        "total_repayable": inr(k.total_repayable_inr),
                        "cooling_off_days": k.cooling_off_days,
                    }
        upd = await say(state, "answer", facts)
        log(state, "respond", "INFO_ONLY")
        return {**upd, "outcome": "INFO_ONLY"}

    async def refuse(state: AgentState) -> dict:
        upd = await say(state, "refuse", {})
        log(state, "refuse", "REFUSED", result={"why": "asked about another merchant's data"})
        return {**upd, "outcome": "REFUSED"}

    async def idle(state: AgentState) -> dict:
        log(state, "idle", "NOTHING_TO_DO")
        return {"outcome": "NOTHING_TO_DO"}

    async def stall(state: AgentState) -> dict:
        nxt = state["next"]
        note = f"WAITING_MERCHANT: {nxt['kind']} not completed"
        if state.get("ticket_id"):
            await tools.update_ticket(state["thread_id"], state["ticket_id"], note, "IN_PROGRESS")
        upd = await say(state, "stalled", {"kind": nxt["kind"]})
        log(state, "stall", "WAITING_MERCHANT", result={"waiting_for": nxt["kind"]})
        return {**upd, "outcome": "WAITING_MERCHANT"}

    async def escalate(state: AgentState) -> dict:
        nxt = state.get("next") or {}
        reason = (
            state.get("escalation_reason")
            or (nxt.get("reason") if nxt.get("type") == "escalate" else None)
            or (state.get("gate_case", {}).get("reason") if state.get("case_escalate") else None)
            or "needs a human decision"
        )
        ctx = MerchantContext.model_validate(state["ctx"])
        scope = scope_of(state, ctx)
        note = f"ESCALATED: {reason}"
        ticket_id = state.get("ticket_id")
        if ticket_id:
            await tools.update_ticket(state["thread_id"], ticket_id, note, "IN_PROGRESS")
        else:
            t = await tools.create_ticket(
                state["thread_id"], state["merchant_id"], state.get("category") or "OTHER",
                f"Escalated: {reason}"[:80], note=note,
            )  # fmt: skip
            ticket_id = t.id
        state = {**state, "actions": rec(state, "create_ticket", {"escalated": True}, True)}
        upd = await say(state, "escalated", {})
        handoff = {
            "reason": reason,
            "merchant": {
                "id": ctx.profile.id,
                "name": ctx.profile.owner_name,
                "language": (state.get("cls") or {}).get("language"),
            },  # fmt: skip
            "category": state.get("category"),
            "merchant_words_masked": state.get("masked", ""),
            "findings": [
                {
                    "cause": f.cause.value,
                    "detail": f.detail,
                    "evidence": f.evidence,
                    "amount_inr": f.amount_inr,
                }
                for f in scope
            ],  # fmt: skip
            "actions_tried": [
                {"action": a["action"], "executed": a["executed"], "tier": a["tier"]}
                for a in state.get("actions", [])
                if a["action"] not in ("send_message", "create_ticket")
            ],
            "gate": state.get("gate") or state.get("gate_case"),
            "confidence": (state.get("diagnosis") or {}).get("explain"),
            "suggested_reply": upd["messages"][-1] if upd.get("messages") else None,
        }
        log(state, "escalate", "ESCALATED", result=handoff)
        return {**upd, "ticket_id": ticket_id, "handoff": handoff, "outcome": "ESCALATED",
                "escalation_reason": reason}  # fmt: skip

    # ---------- wiring ----------

    g = StateGraph(AgentState)
    for name, fn in [
        ("ingest", ingest), ("diagnose", diagnose), ("plan", plan), ("gate", gate_node),
        ("approval_notice", approval_notice), ("approval", approval), ("execute", execute),
        ("ask_merchant", ask_merchant), ("await_merchant", await_merchant), ("verify", verify),
        ("resolve", resolve), ("offer_check", offer_check), ("offer_ask", offer_ask),
        ("offer_wait", offer_wait), ("offer_record", offer_record), ("respond", respond),
        ("refuse", refuse), ("idle", idle), ("stall", stall), ("escalate", escalate),
    ]:  # fmt: skip
        g.add_node(name, fn)

    g.add_edge(START, "ingest")
    g.add_conditional_edges("ingest", route_ingest)
    g.add_edge("diagnose", "plan")
    g.add_conditional_edges("plan", route_plan)
    g.add_conditional_edges("gate", route_gate)
    g.add_edge("approval_notice", "approval")
    g.add_conditional_edges("approval", route_approval)
    g.add_conditional_edges("execute", route_execute)
    g.add_edge("ask_merchant", "await_merchant")
    g.add_edge("await_merchant", "plan")
    g.add_conditional_edges("verify", route_verify)
    g.add_conditional_edges("resolve", route_resolve)
    g.add_conditional_edges("offer_check", route_offer_check)
    g.add_edge("offer_ask", "offer_wait")
    g.add_edge("offer_wait", "offer_record")
    for terminal in ("offer_record", "respond", "refuse", "idle", "stall", "escalate"):
        g.add_edge(terminal, END)
    return g.compile(checkpointer=checkpointer)
