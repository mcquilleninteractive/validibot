"""Tests for MCP transport abuse controls and trusted-proxy handling.

The MCP endpoint must protect work performed before a principal is available.
These tests prove per-IP, failed-authentication, and global shared-cache budgets
cannot be bypassed by rotating an untrusted forwarded header, while preserving
the stable authenticated per-principal limits covered separately.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import Any

import anyio
import pytest
from django.core.cache import cache

from validibot.mcp_server.abuse_controls import MCPAbuseProtectionMiddleware
from validibot.mcp_server.abuse_controls import resolve_client_ip

if TYPE_CHECKING:
    from asgiref.typing import ASGI3Application


@pytest.fixture(autouse=True)
def _empty_transport_limit_cache():
    """Give every transport-limit test independent fixed-window counters."""

    cache.clear()
    yield
    cache.clear()


def _scope(
    *,
    peer: str = "10.0.0.9",
    forwarded_for: str = "198.51.100.10",
) -> dict[str, Any]:
    """Build the bounded HTTP scope needed by the middleware contract."""

    headers = [(b"host", b"app.validibot.test")]
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("app.validibot.test", 443),
        "client": (peer, 44000),
        "state": {},
    }


def _status_application(status: int) -> ASGI3Application:
    """Return a minimal downstream app with one deterministic status."""

    async def application(scope, receive, send) -> None:
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [],
            },
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            },
        )

    return application


def _request_status(application: ASGI3Application, scope: dict[str, Any]) -> int:
    """Invoke one ASGI request and return its observed response status."""

    async def invoke() -> int:
        messages = [{"type": "http.request", "body": b"", "more_body": False}]
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return messages.pop(0)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await application(scope, receive, send)
        starts = [item for item in sent if item["type"] == "http.response.start"]
        return int(starts[0]["status"])

    return anyio.run(invoke)


def _set_limits(
    settings,
    *,
    per_ip: int = 100,
    failed_auth: int = 100,
    global_requests: int = 100,
) -> None:
    """Set focused transport budgets without changing unrelated settings."""

    settings.MCP_REQUESTS_PER_IP_PER_MINUTE = per_ip
    settings.MCP_FAILED_AUTH_PER_IP_PER_MINUTE = failed_auth
    settings.MCP_GLOBAL_REQUESTS_PER_MINUTE = global_requests


def test_trusted_proxy_depth_selects_from_right_and_can_ignore_xff(settings) -> None:
    """Only explicitly trusted proxy hops may influence the caller identity."""

    scope = _scope(
        peer="10.0.0.9",
        forwarded_for="198.51.100.10, 203.0.113.20",
    )

    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    assert resolve_client_ip(scope) == "203.0.113.20"

    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 2}
    assert resolve_client_ip(scope) == "198.51.100.10"

    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 0}
    assert resolve_client_ip(scope) == "10.0.0.9"


def test_malformed_or_short_forwarded_chain_falls_back_to_peer(settings) -> None:
    """Incomplete proxy evidence must not create attacker-selected rate buckets."""

    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 2}

    assert resolve_client_ip(_scope(forwarded_for="198.51.100.10")) == "10.0.0.9"
    assert (
        resolve_client_ip(_scope(forwarded_for="not-an-ip, 203.0.113.20")) == "10.0.0.9"
    )


def test_per_ip_limit_cannot_be_bypassed_with_the_untrusted_leftmost_hop(
    settings,
) -> None:
    """Rotating a spoofed XFF prefix must retain the trusted caller bucket."""

    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    _set_limits(settings, per_ip=1)
    application = MCPAbuseProtectionMiddleware(_status_application(200))

    first = _scope(forwarded_for="192.0.2.1, 198.51.100.10")
    rotated_prefix = _scope(forwarded_for="192.0.2.2, 198.51.100.10")
    other_client = _scope(forwarded_for="192.0.2.2, 198.51.100.11")

    assert _request_status(application, first) == HTTPStatus.OK
    assert _request_status(application, rotated_prefix) == HTTPStatus.TOO_MANY_REQUESTS
    assert _request_status(application, other_client) == HTTPStatus.OK


def test_failed_auth_limit_blocks_repeated_invalid_bearer_attempts(settings) -> None:
    """Repeated 401 responses must eventually stop before token verification."""

    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    _set_limits(settings, failed_auth=1)
    application = MCPAbuseProtectionMiddleware(_status_application(401))
    scope = _scope()

    assert _request_status(application, scope) == HTTPStatus.UNAUTHORIZED
    assert _request_status(application, scope) == HTTPStatus.TOO_MANY_REQUESTS


def test_global_limit_bounds_aggregate_requests_from_distinct_callers(settings) -> None:
    """Distributing calls across IP addresses must not bypass platform capacity."""

    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    _set_limits(settings, global_requests=1)
    application = MCPAbuseProtectionMiddleware(_status_application(200))

    assert (
        _request_status(
            application,
            _scope(forwarded_for="198.51.100.10"),
        )
        == HTTPStatus.OK
    )
    assert (
        _request_status(
            application,
            _scope(forwarded_for="198.51.100.11"),
        )
        == HTTPStatus.TOO_MANY_REQUESTS
    )
