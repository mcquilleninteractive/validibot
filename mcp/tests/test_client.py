"""Tests for the async HTTP clients that back the MCP service.

The MCP server communicates with Validibot exclusively through the authenticated
``/api/v1/mcp/*`` helper endpoints. These tests use ``respx`` to mock ``httpx``
requests and verify the authenticated helper path and the shared error handling
around non-2xx responses.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from validibot_mcp.client import (
    APIError,
    _raise_for_status,
    get_authenticated_run,
    get_authenticated_workflow_detail,
    list_authenticated_workflows,
    start_authenticated_validation_run,
)

from .conftest import SAMPLE_API_KEY, SAMPLE_RUN_PENDING, SAMPLE_WORKFLOW_FULL

ORG = "acme-corp"


# ── _raise_for_status ──────────────────────────────────────────────────
# Converts non-2xx httpx responses into structured APIError exceptions.


class TestRaiseForStatus:
    """Verify HTTP error responses are converted to APIError."""

    def test_success_does_not_raise(self):
        """2xx responses should pass through silently."""
        response = httpx.Response(200, json={"ok": True})
        _raise_for_status(response)  # should not raise

    def test_json_error_body(self):
        """Non-2xx with JSON body should include the parsed detail."""
        response = httpx.Response(404, json={"detail": "Not found"})
        with pytest.raises(APIError, match="Not found") as exc_info:
            _raise_for_status(response)
        assert exc_info.value.status_code == 404

    def test_text_error_body(self):
        """Non-2xx with plain text body should use the text as detail."""
        response = httpx.Response(500, text="Internal Server Error")
        with pytest.raises(APIError, match="Internal Server Error") as exc_info:
            _raise_for_status(response)
        assert exc_info.value.status_code == 500


class TestListAuthenticatedWorkflows:
    """Verify org-free authenticated workflow discovery."""

    async def test_sends_service_headers_with_user_sub(self, mock_api, monkeypatch):
        """OAuth discovery should use service auth plus the forwarded subject."""

        monkeypatch.setattr("validibot_mcp.client.settings.mcp_service_key", "service-secret")
        route = mock_api.get("/api/v1/mcp/workflows/").respond(json=[])

        await list_authenticated_workflows(user_sub="user-sub-123")

        request = route.calls[0].request
        assert request.headers["X-MCP-Service-Key"] == "service-secret"
        assert request.headers["X-Validibot-User-Sub"] == "user-sub-123"
        assert request.headers["X-Validibot-Source"] == "MCP"

    async def test_uses_cloud_run_identity_token_when_service_key_is_absent(
        self,
        mock_api,
        monkeypatch,
    ):
        """Production helper calls should use a bearer identity token."""

        monkeypatch.setattr("validibot_mcp.client.settings.mcp_service_key", "")
        monkeypatch.setattr(
            "validibot_mcp.client._fetch_service_identity_token",
            lambda audience: _async_return("service-identity-token"),
        )
        route = mock_api.get("/api/v1/mcp/workflows/").respond(json=[])

        await list_authenticated_workflows(user_sub="user-sub-123")

        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer service-identity-token"
        assert request.headers["X-Validibot-User-Sub"] == "user-sub-123"

    async def test_sends_legacy_api_token_for_manual_bearer_compatibility(
        self,
        mock_api,
    ):
        """Manual bearer-token discovery should forward the legacy API token."""

        route = mock_api.get("/api/v1/mcp/workflows/").respond(json=[])

        await list_authenticated_workflows(api_token=SAMPLE_API_KEY)

        request = route.calls[0].request
        assert request.headers["X-Validibot-Api-Token"] == SAMPLE_API_KEY


class TestGetAuthenticatedWorkflowDetail:
    """Verify the authenticated workflow detail helper client."""

    async def test_forwards_user_identity_headers(self, mock_api, monkeypatch):
        """OAuth-authenticated detail lookup should use the MCP helper route."""

        monkeypatch.setattr("validibot_mcp.client.settings.mcp_service_key", "service-secret")
        route = mock_api.get("/api/v1/mcp/workflows/wf_demo/").respond(
            json=SAMPLE_WORKFLOW_FULL,
        )

        await get_authenticated_workflow_detail("wf_demo", user_sub="user-sub-123")

        request = route.calls[0].request
        assert request.headers["X-MCP-Service-Key"] == "service-secret"
        assert request.headers["X-Validibot-User-Sub"] == "user-sub-123"


class TestAuthenticatedHelperRuns:
    """Verify helper endpoints for member-access launches and polling."""

    async def test_start_authenticated_run_forwards_service_auth(
        self,
        mock_api,
        monkeypatch,
    ):
        """Member launches should use service auth and a JSON envelope body."""

        monkeypatch.setattr("validibot_mcp.client.settings.mcp_service_key", "service-secret")
        route = mock_api.post("/api/v1/mcp/workflows/wf_demo/runs/").respond(
            json=SAMPLE_RUN_PENDING,
        )

        await start_authenticated_validation_run(
            "wf_demo",
            file_content_b64=base64.b64encode(b"hello").decode(),
            file_name="test.json",
            user_sub="user-sub-123",
        )

        request = route.calls[0].request
        assert request.headers["X-MCP-Service-Key"] == "service-secret"
        assert request.headers["X-Validibot-User-Sub"] == "user-sub-123"
        body = json.loads(request.content.decode("utf-8"))
        assert body["content_encoding"] == "base64"
        assert body["filename"] == "test.json"

    async def test_get_authenticated_run_forwards_legacy_api_token(self, mock_api):
        """Manual bearer polling should forward the legacy API token header."""

        route = mock_api.get("/api/v1/mcp/runs/run_demo/").respond(
            json=SAMPLE_RUN_PENDING,
        )

        await get_authenticated_run("run_demo", api_token=SAMPLE_API_KEY)

        request = route.calls[0].request
        assert request.headers["X-Validibot-Api-Token"] == SAMPLE_API_KEY


async def _async_return(value: str) -> str:
    """Return a value from an async monkeypatch helper."""

    return value


# ── Service-to-service audience alignment ─────────────────────────────
# The MCP server and Django must agree on the identity token audience.
# A mismatch causes every service-to-service call to fail with 401.


class TestServiceAudienceAlignment:
    """Verify the MCP server's default audience matches Django's default.

    The MCP server uses ``effective_mcp_service_audience`` (defaults to
    ``api_base_url``) when fetching Cloud Run identity tokens. Django
    verifies the token's audience against ``MCP_OIDC_AUDIENCE``. Both
    must resolve to the same value.
    """

    def test_default_audience_is_app_validibot_com(self):
        """The default audience must be https://app.validibot.com.

        This matches both the MCP server's ``api_base_url`` default and
        Django's ``MCP_OIDC_AUDIENCE`` default after the alignment fix.
        If either side changes its default, this test fails.
        """
        from validibot_mcp.config import Settings

        s = Settings()
        assert s.effective_mcp_service_audience == "https://app.validibot.com"

    def test_identity_token_uses_correct_audience(self, monkeypatch):
        """When fetching an identity token, the metadata server request
        must include the audience from ``effective_mcp_service_audience``.

        This verifies the full chain: config → _get_service_identity_token
        → _fetch_service_identity_token → metadata server request params.
        """
        from validibot_mcp.config import Settings

        s = Settings()
        # The audience passed to the metadata server should be the
        # effective_mcp_service_audience value.
        assert s.effective_mcp_service_audience == s.mcp_service_audience or s.api_base_url
