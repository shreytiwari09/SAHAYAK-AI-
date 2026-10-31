"""The real startup path: in-process mock under /mock, public links, and the pause watcher."""

import re
import time

from fastapi.testclient import TestClient


def wait_for(fn, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_server_boots_serves_links_under_mock_and_resumes_when_the_link_is_tapped(
    monkeypatch, tmp_path
):
    for key, value in {
        "INPROCESS_MOCK": "1", "FAKE_LLM": "1", "CHECKPOINT_URL": ":memory:",
        "AUDIT_DATABASE_URL": f"sqlite:///{tmp_path / 'audit.db'}", "DATA_DIR": str(tmp_path),
        "PUBLIC_BASE_URL": "https://demo.example", "PAUSE_POLL_SECONDS": "0.2",
        "GEMINI_API_KEY": "", "SARVAM_API_KEY": "", "META_WA_TOKEN": "", "META_PHONE_ID": "",
        "META_APP_SECRET": "", "ACCESS_KEY": "", "WHATSAPP_LINKS": "",  # the real .env must not leak into the test
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)
    from backend.config import get_settings

    get_settings.cache_clear()
    import backend.main as main

    with TestClient(main.app) as c:
        assert c.get("/health").json()["status"] == "ok"
        assert len(c.get("/mock/merchants").json()) == 50  # the mock is reachable under /mock
        assert (
            c.post(
                "/channels/whatsapp/link", json={"phone": "919800000001", "merchant_id": "M1001"}
            ).status_code
            == 200
        )
        c.post(
            "/channels/whatsapp/simulate",
            json={"sender": "919800000001", "text": "settlement nahi aaya"},
        )

        outbox = wait_for(lambda: c.get("/channels/whatsapp/outbox").json())
        link = re.search(r"https://demo\.example(/mock/sim/kyc/\w+/complete)", outbox[0]["body"])
        assert link, outbox[0]["body"]  # links are built from the public address
        page = c.get(link.group(1))  # the merchant taps it on their phone
        assert page.status_code == 200 and "KYC" in page.text

        # No chat message needed: the watcher sees the KYC change and the case moves on.
        wait_for(
            lambda: any(
                x["status"] == "paused" and x["pause"]["type"] == "offer_reply"
                for x in c.get("/cases").json()
            )
        )
        case = c.get("/cases").json()[0]
        assert case["verified"] is True and case["proof"]
