"""Tests for the MCP tool layer.

These tests cover the tool contract the model sees, not just the lower-level
HTTP client helpers. The MCP server is authenticated-only, so the contract is:

- a Bearer credential is required for every tool (no anonymous fallback)
- discovery returns ``workflow_ref`` plus display metadata like ``org_slug``
- workflow detail, validation, and polling all route through opaque refs
- member access goes through the authenticated ``/api/v1/mcp/*`` helper
  endpoints
"""

from __future__ import annotations

import base64

from validibot_mcp.refs import build_member_run_ref, build_workflow_ref
from validibot_mcp.tools.runs import get_run_status, wait_for_run
from validibot_mcp.tools.validate import _MAX_FILE_SIZE_BYTES, validate_file
from validibot_mcp.tools.workflows import get_workflow_details, list_workflows

from .conftest import (
    SAMPLE_RUN_COMPLETED,
    SAMPLE_RUN_PENDING,
    SAMPLE_WORKFLOW_FULL,
    SAMPLE_WORKFLOW_SLIM,
)

ORG = "acme-corp"


class TestListWorkflowsTool:
    """Verify the workflow discovery tool."""

    async def test_returns_authenticated_catalog(self, mock_auth, mock_api, monkeypatch):
        """Authenticated discovery should use the MCP helper catalog.

        WHY: discovery is the entry point agents call first; it must forward the
        user's identity to the authenticated helper catalog and surface the
        opaque ``workflow_ref`` handles the rest of the contract depends on.
        """

        monkeypatch.setattr(
            "validibot_mcp.auth.get_authenticated_user_sub_or_none",
            lambda: "user-sub-123",
        )
        mock_api.get("/api/v1/mcp/workflows/").respond(
            json=[{**SAMPLE_WORKFLOW_SLIM, "workflow_ref": "wf_demo"}],
        )

        result = await list_workflows()

        assert isinstance(result, list)
        assert result[0]["workflow_ref"] == "wf_demo"

    async def test_no_auth_returns_unauthorized(self, mock_access_token):
        """Discovery without a bearer token must return an UNAUTHORIZED error.

        WHY: there is no anonymous surface anymore, so a missing credential is a
        hard stop — the tool must surface ``UNAUTHORIZED`` rather than silently
        falling back to a public catalog.
        """

        mock_access_token(None)

        result = await list_workflows()

        assert result["error"]["code"] == "UNAUTHORIZED"

    async def test_gating_error_returns_structured_dict(self, mock_auth, monkeypatch):
        """The global kill switch should produce a structured error response.

        WHY: the ``MCP_ENABLED`` operator kill switch must short-circuit before
        any auth or API work and return a stable ``SERVICE_UNAVAILABLE`` payload.
        """

        monkeypatch.setenv("VALIDIBOT_MCP_ENABLED", "false")

        result = await list_workflows()

        assert result["error"]["code"] == "SERVICE_UNAVAILABLE"


class TestGetWorkflowDetailsTool:
    """Verify the workflow detail tool."""

    async def test_returns_enriched_member_workflow_details(
        self,
        mock_auth,
        mock_api,
        monkeypatch,
    ):
        """Authenticated detail should come from the MCP helper endpoint.

        WHY: detail is what agents use to decide what file to submit, so it must
        route through the authenticated helper and attach the computed
        enrichment fields (extensions, pricing, summary).
        """

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="energy-check")
        monkeypatch.setattr(
            "validibot_mcp.auth.get_authenticated_user_sub_or_none",
            lambda: "user-sub-123",
        )
        mock_api.get(f"/api/v1/mcp/workflows/{workflow_ref}/").respond(
            json={
                **SAMPLE_WORKFLOW_FULL,
                "workflow_ref": workflow_ref,
                "org_slug": ORG,
            },
        )

        result = await get_workflow_details(workflow_ref=workflow_ref)

        assert result["workflow_ref"] == workflow_ref
        assert "accepted_extensions" in result
        assert "pricing" in result
        assert "validation_summary" in result

    async def test_member_workflow_detail_does_not_require_agent_publication(
        self,
        mock_auth,
        mock_api,
    ):
        """Authenticated member workflows should stay visible when not public.

        WHY: ``agent_access_enabled`` gates the anonymous/public surface, which
        no longer exists here. An authenticated member must still see their own
        workflow even with that flag off, so detail must not gate on it.
        """

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="private-member")
        mock_api.get(f"/api/v1/mcp/workflows/{workflow_ref}/").respond(
            json={
                **SAMPLE_WORKFLOW_FULL,
                "workflow_ref": workflow_ref,
                "org_slug": ORG,
                "agent_access_enabled": False,
                "access_modes": ["member_access"],
            },
        )

        result = await get_workflow_details(workflow_ref=workflow_ref)

        assert result["workflow_ref"] == workflow_ref
        assert result["pricing"]["payment_required"] is False

    async def test_no_auth_returns_unauthorized(self, mock_access_token):
        """Detail without a bearer token must return an UNAUTHORIZED error.

        WHY: like discovery, workflow detail has no anonymous path; an absent
        credential must fail closed with ``UNAUTHORIZED``.
        """

        mock_access_token(None)
        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="energy-check")

        result = await get_workflow_details(workflow_ref=workflow_ref)

        assert result["error"]["code"] == "UNAUTHORIZED"


