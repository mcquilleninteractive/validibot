"""Shared-cache abuse protection for public OAuth token endpoints."""

from __future__ import annotations

import hashlib
import hmac
import time
from http import HTTPStatus
from typing import TYPE_CHECKING

from django.conf import settings
from django.http import JsonResponse
from django.urls import NoReverseMatch
from django.urls import reverse

from validibot.core.client_ip import resolve_client_ip
from validibot.core.rate_limit_counters import increment_rate_limit_counter

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest
    from django.http import HttpResponse

_WINDOW_SECONDS = 60
_CACHE_TIMEOUT_SECONDS = 70
_ENDPOINT_SETTINGS = (
    ("idp:oidc:token", "IDP_OIDC_TOKEN_REQUESTS_PER_IP_PER_MINUTE"),
    ("idp:oidc:revoke", "IDP_OIDC_REVOKE_REQUESTS_PER_IP_PER_MINUTE"),
)


class OIDCEndpointAbuseProtectionMiddleware:
    """Bound unauthenticated token and revocation requests before parsing."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.endpoint_limits: dict[str, str] = {}
        for view_name, setting_name in _ENDPOINT_SETTINGS:
            try:
                endpoint_path = reverse(view_name)
            except NoReverseMatch:
                # Worker and other deliberately reduced URL configurations do
                # not expose OAuth. Middleware construction must remain valid
                # there; an absent route cannot receive work or consume a
                # budget.
                continue
            self.endpoint_limits[endpoint_path] = setting_name

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Return OAuth-compatible 429 responses after either budget is spent."""

        setting_name = self.endpoint_limits.get(request.path)
        if request.method == "POST" and setting_name:
            endpoint = (
                "token" if setting_name.startswith("IDP_OIDC_TOKEN") else "revoke"
            )
            identity = _client_identity(request)
            per_ip = max(0, int(getattr(settings, setting_name, 0)))
            global_limit = max(
                0,
                int(
                    getattr(
                        settings,
                        "IDP_OIDC_ENDPOINT_GLOBAL_REQUESTS_PER_MINUTE",
                        0,
                    ),
                ),
            )
            if _limit_exceeded(endpoint, "ip", identity, per_ip) or _limit_exceeded(
                endpoint,
                "global",
                "all",
                global_limit,
            ):
                return JsonResponse(
                    {
                        "error": "temporarily_unavailable",
                        "error_description": "Too many requests. Retry shortly.",
                    },
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                )
        return self.get_response(request)


def _client_identity(request: HttpRequest) -> str:
    """Hash the strict trusted-proxy address so cache keys contain no raw IP."""

    proxy_depth = max(
        0,
        int(getattr(settings, "REST_FRAMEWORK", {}).get("NUM_PROXIES", 0)),
    )
    address = resolve_client_ip(
        peer_host=str(request.META.get("REMOTE_ADDR", "")),
        forwarded_for=str(request.headers.get("x-forwarded-for", "")),
        proxy_depth=proxy_depth,
    )
    return hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        address.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _limit_exceeded(endpoint: str, dimension: str, identity: str, limit: int) -> bool:
    """Consume one fixed-window budget, with zero reserved for non-production use."""

    if limit == 0:
        return False
    window = int(time.time() // _WINDOW_SECONDS)
    key = f"oidc-endpoint:{endpoint}:{dimension}:{identity}:{window}"
    count = increment_rate_limit_counter(
        key=key,
        timeout=_CACHE_TIMEOUT_SECONDS,
    )
    return count > limit
