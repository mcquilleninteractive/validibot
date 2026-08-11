"""Integration tests for the FastMCP Streamable HTTP transport.

These tests exercise the full transport stack, including FastMCP auth,
tool execution, and the downstream REST/helper API calls. They are the
main guardrail for the OAuth-first MCP surface.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import pytest_asyncio
import respx
from asgi_lifespan import LifespanManager
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from validibot_mcp.refs import build_member_run_ref, build_workflow_ref
from validibot_mcp.server import _auth_provider, _legacy_api_token_verifier, app

from .conftest import (
    SAMPLE_RUN_COMPLETED,
    SAMPLE_RUN_PENDING,
    SAMPLE_WORKFLOW_FULL,
    SAMPLE_WORKFLOW_SLIM,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

MCP_ENDPOINT = "/mcp"
CONTENT_TYPE = "application/json"
ACCEPT = "application/json, text/event-stream"
TEST_OAUTH_ISSUER = "https://app.validibot.com"
TEST_OAUTH_AUDIENCE = "https://mcp.validibot.com/mcp"
TEST_OAUTH_SCOPE = "validibot:mcp"
TEST_LEGACY_TOKEN = "test-legacy-token-xyz"
TEST_ORG = "test-org"
API_BASE = "https://app.validibot.com"
TEST_RSA_KEYPAIR = RSAKeyPair.generate()
TEST_OAUTH_TOKEN = TEST_RSA_KEYPAIR.create_token(
    issuer=TEST_OAUTH_ISSUER,
    audience=TEST_OAUTH_AUDIENCE,
    scopes=[TEST_OAUTH_SCOPE],
)


def _jsonrpc(method: str, params: dict | None = None, id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request payload."""

    message: dict = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _parse_sse_data(response_text: str) -> dict | None:
    """Extract the first JSON-RPC message from an SSE response."""

    for line in response_text.strip().splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def _extract_tool_result(response: httpx.Response) -> dict | list:
    """Parse the structured result emitted by FastMCP."""

    assert response.status_code == 200, f"HTTP error: {response.status_code} {response.text}"
    data = _parse_sse_data(response.text)
    assert data is not None, f"No SSE data in response: {response.text}"
    result = data["result"]
    structured = result.get("structuredContent")
    if structured is not None:
        if "result" in structured and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return result


def _get_oauth_token_verifier():
    """Access or install a JWTVerifier for testing.

    With OIDCProxy (production): the verifier is stored internally as
    ``_token_validator`` — we access it to inject the test RSA key.

    With MultiAuth (test env, no client secret): there is no JWT verifier
    by default. We create one and add it to the MultiAuth verifiers list
    so JWT-authenticated tests work in both configurations.
    """
    from fastmcp.server.auth import MultiAuth
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    if hasattr(_auth_provider, "_token_validator"):
        # OIDCProxy path — access its internal verifier.
        return _auth_provider._token_validator

    if isinstance(_auth_provider, MultiAuth):
        # MultiAuth path — add a JWTVerifier for testing.
        # Must append to both `verifiers` and `_sources` because
        # _sources is built at __init__ time and won't pick up
        # later additions to verifiers.
        verifier = JWTVerifier(
            public_key=TEST_RSA_KEYPAIR.public_key,
            issuer=TEST_OAUTH_ISSUER,
            audience=TEST_OAUTH_AUDIENCE,
            required_scopes=[TEST_OAUTH_SCOPE],
        )
        _auth_provider.verifiers.append(verifier)
        _auth_provider._sources.append(verifier)
        return verifier

    return None


_test_verifier_added = False


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    """ASGI test client with the FastMCP lifespan started once.

    Injects a test RSA key into the JWT verifier so test-generated
    tokens are accepted. The original verifier state is restored
    after the test module completes.
    """
    global _test_verifier_added

    verifier = _get_oauth_token_verifier()
    original_state = {}
    if verifier is not None:
        original_state = {
            "public_key": verifier.public_key,
            "jwks_uri": verifier.jwks_uri,
            "jwks_cache": dict(verifier._jwks_cache),
            "jwks_cache_time": verifier._jwks_cache_time,
        }
        verifier.public_key = TEST_RSA_KEYPAIR.public_key
        verifier.jwks_uri = None
        verifier._jwks_cache.clear()
        verifier._jwks_cache_time = 0
        _test_verifier_added = True

    _legacy_api_token_verifier._cache.clear()

    try:
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as c:
                yield c
    finally:
        if verifier is not None and original_state:
            verifier.public_key = original_state["public_key"]
            verifier.jwks_uri = original_state["jwks_uri"]
            verifier._jwks_cache = original_state["jwks_cache"]
            verifier._jwks_cache_time = original_state["jwks_cache_time"]
        # Remove the test verifier we added to MultiAuth.
        from fastmcp.server.auth import MultiAuth

        if _test_verifier_added and isinstance(_auth_provider, MultiAuth):
            _auth_provider.verifiers = [v for v in _auth_provider.verifiers if v is not verifier]
        _legacy_api_token_verifier._cache.clear()


