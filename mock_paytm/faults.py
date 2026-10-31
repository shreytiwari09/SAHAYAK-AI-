"""Fault injection: put any merchant into a known broken state for demos and evals.

Each fault returns a short description of what it changed, so the console fault panel and the
eval harness can show it.
"""

import secrets
from datetime import timedelta
from enum import StrEnum

from sqlmodel import Session, col, select

from mock_paytm import clock
from mock_paytm import models as m
from mock_paytm.service import ServiceError, _merchant, new_id


class Fault(StrEnum):
    KYC_HOLD = "kyc_hold"
    BANK_FAIL = "bank_fail"
    SETTLEMENT_STUCK = "settlement_stuck"
    HEARTBEAT_LOSS = "heartbeat_loss"
    RENTAL_DESYNC = "rental_desync"
    HIGH_REFUND = "high_refund"
    RISK_FLAG = "risk_flag"


def _latest_settlement(session: Session, merchant_id: str) -> m.Settlement:
    st = session.exec(
        select(m.Settlement)
        .where(m.Settlement.merchant_id == merchant_id)
        .order_by(col(m.Settlement.business_date).desc())
    ).first()
    if not st:
        raise ServiceError("NO_SETTLEMENT", f"merchant {merchant_id} has no settlements", 404)
    return st


def _hold(st: m.Settlement, status: m.SettlementStatus, reason: m.HoldReason | None) -> None:
    st.status = status
    st.hold_reason = reason
    st.utr = None
    st.credited_at = None
    st.completes_at = None
    st.processing_started_at = None


def _device(session: Session, merchant_id: str) -> m.Device:
    """The merchant's Soundbox if they have one, else any device."""
    devices = session.exec(select(m.Device).where(m.Device.merchant_id == merchant_id)).all()
    if not devices:
        raise ServiceError("NO_DEVICE", f"merchant {merchant_id} has no device", 404)
    return next((d for d in devices if d.type == m.DeviceType.SOUNDBOX), devices[0])


def inject(
    session: Session,
    fault: Fault,
    merchant_id: str,
    amount_inr: float | None = None,
    device_fault: m.DeviceFault = m.DeviceFault.POWER,
) -> dict:
    merchant = _merchant(session, merchant_id)
    now = clock.now()
    changed: dict = {"fault": fault, "merchant_id": merchant_id}

    match fault:
        case Fault.KYC_HOLD:
            merchant.kyc_status = m.KycStatus.PENDING_REVERIFICATION
            st = _latest_settlement(session, merchant_id)
            _hold(st, m.SettlementStatus.ON_HOLD, m.HoldReason.KYC_REVERIFICATION)
            changed["settlement_id"] = st.id
        case Fault.BANK_FAIL:
            merchant.bank_status = m.BankStatus.FAILED
            st = _latest_settlement(session, merchant_id)
            _hold(st, m.SettlementStatus.FAILED, m.HoldReason.BANK_ACCOUNT_FAILED)
            changed["settlement_id"] = st.id
        case Fault.SETTLEMENT_STUCK:
            st = _latest_settlement(session, merchant_id)
            _hold(st, m.SettlementStatus.PENDING, None)
            st.expected_at = now - timedelta(hours=26)
            changed["settlement_id"] = st.id
        case Fault.RISK_FLAG:
            merchant.risk_flag = True
            st = _latest_settlement(session, merchant_id)
            _hold(st, m.SettlementStatus.ON_HOLD, m.HoldReason.RISK_REVIEW)
            changed["settlement_id"] = st.id
        case Fault.HEARTBEAT_LOSS:
            d = _device(session, merchant_id)
            d.status = m.DeviceStatus.ACTIVE
            d.fault = device_fault
            d.last_heartbeat_at = now - timedelta(minutes=45)
            changed.update(device_id=d.id, device_fault=device_fault)
        case Fault.RENTAL_DESYNC:
            plan = session.exec(
                select(m.BillingPlan).where(m.BillingPlan.merchant_id == merchant_id)
            ).first()
            if not plan:
                raise ServiceError("NO_PLAN", f"merchant {merchant_id} has no billing plan", 404)
            d = session.get(m.Device, plan.device_id)
            d.status = m.DeviceStatus.RETURNED
            d.returned_at = now - timedelta(days=40)
            plan.rental_active = True
            plan.stopped_at = None
            deduction = m.RentalDeduction(
                id=new_id("RNT"),
                plan_id=plan.id,
                merchant_id=merchant_id,
                period=(now - timedelta(days=5)).strftime("%Y-%m"),
                amount_paise=plan.monthly_rent_paise or 12_500,
                deducted_at=now - timedelta(days=5),
            )
            session.add(deduction)
            changed.update(plan_id=plan.id, device_id=d.id, deduction_id=deduction.id)
        case Fault.HIGH_REFUND:
            txn = m.Transaction(
                id=f"TXN{secrets.token_hex(6).upper()}",
                merchant_id=merchant_id,
                amount_paise=round((amount_inr or 50_000) * 100),
                mode="UPI",
                payer_vpa="customer.pay@okaxis",
                status=m.TxnStatus.DEBITED_NOT_CREDITED,
                created_at=now - timedelta(days=2),
            )
            session.add(txn)
            changed["transaction_id"] = txn.id

    session.commit()
    return changed
