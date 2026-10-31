"""API contracts: what callers (the Sahayak tool layer, MCP clients) see.

Money is exposed in rupees (`*_inr`), stored internally as paise.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from mock_paytm import models as m


def inr(paise: int) -> float:
    return round(paise / 100, 2)


def paise(rupees: float) -> int:
    return round(rupees * 100)


def fields(row: Any, exclude: set[str]) -> dict[str, Any]:
    """Column values by attribute access (model_dump skips attributes expired by a commit)."""
    return {k: getattr(row, k) for k in type(row).model_fields if k not in exclude}


class ErrorBody(BaseModel):
    code: str
    message: str


# ---------- reads ----------


class MerchantProfile(BaseModel):
    id: str
    owner_name: str
    business_name: str
    business_type: str
    city: str
    state: str
    language: str
    phone: str
    pan: str
    kyc_status: m.KycStatus
    kyc_expiry: date
    bank_account: str
    ifsc: str
    bank_status: m.BankStatus
    risk_flag: bool
    onboarded_at: datetime

    @classmethod
    def of(cls, x: m.Merchant) -> "MerchantProfile":
        return cls.model_validate(x, from_attributes=True)


class TransactionOut(BaseModel):
    id: str
    amount_inr: float
    mode: str
    payer_vpa: str | None
    status: m.TxnStatus
    created_at: datetime
    settlement_id: str | None

    @classmethod
    def of(cls, x: m.Transaction) -> "TransactionOut":
        return cls(**fields(x, {"amount_paise", "merchant_id"}), amount_inr=inr(x.amount_paise))


class TransactionSummary(BaseModel):
    merchant_id: str
    days: int
    count: int
    success_amount_inr: float
    failed_count: int
    debited_not_credited: list[TransactionOut]
    transactions: list[TransactionOut]


class SettlementOut(BaseModel):
    id: str
    merchant_id: str
    business_date: date
    amount_inr: float
    rental_deducted_inr: float
    status: m.SettlementStatus
    hold_reason: m.HoldReason | None
    expected_at: datetime
    overdue: bool
    processing_started_at: datetime | None
    completes_at: datetime | None
    credited_at: datetime | None
    utr: str | None
    attempts: int

    @classmethod
    def of(cls, x: m.Settlement, now: datetime) -> "SettlementOut":
        data = fields(x, {"amount_paise", "rental_deducted_paise"})
        overdue = x.status != m.SettlementStatus.CREDITED and now > x.expected_at
        return cls(
            **data,
            amount_inr=inr(x.amount_paise),
            rental_deducted_inr=inr(x.rental_deducted_paise),
            overdue=overdue,
        )


class KycStatusOut(BaseModel):
    merchant_id: str
    kyc_status: m.KycStatus
    kyc_expiry: date
    days_to_expiry: int
    pending_links: list[str]


class DeviceOut(BaseModel):
    id: str
    merchant_id: str
    type: m.DeviceType
    model: str
    serial: str
    status: m.DeviceStatus
    online: bool
    last_heartbeat_at: datetime
    seconds_since_heartbeat: int
    warranty_till: date
    in_warranty: bool
    returned_at: datetime | None

    @classmethod
    def of(cls, x: m.Device, now: datetime, timeout_s: int) -> "DeviceOut":
        since = int((now - x.last_heartbeat_at).total_seconds())
        return cls(
            **fields(x, {"fault"}),
            online=x.status == m.DeviceStatus.ACTIVE and since <= timeout_s,
            seconds_since_heartbeat=since,
            in_warranty=now.date() <= x.warranty_till,
        )


class RentalDeductionOut(BaseModel):
    id: str
    plan_id: str
    period: str
    amount_inr: float
    deducted_at: datetime
    reversed: bool
    reversed_at: datetime | None
    reversal_ref: str | None

    @classmethod
    def of(cls, x: m.RentalDeduction) -> "RentalDeductionOut":
        return cls(**fields(x, {"amount_paise", "merchant_id"}), amount_inr=inr(x.amount_paise))


class BillingPlanOut(BaseModel):
    id: str
    device_id: str
    device_status: m.DeviceStatus
    device_returned_at: datetime | None
    plan_name: str
    monthly_rent_inr: float
    rental_active: bool
    started_at: datetime
    stopped_at: datetime | None
    # True when billing disagrees with the device: rent still active on a returned/inactive device.
    out_of_sync: bool
    deductions: list[RentalDeductionOut]


class BillingOut(BaseModel):
    merchant_id: str
    plans: list[BillingPlanOut]


class TicketOut(BaseModel):
    id: str
    merchant_id: str
    category: str
    subject: str
    status: m.TicketStatus
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    reopen_count: int
    resolution_note: str | None
    verified_resolution: bool
    history: list[dict[str, Any]]

    @classmethod
    def of(cls, x: m.Ticket) -> "TicketOut":
        return cls.model_validate(x, from_attributes=True)


class OfferOut(BaseModel):
    id: str
    product: str
    amount_inr: float
    tenure_months: int
    apr_percent: float
    processing_fee_inr: float
    lender: str
    status: m.OfferStatus

    @classmethod
    def of(cls, x: m.Offer) -> "OfferOut":
        return cls(
            **fields(x, {"amount_paise", "processing_fee_paise", "merchant_id", "created_at"}),
            amount_inr=inr(x.amount_paise),
            processing_fee_inr=inr(x.processing_fee_paise),
        )


class KeyFactStatement(BaseModel):
    """Digital Lending Directions 2025: KFS shown before acceptance."""

    lender: str
    loan_amount_inr: float
    tenure_months: int
    apr_percent: float
    processing_fee_inr: float
    total_repayable_inr: float
    emi_inr: float
    cooling_off_days: int
    notice_version: str


class LoanEligibilityOut(BaseModel):
    merchant_id: str
    eligible: bool
    reasons: list[str]
    gmv_30d_inr: float
    active_days_30d: int
    offer: OfferOut | None
    kfs: KeyFactStatement | None


# ---------- writes ----------


class KycLinkOut(BaseModel):
    token: str
    merchant_id: str
    url: str
    status: m.LinkStatus
    created_at: datetime


class BankUpdateIn(BaseModel):
    reason: str = "settlement failed: bank account rejected"


class BankUpdateOut(BaseModel):
    token: str
    merchant_id: str
    url: str
    status: m.LinkStatus
    created_at: datetime


class DeviceResetOut(BaseModel):
    device_id: str
    result: str  # RECOVERED / NO_RESPONSE
    device: DeviceOut


class ReplacementOut(BaseModel):
    id: str
    device_id: str
    status: str
    tracking_id: str
    chargeable: bool
    booked_at: datetime


class StopRentalOut(BaseModel):
    plan_id: str
    rental_active: bool
    stopped_at: datetime | None


class ReverseDeductionOut(BaseModel):
    deduction_id: str
    amount_inr: float
    reversed: bool
    reversal_ref: str | None
    reversed_at: datetime | None


class RefundIn(BaseModel):
    amount_inr: float = Field(gt=0)
    reason: str
    transaction_id: str | None = None


class RefundOut(BaseModel):
    id: str
    merchant_id: str
    transaction_id: str | None
    amount_inr: float
    reason: str
    status: m.RefundStatus
    created_at: datetime
    completes_at: datetime
    posted_at: datetime | None

    @classmethod
    def of(cls, x: m.RefundRequest) -> "RefundOut":
        return cls(**fields(x, {"amount_paise"}), amount_inr=inr(x.amount_paise))


class TicketCreateIn(BaseModel):
    category: str
    subject: str
    note: str | None = None


class TicketUpdateIn(BaseModel):
    status: m.TicketStatus | None = None
    note: str


class TicketCloseIn(BaseModel):
    resolution_note: str
    verified_resolution: bool


class ConsentIn(BaseModel):
    purpose: str
    notice_version: str
    granted: bool
    channel: str
    offer_id: str | None = None
    button_payload: str | None = None


class ConsentOut(BaseModel):
    id: str
    merchant_id: str
    purpose: str
    notice_version: str
    offer_id: str | None
    granted: bool
    channel: str
    button_payload: str | None
    created_at: datetime

    @classmethod
    def of(cls, x: m.Consent) -> "ConsentOut":
        return cls.model_validate(x, from_attributes=True)


class LeadIn(BaseModel):
    offer_id: str
    consent_id: str


class LeadOut(BaseModel):
    id: str
    merchant_id: str
    offer_id: str
    consent_id: str
    status: str
    cooling_off_until: datetime
    created_at: datetime

    @classmethod
    def of(cls, x: m.Lead) -> "LeadOut":
        return cls.model_validate(x, from_attributes=True)


class MessageIn(BaseModel):
    channel: str = "whatsapp"
    text: str


class MessageOut(BaseModel):
    id: str
    merchant_id: str
    channel: str
    text: str
    created_at: datetime

    @classmethod
    def of(cls, x: m.OutboundMessage) -> "MessageOut":
        return cls.model_validate(x, from_attributes=True)
