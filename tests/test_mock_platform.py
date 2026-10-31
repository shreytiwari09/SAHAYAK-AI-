"""Idempotency, fault injection, admin reset and the MCP server."""

import pytest

from tests.conftest import HEALTHY


def held(client, merchant_id):
    return [
        x
        for x in client.get(f"/merchants/{merchant_id}/settlements").json()
        if x["status"] != "CREDITED"
    ]


# ---------- idempotency ----------


def test_same_key_replays_instead_of_acting_twice(client):
    [st] = held(client, "M1006")
    h = {"Idempotency-Key": "thread1:retrigger:" + st["id"]}
    first = client.post(f"/settlements/{st['id']}/retrigger", headers=h).json()
    client.post("/sim/clock/advance", json={"seconds": 11})
    replay = client.post(f"/settlements/{st['id']}/retrigger", headers=h).json()
    assert replay == first  # stored response, not ALREADY_CREDITED
    assert client.get(f"/settlements/{st['id']}").json()["attempts"] == 2


def test_duplicate_refund_webhook_creates_one_refund(client):
    h = {"Idempotency-Key": "wa-msg-123:refund"}
    body = {"amount_inr": 100, "reason": "duplicate webhook test"}
    a = client.post(f"/merchants/{HEALTHY}/refunds", json=body, headers=h).json()
    b = client.post(f"/merchants/{HEALTHY}/refunds", json=body, headers=h).json()
    assert a["id"] == b["id"]


def test_key_reused_for_different_operation_is_rejected(client):
    h = {"Idempotency-Key": "k1"}
    client.post(f"/merchants/{HEALTHY}/messages", json={"text": "hi"}, headers=h)
    r = client.post(
        f"/merchants/{HEALTHY}/tickets", json={"category": "OTHER", "subject": "x"}, headers=h
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ---------- faults ----------


def test_fault_kyc_hold(client):
    r = client.post("/faults/kyc_hold", json={"merchant_id": HEALTHY}).json()
    st = client.get(f"/settlements/{r['settlement_id']}").json()
    assert st["status"] == "ON_HOLD" and st["hold_reason"] == "KYC_REVERIFICATION"
    assert client.get(f"/merchants/{HEALTHY}/kyc").json()["kyc_status"] == "PENDING_REVERIFICATION"


def test_fault_bank_fail(client):
    r = client.post("/faults/bank_fail", json={"merchant_id": HEALTHY}).json()
    assert client.get(f"/settlements/{r['settlement_id']}").json()["status"] == "FAILED"
    assert client.get(f"/merchants/{HEALTHY}").json()["bank_status"] == "FAILED"


def test_fault_settlement_stuck(client):
    r = client.post("/faults/settlement_stuck", json={"merchant_id": HEALTHY}).json()
    st = client.get(f"/settlements/{r['settlement_id']}").json()
    assert st["status"] == "PENDING" and st["overdue"] is True


def test_fault_risk_flag(client):
    r = client.post("/faults/risk_flag", json={"merchant_id": HEALTHY}).json()
    assert client.get(f"/settlements/{r['settlement_id']}").json()["hold_reason"] == "RISK_REVIEW"


def test_fault_heartbeat_loss(client):
    r = client.post("/faults/heartbeat_loss", json={"merchant_id": HEALTHY}).json()
    assert client.get(f"/devices/{r['device_id']}").json()["online"] is False


def test_fault_rental_desync(client):
    client.post("/faults/rental_desync", json={"merchant_id": HEALTHY})
    plans = client.get(f"/merchants/{HEALTHY}/billing").json()["plans"]
    assert any(p["out_of_sync"] for p in plans)


def test_fault_high_refund(client):
    r = client.post(
        "/faults/high_refund", json={"merchant_id": HEALTHY, "amount_inr": 75000}
    ).json()
    [txn] = client.get(f"/merchants/{HEALTHY}/transactions").json()["debited_not_credited"]
    assert txn["id"] == r["transaction_id"] and txn["amount_inr"] == 75000


def test_unknown_fault_is_rejected(client):
    assert client.post("/faults/meteor_strike", json={"merchant_id": HEALTHY}).status_code == 422


def test_admin_reset_restores_seed(client):
    client.post("/faults/kyc_hold", json={"merchant_id": HEALTHY})
    seeded = client.post("/admin/reset").json()["seeded"]
    assert seeded["merchants"] == 15
    assert client.get(f"/merchants/{HEALTHY}/kyc").json()["kyc_status"] == "VERIFIED"


# ---------- MCP ----------

READ_TOOLS = {
    "get_merchant_profile",
    "get_transactions",
    "get_settlement_status",
    "get_settlement",
    "get_kyc_status",
    "get_device_status",
    "get_billing",
    "get_ticket_history",
    "check_loan_eligibility",
}
WRITE_TOOLS = {
    "send_kyc_link",
    "raise_bank_update",
    "retrigger_settlement",
    "remote_device_reset",
    "book_device_replacement",
    "stop_rental",
    "reverse_rental_charge",
    "create_refund_request",
    "create_ticket",
    "update_ticket",
    "close_ticket",
    "record_consent",
    "create_lead",
    "send_message",
}


@pytest.fixture
def mcp(engine):
    from mock_paytm.mcp_server import mcp

    return mcp


async def test_mcp_lists_every_tool(mcp):
    names = {t.name for t in await mcp.list_tools()}
    assert READ_TOOLS | WRITE_TOOLS <= names


async def test_mcp_read_and_write(mcp):
    r = await mcp.call_tool("get_merchant_profile", {"merchant_id": "M1001"})
    assert not r.is_error
    assert "Ramesh Kumar" in str(r.structured_content or r.content)

    r = await mcp.call_tool("send_kyc_link", {"merchant_id": "M1001", "idempotency_key": "mcp-1"})
    assert not r.is_error
    again = await mcp.call_tool(
        "send_kyc_link", {"merchant_id": "M1001", "idempotency_key": "mcp-1"}
    )
    assert (again.structured_content or again.content) == (r.structured_content or r.content)


async def test_mcp_service_errors_are_tool_errors(mcp):
    # Raised as ToolError; the MCP protocol layer turns it into an is_error result for clients.
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="KYC_MANUAL_REVIEW"):
        await mcp.call_tool("send_kyc_link", {"merchant_id": "M1011"})