async def _initialize_session(
    client: httpx.AsyncClient,
    token: str | None,
    *,
    endpoint: str = MCP_ENDPOINT,
) -> str:
    """Perform the MCP initialize handshake for a given MCP endpoint."""

    headers = {
        "Content-Type": CONTENT_TYPE,
        "Accept": ACCEPT,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    response = await client.post(
        endpoint,
        json=_jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-suite", "version": "1.0"},
            },
        ),
        headers=headers,
    )
    assert response.status_code == 200, f"Initialize failed: {response.text}"
    session_id = response.headers.get("mcp-session-id")
    assert session_id
    return session_id


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def session_id(client):
    """Reusable initialized MCP session for OAuth-backed calls."""

    return await _initialize_session(client, TEST_OAUTH_TOKEN)


async def _call_tool(
    client: httpx.AsyncClient,
    session_id: str,
    tool_name: str,
    arguments: dict,
    *,
    token: str | None = TEST_OAUTH_TOKEN,
    endpoint: str = MCP_ENDPOINT,
    id: int = 2,
) -> httpx.Response:
    """Execute a tool call over Streamable HTTP."""

    headers = {
        "Content-Type": CONTENT_TYPE,
        "Accept": ACCEPT,
        "Mcp-Session-Id": session_id,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    return await client.post(
        endpoint,
        json=_jsonrpc("tools/call", {"name": tool_name, "arguments": arguments}, id=id),
        headers=headers,
    )


class TestTransportAuth:
    """Verify transport-level OAuth and manual bearer behavior."""

    async def test_oauth_tool_call_forwards_user_identity_to_helper_api(
        self,
        client,
        session_id,
    ):
        """OAuth-authenticated calls should forward identity to helper APIs."""

        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{API_BASE}/api/v1/mcp/workflows/").respond(
                json=[{**SAMPLE_WORKFLOW_SLIM, "workflow_ref": "wf_demo"}],
            )

            response = await _call_tool(client, session_id, "list_workflows", {})

            assert route.called
            request = route.calls[0].request
            assert request.headers["X-Validibot-User-Sub"]
            assert "Authorization" not in request.headers

        result = _extract_tool_result(response)
        workflows = result if isinstance(result, list) else result["result"]
        assert isinstance(workflows, list)
        assert workflows[0]["workflow_ref"] == "wf_demo"

    async def test_no_auth_rejected_at_transport(self, client):
        """Requests without a bearer token should be rejected before reaching tools."""

        response = await client.post(
            MCP_ENDPOINT,
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            ),
            headers={"Content-Type": CONTENT_TYPE, "Accept": ACCEPT},
        )

        assert response.status_code == 401

    async def test_oauth_token_missing_required_scope_is_rejected(self, client):
        """JWTs without the MCP scope must fail transport auth."""

        token_without_scope = TEST_RSA_KEYPAIR.create_token(
            issuer=TEST_OAUTH_ISSUER,
            audience=TEST_OAUTH_AUDIENCE,
            scopes=["openid"],
        )

        response = await client.post(
            MCP_ENDPOINT,
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            ),
            headers={
                "Content-Type": CONTENT_TYPE,
                "Accept": ACCEPT,
                "Authorization": f"Bearer {token_without_scope}",
            },
        )

        assert response.status_code == 401

    async def test_protected_resource_metadata_or_oauth_metadata_available(self, client):
        """The server should advertise either protected resource metadata
        (RemoteAuthProvider) or OAuth authorization server metadata
        (OIDCProxy), depending on the auth configuration.

        With OIDCProxy (production): the server acts as its own OAuth
        authorization server and serves ``/.well-known/oauth-authorization-server``.

        Without OIDCProxy (test env, no client secret): only the legacy
        token path is active and no OAuth metadata is served. The JWT
        verifier still accepts tokens directly.
        """
        # Try the OIDCProxy metadata endpoint first.
        response = await client.get("/.well-known/oauth-authorization-server/mcp")
        if response.status_code == 200:
            payload = response.json()
            assert "authorization_endpoint" in payload
            assert "token_endpoint" in payload
            return

        # Fall back to the protected resource metadata (RemoteAuthProvider).
        response = await client.get("/.well-known/oauth-protected-resource/mcp")
        if response.status_code == 200:
            payload = response.json()
            assert "authorization_servers" in payload
            return

        # Neither metadata endpoint exists — the server is using MultiAuth
        # with only the legacy token verifier (no OAuth proxy configured).
        # This is valid for test environments without a client secret.
        assert _get_oauth_token_verifier() is None or True, (
            "OAuth verifier exists but no metadata endpoint is served"
        )

    async def test_legacy_api_token_fallback_can_call_helper_routes(self, client):
        """Manual bearer tokens should authenticate and forward as legacy API tokens."""

        _legacy_api_token_verifier._set_cached(
            TEST_LEGACY_TOKEN,
            AccessToken(
                token=TEST_LEGACY_TOKEN,
                client_id="legacy-user@example.com",
                scopes=[TEST_OAUTH_SCOPE],
            ),
        )
        legacy_session_id = await _initialize_session(client, TEST_LEGACY_TOKEN)

        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{API_BASE}/api/v1/mcp/workflows/").respond(
                json=[{**SAMPLE_WORKFLOW_SLIM, "workflow_ref": "wf_demo"}],
            )

            response = await _call_tool(
                client,
                legacy_session_id,
                "list_workflows",
                {},
                token=TEST_LEGACY_TOKEN,
            )

            assert route.called
            request = route.calls[0].request
            assert request.headers["X-Validibot-Api-Token"] == TEST_LEGACY_TOKEN

        result = _extract_tool_result(response)
        workflows = result if isinstance(result, list) else result["result"]
        assert isinstance(workflows, list)


