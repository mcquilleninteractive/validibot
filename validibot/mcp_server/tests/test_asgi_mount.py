"""ASGI integration tests for licensing and ordinary Django traffic.

The MCP implementation is public community code but its route is a Pro
capability. These tests prove the application is built after license loading,
that Community does not advertise the route, and that mounting MCP does not
steal normal Django HTTP paths.
"""

from __future__ import annotations

import importlib

import anyio
import httpx
import pytest
from django.test import override_settings

from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import get_license
from validibot.core.license import set_license

pytestmark = pytest.mark.django_db


def test_community_hides_mcp_while_health_remains_available(settings) -> None:
    """Community should receive Django's 404 and keep normal ASGI HTTP healthy."""

    settings.SITE_URL = "http://localhost:8000"
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = "http://localhost:8000/mcp"
    settings.ALLOWED_HOSTS = ["localhost"]
    original_license = get_license()
    try:
        set_license(License(edition=Edition.COMMUNITY))
        import config.asgi as asgi_module

        asgi_module = importlib.reload(asgi_module)
        statuses = anyio.run(_request_mount_and_health, asgi_module.application)
    finally:
        set_license(original_license)
        importlib.reload(asgi_module)

    assert statuses == (404, 200)


def test_pro_mounts_authenticated_mcp_without_stealing_health(settings) -> None:
    """The Pro feature key should activate MCP and preserve Django routing."""

    settings.SITE_URL = "http://localhost:8000"
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = "http://localhost:8000/mcp"
    settings.ALLOWED_HOSTS = ["localhost"]
    original_license = get_license()
    try:
        set_license(
            License(
                edition=Edition.PRO,
                features=frozenset({CommercialFeature.MCP_SERVER.value}),
            ),
        )
        import config.asgi as asgi_module

        asgi_module = importlib.reload(asgi_module)
        statuses = anyio.run(_request_mount_and_health, asgi_module.application)
    finally:
        set_license(original_license)
        importlib.reload(asgi_module)

    assert statuses == (401, 200)


def test_worker_does_not_initialize_mcp_for_an_activated_license() -> None:
    """Worker startup must not depend on configuration for the web-only MCP app."""

    import config.asgi as asgi_module

    original_license = get_license()
    try:
        with override_settings(
            APP_ROLE="worker",
            APP_IS_WORKER=True,
            MCP_STRICT_CONFIGURATION=True,
            MCP_FILE_ALLOWED_HOSTS=[],
        ):
            set_license(
                License(
                    edition=Edition.PRO,
                    features=frozenset({CommercialFeature.MCP_SERVER.value}),
                ),
            )
            asgi_module = importlib.reload(asgi_module)

            assert asgi_module.mcp_application is None
    finally:
        set_license(original_license)
        importlib.reload(asgi_module)


async def _request_mount_and_health(application) -> tuple[int, int]:
    """Request the two routing branches through one ASGI application."""

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:8000",
    ) as client:
        mcp_response = await client.get("/mcp")
        health_response = await client.get("/health/")
    return mcp_response.status_code, health_response.status_code
