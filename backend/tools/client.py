"""Typed async client for the Paytm merchant systems (mock today, production later).

Every write carries an idempotency key derived from (thread, action, reference), so a resumed
graph node, a retry or a duplicate webhook never acts twice (design decision D6).
"""

import hashlib
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter

from backend.config import get_settings
from backend.net import ipv4_client
from mock_paytm import schemas as s

T = TypeVar("T")


class ToolCallError(Exception):
    """A Paytm system refused or failed a call. `code` is the system's error code."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def idempotency_key(thread_id: str, action: str, ref: str) -> str:
    return hashlib.sha256(f"{thread_id}|{action}|{ref}".encode()).hexdigest()[:32]


class PaytmTools:
    """One method per tool in PRD §9. Reads return contract models; writes need a thread id."""

    def __init__(self, http: httpx.AsyncClient | None = None):
        settings = get_settings()
        self.http = http or ipv4_client(settings.tool_timeout_seconds)
        if http is None:
            self.http.base_url = httpx.URL(settings.mock_paytm_base_url)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        out: type[T] | Any,
        *,
        json: BaseModel | None = None,
        params: dict | None = None,
        key: str | None = None,
    ) -> T:
        headers = {"Idempotency-Key": key} if key else None
        body = json.model_dump(mode="json") if json is not None else None
        resp = await self.http.request(method, path, json=body, params=params, headers=headers)
        if resp.is_error:
            try:
                err = resp.json()["error"]
                raise ToolCallError(err["code"], err["message"], resp.status_code)
            except (ValueError, KeyError, TypeError):
                raise ToolCallError("HTTP_ERROR", resp.text[:200], resp.status_code) from None
        return TypeAdapter(out).validate_python(resp.json())

    # ---------- reads ----------

    async def list_merchants(self) -> list[dict]:
        return await self._call("GET", "/merchants", list[dict])

    async def get_merchant_profile(self, merchant_id: str) -> s.MerchantProfile:
        return await self._call("GET", f"/merchants/{merchant_id}", s.MerchantProfile)

    async def get_transactions(self, merchant_id: str, days: int = 7) -> s.TransactionSummary:
        return await self._call(
            "GET",
            f"/merchants/{merchant_id}/transactions",
            s.TransactionSummary,
            params={"days": days},
        )

    async def get_settlement_status(self, merchant_id: str, days: int = 7) -> list[s.SettlementOut]:
        return await self._call(
            "GET",
            f"/merchants/{merchant_id}/settlements",
            list[s.SettlementOut],
            params={"days": days},
        )

    async def get_settlement(self, settlement_id: str) -> s.SettlementOut:
        return await self._call("GET", f"/settlements/{settlement_id}", s.SettlementOut)

    async def get_kyc_status(self, merchant_id: str) -> s.KycStatusOut:
        return await self._call("GET", f"/merchants/{merchant_id}/kyc", s.KycStatusOut)

    async def get_device_status(self, merchant_id: str) -> list[s.DeviceOut]:
        return await self._call("GET", f"/merchants/{merchant_id}/devices", list[s.DeviceOut])

    async def get_device(self, device_id: str) -> s.DeviceOut:
        return await self._call("GET", f"/devices/{device_id}", s.DeviceOut)

    async def get_billing(self, merchant_id: str) -> s.BillingOut:
        return await self._call("GET", f"/merchants/{merchant_id}/billing", s.BillingOut)

    async def get_ticket_history(self, merchant_id: str, days: int = 90) -> list[s.TicketOut]:
        return await self._call(
            "GET", f"/merchants/{merchant_id}/tickets", list[s.TicketOut], params={"days": days}
        )

    async def check_loan_eligibility(self, merchant_id: str) -> s.LoanEligibilityOut:
        return await self._call(
            "GET", f"/merchants/{merchant_id}/loan-eligibility", s.LoanEligibilityOut
        )

    async def get_refund(self, refund_id: str) -> s.RefundOut:
        return await self._call("GET", f"/refunds/{refund_id}", s.RefundOut)

    # ---------- writes ----------

    async def send_kyc_link(self, thread_id: str, merchant_id: str) -> s.KycLinkOut:
        key = idempotency_key(thread_id, "send_kyc_link", merchant_id)
        return await self._call("POST", f"/merchants/{merchant_id}/kyc/link", s.KycLinkOut, key=key)

    async def raise_bank_update(
        self, thread_id: str, merchant_id: str, reason: str
    ) -> s.BankUpdateOut:
        key = idempotency_key(thread_id, "raise_bank_update", merchant_id)
        return await self._call(
            "POST",
            f"/merchants/{merchant_id}/bank-update",
            s.BankUpdateOut,
            json=s.BankUpdateIn(reason=reason),
            key=key,
        )

    async def retrigger_settlement(self, thread_id: str, settlement_id: str) -> s.SettlementOut:
        key = idempotency_key(thread_id, "retrigger_settlement", settlement_id)
        return await self._call(
            "POST", f"/settlements/{settlement_id}/retrigger", s.SettlementOut, key=key
        )

    async def remote_device_reset(self, thread_id: str, device_id: str) -> s.DeviceResetOut:
        key = idempotency_key(thread_id, "remote_device_reset", device_id)
        return await self._call("POST", f"/devices/{device_id}/reset", s.DeviceResetOut, key=key)

    async def book_device_replacement(self, thread_id: str, device_id: str) -> s.ReplacementOut:
        key = idempotency_key(thread_id, "book_device_replacement", device_id)
        return await self._call(
            "POST", f"/devices/{device_id}/replacement", s.ReplacementOut, key=key
        )

    async def stop_rental(self, thread_id: str, plan_id: str) -> s.StopRentalOut:
        key = idempotency_key(thread_id, "stop_rental", plan_id)
        return await self._call("POST", f"/billing/plans/{plan_id}/stop", s.StopRentalOut, key=key)

    async def reverse_rental_charge(
        self, thread_id: str, deduction_id: str
    ) -> s.ReverseDeductionOut:
        key = idempotency_key(thread_id, "reverse_rental_charge", deduction_id)
        return await self._call(
            "POST", f"/billing/deductions/{deduction_id}/reverse", s.ReverseDeductionOut, key=key
        )

    async def create_refund_request(
        self,
        thread_id: str,
        merchant_id: str,
        amount_inr: float,
        reason: str,
        transaction_id: str | None = None,
    ) -> s.RefundOut:
        key = idempotency_key(
            thread_id, "create_refund_request", transaction_id or f"{merchant_id}:{amount_inr}"
        )
        body = s.RefundIn(amount_inr=amount_inr, reason=reason, transaction_id=transaction_id)
        return await self._call(
            "POST", f"/merchants/{merchant_id}/refunds", s.RefundOut, json=body, key=key
        )

    async def create_ticket(
        self, thread_id: str, merchant_id: str, category: str, subject: str, note: str | None = None
    ) -> s.TicketOut:
        key = idempotency_key(thread_id, "create_ticket", f"{merchant_id}:{category}")
        body = s.TicketCreateIn(category=category, subject=subject, note=note)
        return await self._call(
            "POST", f"/merchants/{merchant_id}/tickets", s.TicketOut, json=body, key=key
        )

    async def update_ticket(
        self, thread_id: str, ticket_id: str, note: str, status: str | None = None
    ) -> s.TicketOut:
        # Each distinct note is its own update; the same note replayed is a duplicate.
        ref = f"{ticket_id}:{status}:{hashlib.sha256(note.encode()).hexdigest()[:12]}"
        key = idempotency_key(thread_id, "update_ticket", ref)
        body = s.TicketUpdateIn(status=status, note=note)
        return await self._call("PATCH", f"/tickets/{ticket_id}", s.TicketOut, json=body, key=key)

    async def close_ticket(
        self, thread_id: str, ticket_id: str, resolution_note: str, verified_resolution: bool
    ) -> s.TicketOut:
        key = idempotency_key(thread_id, "close_ticket", ticket_id)
        body = s.TicketCloseIn(
            resolution_note=resolution_note, verified_resolution=verified_resolution
        )
        return await self._call(
            "POST", f"/tickets/{ticket_id}/close", s.TicketOut, json=body, key=key
        )

    async def record_consent(
        self, thread_id: str, merchant_id: str, consent: s.ConsentIn
    ) -> s.ConsentOut:
        key = idempotency_key(
            thread_id, "record_consent", f"{merchant_id}:{consent.purpose}:{consent.offer_id}"
        )
        return await self._call(
            "POST", f"/merchants/{merchant_id}/consents", s.ConsentOut, json=consent, key=key
        )

    async def create_lead(
        self, thread_id: str, merchant_id: str, offer_id: str, consent_id: str
    ) -> s.LeadOut:
        key = idempotency_key(thread_id, "create_lead", offer_id)
        body = s.LeadIn(offer_id=offer_id, consent_id=consent_id)
        return await self._call(
            "POST", f"/merchants/{merchant_id}/leads", s.LeadOut, json=body, key=key
        )

    async def send_message(
        self, thread_id: str, merchant_id: str, text: str, channel: str = "whatsapp"
    ) -> s.MessageOut:
        ref = f"{merchant_id}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        key = idempotency_key(thread_id, "send_message", ref)
        body = s.MessageIn(channel=channel, text=text)
        return await self._call(
            "POST", f"/merchants/{merchant_id}/messages", s.MessageOut, json=body, key=key
        )
