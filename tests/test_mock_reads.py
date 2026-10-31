from tests.conftest import HEALTHY


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_unknown_merchant_is_404_with_error_body(client):
    r = client.get("/merchants/M9999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "MERCHANT_NOT_FOUND"


def test_merchant_profile(client):
    p = client.get("/merchants/M1001").json()
    assert p["owner_name"] == "Ramesh Kumar"
    assert p["language"] == "hi"
    assert p["kyc_status"] == "PENDING_REVERIFICATION"
    assert len(p["pan"]) == 10 and p["pan"][3] == "P"


def test_transactions_summary(client):
    t = client.get(f"/merchants/{HEALTHY}/transactions", params={"days": 7}).json()
    assert t["count"] == len(t["transactions"]) > 0
    assert t["success_amount_inr"] > 0
    assert t["debited_not_credited"] == []


def test_debited_not_credited_is_surfaced(client):
    t = client.get("/merchants/M1009/transactions", params={"days": 7}).json()
    [txn] = t["debited_not_credited"]
    assert txn["amount_inr"] == 48500.0


def test_settlements_show_hold_and_overdue(client):
    sts = client.get("/merchants/M1001/settlements").json()
    held = [x for x in sts if x["status"] == "ON_HOLD"]
    assert len(held) == 2
    assert all(x["hold_reason"] == "KYC_REVERIFICATION" for x in held)
    assert any(x["overdue"] for x in held)


def test_stuck_settlement_is_overdue(client):
    sts = client.get("/merchants/M1006/settlements").json()
    stuck = [x for x in sts if x["status"] == "PENDING" and x["overdue"]]
    assert len(stuck) == 1 and stuck[0]["hold_reason"] is None


def test_settlement_by_id(client):
    first = client.get(f"/merchants/{HEALTHY}/settlements").json()[0]
    one = client.get(f"/settlements/{first['id']}").json()
    assert one["id"] == first["id"]
    assert client.get("/settlements/STLNOPE").status_code == 404


def test_kyc_status(client):
    k = client.get("/merchants/M1007/kyc").json()
    assert k["kyc_status"] == "VERIFIED" and k["days_to_expiry"] == 5


def test_devices_online_and_offline(client):
    healthy = client.get(f"/merchants/{HEALTHY}/devices").json()
    assert healthy and all(d["online"] for d in healthy)
    lakshmi = {d["type"]: d for d in client.get("/merchants/M1002/devices").json()}
    assert lakshmi["SOUNDBOX"]["online"] is False
    assert lakshmi["SOUNDBOX"]["seconds_since_heartbeat"] >= 45 * 60
    assert lakshmi["EDC"]["online"] is True
    assert "fault" not in lakshmi["SOUNDBOX"]  # internal; the agent must diagnose it


def test_device_by_id(client):
    d = client.get("/merchants/M1002/devices").json()[0]
    assert client.get(f"/devices/{d['id']}").json()["id"] == d["id"]


def test_billing_out_of_sync(client):
    plans = client.get("/merchants/M1003/billing").json()["plans"]
    [plan] = plans
    assert plan["device_status"] == "RETURNED"
    assert plan["rental_active"] is True and plan["out_of_sync"] is True
    assert len(plan["deductions"]) >= 1
    healthy = client.get(f"/merchants/{HEALTHY}/billing").json()["plans"]
    assert not any(p["out_of_sync"] for p in healthy)


def test_ticket_history_shows_repeat_contacts(client):
    tickets = client.get("/merchants/M1008/tickets").json()
    settlement = [t for t in tickets if t["category"] == "SETTLEMENT"]
    assert len(settlement) == 2
    assert not any(t["verified_resolution"] for t in settlement)


def test_messages_empty(client):
    assert client.get(f"/merchants/{HEALTHY}/messages").json() == []
