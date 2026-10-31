"""Targeted behaviour of the agent graph: safety under a wrong or missing LLM, grounding, PII,
durable approvals and the audit trail."""

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.agent.driver import Behaviour, SimResponder, run_case
from backend.agent.runtime import build_agent, open_checkpointer
from backend.audit.log import AuditLog
from backend.pii.masker import PiiMasker
from backend.tools.client import PaytmTools
from eval.fake_llm import FakeLLM
from mock_paytm import clock
from mock_paytm.db import make_engine


@pytest.fixture
async def rig(engine):
    from mock_paytm.app import app

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")
    clock_t = {"t": 0.0}

    async def sleep(s):
        clock.advance(s)
        clock_t["t"] += s

    class Rig:
        pass

    r = Rig()
    r.http, r.tools = http, PaytmTools(http)
    r.audit = AuditLog(make_engine("sqlite://"))
    r.sleep, r.mono = sleep, (lambda: clock_t["t"])

    def build(llm, checkpointer=None, **kw):
        return build_agent(
            r.tools, llm, r.audit, checkpointer or InMemorySaver(),
            masker=PiiMasker(use_presidio=False), sleep=sleep, monotonic=r.mono, **kw,
        )[0]  # fmt: skip

    r.build = build
    yield r
    await http.aclose()


def msg(thread, merchant, text="Mera settlement nahi aaya", **kw):
    return {"thread_id": thread, "merchant_id": merchant, "channel": "test", "text": text, **kw}


def executed(state):
    return [a["action"] for a in state["actions"] if a["executed"]]


# ---------- the gate protects against a wrong diagnosis ----------


async def test_a_diagnosis_the_data_does_not_support_is_not_acted_on(rig):
    llm = FakeLLM(agree=False)  # claims DEVICE_OFFLINE for a KYC hold
    state = await run_case(rig.build(llm), msg("t1", "M1001"), SimResponder(rig.http))
    assert state["outcome"] == "ESCALATED"
    assert "send_kyc_link" not in executed(state)
    assert "low_confidence" in str(state["handoff"]["gate"])
    assert state["diagnosis"]["confidence"]["evidence_rule_match"] == 0.0


async def test_a_dead_llm_never_causes_action_and_the_merchant_still_hears_back(rig):
    llm = FakeLLM(fail={"classify", "diagnose", "verify_plan", "compose"})
    state = await run_case(rig.build(llm), msg("t2", "M1001"), SimResponder(rig.http))
    assert state["outcome"] == "ESCALATED"
    assert not [a for a in executed(state) if a not in ("create_ticket", "send_message")]
    assert state["messages"], "the merchant must receive a message even with no LLM"
    assert "Namaste" in state["messages"][0]  # AI disclosure, from a template


async def test_compose_down_still_completes_the_fix_with_template_messages(rig):
    llm = FakeLLM(fail={"compose"})
    state = await run_case(rig.build(llm), msg("t3", "M1001"), SimResponder(rig.http))
    assert state["outcome"] == "RESOLVED_VERIFIED" and state["verified"]
    assert any("UTR" in m for m in state["messages"])  # template carries the proof


# ---------- grounding ----------


async def test_an_amount_the_llm_invents_is_replaced_by_a_template(rig):
    llm = FakeLLM(invent_amount=True)
    state = await run_case(rig.build(llm), msg("t4", "M1001"), SimResponder(rig.http))
    assert state["outcome"] == "RESOLVED_VERIFIED"
    assert not any("99,999" in m for m in state["messages"])
    events = [e.event for e in rig.audit.events(thread_id="t4")]
    assert "MESSAGE_FALLBACK" in events


# ---------- PII ----------


async def test_the_model_never_sees_raw_identifiers(rig):
    llm = FakeLLM()
    text = "Mera PAN ABCPK1234Z hai aur account number 50100234567890 hai, settlement nahi aaya"
    state = await run_case(rig.build(llm), msg("t5", "M1001", text), SimResponder(rig.http))
    shown = " ".join(llm.seen)
    assert "ABCPK1234Z" not in shown and "50100234567890" not in shown
    assert "<PAN_1>" in shown and "<BANK_ACCOUNT_1>" in shown
    assert not any("ABCPK1234Z" in m or "50100234567890" in m for m in state["messages"])


# ---------- durable approval ----------


