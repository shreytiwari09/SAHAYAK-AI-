"""MCP server exposing the mock Paytm tools (same service layer as the REST API).

uv run python -m mock_paytm.mcp_server                       # stdio
uv run python -m mock_paytm.mcp_server --transport streamable-http
"""

import argparse
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from sqlmodel import Session

from mock_paytm import schemas as s
from mock_paytm import service as svc
from mock_paytm.db import get_engine

mcp = MCPServer(
    name="mock-paytm",
    instructions="Paytm merchant systems (mock). Read tools are safe. Write tools change merchant "
    "state; pass idempotency_key so retries never act twice.",
)


def _run(
    operation: str, idempotency_key: str | None, fn: Callable[[Session], BaseModel | list]
) -> Any:
    with Session(get_engine()) as session:
        try:
            if idempotency_key is not None:
                result = svc.idempotent(session, idempotency_key, operation, lambda: fn(session))
            else:
                result = fn(session)
        except svc.ServiceError as e:
            raise ToolError(f"{e.code}: {e.message}") from e
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if isinstance(result, list):
        return [r.model_dump(mode="json") for r in result]
    return result


# ---------- reads ----------


@mcp.tool()
def get_merchant_profile(merchant_id: str) -> dict:
    """Merchant profile: owner, business, city, language, KYC and bank status, risk flag."""
    return _run("read", None, lambda ss: svc.get_merchant_profile(ss, merchant_id))


@mcp.tool()
def get_transactions(merchant_id: str, days: int = 7) -> dict:
    """Recent transactions with totals; lists customer payments debited but not credited."""
    return _run("read", None, lambda ss: svc.get_transactions(ss, merchant_id, days))


@mcp.tool()
def get_settlement_status(merchant_id: str, days: int = 7) -> list:
    """Settlements for recent business days: status, hold reason, overdue flag, UTR."""
    return _run("read", None, lambda ss: svc.get_settlements(ss, merchant_id, days))


@mcp.tool()
def get_settlement(settlement_id: str) -> dict:
    """One settlement by id (use to verify a re-trigger reached CREDITED)."""
    return _run("read", None, lambda ss: svc.get_settlement(ss, settlement_id))


@mcp.tool()
def get_kyc_status(merchant_id: str) -> dict:
    """KYC status, expiry and any re-KYC links already sent."""
    return _run("read", None, lambda ss: svc.get_kyc_status(ss, merchant_id))


@mcp.tool()
def get_device_status(merchant_id: str) -> list:
    """Merchant devices with online status, heartbeat age and warranty."""
    return _run("read", None, lambda ss: svc.get_devices(ss, merchant_id))


@mcp.tool()
def get_billing(merchant_id: str) -> dict:
    """Rental plans and deductions; out_of_sync marks rent active on a returned device."""
    return _run("read", None, lambda ss: svc.get_billing(ss, merchant_id))


@mcp.tool()
def get_ticket_history(merchant_id: str, days: int = 90) -> list:
    """Support tickets in the window, including reopen counts and closure notes."""
    return _run("read", None, lambda ss: svc.get_ticket_history(ss, merchant_id, days))


@mcp.tool()
def check_loan_eligibility(merchant_id: str) -> dict:
    """Loan pre-qualification with offer and Key Fact Statement when eligible."""
    return _run("read", None, lambda ss: svc.check_loan_eligibility(ss, merchant_id))


# ---------- writes ----------


@mcp.tool()
def send_kyc_link(merchant_id: str, idempotency_key: str | None = None) -> dict:
    """Send the merchant a re-KYC link."""
    return _run("send_kyc_link", idempotency_key, lambda ss: svc.send_kyc_link(ss, merchant_id))


@mcp.tool()
def raise_bank_update(merchant_id: str, reason: str, idempotency_key: str | None = None) -> dict:
    """Send the merchant a link to correct their bank account details."""
    body = s.BankUpdateIn(reason=reason)
    return _run(
        "raise_bank_update",
        idempotency_key,
        lambda ss: svc.raise_bank_update(ss, merchant_id, body),
    )


@mcp.tool()
def retrigger_settlement(settlement_id: str, idempotency_key: str | None = None) -> dict:
    """Re-run a pending or failed settlement once its blocker is cleared."""
    return _run(
        "retrigger_settlement",
        idempotency_key,
        lambda ss: svc.retrigger_settlement(ss, settlement_id),
    )


