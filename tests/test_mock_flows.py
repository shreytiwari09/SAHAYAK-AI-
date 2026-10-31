"""State transitions for every workflow the agent will drive."""

from tests.conftest import HEALTHY


def advance(client, seconds):
    client.post("/sim/clock/advance", json={"seconds": seconds})


def held(client, merchant_id):
    return [
        x
        for x in client.get(f"/merchants/{merchant_id}/settlements").json()
        if x["status"] != "CREDITED"
    ]


# ---------- WF-1 settlement ----------


def test_kyc_hold_flow_to_verified_credit(client):
    [newest, older] = held(client, "M1001")

    r = client.post(f"/settlements/{older['id']}/retrigger")
    assert r.status_code == 409 and r.json()["error"]["code"] == "BLOCKED_KYC"

    link = client.post("/merchants/M1001/kyc/link").json()
    assert link["url"].endswith(f"/sim/kyc/{link['token']}/complete")
    # Sending again reuses the open link.
    assert client.post("/merchants/M1001/kyc/link").json()["token"] == link["token"]

    kyc = client.post(f"/sim/kyc/{link['token']}/complete").json()
    assert kyc["kyc_status"] == "VERIFIED" and kyc["pending_links"] == []
    assert {x["status"] for x in held(client, "M1001")} == {"PENDING"}

    for st in (newest, older):
        r = client.post(f"/settlements/{st['id']}/retrigger").json()
        assert r["status"] == "PROCESSING" and r["attempts"] == 2

    advance(client, 11)
    done = client.get(f"/settlements/{older['id']}").json()
    assert done["status"] == "CREDITED" and done["utr"].startswith("UTR")
    assert held(client, "M1001") == []

    r = client.post(f"/settlements/{older['id']}/retrigger")
    assert r.json()["error"]["code"] == "ALREADY_CREDITED"


def test_processing_settlement_is_not_credited_early(client):
    [st] = held(client, "M1006")
    client.post(f"/settlements/{st['id']}/retrigger")
    advance(client, 5)
    assert client.get(f"/settlements/{st['id']}").json()["status"] == "PROCESSING"
    # Re-trigger while processing returns the in-flight settlement, no second attempt.
    assert client.post(f"/settlements/{st['id']}/retrigger").json()["attempts"] == 2
    advance(client, 6)
    assert client.get(f"/settlements/{st['id']}").json()["status"] == "CREDITED"


def test_bank_failure_flow(client):
    [st] = held(client, "M1004")
    assert st["status"] == "FAILED" and st["hold_reason"] == "BANK_ACCOUNT_FAILED"
    assert (
        client.post(f"/settlements/{st['id']}/retrigger").json()["error"]["code"] == "BLOCKED_BANK"
    )

    req = client.post("/merchants/M1004/bank-update", json={"reason": "IFSC changed"}).json()
    prof = client.post(
        f"/sim/bank-update/{req['token']}/complete", json={"new_ifsc": "SBIN0001234"}
    ).json()
    assert prof["bank_status"] == "ACTIVE" and prof["ifsc"] == "SBIN0001234"

    assert client.post(f"/settlements/{st['id']}/retrigger").json()["status"] == "PROCESSING"
    advance(client, 11)
    assert client.get(f"/settlements/{st['id']}").json()["status"] == "CREDITED"


def test_risk_hold_cannot_be_retriggered(client):
    [st] = held(client, "M1005")
    assert st["hold_reason"] == "RISK_REVIEW"
    assert (
        client.post(f"/settlements/{st['id']}/retrigger").json()["error"]["code"] == "BLOCKED_RISK"
    )


def test_kyc_link_rules(client):
    assert client.post("/merchants/M1007/kyc/link").status_code == 200  # expiring soon
    r = client.post("/merchants/M1011/kyc/link")
    assert r.json()["error"]["code"] == "KYC_MANUAL_REVIEW"
    r = client.post(f"/merchants/{HEALTHY}/kyc/link")
    assert r.json()["error"]["code"] == "KYC_ALREADY_VERIFIED"


# ---------- WF-2 device ----------


def soundbox(client, merchant_id):
    return next(
        d for d in client.get(f"/merchants/{merchant_id}/devices").json() if d["type"] == "SOUNDBOX"
    )


