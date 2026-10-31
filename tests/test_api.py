"""The HTTP API, the case service and the proactive monitor, against the mock Paytm in-process."""

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.agent.runtime import build_agent, open_checkpointer
from backend.agents import AgentRoster
from backend.api import ApiContext, create_app
from backend.audit.log import AuditLog
from backend.pii.masker import PiiMasker
from backend.service import CaseService
from backend.tools.client import PaytmTools
from eval.fake_llm import FakeLLM
from mock_paytm import clock
from mock_paytm.db import make_engine


def make_service(tools, audit, saver, llm=None, **kw):
    ticks = {"t": 0.0}

    async def sleep(s):
        clock.advance(s)
        ticks["t"] += s

    graph, deps = build_agent(
        tools, llm or FakeLLM(), audit, saver, masker=PiiMasker(use_presidio=False),
        sleep=sleep, monotonic=lambda: ticks["t"], **kw,
    )  # fmt: skip
    return CaseService(graph, audit, tools, AgentRoster.load("agents.yaml")), deps


@pytest.fixture
async def api(engine, tmp_path):
    from mock_paytm.app import app as mock_app

    mock = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock")
    tools = PaytmTools(mock)
    audit = AuditLog(make_engine(f"sqlite:///{tmp_path / 'audit.db'}"))
    service, deps = make_service(tools, audit, InMemorySaver())
    ctx = ApiContext(service, deps.gate, audit, tools)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(lambda: ctx)), base_url="http://api"
    )
    client.service, client.ctx = service, ctx
    yield client
    await service.aclose()
    await client.aclose()
    await mock.aclose()


async def chat(api, merchant, text="Mera settlement nahi aaya"):
    r = await api.post("/chat", json={"merchant_id": merchant, "text": text})
    assert r.status_code == 202, r.text
    tid = r.json()["thread_id"]
    await api.service.wait(tid)
    return tid


async def case(api, tid):
    return (await api.get(f"/cases/{tid}")).json()


# ---------- lifecycle ----------


async def test_chat_runs_the_hero_flow_and_pauses_on_the_offer(api):
    tid = await chat(api, "M1001")
    c = await case(api, tid)
    # The fix needs the merchant to tap the KYC link first.
    assert c["status"] == "paused" and c["pause"]["waiting_for"] == "kyc_link"
    r = await api.post(f"/cases/{tid}/simulate", json={"done": True})
    assert r.status_code == 202
    await api.service.wait(tid)
    c = await case(api, tid)
    assert (
        c["status"] == "paused" and c["pause"]["type"] == "offer_reply"
    )  # verified, now the offer
    assert c["verified"] is True and c["proof"] and "UTR" in c["proof"]
    await api.post(f"/cases/{tid}/simulate", json={"reply": "Haan"})
    await api.service.wait(tid)
    c = await case(api, tid)
    assert c["status"] == "done" and c["outcome"] == "RESOLVED_VERIFIED"
    assert c["diagnosis"]["confidence"]["total"] == 1.0
    assert any(a["action"] == "create_lead" and a["executed"] for a in c["actions"])


async def test_resume_with_a_plain_answer(api):
    tid = await chat(api, "M1001")
    r = await api.post(f"/cases/{tid}/resume", json={"answer": {"done": False}})
    assert r.status_code == 202
    await api.service.wait(tid)
    c = await case(api, tid)
    assert c["status"] == "done" and c["outcome"] == "WAITING_MERCHANT"


async def test_errors_are_clear(api):
    assert (await api.post("/chat", json={"merchant_id": "M9999", "text": "hi"})).status_code == 404
    assert (await api.get("/cases/nope")).status_code == 404
    assert (await api.post("/cases/nope/resume", json={"answer": {}})).status_code == 404
    tid = await chat(api, "M1005")  # risk hold: escalates and finishes
    assert (await case(api, tid))["status"] == "done"
    r = await api.post(f"/cases/{tid}/resume", json={"answer": {"done": True}})
    assert r.status_code == 409


# ---------- approvals and escalations ----------