class TestValidateFileTool:
    """Verify the validation-launch tool."""

    async def test_submits_member_access_file_successfully(self, mock_auth, mock_api):
        """Member-access launches should go through the helper run endpoint.

        WHY: this is the primary tool; it must POST to the authenticated helper
        run-launch route and return the run identity plus the opaque member
        ``run_ref`` callers poll with.
        """

        slug = "energy-check"
        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug=slug)
        run_ref = build_member_run_ref(org_slug=ORG, run_id=SAMPLE_RUN_PENDING["id"])
        b64 = base64.b64encode(b"test content").decode()

        mock_api.post(f"/api/v1/mcp/workflows/{workflow_ref}/runs/").respond(
            json={**SAMPLE_RUN_PENDING, "run_ref": run_ref},
        )

        result = await validate_file(
            file_content=b64,
            file_name="test.json",
            workflow_ref=workflow_ref,
        )

        assert result["id"] == SAMPLE_RUN_PENDING["id"]
        assert result["run_ref"] == run_ref

    async def test_oversized_file_returns_error(self, mock_auth):
        """Files over the encoded size limit should fail fast.

        WHY: the size guard runs before any network call so a too-large upload
        is rejected locally with ``INVALID_PARAMS`` rather than wasting a
        round-trip.
        """

        huge_content = "A" * (_MAX_FILE_SIZE_BYTES + 1)

        result = await validate_file(
            file_content=huge_content,
            file_name="huge.json",
            workflow_ref=build_workflow_ref(org_slug=ORG, workflow_slug="energy-check"),
        )

        assert result["error"]["code"] == "INVALID_PARAMS"

    async def test_missing_workflow_ref_returns_error(self, mock_auth):
        """A blank workflow_ref should fail fast with INVALID_PARAMS.

        WHY: there is nothing to launch without a target workflow, so the tool
        must reject the call locally before authenticating or calling the API.
        """

        b64 = base64.b64encode(b"test content").decode()

        result = await validate_file(
            file_content=b64,
            file_name="test.json",
            workflow_ref="",
        )

        assert result["error"]["code"] == "INVALID_PARAMS"

    async def test_no_auth_returns_unauthorized(self, mock_access_token):
        """Validation without a bearer token must return UNAUTHORIZED.

        WHY: MCP agents always act on behalf of an authenticated user, so a
        launch with no credential must fail closed — never an anonymous or
        payment-backed launch.
        """

        mock_access_token(None)
        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="energy-check")
        b64 = base64.b64encode(b"test content").decode()

        result = await validate_file(
            file_content=b64,
            file_name="test.json",
            workflow_ref=workflow_ref,
        )

        assert result["error"]["code"] == "UNAUTHORIZED"

    async def test_member_helper_api_error_returns_structured_error(self, mock_auth, mock_api):
        """Helper endpoint errors should be surfaced in the standard MCP format.

        WHY: a downstream 4xx from the helper must be translated into the stable
        ``API_ERROR`` envelope rather than leaking an httpx exception to the
        model.
        """

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="energy-check")
        b64 = base64.b64encode(b"test content").decode()
        mock_api.post(f"/api/v1/mcp/workflows/{workflow_ref}/runs/").respond(
            status_code=403,
            json={"detail": "Forbidden"},
        )

        result = await validate_file(
            file_content=b64,
            file_name="test.json",
            workflow_ref=workflow_ref,
        )

        assert result["error"]["code"] == "API_ERROR"


