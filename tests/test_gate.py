"""Table-driven tests for the deterministic gate (PRD §6)."""

import pytest

from backend.gate.engine import Gate, Policy, ProposedAction, Tier


@pytest.fixture(scope="module")
def gate():
    return Gate(Policy.load())


def P(action="retrigger_settlement", **kw):
    kw.setdefault("confidence", 0.95)
    return ProposedAction(action=action, **kw)


CASES = [
    # (name, proposal, expected tier, rule that must fire)
    ("routine retrigger", P(amount_inr=9_860), Tier.AUTO, None),
    ("kyc link", P("send_kyc_link"), Tier.AUTO, None),
    ("stop rental", P("stop_rental"), Tier.AUTO, None),
    ("message", P("send_message"), Tier.AUTO, None),
    ("at auto amount limit", P(amount_inr=25_000), Tier.AUTO, None),
    ("just over auto limit", P(amount_inr=25_001), Tier.APPROVE, "amount_above_auto_limit"),
    ("at approval limit", P(amount_inr=100_000), Tier.APPROVE, "amount_above_auto_limit"),
    ("over approval limit", P(amount_inr=100_001), Tier.ESCALATE, "amount_above_approval_limit"),
    ("medium confidence", P(confidence=0.7), Tier.APPROVE, "medium_confidence"),
    ("confidence at auto bar", P(confidence=0.85), Tier.AUTO, None),
    ("confidence at approve bar", P(confidence=0.60), Tier.APPROVE, "medium_confidence"),
    ("low confidence", P(confidence=0.59), Tier.ESCALATE, "low_confidence"),
    ("angry merchant", P(sentiment="angry"), Tier.APPROVE, "sentiment"),
    (
        "refund always approval",
        P("create_refund_request", amount_inr=500),
        Tier.APPROVE,
        "action_needs_approval",
    ),
    (
        "₹48.5k credit adjustment",
        P("create_refund_request", amount_inr=48_500),
        Tier.APPROVE,
        "amount_above_auto_limit",
    ),
    (
        "₹50k angry refund",
        P("create_refund_request", amount_inr=50_000, sentiment="angry"),
        Tier.APPROVE,
        "sentiment",
    ),
    ("rental reversal", P("reverse_rental_charge", amount_inr=125), Tier.APPROVE, "irreversible"),
    ("device replacement", P("book_device_replacement"), Tier.APPROVE, "action_needs_approval"),
    ("unknown action", P("drop_database"), Tier.ESCALATE, "unknown_action"),
    ("fraud signal", P(signals={"fraud_suspected"}), Tier.ESCALATE, "signal:fraud_suspected"),
    (
        "ombudsman mention",
        P(signals={"legal_or_ombudsman_mention"}),
        Tier.ESCALATE,
        "signal:legal_or_ombudsman_mention",
    ),
    (
        "prompt injection",
        P(signals={"prompt_injection_detected"}),
        Tier.ESCALATE,
        "signal:prompt_injection_detected",
    ),
    ("risk hold", P(signals={"risk_review_hold"}), Tier.ESCALATE, "signal:risk_review_hold"),
    ("unlisted signal ignored", P(signals={"sunny_weather"}), Tier.AUTO, None),
    ("second contact ok", P(contact_count=2), Tier.AUTO, None),
    ("third contact", P(contact_count=3), Tier.ESCALATE, "repeat_contact"),
    ("lead without consent", P("create_lead"), Tier.ESCALATE, "consent_missing"),
    ("lead with consent", P("create_lead", consents={"LOAN_OFFER"}), Tier.AUTO, None),
    (
        "lead with wrong consent",
        P("create_lead", consents={"MARKETING"}),
        Tier.ESCALATE,
        "consent_missing",
    ),
    ("kyc link ignores amount", P("send_kyc_link", amount_inr=10_00_000), Tier.AUTO, None),
]


@pytest.mark.parametrize("name,proposal,tier,rule", CASES, ids=[c[0] for c in CASES])
def test_gate_table(gate, name, proposal, tier, rule):
    d = gate.decide(proposal)
    assert d.tier == tier, d.to_dict()
    if rule:
        assert rule in d.rules
    else:
        assert d.rules == []


def test_strictest_rule_wins_and_all_rules_are_recorded(gate):
    d = gate.decide(P(amount_inr=50_000, confidence=0.7, sentiment="angry", contact_count=3))
    assert d.tier == Tier.ESCALATE
    assert {"repeat_contact", "amount_above_auto_limit", "medium_confidence", "sentiment"} <= set(
        d.rules
    )
    assert "contact #3" in d.reason
    assert "medium_confidence" not in d.reason  # reason lists only the decisive rules


def test_auto_reason(gate):
    assert gate.decide(P()).reason == "all AUTO conditions met"


def test_kill_switch_forces_approval():
    pol = Policy.load()
    pol.kill_switch = True
    d = Gate(pol).decide(P("send_kyc_link"))
    assert d.tier == Tier.APPROVE and "kill_switch" in d.rules


def test_decision_is_serialisable_for_audit(gate):
    d = gate.decide(P("create_refund_request", amount_inr=48_500)).to_dict()
    assert d["tier"] == "APPROVE" and d["policy_version"] == 1
    assert all({"rule", "tier", "detail"} <= set(r) for r in d["rules"])


def test_every_policy_action_is_a_real_tool():
    from backend.tools.client import PaytmTools

    for action in Policy.load().actions:
        assert hasattr(PaytmTools, action), action
