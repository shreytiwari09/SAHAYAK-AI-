"""REST API for the mock Paytm systems.

Writes accept an `Idempotency-Key` header: a repeated key replays the first response instead of
acting twice. `/sim/*` routes simulate what the merchant does on their side (finishing KYC,
power-cycling a device); `/faults/*` and `/admin/*` exist for demos, evals and tests.
"""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlmodel import Session

from mock_paytm import clock, faults
from mock_paytm import models as m
from mock_paytm import schemas as s
from mock_paytm import service as svc
from mock_paytm.db import get_engine, get_session, init_db
from mock_paytm.seed import seed


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Mock Paytm merchant systems",
    version="0.1.0",
    description="Contract-first mock of Paytm ledger, settlement, KYC, device, billing, "
    "ticketing, lending and notification services for Sahayak AI.",
    lifespan=lifespan,
)

SessionDep = Annotated[Session, Depends(get_session)]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


@app.exception_handler(svc.ServiceError)
async def service_error_handler(_: Request, exc: svc.ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
    )


ERRORS = {404: {"model": s.ErrorBody}, 409: {"model": s.ErrorBody}}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "now": clock.now().isoformat()}


# ---------- reads ----------


@app.get("/merchants")
def list_merchants(session: SessionDep):
    return svc.list_merchants(session)


@app.get("/merchants/{merchant_id}", response_model=s.MerchantProfile, responses=ERRORS)
def get_merchant(merchant_id: str, session: SessionDep):
    return svc.get_merchant_profile(session, merchant_id)


@app.get(
    "/merchants/{merchant_id}/transactions", response_model=s.TransactionSummary, responses=ERRORS
)
def get_transactions(merchant_id: str, session: SessionDep, days: int = 7):
    return svc.get_transactions(session, merchant_id, days)


@app.get(
    "/merchants/{merchant_id}/settlements", response_model=list[s.SettlementOut], responses=ERRORS
)
def get_settlements(merchant_id: str, session: SessionDep, days: int = 7):
    return svc.get_settlements(session, merchant_id, days)


@app.get("/settlements/{settlement_id}", response_model=s.SettlementOut, responses=ERRORS)
def get_settlement(settlement_id: str, session: SessionDep):
    return svc.get_settlement(session, settlement_id)


@app.get("/merchants/{merchant_id}/kyc", response_model=s.KycStatusOut, responses=ERRORS)
def get_kyc(merchant_id: str, session: SessionDep):
    return svc.get_kyc_status(session, merchant_id)


@app.get("/merchants/{merchant_id}/devices", response_model=list[s.DeviceOut], responses=ERRORS)
def get_devices(merchant_id: str, session: SessionDep):
    return svc.get_devices(session, merchant_id)


@app.get("/devices/{device_id}", response_model=s.DeviceOut, responses=ERRORS)
def get_device(device_id: str, session: SessionDep):
    return svc.get_device(session, device_id)


@app.get("/merchants/{merchant_id}/billing", response_model=s.BillingOut, responses=ERRORS)
def get_billing(merchant_id: str, session: SessionDep):
    return svc.get_billing(session, merchant_id)


@app.get("/merchants/{merchant_id}/tickets", response_model=list[s.TicketOut], responses=ERRORS)
def get_tickets(merchant_id: str, session: SessionDep, days: int = 90):
    return svc.get_ticket_history(session, merchant_id, days)


@app.get(
    "/merchants/{merchant_id}/loan-eligibility",
    response_model=s.LoanEligibilityOut,
    responses=ERRORS,
)
def get_loan_eligibility(merchant_id: str, session: SessionDep):
    return svc.check_loan_eligibility(session, merchant_id)


@app.get("/refunds/{refund_id}", response_model=s.RefundOut, responses=ERRORS)
def get_refund(refund_id: str, session: SessionDep):
    return svc.get_refund(session, refund_id)


@app.get("/merchants/{merchant_id}/messages", response_model=list[s.MessageOut], responses=ERRORS)
def get_messages(merchant_id: str, session: SessionDep):
    return svc.get_messages(session, merchant_id)


# ---------- writes ----------


@app.post("/merchants/{merchant_id}/kyc/link", response_model=s.KycLinkOut, responses=ERRORS)
def send_kyc_link(merchant_id: str, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session, key, "send_kyc_link", lambda: svc.send_kyc_link(session, merchant_id)
    )


@app.post("/merchants/{merchant_id}/bank-update", response_model=s.BankUpdateOut, responses=ERRORS)
def raise_bank_update(
    merchant_id: str, body: s.BankUpdateIn, session: SessionDep, key: IdemKey = None
):
    return svc.idempotent(
        session, key, "raise_bank_update", lambda: svc.raise_bank_update(session, merchant_id, body)
    )


@app.post(
    "/settlements/{settlement_id}/retrigger", response_model=s.SettlementOut, responses=ERRORS
)
def retrigger_settlement(settlement_id: str, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session,
        key,
        "retrigger_settlement",
        lambda: svc.retrigger_settlement(session, settlement_id),
    )


@app.post("/devices/{device_id}/reset", response_model=s.DeviceResetOut, responses=ERRORS)
def remote_device_reset(device_id: str, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session, key, "remote_device_reset", lambda: svc.remote_device_reset(session, device_id)
    )


@app.post("/devices/{device_id}/replacement", response_model=s.ReplacementOut, responses=ERRORS)
def book_device_replacement(device_id: str, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session,
        key,
        "book_device_replacement",
        lambda: svc.book_device_replacement(session, device_id),
    )


