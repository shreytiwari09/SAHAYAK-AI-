"""Business logic for the mock Paytm systems.

Shared by the REST API (app.py) and the MCP server (mcp_server.py) so both behave identically.
Asynchronous state changes (settlement PROCESSING -> CREDITED, refund INITIATED -> POSTED,
healthy device heartbeats) are applied lazily whenever the record is read, driven by clock.now().
"""

import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta

from pydantic import BaseModel
from sqlmodel import Session, col, select

from mock_paytm import clock
from mock_paytm import models as m
from mock_paytm import schemas as s
from mock_paytm.config import get_settings

KFS_NOTICE_VERSION = "KFS-2025-v1"
LOAN_PURPOSE = "LOAN_OFFER"
COOLING_OFF_DAYS = 1


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def new_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(5).upper()}"


def new_utr() -> str:
    return f"UTR{secrets.randbelow(10**12):012d}"


# ---------- idempotency ----------


def idempotent[T: BaseModel](
    session: Session, key: str | None, operation: str, fn: Callable[[], T]
) -> T | dict:
    """Run a write once per idempotency key; replays return the stored response."""
    if key:
        record = session.get(m.IdempotencyRecord, key)
        if record:
            if record.operation != operation:
                raise ServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    f"key already used for {record.operation}",
                    status=422,
                )
            return record.response
    result = fn()
    if key:
        session.add(
            m.IdempotencyRecord(
                key=key,
                operation=operation,
                response=result.model_dump(mode="json"),
                created_at=clock.now(),
            )
        )
    session.commit()
    return result


# ---------- lookups ----------


def _merchant(session: Session, merchant_id: str) -> m.Merchant:
    merchant = session.get(m.Merchant, merchant_id)
    if not merchant:
        raise ServiceError("MERCHANT_NOT_FOUND", f"no merchant {merchant_id}", status=404)
    return merchant


def _get[T](session: Session, model: type[T], key: str, code: str) -> T:
    obj = session.get(model, key)
    if not obj:
        raise ServiceError(code, f"{model.__name__} {key} not found", status=404)
    return obj


# ---------- lazy state advancement ----------


def _advance_settlement(st: m.Settlement, now: datetime) -> None:
    if st.status == m.SettlementStatus.PROCESSING and st.completes_at and now >= st.completes_at:
        st.status = m.SettlementStatus.CREDITED
        st.credited_at = st.completes_at
        st.utr = st.utr or new_utr()


def _advance_refund(session: Session, r: m.RefundRequest, now: datetime) -> None:
    if r.status == m.RefundStatus.INITIATED and now >= r.completes_at:
        r.status = m.RefundStatus.POSTED
        r.posted_at = r.completes_at
        if r.transaction_id:
            txn = session.get(m.Transaction, r.transaction_id)
            if txn and txn.status == m.TxnStatus.DEBITED_NOT_CREDITED:
                txn.status = m.TxnStatus.SUCCESS


def _refresh_heartbeat(d: m.Device, now: datetime) -> None:
    # Healthy, deployed devices ping continuously; only faults make them go quiet.
    if d.status == m.DeviceStatus.ACTIVE and d.fault == m.DeviceFault.NONE:
        d.last_heartbeat_at = now


# ---------- reads ----------


def list_merchants(session: Session) -> list[dict]:
    rows = session.exec(select(m.Merchant).order_by(m.Merchant.id)).all()
    return [
        {"id": x.id, "owner_name": x.owner_name, "business_name": x.business_name,
         "language": x.language, "city": x.city}
        for x in rows
    ]  # fmt: skip


def get_merchant_profile(session: Session, merchant_id: str) -> s.MerchantProfile:
    return s.MerchantProfile.of(_merchant(session, merchant_id))


