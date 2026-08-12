"""Enforce the dependency and deployment boundaries of embedded MCP.

These tests turn the architectural simplification into executable rules: MCP
uses the official SDK, tool registration depends on application services, no
commercial package leaks into Community, and every runtime serves the same ASGI
application rather than reviving a standalone MCP process.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

PROJECT_ROOT = Path(settings.BASE_DIR)
MCP_PACKAGE = PROJECT_ROOT / "validibot" / "mcp_server"


def _python_source(path: Path) -> str:
    """Return all production Python source below ``path`` as one string."""

    return "\n".join(
        source.read_text(encoding="utf-8")
        for source in sorted(path.glob("*.py"))
        if source.name != "__init__.py"
    )


def test_only_official_mcp_sdk_is_declared() -> None:
    """The refactor must never drift back to the third-party FastMCP package."""

    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")

    assert '"mcp==2.0.0"' in pyproject
    assert '"fastmcp' not in pyproject.lower()
    assert 'name = "fastmcp"' not in lockfile.lower()


def test_community_mcp_has_no_commercial_imports() -> None:
    """A public Pro-gated feature must activate through the feature registry."""

    source = _python_source(MCP_PACKAGE)

    assert "validibot_pro" not in source
    assert "validibot_enterprise" not in source
    assert "validibot_cloud" not in source


def test_tool_registration_uses_services_not_transport_or_models() -> None:
    """Tool handlers must not recreate the old HTTP proxy or query models."""

    source = (MCP_PACKAGE / "server.py").read_text(encoding="utf-8")

    assert "validibot.mcp_server.services" in source
    assert "rest_framework" not in source
    assert "validibot.mcp_api" not in source
    assert "httpx" not in source
    assert "Workflow.objects" not in source
    assert "ValidationRun.objects" not in source
    assert "User.objects" not in source


def test_production_command_serves_the_composed_asgi_application() -> None:
    """GCP and self-hosted deployments must exercise MCP's ASGI lifespan."""

    start_script = (
        PROJECT_ROOT / "compose" / "production" / "django" / "start.sh"
    ).read_text(encoding="utf-8")

    assert "config.asgi:application" in start_script
    assert "uvicorn_worker.UvicornWorker" in start_script
    assert "config.wsgi:application" not in start_script


def test_compose_has_no_standalone_mcp_service() -> None:
    """Both local and self-hosted stacks must use the Django web container."""

    compose_sources = (
        (PROJECT_ROOT / "docker-compose.local.yml").read_text(encoding="utf-8"),
        (PROJECT_ROOT / "docker-compose.local-pro.yml").read_text(
            encoding="utf-8",
        ),
        (PROJECT_ROOT / "docker-compose.production.yml").read_text(
            encoding="utf-8",
        ),
    )

    for source in compose_sources:
        assert "\n  mcp:" not in source


def test_retired_proxy_license_endpoint_is_removed() -> None:
    """In-process feature gating must not leave an anonymous proxy probe."""

    api_router = (PROJECT_ROOT / "config" / "api_router.py").read_text(
        encoding="utf-8",
    )

    assert "LicenseFeaturesView" not in api_router
    assert '"license/features/"' not in api_router
    license_view = PROJECT_ROOT / "validibot" / "core" / "api" / "license_views.py"
    assert not license_view.exists()


def test_production_disables_persistent_connections_for_asgi() -> None:
    """ASGI deployment must follow Django's connection-lifetime guidance."""

    production_settings = (
        PROJECT_ROOT / "config" / "settings" / "production.py"
    ).read_text(encoding="utf-8")

    assert 'DATABASES["default"]["CONN_MAX_AGE"] = 0' in production_settings
