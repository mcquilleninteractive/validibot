"""Tests for MCP dispatch through the authenticated helper endpoints.

The MCP server is authenticated-only: every tool acts on behalf of a bearer
credential and routes through the ``/api/v1/mcp/*`` helper API. These tests pin
that contract:

1. ``list_workflows()`` dispatches to the authenticated helper catalog.
2. ``validate_file(workflow_ref=...)`` launches a member-access run through the
   authenticated helper run launcher.
3. ``get_run_status(run_ref=...)`` decodes the opaque member run reference and
   polls the authenticated helper endpoint rather than requiring the caller to
   pass org inputs directly.
"""

from __future__ import annotations

import base64

import pytest

from validibot_mcp.refs import build_member_run_ref, build_workflow_ref
from validibot_mcp.tools.runs import get_run_status
from validibot_mcp.tools.validate import validate_file
from validibot_mcp.tools.workflows import list_workflows

from .conftest import SAMPLE_API_KEY, SAMPLE_WORKFLOW_SLIM

ORG = "acme-corp"


@pytest.fixture
def authenticated(monkeypatch):
    """Simulate an authenticated MCP request with a manual bearer token."""

    monkeypatch.setattr("validibot_mcp.auth.get_api_key", lambda: SAMPLE_API_KEY)
    monkeypatch.setattr("validibot_mcp.auth.get_api_key_or_none", lambda: SAMPLE_API_KEY)
    monkeypatch.setattr(
        "validibot_mcp.auth.get_authenticated_user_sub_or_none",
        lambda: None,
    )


class TestListWorkflowsDispatch:
    """Verify workflow discovery uses the authenticated helper catalog."""

    async def test_authenticated_hits_mcp_catalog_endpoint(
        self,
        authenticated,
        mock_api,
        monkeypatch,
    ):
        """Authenticated discovery should use the MCP helper catalog.

        WHY: the only supported surface is authenticated, so discovery must
        forward the user's identity to ``/api/v1/mcp/workflows/`` — never to
        any anonymous catalog.
        """

        monkeypatch.setattr(
            "validibot_mcp.auth.get_authenticated_user_sub_or_none",
            lambda: "user-sub-123",
        )
        mock_api.get("/api/v1/mcp/workflows/").respond(json=[SAMPLE_WORKFLOW_SLIM])

        result = await list_workflows()

        assert isinstance(result, list)
        assert len(result) == 1


class TestValidateFileDispatch:
    """Verify validation dispatch uses the authenticated helper run launcher."""

    async def test_authenticated_member_access_uses_mcp_helper_run_launcher(
        self,
        authenticated,
        mock_api,
    ):
        """Member-access workflows should launch through the helper endpoint.

        WHY: ``validate_file`` must POST to the authenticated MCP helper
        run-launch route and return the opaque member ``run_ref`` the contract
        promises — proving the run is billed to the user's quota.
        """

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="energy-check")
        b64 = base64.b64encode(b"test content").decode()

        mock_api.post(f"/api/v1/mcp/workflows/{workflow_ref}/runs/").respond(
            json={
                "id": "run-123",
                "run_id": "run-123",
                "state": "PENDING",
                "result": "UNKNOWN",
                "run_ref": build_member_run_ref(org_slug=ORG, run_id="run-123"),
            },
        )

        result = await validate_file(
            workflow_ref=workflow_ref,
            file_content=b64,
            file_name="test.json",
        )

        assert result.get("run_ref") == build_member_run_ref(
            org_slug=ORG,
            run_id="run-123",
        )


class TestGetRunStatusDispatch:
    """Verify run polling dispatches by member run_ref."""

    async def test_authenticated_member_run_hits_mcp_helper(self, authenticated, mock_api):
        """Member run refs should use the authenticated helper endpoint.

        WHY: ``get_run_status`` decodes the opaque member ref and polls the
        authenticated ``/api/v1/mcp/runs/`` route — the caller never supplies an
        org slug or a polling URL directly.
        """

        run_ref = build_member_run_ref(
            org_slug=ORG,
            run_id="550e8400-e29b-41d4-a716-446655440000",
        )
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={"id": "550e8400-e29b-41d4-a716-446655440000", "state": "PENDING"},
        )

        result = await get_run_status(run_ref=run_ref)

        assert result["state"] == "PENDING"

    async def test_missing_run_ref_returns_error(self, authenticated):
        """Run polling should require the opaque run_ref contract.

        WHY: without a ref there is nothing to decode or route, so the tool must
        return a structured ``INVALID_PARAMS`` error rather than guessing.
        """

        result = await get_run_status()

        assert result["error"]["code"] == "INVALID_PARAMS"