def get_transactions(session: Session, merchant_id: str, days: int = 7) -> s.TransactionSummary:
    _merchant(session, merchant_id)
    since = clock.now() - timedelta(days=days)
    rows = session.exec(
        select(m.Transaction)
        .where(m.Transaction.merchant_id == merchant_id, m.Transaction.created_at >= since)
        .order_by(col(m.Transaction.created_at).desc())
    ).all()
    out = [s.TransactionOut.of(t) for t in rows]
    return s.TransactionSummary(
        merchant_id=merchant_id,
        days=days,
        count=len(rows),
        success_amount_inr=s.inr(
            sum(t.amount_paise for t in rows if t.status == m.TxnStatus.SUCCESS)
        ),
        failed_count=sum(t.status == m.TxnStatus.FAILED for t in rows),
        debited_not_credited=[o for o in out if o.status == m.TxnStatus.DEBITED_NOT_CREDITED],
        transactions=out,
    )


def get_settlements(session: Session, merchant_id: str, days: int = 7) -> list[s.SettlementOut]:
    _merchant(session, merchant_id)
    now = clock.now()
    since = (now - timedelta(days=days)).date()
    rows = session.exec(
        select(m.Settlement)
        .where(m.Settlement.merchant_id == merchant_id, m.Settlement.business_date >= since)
        .order_by(col(m.Settlement.business_date).desc())
    ).all()
    for st in rows:
        _advance_settlement(st, now)
    session.commit()
    return [s.SettlementOut.of(st, now) for st in rows]


def get_settlement(session: Session, settlement_id: str) -> s.SettlementOut:
    st = _get(session, m.Settlement, settlement_id, "SETTLEMENT_NOT_FOUND")
    now = clock.now()
    _advance_settlement(st, now)
    session.commit()
    return s.SettlementOut.of(st, now)


def get_kyc_status(session: Session, merchant_id: str) -> s.KycStatusOut:
    merchant = _merchant(session, merchant_id)
    links = session.exec(
        select(m.KycLink).where(
            m.KycLink.merchant_id == merchant_id, m.KycLink.status == m.LinkStatus.SENT
        )
    ).all()
    return s.KycStatusOut(
        merchant_id=merchant_id,
        kyc_status=merchant.kyc_status,
        kyc_expiry=merchant.kyc_expiry,
        days_to_expiry=(merchant.kyc_expiry - clock.now().date()).days,
        pending_links=[link.token for link in links],
    )


def _device_out(d: m.Device, now: datetime) -> s.DeviceOut:
    return s.DeviceOut.of(d, now, get_settings().heartbeat_timeout_seconds)


def get_devices(session: Session, merchant_id: str) -> list[s.DeviceOut]:
    _merchant(session, merchant_id)
    now = clock.now()
    rows = session.exec(select(m.Device).where(m.Device.merchant_id == merchant_id)).all()
    for d in rows:
        _refresh_heartbeat(d, now)
    session.commit()
    return [_device_out(d, now) for d in rows]


def get_device(session: Session, device_id: str) -> s.DeviceOut:
    d = _get(session, m.Device, device_id, "DEVICE_NOT_FOUND")
    now = clock.now()
    _refresh_heartbeat(d, now)
    session.commit()
    return _device_out(d, now)


def get_billing(session: Session, merchant_id: str) -> s.BillingOut:
    _merchant(session, merchant_id)
    plans = session.exec(
        select(m.BillingPlan).where(m.BillingPlan.merchant_id == merchant_id)
    ).all()
    out = []
    for p in plans:
        device = session.get(m.Device, p.device_id)
        deductions = session.exec(
            select(m.RentalDeduction)
            .where(m.RentalDeduction.plan_id == p.id)
            .order_by(col(m.RentalDeduction.deducted_at).desc())
        ).all()
        inactive = device.status in (m.DeviceStatus.RETURNED, m.DeviceStatus.INACTIVE)
        out.append(
            s.BillingPlanOut(
                id=p.id,
                device_id=p.device_id,
                device_status=device.status,
                device_returned_at=device.returned_at,
                plan_name=p.plan_name,
                monthly_rent_inr=s.inr(p.monthly_rent_paise),
                rental_active=p.rental_active,
                started_at=p.started_at,
                stopped_at=p.stopped_at,
                out_of_sync=p.rental_active and inactive,
                deductions=[s.RentalDeductionOut.of(x) for x in deductions],
            )
        )
    return s.BillingOut(merchant_id=merchant_id, plans=out)


