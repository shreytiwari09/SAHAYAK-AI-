"""Database entities for the mock Paytm systems (PRD §9).

Money is stored as integer paise. Timestamps are timezone-aware UTC (see clock.py).
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class KycStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PENDING_REVERIFICATION = "PENDING_REVERIFICATION"
    EXPIRED = "EXPIRED"
    NAME_MISMATCH = "NAME_MISMATCH"


class BankStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"


class TxnStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    # Customer debited, merchant not credited; awaiting reversal or credit.
    DEBITED_NOT_CREDITED = "DEBITED_NOT_CREDITED"


class SettlementStatus(StrEnum):
    PENDING = "PENDING"
    ON_HOLD = "ON_HOLD"
    PROCESSING = "PROCESSING"
    CREDITED = "CREDITED"
    FAILED = "FAILED"


class HoldReason(StrEnum):
    KYC_REVERIFICATION = "KYC_REVERIFICATION"
    BANK_ACCOUNT_FAILED = "BANK_ACCOUNT_FAILED"
    RISK_REVIEW = "RISK_REVIEW"


class DeviceType(StrEnum):
    SOUNDBOX = "SOUNDBOX"
    EDC = "EDC"


class DeviceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETURNED = "RETURNED"
    INACTIVE = "INACTIVE"
    REPLACEMENT_BOOKED = "REPLACEMENT_BOOKED"


class DeviceFault(StrEnum):
    NONE = "NONE"
    SOFT = "SOFT"  # firmware hang; a remote reset fixes it
    POWER = "POWER"  # needs the merchant to power-cycle (dual control)
    HARD = "HARD"  # hardware fault; needs a replacement


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class LinkStatus(StrEnum):
    SENT = "SENT"
    COMPLETED = "COMPLETED"


class RefundStatus(StrEnum):
    INITIATED = "INITIATED"
    POSTED = "POSTED"


class OfferStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"


class Merchant(SQLModel, table=True):
    id: str = Field(primary_key=True)
    owner_name: str
    business_name: str
    business_type: str
    city: str
    state: str
    language: str  # ISO 639-1, e.g. "hi", "ta"
    phone: str
    pan: str
    kyc_status: KycStatus = KycStatus.VERIFIED
    kyc_expiry: date
    bank_account: str
    ifsc: str
    bank_status: BankStatus = BankStatus.ACTIVE
    risk_flag: bool = False
    onboarded_at: datetime


class Transaction(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    amount_paise: int
    mode: str  # UPI / CARD
    payer_vpa: str | None = None
    status: TxnStatus = TxnStatus.SUCCESS
    created_at: datetime = Field(index=True)
    settlement_id: str | None = Field(default=None, index=True)


class Settlement(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    business_date: date
    amount_paise: int
    rental_deducted_paise: int = 0
    status: SettlementStatus
    hold_reason: HoldReason | None = None
    expected_at: datetime
    processing_started_at: datetime | None = None
    completes_at: datetime | None = None
    credited_at: datetime | None = None
    utr: str | None = None
    attempts: int = 0


class Device(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    type: DeviceType
    model: str
    serial: str
    status: DeviceStatus = DeviceStatus.ACTIVE
    fault: DeviceFault = DeviceFault.NONE
    last_heartbeat_at: datetime
    warranty_till: date
    returned_at: datetime | None = None


class BillingPlan(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    device_id: str = Field(foreign_key="device.id")
    plan_name: str
    monthly_rent_paise: int
    rental_active: bool = True
    started_at: datetime
    stopped_at: datetime | None = None


class RentalDeduction(SQLModel, table=True):
    id: str = Field(primary_key=True)
    plan_id: str = Field(foreign_key="billingplan.id", index=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    period: str  # YYYY-MM
    amount_paise: int
    deducted_at: datetime
    reversed: bool = False
    reversed_at: datetime | None = None
    reversal_ref: str | None = None


class Ticket(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    category: str  # SETTLEMENT / DEVICE / BILLING / KYC / LENDING / OTHER
    subject: str
    status: TicketStatus = TicketStatus.OPEN
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    reopen_count: int = 0
    resolution_note: str | None = None
    verified_resolution: bool = False
    history: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))


class KycLink(SQLModel, table=True):
    token: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    status: LinkStatus = LinkStatus.SENT
    created_at: datetime
    completed_at: datetime | None = None


class BankUpdateRequest(SQLModel, table=True):
    token: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    status: LinkStatus = LinkStatus.SENT
    created_at: datetime
    completed_at: datetime | None = None
    new_ifsc: str | None = None


class DeviceReplacement(SQLModel, table=True):
    id: str = Field(primary_key=True)
    device_id: str = Field(foreign_key="device.id", index=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    status: str = "BOOKED"
    tracking_id: str
    chargeable: bool
    booked_at: datetime


class RefundRequest(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    transaction_id: str | None = None
    amount_paise: int
    reason: str
    status: RefundStatus = RefundStatus.INITIATED
    created_at: datetime
    completes_at: datetime
    posted_at: datetime | None = None


class Offer(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    product: str  # MERCHANT_LOAN
    amount_paise: int
    tenure_months: int
    apr_percent: float
    processing_fee_paise: int
    lender: str
    status: OfferStatus = OfferStatus.ELIGIBLE
    created_at: datetime


class Consent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    purpose: str  # e.g. LOAN_OFFER
    notice_version: str
    offer_id: str | None = None
    granted: bool
    channel: str
    button_payload: str | None = None
    created_at: datetime


class Lead(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    offer_id: str = Field(foreign_key="offer.id")
    consent_id: str = Field(foreign_key="consent.id")
    status: str = "CREATED"
    cooling_off_until: datetime
    created_at: datetime


class OutboundMessage(SQLModel, table=True):
    id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchant.id", index=True)
    channel: str
    text: str
    created_at: datetime


class IdempotencyRecord(SQLModel, table=True):
    key: str = Field(primary_key=True)
    operation: str
    response: dict[str, Any] = Field(sa_column=Column(JSON))
    created_at: datetime