async def test_approval_queue_approve_then_money_moves(api):
    tid = await chat(api, "M1009", "Customer ka paisa kat gaya")
    queue = (await api.get("/approvals")).json()
    assert [q["thread_id"] for q in queue] == [tid]
    req = queue[0]["request"]
    assert req["action"] == "create_refund_request" and req["amount_inr"] == 48500.0
    assert "confidence" in req and req["rules"]

    r = await api.post(f"/approvals/{tid}", json={"approved": True, "approver": "priya"})
    assert r.status_code == 202
    await api.service.wait(tid)
    c = await case(api, tid)
    assert c["outcome"] == "RESOLVED_VERIFIED" and (await api.get("/approvals")).json() == []
    events = (await api.get("/audit", params={"thread_id": tid})).json()
    approved = next(e for e in events if e["event"] == "APPROVED")
    assert approved["actor"] == "priya"


async def test_rejected_approval_lands_in_escalations_with_the_handoff(api):
    tid = await chat(api, "M1009", "Customer ka paisa kat gaya")
    await api.post(f"/approvals/{tid}", json={"approved": False, "approver": "priya"})
    await api.service.wait(tid)
    esc = (await api.get("/escalations")).json()
    assert [e["thread_id"] for e in esc] == [tid]
    h = esc[0]["handoff"]
    assert (
        h["findings"][0]["cause"] == "DEBITED_NOT_CREDITED" and "approver rejected" in h["reason"]
    )


async def test_approving_a_case_that_is_not_waiting_is_refused(api):
    tid = await chat(api, "M1001")  # paused on the merchant, not on an approval
    r = await api.post(f"/approvals/{tid}", json={"approved": True})
    assert r.status_code == 409


async def test_kill_switch_forces_every_action_to_a_human(api):
    assert (await api.post("/policy/kill-switch", json={"on": True})).json() == {
        "kill_switch": True
    }
    assert (await api.get("/policy")).json()["kill_switch"] is True
    tid = await chat(api, "M1001")
    c = await case(api, tid)
    assert c["pause"]["type"] == "approval" and c["pause"]["action"] == "send_kyc_link"
    assert "kill_switch" in c["pause"]["rules"]
    events = (await api.get("/audit", params={"thread_id": "policy"})).json()
    assert events[0]["event"] == "KILL_SWITCH"


# ---------- trail, audit, metrics ----------


async def test_event_stream_replays_the_trail_and_ends(api):
    tid = await chat(api, "M1005")  # finishes on its own
    r = await api.get(f"/events/{tid}")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: audit" in r.text and "UNDERSTOOD" in r.text and "ESCALATED" in r.text
    assert r.text.rstrip().endswith('"outcome": "ESCALATED"}') or "event: end" in r.text


async def test_audit_filters_and_integrity(api):
    t1 = await chat(api, "M1005")
    t2 = await chat(api, "M1011")
    one = (await api.get("/audit", params={"thread_id": t1})).json()
    assert one and all(e["thread_id"] == t1 for e in one)
    by_merchant = (await api.get("/audit", params={"merchant_id": "M1011"})).json()
    assert {e["thread_id"] for e in by_merchant} == {t2}
    later = (await api.get("/audit", params={"after": one[-1]["seq"]})).json()
    assert all(e["seq"] > one[-1]["seq"] for e in later)
    assert (await api.get("/audit/verify")).json() == {"intact": True, "first_bad_seq": None}


async def test_metrics_reflect_finished_cases(api):
    await chat(api, "M1005")
    await chat(api, "M1011")
    tid = await chat(api, "M1006", "Parso ka settlement nahi aaya")
    await api.post(f"/cases/{tid}/simulate", json={"reply": None})  # decline to answer the offer
    await api.service.wait(tid)
    m = (await api.get("/metrics")).json()["live"]
    assert m["cases_total"] == 3 and m["finished"] == 3
    assert m["by_outcome"] == {"ESCALATED": 2, "RESOLVED_VERIFIED": 1}
    assert m["escalation_rate"] == pytest.approx(0.667, abs=0.001)
    assert m["auto_resolved"] == 1 and m["median_seconds_to_verified"] is not None