def get_ticket_history(session: Session, merchant_id: str, days: int = 90) -> list[s.TicketOut]:
    _merchant(session, merchant_id)
    since = clock.now() - timedelta(days=days)
    rows = session.exec(
        select(m.Ticket)
        .where(m.Ticket.merchant_id == merchant_id, m.Ticket.created_at >= since)
        .order_by(col(m.Ticket.created_at).desc())
    ).all()
    return [s.TicketOut.of(t) for t in rows]


def get_refund(session: Session, refund_id: str) -> s.RefundOut:
    r = _get(session, m.RefundRequest, refund_id, "REFUND_NOT_FOUND")
    _advance_refund(session, r, clock.now())
    session.commit()
    return s.RefundOut.of(r)


def get_messages(session: Session, merchant_id: str) -> list[s.MessageOut]:
    _merchant(session, merchant_id)
    rows = session.exec(
        select(m.OutboundMessage)
        .where(m.OutboundMessage.merchant_id == merchant_id)
        .order_by(col(m.OutboundMessage.created_at))
    ).all()
    return [s.MessageOut.of(x) for x in rows]


# ---------- lending ----------


def _emi(principal: float, apr_percent: float, months: int) -> float:
    r = apr_percent / 100 / 12
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


def _kfs(offer: m.Offer) -> s.KeyFactStatement:
    principal = s.inr(offer.amount_paise)
    emi = _emi(principal, offer.apr_percent, offer.tenure_months)
    return s.KeyFactStatement(
        lender=offer.lender,
        loan_amount_inr=principal,
        tenure_months=offer.tenure_months,
        apr_percent=offer.apr_percent,
        processing_fee_inr=s.inr(offer.processing_fee_paise),
        total_repayable_inr=round(emi * offer.tenure_months, 2),
        emi_inr=round(emi, 2),
        cooling_off_days=COOLING_OFF_DAYS,
        notice_version=KFS_NOTICE_VERSION,
    )


