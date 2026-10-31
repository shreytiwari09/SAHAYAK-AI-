"""A shared access key for when the server is reachable from the internet (a tunnel).

With no key configured nothing changes. With one configured, a request is allowed when it is:
  - clearly local (a localhost Host header and no forwarding headers), or
  - carrying the key (cookie, `x-access-key`, or `Authorization: Bearer`), or
  - one of the few things that must stay public: Meta's webhook (itself checked by Meta's
    signature, and only left open when the app secret is set), the KYC and bank-details link pages
    a merchant taps on a phone, and the health check.
Opening any page with `?key=...` sets a cookie and redirects, so the console works in a browser.
"""

import hmac

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "sahayak_key"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
FORWARDING_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "cf-connecting-ip",
    "forwarded",
    "x-real-ip",
)
PUBLIC_LINK_PREFIXES = (
    "/mock/sim/kyc/",
    "/mock/sim/bank-update/",
)  # GET: a merchant tapping a link
WEBHOOK = "/channels/whatsapp/webhook"


def _hostname(host: str) -> str:
    if host.startswith("["):  # an IPv6 literal, e.g. [::1]:8000
        return host.split("]")[0] + "]"
    return host.split(":")[0]


def is_local(request: Request) -> bool:
    """Addressed to this machine by name, and not relayed by a proxy or tunnel."""
    forwarded = any(h in request.headers for h in FORWARDING_HEADERS)
    return _hostname(request.headers.get("host", "")) in LOCAL_HOSTS and not forwarded


def presented_key(request: Request) -> str:
    bearer = request.headers.get("authorization", "")
    return (
        request.headers.get("x-access-key")
        or (bearer.removeprefix("Bearer ").strip() if bearer.startswith("Bearer ") else "")
        or request.cookies.get(COOKIE, "")
    )


def is_public_path(request: Request, webhook_signed: bool) -> bool:
    path = request.url.path
    if path == "/health":
        return True
    if path == WEBHOOK:
        return webhook_signed or request.method == "GET"  # GET is only the verify-token handshake
    return request.method == "GET" and path.startswith(PUBLIC_LINK_PREFIXES)


async def enforce(request: Request, call_next, key: str, webhook_signed: bool):
    if not key or is_local(request) or is_public_path(request, webhook_signed):
        return await call_next(request)

    query_key = request.query_params.get("key", "")
    if query_key and hmac.compare_digest(query_key, key):
        # Take the key out of the address and remember it for this browser.
        clean = request.url.remove_query_params("key")
        response = RedirectResponse(str(clean), status_code=303)
        response.set_cookie(
            COOKIE,
            key,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=86400,
        )
        return response

    given = presented_key(request)
    if given and hmac.compare_digest(given, key):
        return await call_next(request)

    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<body style='font-family:system-ui;max-width:28rem;margin:4rem auto;padding:0 1rem'>"
            "<h2>Access key needed</h2>"
            "<p>Open the link you were given, which ends in <code>?key=…</code>.</p>",
            status_code=401,
        )
    return JSONResponse({"detail": "access key required"}, status_code=401)