async def test_a_pending_approval_survives_a_restart_and_acts_exactly_once(rig, tmp_path):
    ckpt = str(tmp_path / "checkpoints.db")
    cfg = msg("t6", "M1009", "Customer ka paisa kat gaya mere paas nahi aaya")

    async with open_checkpointer(ckpt) as saver:
        graph = rig.build(FakeLLM(), saver)
        paused = await graph.ainvoke(cfg, {"configurable": {"thread_id": "t6"}})
    pause = paused["__interrupt__"][0].value
    assert pause["type"] == "approval" and pause["action"] == "create_refund_request"
    assert pause["amount_inr"] == 48500.0
    assert (await rig.tools.get_transactions("M1009", 7)).debited_not_credited  # nothing moved yet

    # "Restart": a brand-new graph and checkpointer connection on the same file.
    async with open_checkpointer(ckpt) as saver:
        graph = rig.build(FakeLLM(), saver)
        state = await graph.aget_state({"configurable": {"thread_id": "t6"}})
        assert state.next == ("approval",)
        from langgraph.types import Command

        result = await graph.ainvoke(
            Command(resume={"approved": True, "approver": "priya"}),
            {"configurable": {"thread_id": "t6"}},
        )
    assert result["outcome"] == "RESOLVED_VERIFIED"
    assert executed(result).count("create_refund_request") == 1
    assert not (await rig.tools.get_transactions("M1009", 7)).debited_not_credited


async def test_a_rejected_approval_moves_no_money_and_hands_off(rig):
    behaviour = Behaviour(approver_approves=False)
    state = await run_case(
        rig.build(FakeLLM()),
        msg("t7", "M1009", "paisa nahi aaya"),
        SimResponder(rig.http, behaviour),
    )
    assert state["outcome"] == "ESCALATED" and "create_refund_request" not in executed(state)
    assert "approver rejected" in state["escalation_reason"]
    assert (await rig.tools.get_transactions("M1009", 7)).debited_not_credited


# ---------- audit ----------


async def test_audit_trail_explains_every_decision_and_is_tamper_evident(rig):
    state = await run_case(rig.build(FakeLLM()), msg("t8", "M1001"), SimResponder(rig.http))
    events = rig.audit.events(thread_id="t8")
    names = [e.event for e in events]
    for needed in ("UNDERSTOOD", "DIAGNOSED", "GATE_DECIDED", "EXECUTED", "MERCHANT_RESPONDED",
                   "VERIFIED", "RESOLVED"):  # fmt: skip
        assert needed in names, needed
    gate = next(e for e in events if e.event == "GATE_DECIDED")
    assert (
        gate.gate["tier"] == "AUTO"
        and gate.action == "send_kyc_link"
        and gate.confidence["total"] == 1.0
    )
    verified = next(e for e in events if e.event == "VERIFIED")
    assert verified.verified is True and verified.result["proof"]["settlements"]
    assert rig.audit.verify_chain() == (True, None)
    # No raw message text is stored: only a hash.
    assert all("settlement nahi aaya" not in str(e.result) for e in events)
    assert state["outcome"] == "RESOLVED_VERIFIED"


async def test_ai_disclosure_is_sent_once_and_first(rig):
    state = await run_case(rig.build(FakeLLM()), msg("t9", "M1001"), SimResponder(rig.http))
    assert "AI assistant" in state["messages"][0]
    assert sum("AI assistant" in m for m in state["messages"]) == 1


async def test_escalation_hands_a_human_the_whole_case(rig):
    state = await run_case(
        rig.build(FakeLLM()),
        msg("t10", "M1005", "Settlement hold par kyu hai?"),
        SimResponder(rig.http),
    )
    h = state["handoff"]
    assert state["outcome"] == "ESCALATED"
    assert h["findings"][0]["cause"] == "RISK_HOLD" and h["findings"][0]["evidence"]
    assert h["merchant"]["id"] == "M1005" and h["merchant_words_masked"]
    assert h["suggested_reply"] and h["gate"]
    tickets = await rig.tools.get_ticket_history("M1005")
    assert any(t.history[-1]["note"].startswith("ESCALATED") for t in tickets)


async def test_a_named_problem_is_acted_on_even_when_the_intent_label_is_other(rig):
    from backend.agent.schemas import Classification

    cls = Classification(
        intent="other", topic="money", language="hi"
    )  # what Gemini returned for X06
    state = await run_case(rig.build(FakeLLM(cls)), msg("t11", "M1004", "naya account daal do"),
                           SimResponder(rig.http))  # fmt: skip
    assert state["outcome"] == "RESOLVED_VERIFIED" and "raise_bank_update" in executed(state)


async def test_small_talk_with_no_named_problem_triggers_no_action(rig):
    from backend.agent.schemas import Classification

    cls = Classification(intent="other", topic="other", language="hi")
    state = await run_case(
        rig.build(FakeLLM(cls)), msg("t12", "M1001", "namaste"), SimResponder(rig.http)
    )
    assert state["outcome"] == "INFO_ONLY"
    assert not [a for a in executed(state) if a not in ("send_message",)]


async def test_when_every_diagnosis_call_fails_confidence_is_zero_not_a_partial_score(rig):
    state = await run_case(
        rig.build(FakeLLM(fail={"diagnose"})), msg("t13", "M1006"), SimResponder(rig.http)
    )
    assert state["diagnosis"]["confidence"]["total"] == 0.0
    assert state["outcome"] == "ESCALATED"