def check_loan_eligibility(session: Session, merchant_id: str) -> s.LoanEligibilityOut:
    merchant = _merchant(session, merchant_id)
    now = clock.now()
    rows = session.exec(
        select(m.Transaction).where(
            m.Transaction.merchant_id == merchant_id,
            m.Transaction.created_at >= now - timedelta(days=30),
            m.Transaction.status == m.TxnStatus.SUCCESS,
        )
    ).all()
    gmv = sum(t.amount_paise for t in rows)
    active_days = len({t.created_at.date() for t in rows})

    reasons = []
    if merchant.kyc_status != m.KycStatus.VERIFIED:
        reasons.append("KYC_NOT_VERIFIED")
    if merchant.risk_flag:
        reasons.append("RISK_FLAG")
    if merchant.bank_status != m.BankStatus.ACTIVE:
        reasons.append("BANK_ACCOUNT_NOT_ACTIVE")
    if gmv < s.paise(60_000):
        reasons.append("LOW_30D_VOLUME")
    if active_days < 20:
        reasons.append("FEW_ACTIVE_DAYS")
    declined = session.exec(
        select(m.Offer).where(
            m.Offer.merchant_id == merchant_id,
            m.Offer.status == m.OfferStatus.DECLINED,
            m.Offer.created_at >= now - timedelta(days=30),
        )
    ).first()
    if declined:
        reasons.append("DECLINED_IN_LAST_30_DAYS")

    if reasons:
        return s.LoanEligibilityOut(
            merchant_id=merchant_id,
            eligible=False,
            reasons=reasons,
            gmv_30d_inr=s.inr(gmv),
            active_days_30d=active_days,
            offer=None,
            kfs=None,
        )

    offer = session.exec(
        select(m.Offer).where(
            m.Offer.merchant_id == merchant_id,
            col(m.Offer.status).in_([m.OfferStatus.ELIGIBLE, m.OfferStatus.OFFERED]),
        )
    ).first()
    if not offer:
        amount_inr = min(500_000, max(25_000, int(s.inr(gmv) * 0.6) // 5_000 * 5_000))
        offer = m.Offer(
            id=new_id("OFR"),
            merchant_id=merchant_id,
            product="MERCHANT_LOAN",
            amount_paise=s.paise(amount_inr),
            tenure_months=12,
            apr_percent=22.0,
            processing_fee_paise=s.paise(amount_inr * 0.02),
            lender="Mock NBFC Partner",
            created_at=now,
        )
        session.add(offer)
        session.commit()
    return s.LoanEligibilityOut(
        merchant_id=merchant_id,
        eligible=True,
        reasons=[],
        gmv_30d_inr=s.inr(gmv),
        active_days_30d=active_days,
        offer=s.OfferOut.of(offer),
        kfs=_kfs(offer),
    )


# ---------- writes: KYC and bank ----------


def send_kyc_link(session: Session, merchant_id: str) -> s.KycLinkOut:
    merchant = _merchant(session, merchant_id)
    now = clock.now()
    if merchant.kyc_status == m.KycStatus.NAME_MISMATCH:
        raise ServiceError("KYC_MANUAL_REVIEW", "name mismatch needs a human KYC review")
    expiring = (merchant.kyc_expiry - now.date()).days <= 30
    if merchant.kyc_status == m.KycStatus.VERIFIED and not expiring:
        raise ServiceError("KYC_ALREADY_VERIFIED", "KYC is verified and not expiring")
    link = session.exec(
        select(m.KycLink).where(
            m.KycLink.merchant_id == merchant_id, m.KycLink.status == m.LinkStatus.SENT
        )
    ).first()
    if not link:
        link = m.KycLink(token=new_id("KYC"), merchant_id=merchant_id, created_at=now)
        session.add(link)
        session.commit()
    return s.KycLinkOut(
        token=link.token,
        merchant_id=merchant_id,
        url=f"{get_settings().mock_paytm_public_url}/sim/kyc/{link.token}/complete",
        status=link.status,
        created_at=link.created_at,
    )


def complete_kyc(session: Session, token: str) -> s.KycStatusOut:
    """Simulates the merchant finishing re-KYC from the link. Releases KYC holds to PENDING."""
    link = _get(session, m.KycLink, token, "KYC_LINK_NOT_FOUND")
    merchant = _merchant(session, link.merchant_id)
    now = clock.now()
    if link.status == m.LinkStatus.SENT:
        link.status = m.LinkStatus.COMPLETED
        link.completed_at = now
        merchant.kyc_status = m.KycStatus.VERIFIED
        merchant.kyc_expiry = now.date() + timedelta(days=730)
        held = session.exec(
            select(m.Settlement).where(
                m.Settlement.merchant_id == merchant.id,
                m.Settlement.status == m.SettlementStatus.ON_HOLD,
                m.Settlement.hold_reason == m.HoldReason.KYC_REVERIFICATION,
            )
        ).all()
        for st in held:
            st.status = m.SettlementStatus.PENDING
            st.hold_reason = None
        session.commit()
    return get_kyc_status(session, merchant.id)


def raise_bank_update(session: Session, merchant_id: str, body: s.BankUpdateIn) -> s.BankUpdateOut:
    _merchant(session, merchant_id)
    req = session.exec(
        select(m.BankUpdateRequest).where(
            m.BankUpdateRequest.merchant_id == merchant_id,
            m.BankUpdateRequest.status == m.LinkStatus.SENT,
        )
    ).first()
    if not req:
        req = m.BankUpdateRequest(
            token=new_id("BNK"), merchant_id=merchant_id, created_at=clock.now()
        )
        session.add(req)
        session.commit()
    return s.BankUpdateOut(
        token=req.token,
        merchant_id=merchant_id,
        url=f"{get_settings().mock_paytm_public_url}/sim/bank-update/{req.token}/complete",
        status=req.status,
        created_at=req.created_at,
    )


def complete_bank_update(
    session: Session, token: str, new_ifsc: str | None = None
) -> s.MerchantProfile:
    """Simulates the merchant submitting corrected bank details from the link."""
    req = _get(session, m.BankUpdateRequest, token, "BANK_UPDATE_NOT_FOUND")
    merchant = _merchant(session, req.merchant_id)
    if req.status == m.LinkStatus.SENT:
        req.status = m.LinkStatus.COMPLETED
        req.completed_at = clock.now()
        req.new_ifsc = new_ifsc or merchant.ifsc
        merchant.ifsc = req.new_ifsc
        merchant.bank_status = m.BankStatus.ACTIVE
        failed = session.exec(
            select(m.Settlement).where(
                m.Settlement.merchant_id == merchant.id,
                m.Settlement.hold_reason == m.HoldReason.BANK_ACCOUNT_FAILED,
            )
        ).all()
        for st in failed:
            st.hold_reason = None
        session.commit()
    return s.MerchantProfile.of(merchant)


# ---------- writes: settlement ----------


def retrigger_settlement(session: Session, settlement_id: str) -> s.SettlementOut:
    st = _get(session, m.Settlement, settlement_id, "SETTLEMENT_NOT_FOUND")
    merchant = _merchant(session, st.merchant_id)
    now = clock.now()
    _advance_settlement(st, now)

    if st.status == m.SettlementStatus.CREDITED:
        raise ServiceError("ALREADY_CREDITED", f"settlement credited, UTR {st.utr}")
    if st.status == m.SettlementStatus.PROCESSING:
        return s.SettlementOut.of(st, now)
    if st.hold_reason == m.HoldReason.RISK_REVIEW or merchant.risk_flag:
        raise ServiceError("BLOCKED_RISK", "settlement is under risk review; needs a human")
    if (
        st.hold_reason == m.HoldReason.KYC_REVERIFICATION
        or merchant.kyc_status != m.KycStatus.VERIFIED
    ):
        raise ServiceError("BLOCKED_KYC", "merchant must complete KYC re-verification first")
    if (
        st.hold_reason == m.HoldReason.BANK_ACCOUNT_FAILED
        or merchant.bank_status != m.BankStatus.ACTIVE
    ):
        raise ServiceError("BLOCKED_BANK", "merchant bank account must be updated first")

    st.status = m.SettlementStatus.PROCESSING
    st.processing_started_at = now
    st.completes_at = now + timedelta(seconds=get_settings().settlement_processing_seconds)
    st.attempts += 1
    _advance_settlement(st, now)
    session.commit()
    return s.SettlementOut.of(st, now)


# ---------- writes: devices ----------


def remote_device_reset(session: Session, device_id: str) -> s.DeviceResetOut:
    d = _get(session, m.Device, device_id, "DEVICE_NOT_FOUND")
    if d.status != m.DeviceStatus.ACTIVE:
        raise ServiceError("DEVICE_NOT_ACTIVE", f"device is {d.status}")
    now = clock.now()
    result = "NO_RESPONSE"
    if d.fault == m.DeviceFault.SOFT:
        d.fault = m.DeviceFault.NONE
        d.last_heartbeat_at = now
        result = "RECOVERED"
    elif d.fault == m.DeviceFault.NONE:
        result = "RECOVERED"
    session.commit()
    return s.DeviceResetOut(device_id=d.id, result=result, device=_device_out(d, now))


def power_cycle_device(session: Session, device_id: str) -> s.DeviceOut:
    """Simulates the merchant switching the device off and on (dual control)."""
    d = _get(session, m.Device, device_id, "DEVICE_NOT_FOUND")
    now = clock.now()
    if d.fault in (m.DeviceFault.POWER, m.DeviceFault.SOFT):
        d.fault = m.DeviceFault.NONE
    if d.fault == m.DeviceFault.NONE and d.status == m.DeviceStatus.ACTIVE:
        d.last_heartbeat_at = now
    session.commit()
    return _device_out(d, now)


def book_device_replacement(session: Session, device_id: str) -> s.ReplacementOut:
    d = _get(session, m.Device, device_id, "DEVICE_NOT_FOUND")
    if d.status != m.DeviceStatus.ACTIVE:
        raise ServiceError("DEVICE_NOT_ACTIVE", f"device is {d.status}")
    now = clock.now()
    r = m.DeviceReplacement(
        id=new_id("RPL"),
        device_id=d.id,
        merchant_id=d.merchant_id,
        tracking_id=f"TRK{secrets.randbelow(10**9):09d}",
        chargeable=now.date() > d.warranty_till,
        booked_at=now,
    )
    d.status = m.DeviceStatus.REPLACEMENT_BOOKED
    session.add(r)
    session.commit()
    return s.ReplacementOut.model_validate(r, from_attributes=True)


# ---------- writes: billing ----------


def stop_rental(session: Session, plan_id: str) -> s.StopRentalOut:
    p = _get(session, m.BillingPlan, plan_id, "PLAN_NOT_FOUND")
    if p.rental_active:
        p.rental_active = False
        p.stopped_at = clock.now()
        session.commit()
    return s.StopRentalOut(plan_id=p.id, rental_active=p.rental_active, stopped_at=p.stopped_at)


def reverse_rental_charge(session: Session, deduction_id: str) -> s.ReverseDeductionOut:
    x = _get(session, m.RentalDeduction, deduction_id, "DEDUCTION_NOT_FOUND")
    if x.reversed:
        raise ServiceError("ALREADY_REVERSED", f"reversed on {x.reversed_at} ({x.reversal_ref})")
    x.reversed = True
    x.reversed_at = clock.now()
    x.reversal_ref = new_id("RRV")
    session.commit()
    return s.ReverseDeductionOut(
        deduction_id=x.id,
        amount_inr=s.inr(x.amount_paise),
        reversed=x.reversed,
        reversal_ref=x.reversal_ref,
        reversed_at=x.reversed_at,
    )


# ---------- writes: refunds ----------


def create_refund_request(session: Session, merchant_id: str, body: s.RefundIn) -> s.RefundOut:
    """A credit adjustment to the merchant for money owed to them (e.g. debited-not-credited)."""
    _merchant(session, merchant_id)
    if body.transaction_id:
        txn = _get(session, m.Transaction, body.transaction_id, "TRANSACTION_NOT_FOUND")
        if txn.merchant_id != merchant_id:
            raise ServiceError(
                "TRANSACTION_NOT_FOUND", "transaction belongs to another merchant", 404
            )
        if s.paise(body.amount_inr) > txn.amount_paise:
            raise ServiceError(
                "AMOUNT_EXCEEDS_TRANSACTION", "refund exceeds transaction amount", 422
            )
    now = clock.now()
    r = m.RefundRequest(
        id=new_id("RFD"),
        merchant_id=merchant_id,
        transaction_id=body.transaction_id,
        amount_paise=s.paise(body.amount_inr),
        reason=body.reason,
        created_at=now,
        completes_at=now + timedelta(seconds=get_settings().refund_processing_seconds),
    )
    session.add(r)
    _advance_refund(session, r, now)
    session.commit()
    return s.RefundOut.of(r)


# ---------- writes: tickets ----------


def _event(action: str, note: str | None, actor: str) -> dict:
    return {"at": clock.now().isoformat(), "action": action, "note": note, "actor": actor}


def create_ticket(
    session: Session, merchant_id: str, body: s.TicketCreateIn, actor: str = "sahayak"
) -> s.TicketOut:
    _merchant(session, merchant_id)
    now = clock.now()
    t = m.Ticket(
        id=new_id("TKT"),
        merchant_id=merchant_id,
        category=body.category,
        subject=body.subject,
        created_at=now,
        updated_at=now,
        history=[_event("CREATED", body.note, actor)],
    )
    session.add(t)
    session.commit()
    return s.TicketOut.of(t)


def update_ticket(
    session: Session, ticket_id: str, body: s.TicketUpdateIn, actor: str = "sahayak"
) -> s.TicketOut:
    t = _get(session, m.Ticket, ticket_id, "TICKET_NOT_FOUND")
    if t.status == m.TicketStatus.CLOSED:
        raise ServiceError("TICKET_CLOSED", "reopen the ticket before updating it")
    if body.status:
        t.status = body.status
    t.updated_at = clock.now()
    t.history = [*t.history, _event(f"UPDATED:{t.status}", body.note, actor)]
    session.commit()
    return s.TicketOut.of(t)


def close_ticket(
    session: Session, ticket_id: str, body: s.TicketCloseIn, actor: str = "sahayak"
) -> s.TicketOut:
    t = _get(session, m.Ticket, ticket_id, "TICKET_NOT_FOUND")
    if t.status != m.TicketStatus.CLOSED:
        now = clock.now()
        t.status = m.TicketStatus.CLOSED
        t.closed_at = now
        t.updated_at = now
        t.resolution_note = body.resolution_note
        t.verified_resolution = body.verified_resolution
        t.history = [*t.history, _event("CLOSED", body.resolution_note, actor)]
        session.commit()
    return s.TicketOut.of(t)


def reopen_ticket(session: Session, ticket_id: str, note: str | None = None) -> s.TicketOut:
    t = _get(session, m.Ticket, ticket_id, "TICKET_NOT_FOUND")
    t.status = m.TicketStatus.OPEN
    t.closed_at = None
    t.reopen_count += 1
    t.updated_at = clock.now()
    t.history = [*t.history, _event("REOPENED", note, "merchant")]
    session.commit()
    return s.TicketOut.of(t)


# ---------- writes: consent, leads, messages ----------


def record_consent(session: Session, merchant_id: str, body: s.ConsentIn) -> s.ConsentOut:
    _merchant(session, merchant_id)
    if body.offer_id:
        offer = _get(session, m.Offer, body.offer_id, "OFFER_NOT_FOUND")
        if offer.merchant_id != merchant_id:
            raise ServiceError("OFFER_NOT_FOUND", "offer belongs to another merchant", 404)
        offer.status = m.OfferStatus.OFFERED if body.granted else m.OfferStatus.DECLINED
    c = m.Consent(
        id=new_id("CNS"), merchant_id=merchant_id, created_at=clock.now(), **body.model_dump()
    )
    session.add(c)
    session.commit()
    return s.ConsentOut.of(c)


def create_lead(session: Session, merchant_id: str, body: s.LeadIn) -> s.LeadOut:
    """Defence in depth: the lending system itself refuses a lead without matching consent."""
    _merchant(session, merchant_id)
    consent = _get(session, m.Consent, body.consent_id, "CONSENT_NOT_FOUND")
    offer = _get(session, m.Offer, body.offer_id, "OFFER_NOT_FOUND")
    if (
        consent.merchant_id != merchant_id
        or not consent.granted
        or consent.purpose != LOAN_PURPOSE
        or consent.offer_id != offer.id
    ):
        raise ServiceError(
            "CONSENT_REQUIRED", "a granted LOAN_OFFER consent for this offer is required"
        )
    if offer.status == m.OfferStatus.DECLINED:
        raise ServiceError("OFFER_DECLINED", "merchant declined this offer")
    now = clock.now()
    offer.status = m.OfferStatus.ACCEPTED
    lead = m.Lead(
        id=new_id("LED"),
        merchant_id=merchant_id,
        offer_id=offer.id,
        consent_id=consent.id,
        cooling_off_until=now + timedelta(days=COOLING_OFF_DAYS),
        created_at=now,
    )
    session.add(lead)
    session.commit()
    return s.LeadOut.of(lead)


def send_message(session: Session, merchant_id: str, body: s.MessageIn) -> s.MessageOut:
    _merchant(session, merchant_id)
    msg = m.OutboundMessage(
        id=new_id("MSG"),
        merchant_id=merchant_id,
        channel=body.channel,
        text=body.text,
        created_at=clock.now(),
    )
    session.add(msg)
    session.commit()
    return s.MessageOut.of(msg)


def today() -> date:
    return clock.now().date()
