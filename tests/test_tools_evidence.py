"""The typed tool client against the mock service, plus rule-based cause detection."""

import httpx
import pytest

from backend.gate.engine import Gate, ProposedAction, Tier
from backend.gate.evidence import Cause, contact_count, detect, evidence_rule_match, signals
from backend.tools.client import PaytmTools, ToolCallError, idempotency_key
from backend.tools.context import fetch_context
from mock_paytm import clock
from tests.conftest import HEALTHY


@pytest.fixture
async def tools(engine):
    from mock_paytm.app import app

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")
    t = PaytmTools(http)
    yield t
    await t.aclose()


async def causes(tools, merchant_id):
    return {f.cause for f in detect(await fetch_context(tools, merchant_id))}


def test_idempotency_key_is_stable_and_scoped():
    a = idempotency_key("t1", "retrigger_settlement", "STL1")
    assert a == idempotency_key("t1", "retrigger_settlement", "STL1")
    assert a != idempotency_key("t2", "retrigger_settlement", "STL1")
    assert a != idempotency_key("t1", "retrigger_settlement", "STL2")
    assert len(a) == 32


@pytest.mark.parametrize(
    "merchant_id,expected",
    [
        ("M1001", {Cause.KYC_HOLD}),
        ("M1002", {Cause.DEVICE_OFFLINE}),
        ("M1003", {Cause.RENTAL_DESYNC}),
        ("M1004", {Cause.BANK_ACCOUNT_FAILED}),
        ("M1005", {Cause.RISK_HOLD}),
        ("M1006", {Cause.SETTLEMENT_STUCK}),
        ("M1007", {Cause.KYC_EXPIRING}),
        ("M1008", {Cause.KYC_HOLD}),
        ("M1009", {Cause.DEBITED_NOT_CREDITED}),
        ("M1010", set()),
        ("M1011", {Cause.KYC_NAME_MISMATCH}),
        ("M1012", {Cause.DEVICE_OFFLINE}),
        (HEALTHY, set()),
    ],
)
async def test_detect_scenario_causes(tools, merchant_id, expected):
    assert await causes(tools, merchant_id) == expected


async def test_findings_carry_evidence_and_amounts(tools):
    [f] = detect(await fetch_context(tools, "M1009"))
    assert f.amount_inr == 48_500 and f.evidence[0].startswith("TXN")
    [f] = detect(await fetch_context(tools, "M1003"))
    assert f.amount_inr > 0 and len(f.evidence) >= 3  # plan, device, deductions


async def test_evidence_rule_match_and_signals(tools):
    findings = detect(await fetch_context(tools, "M1001"))
    assert evidence_rule_match(findings, "KYC_HOLD", "send_kyc_link") == 1.0
    assert evidence_rule_match(findings, "KYC_HOLD", "create_refund_request") == 0.5
    assert evidence_rule_match(findings, "RISK_HOLD", None) == 0.0
    assert signals(detect(await fetch_context(tools, "M1005"))) == {"risk_review_hold"}
    assert signals(detect(await fetch_context(tools, "M1011"))) == {"kyc_name_mismatch"}


async def test_repeat_contact_count_drives_escalation(tools):
    ctx = await fetch_context(tools, "M1008")
    n = contact_count(ctx, "SETTLEMENT", clock.now())
    assert n == 3
    d = Gate().decide(ProposedAction(action="send_kyc_link", confidence=0.95, contact_count=n))
    assert d.tier == Tier.ESCALATE
    assert contact_count(await fetch_context(tools, "M1001"), "SETTLEMENT", clock.now()) == 1


async def test_hero_flow_through_the_tool_client(tools, client):
    """The KYC-hold fix end to end, verified by re-reading the source of truth."""
    thread = "thread-hero"
    ctx = await fetch_context(tools, "M1001")
    [finding] = detect(ctx)
    held = finding.evidence

    with pytest.raises(ToolCallError) as e:
        await tools.retrigger_settlement(thread, held[0])
    assert e.value.code == "BLOCKED_KYC" and e.value.status == 409

    link = await tools.send_kyc_link(thread, "M1001")
    client.post(f"/sim/kyc/{link.token}/complete")  # the merchant taps the link

    after_kyc = detect(await fetch_context(tools, "M1001"))
    assert {f.cause for f in after_kyc} == {Cause.SETTLEMENT_STUCK}

    for sid in held:
        first = await tools.retrigger_settlement(thread, sid)
        replay = await tools.retrigger_settlement(thread, sid)  # resumed node / retry
        assert replay.attempts == first.attempts

    clock.advance(11)
    for sid in held:
        st = await tools.get_settlement(sid)
        assert st.status == "CREDITED" and st.utr
    assert detect(await fetch_context(tools, "M1001")) == []


async def test_rental_desync_fix_verified_across_systems(tools):
    thread = "thread-rent"
    [f] = detect(await fetch_context(tools, "M1003"))
    plan_id, deduction_ids = f.evidence[0], f.evidence[2:]
    await tools.stop_rental(thread, plan_id)
    for d in deduction_ids:
        await tools.reverse_rental_charge(thread, d)
    assert detect(await fetch_context(tools, "M1003")) == []


async def test_tool_errors_are_typed(tools):
    with pytest.raises(ToolCallError) as e:
        await tools.get_merchant_profile("M9999")
    assert e.value.code == "MERCHANT_NOT_FOUND" and e.value.status == 404


async def test_consent_then_lead(tools):
    from mock_paytm import schemas as s

    el = await tools.check_loan_eligibility("M1010")
    consent = await tools.record_consent(
        "t",
        "M1010",
        s.ConsentIn(
            purpose="LOAN_OFFER",
            notice_version=el.kfs.notice_version,
            granted=True,
            channel="whatsapp",
            offer_id=el.offer.id,
            button_payload="HAAN",
        ),
    )
    lead = await tools.create_lead("t", "M1010", el.offer.id, consent.id)
    assert lead.status == "CREATED"


async def test_ticket_and_message_writes(tools):
    t = await tools.create_ticket("t", "M1001", "SETTLEMENT", "Settlement not received")
    t = await tools.update_ticket("t", t.id, "KYC link sent", "IN_PROGRESS")
    t = await tools.close_ticket("t", t.id, "Credited, UTR verified", True)
    assert t.status == "CLOSED" and t.verified_resolution
    msg = await tools.send_message("t", "M1001", "Namaste Ramesh ji")
    again = await tools.send_message("t", "M1001", "Namaste Ramesh ji")
    assert msg.id == again.id  # duplicate send is suppressed