# ---------- proactive monitor and faults ----------


async def test_monitor_starts_cases_for_proven_problems_and_not_twice(api):
    r = await api.post("/monitor/scan", json={"merchant_ids": ["M1002", "M1007", "M1010", "M1005"]})
    started = r.json()["started"]
    by_merchant = {s["merchant_id"]: s for s in started}
    assert set(by_merchant) == {
        "M1002",
        "M1007",
    }  # offline device, KYC expiring; healthy and risk-hold skipped
    assert by_merchant["M1002"]["causes"] == ["DEVICE_OFFLINE"]
    for s in started:
        await api.service.wait(s["thread_id"])
    again = (await api.post("/monitor/scan", json={"merchant_ids": ["M1002", "M1007"]})).json()[
        "started"
    ]
    assert again == []  # a case is already open for each

    c = await case(api, by_merchant["M1002"]["thread_id"])
    assert c["proactive"] is True and c["pause"]["waiting_for"] == "power_cycle"


async def test_fault_injection_then_the_monitor_finds_it(api):
    r = await api.post(
        "/faults/heartbeat_loss", json={"merchant_id": "M1015", "device_fault": "SOFT"}
    )
    assert r.status_code == 200 and r.json()["fault"] == "heartbeat_loss"
    started = (await api.post("/monitor/scan", json={"merchant_ids": ["M1015"]})).json()["started"]
    assert [s["causes"] for s in started] == [["DEVICE_OFFLINE"]]
    await api.service.wait(started[0]["thread_id"])
    c = await case(api, started[0]["thread_id"])
    assert c["outcome"] == "RESOLVED_VERIFIED"  # a soft fault is fixed by the remote reset alone
    assert (await api.post("/faults/meteor", json={"merchant_id": "M1015"})).status_code == 422


async def test_lists_merchants(api):
    ids = [m["id"] for m in (await api.get("/merchants")).json()]
    assert ids[0] == "M1001" and len(ids) == 15


# ---------- restart ----------


async def test_after_a_restart_a_pending_approval_is_still_there_and_still_works(engine, tmp_path):
    from mock_paytm.app import app as mock_app

    mock = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock")
    tools = PaytmTools(mock)
    audit_url, ckpt = f"sqlite:///{tmp_path / 'audit.db'}", str(tmp_path / "ckpt.db")

    async with open_checkpointer(ckpt) as saver:
        s1, _ = make_service(tools, AuditLog(make_engine(audit_url)), saver)
        rec = s1.start("M1009", "paisa nahi aaya", "api")
        await s1.wait(rec.thread_id)
        assert s1.get(rec.thread_id).pause["type"] == "approval"
        await s1.aclose()

    async with open_checkpointer(ckpt) as saver:  # a new process
        s2, _ = make_service(tools, AuditLog(make_engine(audit_url)), saver)
        assert await s2.rehydrate() == 1
        again = s2.get(rec.thread_id)
        assert again.status == "paused" and again.pause["action"] == "create_refund_request"
        s2.resume(rec.thread_id, {"approved": True, "approver": "priya"})
        await s2.wait(rec.thread_id)
        assert s2.get(rec.thread_id).summary["outcome"] == "RESOLVED_VERIFIED"
        refunds = [
            a
            for a in s2.get(rec.thread_id).summary["actions"]
            if a["action"] == "create_refund_request"
        ]
        assert len(refunds) == 1
        await s2.aclose()
    await mock.aclose()


# ---------- the reps ----------


async def test_agents_listing_and_filtering_cases_by_rep(api):
    tid = await chat(api, "M1001")
    reps = {a["id"]: a for a in (await api.get("/agents")).json()}
    owner = (await case(api, tid))["agent_id"]
    assert owner and "M1001" in reps[owner]["merchants"]
    assert reps[owner]["waiting"] + reps[owner]["working"] + reps[owner]["finished"] == 1
    mine = (await api.get("/cases", params={"agent_id": owner})).json()
    other = (await api.get("/cases", params={"agent_id": "nobody"})).json()
    assert [c["thread_id"] for c in mine] == [tid] and other == []
