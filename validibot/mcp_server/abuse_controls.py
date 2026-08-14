"""Transport-level abuse controls for the embedded MCP ASGI surface.

Authenticated per-principal quotas remain in ``rate_limits.py``. This module
protects the earlier unauthenticated boundary with shared-cache IP, failed-auth,
and global request ceilings before expensive tool or validation work begins.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import Protocol
from typing import cast

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache

from validibot.core.client_ip import resolve_client_ip as resolve_trusted_client_ip
from validibot.core.rate_limit_counters import increment_rate_limit_counter

if TYPE_CHECKING:
    from asgiref.typing import ASGI3Application
    from asgiref.typing import ASGIReceiveCallable
    from asgiref.typing import ASGISendCallable
    from asgiref.typing import ASGISendEvent
    from asgiref.typing import Scope
    from starlette.routing import Router

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60
_CACHE_TIMEOUT_SECONDS = 70
_RATE_LIMIT_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "error": {
            "code": -32002,
            "message": "Too many MCP requests. Retry shortly.",
        },
        "id": None,
    },
    separators=(",", ":"),
).encode("utf-8")


class _RoutableASGIApplication(Protocol):
    """ASGI application shape exposed by the official SDK's Starlette app."""

    router: Router


@dataclass(frozen=True, slots=True)
class TransportLimit:
    """One shared-cache request budget at the unauthenticated boundary."""

    dimension: str
    limit: int
    identity: str


class MCPAbuseProtectionMiddleware:
    """Apply transport limits and learn failed bearer attempts from responses."""

    def __init__(self, application: ASGI3Application) -> None:
        self.application = application

    @property
    def router(self) -> Router:
        """Expose the wrapped SDK router for explicit lifespan management."""

        return cast("_RoutableASGIApplication", self.application).router

    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
    ) -> None:
        """Protect HTTP requests while leaving the SDK lifespan unchanged."""

        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        client_identity = _client_identity(scope)
        if await _failed_auth_is_blocked(client_identity):
            await _send_rate_limited(send, dimension="failed_auth")
            return

        limits = (
            TransportLimit(
                dimension="ip",
                limit=_configured_limit("MCP_REQUESTS_PER_IP_PER_MINUTE", 240),
                identity=client_identity,
            ),
            TransportLimit(
                dimension="global",
                limit=_configured_limit("MCP_GLOBAL_REQUESTS_PER_MINUTE", 3_000),
                identity="all",
            ),
        )
        for transport_limit in limits:
            if await _limit_exceeded(transport_limit):
                await _send_rate_limited(
                    send,
                    dimension=transport_limit.dimension,
                )
                return

        response_status: int | None = None

        async def observe_response(message: ASGISendEvent) -> None:
            """Capture only the status needed for the failed-auth counter."""

            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = int(message["status"])
            await send(message)

        await self.application(scope, receive, observe_response)
        if response_status == HTTPStatus.UNAUTHORIZED:
            await _record_failed_auth(client_identity)


def _configured_limit(name: str, default: int) -> int:
    """Return a non-negative configured limit; zero deliberately disables it."""

    return max(0, int(getattr(settings, name, default)))


def _client_identity(scope: Scope) -> str:
    """Return a keyed digest of the client IP selected through trusted proxies."""

    client_ip = resolve_client_ip(scope)
    secret = str(settings.SECRET_KEY).encode("utf-8")
    return hmac.new(secret, client_ip.encode("utf-8"), hashlib.sha256).hexdigest()


def resolve_client_ip(scope: Scope) -> str:
    """Resolve the caller using the same explicit proxy depth as DRF.

    A zero proxy depth ignores ``X-Forwarded-For``. A positive depth trusts the
    Nth address from the right only when the complete expected chain and a
    syntactically valid address are present; otherwise it falls back to the
    ASGI peer address rather than trusting attacker-controlled input.
    """

    proxy_depth = max(
        0,
        int(getattr(settings, "REST_FRAMEWORK", {}).get("NUM_PROXIES", 0)),
    )
    return resolve_trusted_client_ip(
        peer_host=_peer_host(scope),
        forwarded_for=_forwarded_header(scope),
        proxy_depth=proxy_depth,
    )


def _peer_host(scope: Scope) -> str:
    """Return the ASGI peer host without assuming a client tuple is present."""

    client = scope.get("client")
    if isinstance(client, tuple) and client:
        return str(client[0])
    return ""


def _forwarded_header(scope: Scope) -> str:
    """Return the final forwarded-for header for strict shared parsing."""

    raw_value = ""
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return ""
    for name, value in headers:
        if name.lower() == b"x-forwarded-for":
            raw_value = value.decode("latin-1")
    return raw_value


async def _failed_auth_is_blocked(identity: str) -> bool:
    """Check the current failed-auth bucket without consuming request budget."""

    limit = _configured_limit("MCP_FAILED_AUTH_PER_IP_PER_MINUTE", 20)
    if limit == 0:
        return False
    count = await sync_to_async(cache.get, thread_sensitive=True)(
        _cache_key("failed_auth", identity),
        0,
    )
    return int(count or 0) >= limit


async def _record_failed_auth(identity: str) -> None:
    """Record one 401 response without retaining the caller's raw address."""

    limit = _configured_limit("MCP_FAILED_AUTH_PER_IP_PER_MINUTE", 20)
    if limit == 0:
        return
    await sync_to_async(_increment_counter, thread_sensitive=True)(
        _cache_key("failed_auth", identity),
    )


async def _limit_exceeded(transport_limit: TransportLimit) -> bool:
    """Atomically consume one fixed-window budget from the shared cache."""

    if transport_limit.limit == 0:
        return False
    count = await sync_to_async(_increment_counter, thread_sensitive=True)(
        _cache_key(transport_limit.dimension, transport_limit.identity),
    )
    return count > transport_limit.limit


def _increment_counter(key: str) -> int:
    """Increment one counter atomically on every supported production cache."""

    return increment_rate_limit_counter(
        key=key,
        timeout=_CACHE_TIMEOUT_SECONDS,
    )


def _cache_key(dimension: str, identity: str) -> str:
    """Build one namespaced minute bucket without embedding a raw client IP."""

    window = int(time.time() // _WINDOW_SECONDS)
    return f"mcp-transport:{dimension}:{identity}:{window}"


async def _send_rate_limited(
    send: ASGISendCallable,
    *,
    dimension: str,
) -> None:
    """Return a bounded transport response and emit a payload-free warning."""

    logger.warning(
        "MCP transport request rate limited",
        extra={"mcp_rate_limit_dimension": dimension},
    )
    await send(
        {
            "type": "http.response.start",
            "status": HTTPStatus.TOO_MANY_REQUESTS,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_RATE_LIMIT_BODY)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"retry-after", b"60"),
            ],
            "trailers": False,
        },
    )
    await send(
        {
            "type": "http.response.body",
            "body": _RATE_LIMIT_BODY,
            "more_body": False,
        },
    )


__all__ = ["MCPAbuseProtectionMiddleware", "resolve_client_ip"]
