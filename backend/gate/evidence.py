"""Rule-based cause signatures: the deterministic half of diagnosis.

`detect` reads a MerchantContext and lists every known problem the data proves, with the record
ids as evidence. The LLM's proposed (cause, action) is then checked against these findings to
get the `evidence_rule_match` component of confidence (design decision D3), and the same
findings drive proactive outreach (D13).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from backend.tools.context import MerchantContext
from mock_paytm import models as m

KYC_EXPIRY_WARNING_DAYS = 30
REPEAT_CONTACT_WINDOW_DAYS = 30


class Cause(StrEnum):
    KYC_HOLD = "KYC_HOLD"
    KYC_NAME_MISMATCH = "KYC_NAME_MISMATCH"
    KYC_EXPIRING = "KYC_EXPIRING"
    BANK_ACCOUNT_FAILED = "BANK_ACCOUNT_FAILED"
    SETTLEMENT_STUCK = "SETTLEMENT_STUCK"
    RISK_HOLD = "RISK_HOLD"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    RENTAL_DESYNC = "RENTAL_DESYNC"
    DEBITED_NOT_CREDITED = "DEBITED_NOT_CREDITED"


# Actions that legitimately address each cause. An empty tuple means no self-serve fix: escalate.
CAUSE_ACTIONS: dict[Cause, tuple[str, ...]] = {
    Cause.KYC_HOLD: ("send_kyc_link", "retrigger_settlement"),
    Cause.KYC_NAME_MISMATCH: (),
    Cause.KYC_EXPIRING: ("send_kyc_link",),
    Cause.BANK_ACCOUNT_FAILED: ("raise_bank_update", "retrigger_settlement"),
    Cause.SETTLEMENT_STUCK: ("retrigger_settlement",),
    Cause.RISK_HOLD: (),
    Cause.DEVICE_OFFLINE: ("remote_device_reset", "book_device_replacement"),
    Cause.RENTAL_DESYNC: ("stop_rental", "reverse_rental_charge"),
    Cause.DEBITED_NOT_CREDITED: ("create_refund_request",),
}

# Causes that must reach a human; they become gate signals.
CAUSE_SIGNALS: dict[Cause, str] = {
    Cause.KYC_NAME_MISMATCH: "kyc_name_mismatch",
    Cause.RISK_HOLD: "risk_review_hold",
}


class Finding(BaseModel):
    cause: Cause
    evidence: list[str]  # record ids the agent can cite
    detail: str
    amount_inr: float = 0.0


def detect(ctx: MerchantContext) -> list[Finding]:
    out: list[Finding] = []
    p, kyc = ctx.profile, ctx.kyc

    kyc_held = [x for x in ctx.settlements if x.hold_reason == m.HoldReason.KYC_REVERIFICATION]
    if p.kyc_status == m.KycStatus.NAME_MISMATCH:
        out.append(
            Finding(
                cause=Cause.KYC_NAME_MISMATCH,
                evidence=[p.id, *(x.id for x in kyc_held)],
                detail="KYC name does not match bank records; needs manual review",
                amount_inr=sum(x.amount_inr for x in kyc_held),
            )
        )
    elif kyc_held:
        out.append(
            Finding(
                cause=Cause.KYC_HOLD,
                evidence=[x.id for x in kyc_held],
                detail=f"{len(kyc_held)} settlement(s) held for KYC re-verification "
                f"(KYC status {p.kyc_status})",
                amount_inr=sum(x.amount_inr for x in kyc_held),
            )
        )
    elif p.kyc_status == m.KycStatus.VERIFIED and kyc.days_to_expiry <= KYC_EXPIRY_WARNING_DAYS:
        out.append(
            Finding(
                cause=Cause.KYC_EXPIRING,
                evidence=[p.id],
                detail=f"KYC expires in {kyc.days_to_expiry} days",
            )
        )

    bank_failed = [x for x in ctx.settlements if x.hold_reason == m.HoldReason.BANK_ACCOUNT_FAILED]
    if bank_failed or p.bank_status == m.BankStatus.FAILED:
        out.append(
            Finding(
                cause=Cause.BANK_ACCOUNT_FAILED,
                evidence=[x.id for x in bank_failed] or [p.id],
                detail=f"bank account rejected (IFSC {p.ifsc}); settlement could not be paid",
                amount_inr=sum(x.amount_inr for x in bank_failed),
            )
        )

    risk = [x for x in ctx.settlements if x.hold_reason == m.HoldReason.RISK_REVIEW]
    if risk or p.risk_flag:
        out.append(
            Finding(
                cause=Cause.RISK_HOLD,
                evidence=[x.id for x in risk] or [p.id],
                detail="settlement under risk review",
                amount_inr=sum(x.amount_inr for x in risk),
            )
        )

    stuck = [
        x
        for x in ctx.settlements
        if x.status in (m.SettlementStatus.PENDING, m.SettlementStatus.FAILED)
        and x.overdue
        and x.hold_reason is None
    ]
    if stuck:
        out.append(
            Finding(
                cause=Cause.SETTLEMENT_STUCK,
                evidence=[x.id for x in stuck],
                detail=f"{len(stuck)} settlement(s) past their T+1 time with no hold reason",
                amount_inr=sum(x.amount_inr for x in stuck),
            )
        )

    for d in ctx.devices:
        if d.status == m.DeviceStatus.ACTIVE and not d.online:
            out.append(
                Finding(
                    cause=Cause.DEVICE_OFFLINE,
                    evidence=[d.id],
                    detail=f"{d.type} {d.model} silent for {d.seconds_since_heartbeat // 60} min"
                    f" ({'in' if d.in_warranty else 'out of'} warranty)",
                )
            )

    for plan in ctx.billing.plans:
        # Rent still active on a returned device, or deductions after the return not yet
        # reversed (stopping the rent alone does not give the merchant the money back).
        after_return = [
            x
            for x in plan.deductions
            if not x.reversed
            and plan.device_returned_at is not None
            and x.deducted_at > plan.device_returned_at
        ]
        if plan.out_of_sync or after_return:
            out.append(
                Finding(
                    cause=Cause.RENTAL_DESYNC,
                    evidence=[plan.id, plan.device_id, *(x.id for x in after_return)],
                    detail=f"rent still active on {plan.device_status} device; "
                    f"{len(after_return)} deduction(s) after return",
                    amount_inr=sum(x.amount_inr for x in after_return),
                )
            )

    for t in ctx.transactions.debited_not_credited:
        out.append(
            Finding(
                cause=Cause.DEBITED_NOT_CREDITED,
                evidence=[t.id],
                detail=f"customer debited ₹{t.amount_inr:,.0f} on {t.created_at:%d %b}; "
                "merchant not credited",
                amount_inr=t.amount_inr,
            )
        )
    return out


def evidence_rule_match(findings: list[Finding], cause: str, action: str | None) -> float:
    """1.0 when the data proves the cause and the action fits it; 0.5 cause only; else 0."""
    for f in findings:
        if f.cause == cause:
            if action is None or action in CAUSE_ACTIONS[f.cause]:
                return 1.0
            return 0.5
    return 0.0


def signals(findings: list[Finding]) -> set[str]:
    return {CAUSE_SIGNALS[f.cause] for f in findings if f.cause in CAUSE_SIGNALS}


def contact_count(ctx: MerchantContext, category: str, now: datetime) -> int:
    """This contact plus recent tickets on the same issue that were not verifiably fixed."""
    recent = [
        t
        for t in ctx.tickets
        if t.category == category
        and not t.verified_resolution
        and (now - t.created_at).days <= REPEAT_CONTACT_WINDOW_DAYS
    ]
    return 1 + len(recent)