def test_power_fault_needs_merchant_power_cycle(client):
    d = soundbox(client, "M1002")
    r = client.post(f"/devices/{d['id']}/reset").json()
    assert r["result"] == "NO_RESPONSE" and r["device"]["online"] is False
    assert client.post(f"/sim/devices/{d['id']}/power-cycle").json()["online"] is True
    assert soundbox(client, "M1002")["online"] is True


def test_soft_fault_fixed_by_remote_reset(client):
    client.post("/faults/heartbeat_loss", json={"merchant_id": HEALTHY, "device_fault": "SOFT"})
    d = soundbox(client, HEALTHY)
    assert d["online"] is False
    r = client.post(f"/devices/{d['id']}/reset").json()
    assert r["result"] == "RECOVERED" and r["device"]["online"] is True


def test_hard_fault_needs_replacement(client):
    d = soundbox(client, "M1012")
    assert client.post(f"/devices/{d['id']}/reset").json()["result"] == "NO_RESPONSE"
    assert client.post(f"/sim/devices/{d['id']}/power-cycle").json()["online"] is False
    rpl = client.post(f"/devices/{d['id']}/replacement").json()
    assert rpl["status"] == "BOOKED" and rpl["tracking_id"].startswith("TRK")
    assert rpl["chargeable"] is (not d["in_warranty"])
    assert client.get(f"/devices/{d['id']}").json()["status"] == "REPLACEMENT_BOOKED"
    again = client.post(f"/devices/{d['id']}/replacement")
    assert again.json()["error"]["code"] == "DEVICE_NOT_ACTIVE"


def test_reset_on_returned_device_is_rejected(client):
    d = soundbox(client, "M1003")
    assert client.post(f"/devices/{d['id']}/reset").json()["error"]["code"] == "DEVICE_NOT_ACTIVE"


# ---------- WF-3 rental desync ----------


def test_rental_desync_fixed_only_when_billing_matches_device(client):
    [plan] = client.get("/merchants/M1003/billing").json()["plans"]
    stopped = client.post(f"/billing/plans/{plan['id']}/stop").json()
    assert stopped["rental_active"] is False and stopped["stopped_at"]
    # Stopping again is a no-op.
    assert (
        client.post(f"/billing/plans/{plan['id']}/stop").json()["stopped_at"]
        == stopped["stopped_at"]
    )

    after = [d for d in plan["deductions"] if d["deducted_at"] > plan["device_returned_at"]]
    assert after
    for ded in after:
        rev = client.post(f"/billing/deductions/{ded['id']}/reverse").json()
        assert rev["reversed"] is True and rev["reversal_ref"].startswith("RRV")
    r = client.post(f"/billing/deductions/{after[0]['id']}/reverse")
    assert r.json()["error"]["code"] == "ALREADY_REVERSED"

    [plan] = client.get("/merchants/M1003/billing").json()["plans"]
    assert plan["out_of_sync"] is False
    assert all(d["reversed"] for d in plan["deductions"])


# ---------- refunds ----------


def test_credit_adjustment_for_debited_not_credited(client):
    [txn] = client.get("/merchants/M1009/transactions").json()["debited_not_credited"]
    too_much = client.post(
        "/merchants/M1009/refunds",
        json={"amount_inr": 60000, "reason": "x", "transaction_id": txn["id"]},
    )
    assert (
        too_much.status_code == 422
        and too_much.json()["error"]["code"] == "AMOUNT_EXCEEDS_TRANSACTION"
    )

    r = client.post(
        "/merchants/M1009/refunds",
        json={
            "amount_inr": 48500,
            "reason": "customer debited, merchant not credited",
            "transaction_id": txn["id"],
        },
    ).json()
    assert r["status"] == "INITIATED"
    advance(client, 11)
    assert client.get(f"/refunds/{r['id']}").json()["status"] == "POSTED"
    assert client.get("/merchants/M1009/transactions").json()["debited_not_credited"] == []


def test_refund_on_other_merchants_transaction_rejected(client):
    [txn] = client.get("/merchants/M1009/transactions").json()["debited_not_credited"]
    r = client.post(
        f"/merchants/{HEALTHY}/refunds",
        json={"amount_inr": 10, "reason": "x", "transaction_id": txn["id"]},
    )
    assert r.status_code == 404


# ---------- tickets ----------


