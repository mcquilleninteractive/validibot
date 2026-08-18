"""Shared-cache abuse protection and issuer binding for public OAuth routes."""

from __future__ import annotations

import hashlib
import hmac
import time
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

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
    (
        "idp:oidc:client_registration",
        "registration",
        "IDP_OIDC_REGISTRATION_REQUESTS_PER_IP_PER_MINUTE",
    ),
    ("idp:oidc:token", "token", "IDP_OIDC_TOKEN_REQUESTS_PER_IP_PER_MINUTE"),
    ("idp:oidc:revoke", "revoke", "IDP_OIDC_REVOKE_REQUESTS_PER_IP_PER_MINUTE"),
)


class OIDCAuthorizationResponseIssuerMiddleware:
    """Add RFC 9207 issuer identification to final OAuth redirects.

    django-allauth validates the client and exact redirect URI before emitting
    a callback, but does not yet include ``iss`` in that response.  MCP clients
    use the parameter to prevent authorization-server mix-up attacks.  Login
    and other intermediate redirects are untouched because they contain
    neither an authorization code nor an OAuth error.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.authorization_path: str | None
        try:
            self.authorization_path = reverse("idp:oidc:authorization")
        except NoReverseMatch:
            self.authorization_path = None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Bind successful and error callbacks to the configured issuer."""

        response = self.get_response(request)
        is_redirect = (
            HTTPStatus.MULTIPLE_CHOICES <= response.status_code < HTTPStatus.BAD_REQUEST
        )
        if request.path != self.authorization_path or not is_redirect:
            return response

        location = response.headers.get("Location")
        if not location:
            return response
        parsed = urlsplit(location)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if not any(key in {"code", "error"} for key, _value in query):
            return response

        query = [(key, value) for key, value in query if key != "iss"]
        query.append(("iss", str(settings.SITE_URL).rstrip("/")))
        response["Location"] = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query),
                parsed.fragment,
            ),
        )
        return response


class OIDCEndpointAbuseProtectionMiddleware:
    """Bound registration, token, and revocation requests before parsing."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.endpoint_limits: dict[str, tuple[str, str]] = {}
        for view_name, endpoint, setting_name in _ENDPOINT_SETTINGS:
            try:
                endpoint_path = reverse(view_name)
            except NoReverseMatch:
                # Worker and other deliberately reduced URL configurations do
                # not expose OAuth. Middleware construction must remain valid
                # there; an absent route cannot receive work or consume a
                # budget.
                continue
            self.endpoint_limits[endpoint_path] = (endpoint, setting_name)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Return OAuth-compatible 429 responses after either budget is spent."""

        endpoint_policy = self.endpoint_limits.get(request.path)
        if request.method == "POST" and endpoint_policy:
            endpoint, setting_name = endpoint_policy
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
                "all",
                "global",
                "all",
                global_limit,
            ):
                response = JsonResponse(
                    {
                        "error": "temporarily_unavailable",
                        "error_description": "Too many requests. Retry shortly.",
                    },
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                )
                response["Retry-After"] = str(_WINDOW_SECONDS)
                return response
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
