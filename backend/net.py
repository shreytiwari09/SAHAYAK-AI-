"""Shared HTTP client factory.

This machine's network has no working IPv6 route. Python tries the IPv6 address first and waits
out the full timeout before falling back, so calls look hung. Binding the local address to
0.0.0.0 makes httpx connect over IPv4 only. Every outbound client (Gemini, Sarvam, Meta) should
come from here.
"""

import httpx


def ipv4_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
        timeout=httpx.Timeout(timeout),
    )
