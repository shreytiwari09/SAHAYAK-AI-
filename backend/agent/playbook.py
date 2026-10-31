"""The remedy playbook: given what the data proves, what is the next step?

Pure functions over the merchant context and a small case memory, so every branch is unit
tested without an LLM. The LLM diagnoses and its agreement with the data becomes the confidence
score; the *remedy* itself comes from here and still passes through the gate step by step.

Step kinds returned by `next_step`:
  {"type": "step", ...}      an action to run (after the gate)
  {"type": "wait", ...}      something only the merchant can do (tap a link, power-cycle)
  {"type": "stalled", ...}   the merchant did not do it
  {"type": "escalate", ...}  no self-serve fix exists
  None                       nothing left to do
"""

from typing import Any

from backend.gate.evidence import Cause, Finding
from backend.tools.context import MerchantContext

# Which kind of merchant problem each cause belongs to. A merchant who says "settlement" is also
# helped when the root cause is KYC or a bank failure, so those share the "money" topic.
TOPIC_OF: dict[Cause, str] = {
    Cause.KYC_HOLD: "money",
    Cause.KYC_NAME_MISMATCH: "money",
    Cause.BANK_ACCOUNT_FAILED: "money",
    Cause.SETTLEMENT_STUCK: "money",
    Cause.RISK_HOLD: "money",
    Cause.DEBITED_NOT_CREDITED: "money",
    Cause.DEVICE_OFFLINE: "device",
    Cause.RENTAL_DESYNC: "billing",
    Cause.KYC_EXPIRING: "kyc",
}
CATEGORY_OF_TOPIC = {"money": "SETTLEMENT", "device": "DEVICE", "billing": "BILLING", "kyc": "KYC"}

# When several things are wrong, handle the ones that need a human first.
PRIORITY = [
    Cause.RISK_HOLD,
    Cause.KYC_NAME_MISMATCH,
    Cause.KYC_HOLD,
    Cause.BANK_ACCOUNT_FAILED,
    Cause.SETTLEMENT_STUCK,
    Cause.DEBITED_NOT_CREDITED,
    Cause.DEVICE_OFFLINE,
    Cause.RENTAL_DESYNC,
    Cause.KYC_EXPIRING,
]


def new_case() -> dict[str, Any]:
    return {
        "retriggered": [],
        "reset": {},  # device_id -> RECOVERED / NO_RESPONSE
        "waited": [],  # wait refs the merchant has already been asked about
        "declined": [],  # wait refs the merchant said they did not complete
        "stopped": [],
        "reversed": [],
        "refunded": [],
        "refund_ids": [],
        "replacements": [],
        "bank_token": None,
        "links": {},  # token -> url
    }


def scope_findings(findings: list[Finding], topics: set[str]) -> list[Finding]:
    chosen = [f for f in findings if TOPIC_OF[f.cause] in topics]
    return sorted(chosen, key=lambda f: PRIORITY.index(f.cause))


def pick_scope(
    findings: list[Finding], intent: str, topic: str, trigger_causes: list[str] | None
) -> list[Finding]:
    """The findings this conversation is about."""
    if trigger_causes:
        return [f for f in findings if f.cause in trigger_causes]
    if intent not in ("report_problem", "ask_status", "loan_request") and topic == "other":
        return []  # small talk or an unrelated question; a named problem topic is acted on
    matching = [f for f in findings if TOPIC_OF[f.cause] == topic]
    return matching or list(findings)  # the merchant clearly has a problem: do not ignore it


def post_return_deductions(ctx: MerchantContext) -> list:
    out = []
    for plan in ctx.billing.plans:
        if plan.device_returned_at is None:
            continue
        out += [
            x for x in plan.deductions if not x.reversed and x.deducted_at > plan.device_returned_at
        ]
    return out


def _settlement(ctx: MerchantContext, sid: str):
    return next((s for s in ctx.settlements if s.id == sid), None)


