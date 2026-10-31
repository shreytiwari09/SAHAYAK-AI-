"""The shared access key: closed to the internet, open to this machine, with a few public doors."""

import httpx
import pytest

from backend.api import ApiContext, create_app
from tests.test_whatsapp_channel import rig  # noqa: F401  (the shared fixture)

KEY = "demo-key-123"
REMOTE = {"host": "abc-def.trycloudflare.com"}  # how a request through a tunnel looks


def make_client(rig, key=KEY, app_secret="s3cret"):
    ctx = ApiContext(
        rig.service, rig.deps.gate, rig.audit, rig.tools, whatsapp=rig.channel,
        meta_verify_token="verify-me", meta_app_secret=app_secret, access_key=key,
    )  # fmt: skip
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(lambda: ctx)), base_url="http://api"
    )


@pytest.fixture
async def client(rig):
    c = make_client(rig)
    yield c
    await c.aclose()


async def test_with_no_key_configured_nothing_changes(rig):
    c = make_client(rig, key="")
    assert (await c.get("/cases", headers=REMOTE)).status_code == 200
    await c.aclose()


# ---------- from outside ----------


async def test_remote_requests_need_the_key(client):
    r = await client.get("/cases", headers=REMOTE)
    assert r.status_code == 401 and r.json() == {"detail": "access key required"}
    html = await client.get("/cases", headers={**REMOTE, "accept": "text/html"})
    assert html.status_code == 401 and "Access key needed" in html.text


@pytest.mark.parametrize(
    "headers",
    [{"x-access-key": KEY}, {"authorization": f"Bearer {KEY}"}, {"cookie": f"sahayak_key={KEY}"}],
    ids=["header", "bearer", "cookie"],
)
async def test_the_key_works_in_every_form(client, headers):
    assert (await client.get("/cases", headers={**REMOTE, **headers})).status_code == 200


async def test_a_wrong_or_empty_key_is_refused(client):
    for bad in ("nope", "", KEY[:-1], KEY + "x"):
        r = await client.get("/cases", headers={**REMOTE, "x-access-key": bad})
        assert r.status_code == 401, bad


async def test_opening_a_page_with_the_key_sets_a_cookie_and_removes_it_from_the_address(client):
    r = await client.get(
        f"/cases?key={KEY}&status=done", headers={**REMOTE, "x-forwarded-proto": "https"}
    )
    assert (
        r.status_code == 303
        and "key=" not in r.headers["location"]
        and "status=done" in r.headers["location"]
    )
    cookie = r.headers["set-cookie"]
    assert f"sahayak_key={KEY}" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    client.cookies.clear()  # a new browser: the cookie from above must not be what lets it in
    assert (await client.get("/cases?key=wrong", headers=REMOTE)).status_code == 401


async def test_everything_dangerous_is_closed_from_outside(client):
    for method, path in [
        ("POST", "/faults/kyc_hold"), ("POST", "/approvals/x"), ("POST", "/policy/kill-switch"),
        ("POST", "/channels/whatsapp/simulate"), ("POST", "/channels/whatsapp/link"),
        ("POST", "/monitor/scan"), ("POST", "/chat"), ("GET", "/audit"), ("GET", "/metrics"),
        ("GET", "/channels/whatsapp/outbox"), ("POST", "/mock/admin/reset"), ("GET", "/console/"),
    ]:  # fmt: skip
        r = await client.request(method, path, headers=REMOTE)
        assert r.status_code == 401, (method, path, r.status_code)


# ---------- from this machine ----------


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "localhost", "[::1]:8000"])
async def test_this_machine_needs_no_key(client, host):
    assert (await client.get("/cases", headers={"host": host})).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "localhost:8000", "x-forwarded-for": "1.2.3.4"},
        {"host": "localhost:8000", "cf-connecting-ip": "1.2.3.4"},
        {"host": "localhost:8000", "x-real-ip": "1.2.3.4"},
        {"host": "localhost.evil.com"},
        {"host": "evil.com:8000"},
        {"host": "127.0.0.1.evil.com"},
        {"host": ""},
    ],
    ids=[
        "x-forwarded-for",
        "cloudflare",
        "x-real-ip",
        "lookalike",
        "other-host",
        "lookalike-ip",
        "no-host",
    ],
)
async def test_relayed_or_lookalike_requests_do_not_count_as_local(client, headers):
    assert (await client.get("/cases", headers=headers)).status_code == 401


# ---------- the public doors ----------


async def test_the_health_check_is_public(client):
    assert (await client.get("/health", headers=REMOTE)).status_code == 200


async def test_the_webhook_handshake_is_public_but_still_needs_meta_verify_token(client):
    ok = await client.get("/channels/whatsapp/webhook", headers=REMOTE,
                          params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})  # fmt: skip
    assert ok.status_code == 200 and ok.text == "42"
    bad = await client.get("/channels/whatsapp/webhook", headers=REMOTE,
                           params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})  # fmt: skip
    assert bad.status_code == 403  # refused by the webhook itself, not by the access key


async def test_the_webhook_post_is_public_only_because_meta_signs_it(client):
    r = await client.post("/channels/whatsapp/webhook", headers=REMOTE, content=b"{}")
    assert (
        r.status_code == 403 and r.json()["detail"] == "bad signature"
    )  # reached the signature check


async def test_without_an_app_secret_the_webhook_post_is_not_left_open(rig):
    c = make_client(rig, app_secret="")
    r = await c.post("/channels/whatsapp/webhook", headers=REMOTE, content=b"{}")
    assert r.status_code == 401 and r.json()["detail"] == "access key required"
    assert (await c.get("/channels/whatsapp/webhook", headers=REMOTE,
                        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "7"})).status_code == 200  # fmt: skip
    await c.aclose()


async def test_the_link_pages_a_merchant_taps_are_public_but_only_to_read(client):
    for path in ("/mock/sim/kyc/KYCABC/complete", "/mock/sim/bank-update/BNKABC/complete"):
        assert (
            await client.get(path, headers=REMOTE)
        ).status_code != 401  # reaches the page (404 here: no mock mounted)
        assert (await client.post(path, headers=REMOTE)).status_code == 401
    assert (await client.get("/mock/merchants", headers=REMOTE)).status_code == 401