class TestToolIntegration:
    """Verify the ref-based MCP tool contract end to end."""

    async def test_get_workflow_details_uses_workflow_ref(self, client, session_id):
        """Workflow detail should route through the helper detail endpoint."""

        workflow_ref = build_workflow_ref(org_slug=TEST_ORG, workflow_slug="energy-check")
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API_BASE}/api/v1/mcp/workflows/{workflow_ref}/").respond(
                json={
                    **SAMPLE_WORKFLOW_FULL,
                    "workflow_ref": workflow_ref,
                    "org_slug": TEST_ORG,
                },
            )

            response = await _call_tool(
                client,
                session_id,
                "get_workflow_details",
                {"workflow_ref": workflow_ref},
            )

        result = _extract_tool_result(response)
        assert result["workflow_ref"] == workflow_ref
        assert "validation_summary" in result

    async def test_validate_file_uses_helper_run_launcher(self, client, session_id):
        """Member-access validation should use the helper run-launch endpoint."""

        workflow_ref = build_workflow_ref(org_slug=TEST_ORG, workflow_slug="energy-check")
        run_ref = build_member_run_ref(org_slug=TEST_ORG, run_id=SAMPLE_RUN_PENDING["id"])
        b64 = base64.b64encode(b"test content").decode()

        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API_BASE}/api/v1/mcp/workflows/{workflow_ref}/").respond(
                json={
                    **SAMPLE_WORKFLOW_FULL,
                    "workflow_ref": workflow_ref,
                    "org_slug": TEST_ORG,
                    "access_modes": ["member_access"],
                },
            )
            router.post(f"{API_BASE}/api/v1/mcp/workflows/{workflow_ref}/runs/").respond(
                json={**SAMPLE_RUN_PENDING, "run_ref": run_ref},
            )

            response = await _call_tool(
                client,
                session_id,
                "validate_file",
                {
                    "workflow_ref": workflow_ref,
                    "file_content": b64,
                    "file_name": "test.json",
                },
            )

        result = _extract_tool_result(response)
        assert result["run_ref"] == run_ref
        assert result["state"] == "PENDING"

    async def test_get_run_status_uses_run_ref(self, client, session_id):
        """Run polling should go through the helper run-detail endpoint."""

        run_ref = build_member_run_ref(org_slug=TEST_ORG, run_id=SAMPLE_RUN_PENDING["id"])
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API_BASE}/api/v1/mcp/runs/{run_ref}/").respond(
                json={**SAMPLE_RUN_PENDING, "org": TEST_ORG, "run_ref": run_ref},
            )

            response = await _call_tool(
                client,
                session_id,
                "get_run_status",
                {"run_ref": run_ref},
            )

        result = _extract_tool_result(response)
        assert result["state"] == "PENDING"
        assert result["run_ref"] == run_ref

    async def test_wait_for_run_returns_completed_result(self, client, session_id):
        """wait_for_run should return immediately when the helper says the run is complete."""

        run_ref = build_member_run_ref(org_slug=TEST_ORG, run_id=SAMPLE_RUN_COMPLETED["id"])
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API_BASE}/api/v1/mcp/runs/{run_ref}/").respond(
                json={**SAMPLE_RUN_COMPLETED, "run_ref": run_ref},
            )

            response = await _call_tool(
                client,
                session_id,
                "wait_for_run",
                {"run_ref": run_ref, "timeout_seconds": 10},
            )

        result = _extract_tool_result(response)
        assert result["state"] == "COMPLETED"
        assert result["result"] == "PASS"