@app.post("/billing/plans/{plan_id}/stop", response_model=s.StopRentalOut, responses=ERRORS)
def stop_rental(plan_id: str, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(session, key, "stop_rental", lambda: svc.stop_rental(session, plan_id))


@app.post(
    "/billing/deductions/{deduction_id}/reverse",
    response_model=s.ReverseDeductionOut,
    responses=ERRORS,
)
def reverse_rental_charge(deduction_id: str, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session,
        key,
        "reverse_rental_charge",
        lambda: svc.reverse_rental_charge(session, deduction_id),
    )


@app.post("/merchants/{merchant_id}/refunds", response_model=s.RefundOut, responses=ERRORS)
def create_refund_request(
    merchant_id: str, body: s.RefundIn, session: SessionDep, key: IdemKey = None
):
    return svc.idempotent(
        session,
        key,
        "create_refund_request",
        lambda: svc.create_refund_request(session, merchant_id, body),
    )


@app.post("/merchants/{merchant_id}/tickets", response_model=s.TicketOut, responses=ERRORS)
def create_ticket(
    merchant_id: str, body: s.TicketCreateIn, session: SessionDep, key: IdemKey = None
):
    return svc.idempotent(
        session, key, "create_ticket", lambda: svc.create_ticket(session, merchant_id, body)
    )


@app.patch("/tickets/{ticket_id}", response_model=s.TicketOut, responses=ERRORS)
def update_ticket(ticket_id: str, body: s.TicketUpdateIn, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session, key, "update_ticket", lambda: svc.update_ticket(session, ticket_id, body)
    )


@app.post("/tickets/{ticket_id}/close", response_model=s.TicketOut, responses=ERRORS)
def close_ticket(ticket_id: str, body: s.TicketCloseIn, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session, key, "close_ticket", lambda: svc.close_ticket(session, ticket_id, body)
    )


@app.post("/merchants/{merchant_id}/consents", response_model=s.ConsentOut, responses=ERRORS)
def record_consent(merchant_id: str, body: s.ConsentIn, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session, key, "record_consent", lambda: svc.record_consent(session, merchant_id, body)
    )


@app.post("/merchants/{merchant_id}/leads", response_model=s.LeadOut, responses=ERRORS)
def create_lead(merchant_id: str, body: s.LeadIn, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session, key, "create_lead", lambda: svc.create_lead(session, merchant_id, body)
    )


@app.post("/merchants/{merchant_id}/messages", response_model=s.MessageOut, responses=ERRORS)
def send_message(merchant_id: str, body: s.MessageIn, session: SessionDep, key: IdemKey = None):
    return svc.idempotent(
        session, key, "send_message", lambda: svc.send_message(session, merchant_id, body)
    )


# ---------- merchant-side simulation ----------


class BankUpdateComplete(BaseModel):
    new_ifsc: str | None = None


class ClockAdvance(BaseModel):
    seconds: float


@app.post("/sim/kyc/{token}/complete", response_model=s.KycStatusOut, responses=ERRORS)
def sim_complete_kyc(token: str, session: SessionDep):
    return svc.complete_kyc(session, token)


@app.post("/sim/bank-update/{token}/complete", response_model=s.MerchantProfile, responses=ERRORS)
def sim_complete_bank_update(
    token: str, session: SessionDep, body: BankUpdateComplete | None = None
):
    return svc.complete_bank_update(session, token, body.new_ifsc if body else None)


def _page(title: str, line: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<body style='font-family:system-ui;max-width:28rem;margin:4rem auto;padding:0 1rem'>"
        f"<h2>{title}</h2><p>{line}</p>"
        "<p style='color:#666'>Demo page from the mock Paytm systems.</p>"
    )


@app.get("/sim/kyc/{token}/complete", response_class=HTMLResponse, include_in_schema=False)
def tap_kyc_link(token: str, session: SessionDep):
    """What a merchant sees after tapping the KYC link on their phone."""
    svc.complete_kyc(session, token)
    return _page("KYC re-verification complete ✅", "Your settlement will be released shortly.")


@app.get("/sim/bank-update/{token}/complete", response_class=HTMLResponse, include_in_schema=False)
def tap_bank_link(token: str, session: SessionDep):
    svc.complete_bank_update(session, token)
    return _page("Bank details updated ✅", "Your settlement will be retried shortly.")


@app.post("/sim/devices/{device_id}/power-cycle", response_model=s.DeviceOut, responses=ERRORS)
def sim_power_cycle(device_id: str, session: SessionDep):
    return svc.power_cycle_device(session, device_id)


@app.post("/sim/tickets/{ticket_id}/reopen", response_model=s.TicketOut, responses=ERRORS)
def sim_reopen_ticket(ticket_id: str, session: SessionDep):
    return svc.reopen_ticket(session, ticket_id)


@app.get("/sim/clock")
def sim_clock() -> dict:
    return {"now": clock.now().isoformat()}


@app.post("/sim/clock/advance")
def sim_clock_advance(body: ClockAdvance) -> dict:
    return {"now": clock.advance(body.seconds).isoformat()}


# ---------- faults and admin ----------


class FaultIn(BaseModel):
    merchant_id: str
    amount_inr: float | None = None
    device_fault: m.DeviceFault = m.DeviceFault.POWER


@app.post("/faults/{fault}", responses=ERRORS)
def inject_fault(fault: faults.Fault, body: FaultIn, session: SessionDep) -> dict:
    return faults.inject(session, fault, body.merchant_id, body.amount_inr, body.device_fault)


@app.post("/admin/reset")
def admin_reset() -> dict:
    """Drop all data and reseed. Demo and test use only."""
    clock.reset()
    engine = get_engine()
    init_db(engine, drop=True)
    with Session(engine) as session:
        return {"seeded": seed(session)}