@mcp.tool()
def remote_device_reset(device_id: str, idempotency_key: str | None = None) -> dict:
    """Remotely restart a device. Returns RECOVERED or NO_RESPONSE."""
    return _run(
        "remote_device_reset", idempotency_key, lambda ss: svc.remote_device_reset(ss, device_id)
    )


@mcp.tool()
def book_device_replacement(device_id: str, idempotency_key: str | None = None) -> dict:
    """Book a replacement device; chargeable when out of warranty."""
    return _run(
        "book_device_replacement",
        idempotency_key,
        lambda ss: svc.book_device_replacement(ss, device_id),
    )


@mcp.tool()
def stop_rental(plan_id: str, idempotency_key: str | None = None) -> dict:
    """Stop rental billing on a plan."""
    return _run("stop_rental", idempotency_key, lambda ss: svc.stop_rental(ss, plan_id))


@mcp.tool()
def reverse_rental_charge(deduction_id: str, idempotency_key: str | None = None) -> dict:
    """Reverse one rental deduction back to the merchant."""
    return _run(
        "reverse_rental_charge",
        idempotency_key,
        lambda ss: svc.reverse_rental_charge(ss, deduction_id),
    )


@mcp.tool()
def create_refund_request(
    merchant_id: str,
    amount_inr: float,
    reason: str,
    transaction_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Credit adjustment to the merchant for money owed to them."""
    body = s.RefundIn(amount_inr=amount_inr, reason=reason, transaction_id=transaction_id)
    return _run(
        "create_refund_request",
        idempotency_key,
        lambda ss: svc.create_refund_request(ss, merchant_id, body),
    )


@mcp.tool()
def create_ticket(
    merchant_id: str,
    category: str,
    subject: str,
    note: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Open a support ticket."""
    body = s.TicketCreateIn(category=category, subject=subject, note=note)
    return _run(
        "create_ticket", idempotency_key, lambda ss: svc.create_ticket(ss, merchant_id, body)
    )


@mcp.tool()
def update_ticket(
    ticket_id: str, note: str, status: str | None = None, idempotency_key: str | None = None
) -> dict:
    """Add a note to a ticket and optionally change its status."""
    body = s.TicketUpdateIn(note=note, status=status)
    return _run("update_ticket", idempotency_key, lambda ss: svc.update_ticket(ss, ticket_id, body))


@mcp.tool()
def close_ticket(
    ticket_id: str,
    resolution_note: str,
    verified_resolution: bool,
    idempotency_key: str | None = None,
) -> dict:
    """Close a ticket, recording whether the fix was verified."""
    body = s.TicketCloseIn(resolution_note=resolution_note, verified_resolution=verified_resolution)
    return _run("close_ticket", idempotency_key, lambda ss: svc.close_ticket(ss, ticket_id, body))


@mcp.tool()
def record_consent(
    merchant_id: str,
    purpose: str,
    notice_version: str,
    granted: bool,
    channel: str,
    offer_id: str | None = None,
    button_payload: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Record the merchant's explicit yes/no to a purpose (e.g. LOAN_OFFER)."""
    body = s.ConsentIn(
        purpose=purpose,
        notice_version=notice_version,
        granted=granted,
        channel=channel,
        offer_id=offer_id,
        button_payload=button_payload,
    )
    return _run(
        "record_consent", idempotency_key, lambda ss: svc.record_consent(ss, merchant_id, body)
    )


@mcp.tool()
def create_lead(
    merchant_id: str, offer_id: str, consent_id: str, idempotency_key: str | None = None
) -> dict:
    """Create a lending lead. Refused without a granted consent for that offer."""
    body = s.LeadIn(offer_id=offer_id, consent_id=consent_id)
    return _run("create_lead", idempotency_key, lambda ss: svc.create_lead(ss, merchant_id, body))


@mcp.tool()
def send_message(
    merchant_id: str, text: str, channel: str = "whatsapp", idempotency_key: str | None = None
) -> dict:
    """Send the merchant a message on their channel."""
    body = s.MessageIn(channel=channel, text=text)
    return _run("send_message", idempotency_key, lambda ss: svc.send_message(ss, merchant_id, body))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    mcp.run(transport=parser.parse_args().transport)


if __name__ == "__main__":
    main()