def _step(action: str, args: dict, cause: Cause, summary: str, amount: float = 0.0) -> dict:
    return {
        "type": "step",
        "action": action,
        "args": args,
        "amount_inr": amount,
        "cause": cause.value,
        "summary": summary,
    }


def _step_for(f: Finding, ctx: MerchantContext, case: dict[str, Any]) -> dict | None:
    c = f.cause

    if c in (Cause.RISK_HOLD, Cause.KYC_NAME_MISMATCH):
        return {"type": "escalate", "reason": f.detail, "cause": c.value}

    if c in (Cause.KYC_HOLD, Cause.KYC_EXPIRING):
        pending = ctx.kyc.pending_links
        if pending:
            token = pending[0]
            if token in case["declined"] or token in case["waited"]:
                return {"type": "stalled", "kind": "kyc_link", "ref": token, "cause": c.value}
            return {"type": "wait", "kind": "kyc_link", "ref": token, "cause": c.value}
        return _step("send_kyc_link", {}, c, "send the re-KYC link")

    if c == Cause.BANK_ACCOUNT_FAILED:
        token = case["bank_token"]
        if token:
            if token in case["waited"]:
                return {"type": "stalled", "kind": "bank_update", "ref": token, "cause": c.value}
            return {"type": "wait", "kind": "bank_update", "ref": token, "cause": c.value}
        return _step("raise_bank_update", {"reason": f.detail}, c, "send the bank-details link")

    if c == Cause.SETTLEMENT_STUCK:
        for sid in f.evidence:
            if sid not in case["retriggered"]:
                st = _settlement(ctx, sid)
                amount = st.amount_inr if st else 0.0
                return _step("retrigger_settlement", {"settlement_id": sid}, c,
                             f"re-trigger settlement {sid}", amount)  # fmt: skip
        return {
            "type": "escalate",
            "reason": "settlement still not released after a re-trigger",
            "cause": c.value,
        }

    if c == Cause.DEVICE_OFFLINE:
        did = f.evidence[0]
        if did not in case["reset"]:
            return _step(
                "remote_device_reset", {"device_id": did}, c, "restart the device remotely"
            )
        ref = f"power_cycle:{did}"
        if ref in case["declined"]:
            return {"type": "stalled", "kind": "power_cycle", "ref": ref, "cause": c.value}
        if ref not in case["waited"]:
            return {"type": "wait", "kind": "power_cycle", "ref": ref, "device_id": did,
                    "cause": c.value}  # fmt: skip
        if did not in case["replacements"]:
            return _step("book_device_replacement", {"device_id": did}, c, "book a replacement")
        return {
            "type": "escalate",
            "reason": "device still offline after replacement",
            "cause": c.value,
        }

    if c == Cause.RENTAL_DESYNC:
        for plan in ctx.billing.plans:
            if plan.rental_active and plan.device_status in ("RETURNED", "INACTIVE"):
                if plan.id not in case["stopped"]:
                    return _step("stop_rental", {"plan_id": plan.id}, c,
                                 "stop rent on the returned device")  # fmt: skip
        for x in post_return_deductions(ctx):
            if x.id not in case["reversed"]:
                return _step("reverse_rental_charge", {"deduction_id": x.id}, c,
                             f"reverse the {x.period} rent deduction", x.amount_inr)  # fmt: skip
        return {"type": "escalate", "reason": "billing still out of sync", "cause": c.value}

    if c == Cause.DEBITED_NOT_CREDITED:
        txn_id = f.evidence[0]
        if txn_id not in case["refunded"]:
            return _step(
                "create_refund_request",
                {"transaction_id": txn_id, "amount_inr": f.amount_inr,
                 "reason": "customer debited, merchant not credited"},
                c, "credit the merchant for the failed payment", f.amount_inr,
            )  # fmt: skip
        return None  # raised; the verify step waits for it to post
    return None


def next_step(scope: list[Finding], ctx: MerchantContext, case: dict[str, Any]) -> dict | None:
    for f in sorted(scope, key=lambda x: PRIORITY.index(x.cause)):
        step = _step_for(f, ctx, case)
        if step is not None:
            return step
    return None
