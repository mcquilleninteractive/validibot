"""Tests for abuse controls on public OAuth token lifecycle endpoints.

The MCP SDK transport limiter cannot see django-allauth's token or revocation
routes. These tests prove those CSRF-exempt endpoints have independent,
shared-cache IP and global request ceilings before OAuth body processing.
"""

from __future__ import annotations

import json
from http import HTTPStatus

import pytest
from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from validibot.idp.middleware import OIDCEndpointAbuseProtectionMiddleware


@pytest.fixture(autouse=True)
def _clear_rate_limit_cache() -> None:
    """Keep fixed-window counters independent between security scenarios."""

    cache.clear()


@pytest.mark.parametrize("endpoint_name", ["token", "revoke"])
def test_post_endpoints_enforce_per_ip_limits(settings, endpoint_name: str) -> None:
    """Repeated unauthenticated OAuth work from one address must receive 429."""

    settings.IDP_OIDC_TOKEN_REQUESTS_PER_IP_PER_MINUTE = 1
    settings.IDP_OIDC_REVOKE_REQUESTS_PER_IP_PER_MINUTE = 1
    settings.IDP_OIDC_ENDPOINT_GLOBAL_REQUESTS_PER_MINUTE = 100
    request_factory = RequestFactory()
    middleware = OIDCEndpointAbuseProtectionMiddleware(
        lambda request: HttpResponse(status=HTTPStatus.NO_CONTENT),
    )
    path = reverse(f"idp:oidc:{endpoint_name}")

    first = middleware(request_factory.post(path, REMOTE_ADDR="203.0.113.10"))
    blocked = middleware(request_factory.post(path, REMOTE_ADDR="203.0.113.10"))

    assert first.status_code == HTTPStatus.NO_CONTENT
    assert blocked.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert json.loads(blocked.content)["error"] == "temporarily_unavailable"


def test_global_limit_spans_distinct_client_addresses(settings) -> None:
    """Address rotation must not bypass the deployment-wide OAuth work budget."""

    settings.IDP_OIDC_TOKEN_REQUESTS_PER_IP_PER_MINUTE = 100
    settings.IDP_OIDC_ENDPOINT_GLOBAL_REQUESTS_PER_MINUTE = 1
    request_factory = RequestFactory()
    middleware = OIDCEndpointAbuseProtectionMiddleware(
        lambda request: HttpResponse(status=HTTPStatus.NO_CONTENT),
    )
    path = reverse("idp:oidc:token")

    first = middleware(request_factory.post(path, REMOTE_ADDR="203.0.113.10"))
    blocked = middleware(request_factory.post(path, REMOTE_ADDR="203.0.113.11"))

    assert first.status_code == HTTPStatus.NO_CONTENT
    assert blocked.status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_non_post_and_unrelated_routes_are_not_consumed(settings) -> None:
    """The guard must not interfere with discovery or normal application traffic."""

    settings.IDP_OIDC_TOKEN_REQUESTS_PER_IP_PER_MINUTE = 1
    settings.IDP_OIDC_ENDPOINT_GLOBAL_REQUESTS_PER_MINUTE = 1
    request_factory = RequestFactory()
    middleware = OIDCEndpointAbuseProtectionMiddleware(
        lambda request: HttpResponse(status=HTTPStatus.NO_CONTENT),
    )

    token_get = middleware(
        request_factory.get(
            reverse("idp:oidc:token"),
            REMOTE_ADDR="203.0.113.10",
        ),
    )
    unrelated = middleware(
        request_factory.post("/health/", REMOTE_ADDR="203.0.113.10"),
    )

    assert token_get.status_code == HTTPStatus.NO_CONTENT
    assert unrelated.status_code == HTTPStatus.NO_CONTENT