def test_ticket_lifecycle(client):
    t = client.post(
        "/merchants/M1001/tickets",
        json={"category": "SETTLEMENT", "subject": "Settlement not received"},
    ).json()
    assert t["status"] == "OPEN" and t["history"][0]["action"] == "CREATED"
    t = client.patch(
        f"/tickets/{t['id']}", json={"status": "IN_PROGRESS", "note": "KYC link sent"}
    ).json()
    assert t["status"] == "IN_PROGRESS" and len(t["history"]) == 2
    t = client.post(
        f"/tickets/{t['id']}/close",
        json={"resolution_note": "Credited, UTR123", "verified_resolution": True},
    ).json()
    assert t["status"] == "CLOSED" and t["verified_resolution"] is True
    r = client.patch(f"/tickets/{t['id']}", json={"note": "late"})
    assert r.json()["error"]["code"] == "TICKET_CLOSED"
    t = client.post(f"/sim/tickets/{t['id']}/reopen").json()
    assert t["status"] == "OPEN" and t["reopen_count"] == 1 and t["closed_at"] is None


# ---------- WF-4 lending with consent ----------


def test_consented_loan_offer(client):
    el = client.get("/merchants/M1010/loan-eligibility").json()
    assert el["eligible"] is True
    offer, kfs = el["offer"], el["kfs"]
    assert 25000 <= offer["amount_inr"] <= 500000
    assert kfs["cooling_off_days"] >= 1 and kfs["emi_inr"] > 0
    assert kfs["total_repayable_inr"] > kfs["loan_amount_inr"]
    # Re-checking returns the same open offer.
    assert client.get("/merchants/M1010/loan-eligibility").json()["offer"]["id"] == offer["id"]

    consent = client.post(
        "/merchants/M1010/consents",
        json={
            "purpose": "LOAN_OFFER",
            "notice_version": kfs["notice_version"],
            "granted": True,
            "channel": "whatsapp",
            "offer_id": offer["id"],
            "button_payload": "HAAN",
        },
    ).json()
    lead = client.post(
        "/merchants/M1010/leads", json={"offer_id": offer["id"], "consent_id": consent["id"]}
    ).json()
    assert lead["status"] == "CREATED" and lead["cooling_off_until"] > lead["created_at"]


def test_lead_refused_without_matching_consent(client):
    offer = client.get("/merchants/M1010/loan-eligibility").json()["offer"]
    other = client.post(
        "/merchants/M1010/consents",
        json={
            "purpose": "MARKETING",
            "notice_version": "v1",
            "granted": True,
            "channel": "whatsapp",
        },
    ).json()
    r = client.post(
        "/merchants/M1010/leads", json={"offer_id": offer["id"], "consent_id": other["id"]}
    )
    assert r.json()["error"]["code"] == "CONSENT_REQUIRED"


def test_declined_offer_is_suppressed(client):
    offer = client.get("/merchants/M1010/loan-eligibility").json()["offer"]
    no = client.post(
        "/merchants/M1010/consents",
        json={
            "purpose": "LOAN_OFFER",
            "notice_version": "KFS-2025-v1",
            "granted": False,
            "channel": "whatsapp",
            "offer_id": offer["id"],
            "button_payload": "NAHI",
        },
    ).json()
    r = client.post(
        "/merchants/M1010/leads", json={"offer_id": offer["id"], "consent_id": no["id"]}
    )
    assert r.json()["error"]["code"] == "CONSENT_REQUIRED"
    el = client.get("/merchants/M1010/loan-eligibility").json()
    assert el["eligible"] is False and "DECLINED_IN_LAST_30_DAYS" in el["reasons"]


def test_ineligible_until_kyc_fixed(client):
    el = client.get("/merchants/M1001/loan-eligibility").json()
    assert el["eligible"] is False and "KYC_NOT_VERIFIED" in el["reasons"]
    link = client.post("/merchants/M1001/kyc/link").json()
    client.post(f"/sim/kyc/{link['token']}/complete")
    assert client.get("/merchants/M1001/loan-eligibility").json()["eligible"] is True


# ---------- messages ----------


def test_send_message(client):
    client.post("/merchants/M1001/messages", json={"text": "Namaste Ramesh ji"})
    [msg] = client.get("/merchants/M1001/messages").json()
    assert msg["text"] == "Namaste Ramesh ji" and msg["channel"] == "whatsapp"