class TestRunTools:
    """Verify status and wait tools."""

    async def test_get_run_status_returns_member_run(self, mock_auth, mock_api):
        """Member run refs should resolve through the authenticated helper route.

        WHY: polling must decode the opaque member ref and hit the authenticated
        ``/api/v1/mcp/runs/`` endpoint, echoing back the same ``run_ref``.
        """

        run_id = SAMPLE_RUN_PENDING["id"]
        run_ref = build_member_run_ref(org_slug=ORG, run_id=run_id)
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={**SAMPLE_RUN_PENDING, "run_ref": run_ref},
        )

        result = await get_run_status(run_ref=run_ref)

        assert result["state"] == "PENDING"
        assert result["run_ref"] == run_ref

    async def test_get_run_status_api_error_returns_structured_dict(self, mock_auth, mock_api):
        """Authenticated helper errors should produce MCP API_ERROR payloads.

        WHY: a 404 (or other non-2xx) from the helper run-detail endpoint must
        become the stable ``API_ERROR`` envelope, not a raw exception.
        """

        run_ref = build_member_run_ref(org_slug=ORG, run_id="nonexistent")
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            status_code=404,
            json={"detail": "Not found"},
        )

        result = await get_run_status(run_ref=run_ref)

        assert result["error"]["code"] == "API_ERROR"

    async def test_wait_for_run_returns_completed_run(self, mock_auth, mock_api):
        """Completed runs should return immediately without polling further.

        WHY: when the helper already reports ``state=COMPLETED`` the tool must
        return the terminal snapshot at once rather than idling on its poll loop.
        """

        run_id = SAMPLE_RUN_COMPLETED["id"]
        run_ref = build_member_run_ref(org_slug=ORG, run_id=run_id)
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={**SAMPLE_RUN_COMPLETED, "run_ref": run_ref},
        )

        result = await wait_for_run(run_ref=run_ref, timeout_seconds=10)

        assert result["state"] == "COMPLETED"
        assert result["result"] == "PASS"

    async def test_wait_for_run_timeout_returns_timed_out(self, mock_auth, mock_api):
        """Timeout should return the last known run state plus TIMED_OUT.

        WHY: a non-terminal run past the client-side budget must yield the last
        snapshot annotated with ``result=TIMED_OUT`` and ``is_complete=False``,
        distinct from a server-side timeout outcome.
        """

        run_id = SAMPLE_RUN_PENDING["id"]
        run_ref = build_member_run_ref(org_slug=ORG, run_id=run_id)
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={**SAMPLE_RUN_PENDING, "run_ref": run_ref},
        )

        result = await wait_for_run(run_ref=run_ref, timeout_seconds=-1)

        assert result["result"] == "TIMED_OUT"
        assert result["is_complete"] is False

    async def test_wait_for_run_returns_when_server_reports_timed_out(self, mock_auth, mock_api):
        """A terminal server-side ``TIMED_OUT`` must return immediately.

        WHY: after the wire-format unification both backends emit
        ``state="COMPLETED"`` for any terminal status with the granular outcome
        in ``result``. When the helper says a run is done with
        ``result="TIMED_OUT"``, ``wait_for_run`` must surface that snapshot
        without idling for the full client-side budget — otherwise an agent
        would wait pointlessly and then fabricate its own timeout envelope,
        hiding the real verdict.
        """

        run_id = SAMPLE_RUN_PENDING["id"]
        run_ref = build_member_run_ref(org_slug=ORG, run_id=run_id)
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={
                **SAMPLE_RUN_PENDING,
                "state": "COMPLETED",
                "result": "TIMED_OUT",
                "run_ref": run_ref,
            },
        )

        # A generous client-side timeout proves the function returns based on
        # the server's terminal state, not because the budget expired.
        result = await wait_for_run(run_ref=run_ref, timeout_seconds=60)

        assert result["state"] == "COMPLETED"
        assert result["result"] == "TIMED_OUT"
        # The client-side timeout helper would have stamped ``is_complete``
        # to ``False``; an early-exit must not.
        assert "is_complete" not in result or result.get("is_complete") is not False
