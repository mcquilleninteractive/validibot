"""Official-client integration tests for the embedded Streamable HTTP server.

These tests cross the real MCP serialization, authentication middleware, tool
schema generation, stateless transport, and Django service boundary. They use
the official SDK client because a direct Python handler call cannot prove the
contract ChatGPT and Codex will consume.
"""

from __future__ import annotations

from http import HTTPStatus

import anyio
import httpx2
import pytest
from asgiref.sync import sync_to_async
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.types import TextContent

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditLogEntry
from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import get_license
from validibot.core.license import set_license
from validibot.mcp_server.auth import ValidibotTokenVerifier
from validibot.mcp_server.constants import MCPErrorCode
from validibot.mcp_server.exceptions import MCPApplicationError
from validibot.mcp_server.file_downloads import DownloadedFile
from validibot.mcp_server.references import build_workflow_reference
from validibot.mcp_server.schemas import OpenAIFileInput
from validibot.mcp_server.server import _ensure_bounded_result
from validibot.mcp_server.server import _start_validation_from_openai_file
from validibot.mcp_server.server import build_mcp_asgi_application
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.models import ValidationRun
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db(transaction=True)

RESOURCE_URL = "http://localhost:8000/mcp"
ISSUER_URL = "http://localhost:8000"
TEST_BEARER = "official-client-test-token"
EXPECTED_AUDIT_ENTRY_COUNT = 6


@pytest.fixture(autouse=True)
def _licensed_protocol_server():
    """Run the official-client contract with the production Pro gate active."""

    original_license = get_license()
    set_license(
        License(
            edition=Edition.PRO,
            features=frozenset({CommercialFeature.MCP_SERVER.value}),
        ),
    )
    try:
        yield
    finally:
        set_license(original_license)


def _accept_test_bearer(monkeypatch: pytest.MonkeyPatch, *, user_id: int) -> None:
    """Install the smallest verifier stub while retaining real SDK middleware."""

    async def verify_test_token(
        self: ValidibotTokenVerifier,
        token: str,
    ) -> AccessToken | None:
        """Attach the requested Django principal for the fixed fixture token."""

        del self
        if token != TEST_BEARER:
            return None
        return AccessToken(
            token=token,
            client_id="validibot-chatgpt-test",
            scopes=["validibot:mcp"],
            resource=RESOURCE_URL,
            subject=str(user_id),
            claims={"iss": ISSUER_URL, "user_id": user_id},
        )

    monkeypatch.setattr(ValidibotTokenVerifier, "verify_token", verify_test_token)


def test_official_client_exercises_all_five_tools(
    monkeypatch,
    settings,
) -> None:
    """The released client must discover and call every plugin operation."""

    settings.SITE_URL = ISSUER_URL
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE_URL
    settings.ALLOWED_HOSTS = ["localhost"]
    org = OrganizationFactory(mcp_allowed=True)
    user = UserFactory(is_superuser=True, orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        name="Official client workflow",
        description="Validate JSON through the MCP protocol.",
        mcp_enabled=True,
    )
    WorkflowStepFactory(workflow=workflow, name="Schema check")

    def do_not_dispatch(*, validation_run_id, user_id) -> None:
        """Keep protocol-launched runs pending so status calls are deterministic."""

        assert validation_run_id
        assert user_id == user.pk

    _accept_test_bearer(monkeypatch, user_id=user.pk)
    monkeypatch.setattr(
        "validibot.core.tasks.enqueue_validation_run",
        do_not_dispatch,
    )
    monkeypatch.setattr(
        "validibot.mcp_server.server.download_openai_file",
        lambda file: DownloadedFile(
            file_name=file.file_name or "upload.bin",
            content_type=file.mime_type or "application/octet-stream",
            content=b"{}",
        ),
    )

    async def exercise_protocol() -> None:
        """Run one complete model-facing workflow through Streamable HTTP."""

        app = build_mcp_asgi_application()
        transport = httpx2.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=transport,
                base_url=ISSUER_URL,
                headers={"Authorization": f"Bearer {TEST_BEARER}"},
            ) as http_client,
        ):
            metadata = await http_client.get(
                "/.well-known/oauth-protected-resource/mcp",
            )
            assert metadata.status_code == HTTPStatus.OK
            assert metadata.json()["resource"] == RESOURCE_URL
            assert metadata.json()["authorization_servers"] == [ISSUER_URL]

            async with (
                streamable_http_client(
                    RESOURCE_URL,
                    http_client=http_client,
                    terminate_on_close=False,
                ) as streams,
                ClientSession(*streams) as session,
            ):
                initialized = await session.initialize()
                assert initialized.server_info.name == "validibot-validation"

                listed_tools = await session.list_tools()
                by_name = {tool.name: tool for tool in listed_tools.tools}
                assert set(by_name) == {
                    "list_workflows",
                    "get_workflow",
                    "start_validation",
                    "get_validation_run",
                    "list_validation_findings",
                }
                for tool_name, tool in by_name.items():
                    assert tool.title
                    assert tool.description
                    assert tool.output_schema
                    assert tool.annotations is not None
                    expected_meta: dict[str, object] = {
                        "securitySchemes": [
                            {
                                "type": "oauth2",
                                "scopes": ["validibot:mcp"],
                            },
                        ],
                    }
                    if tool_name == "start_validation":
                        expected_meta["openai/fileParams"] = ["file"]
                    assert tool.meta == expected_meta
                assert set(
                    by_name["start_validation"].input_schema["required"],
                ) == {
                    "workflow_ref",
                    "file",
                    "idempotency_key",
                }
                file_schema_ref = by_name["start_validation"].input_schema[
                    "properties"
                ]["file"]["$ref"]
                file_schema = by_name["start_validation"].input_schema["$defs"][
                    file_schema_ref.rsplit("/", 1)[-1]
                ]
                assert set(file_schema["properties"]) == {
                    "download_url",
                    "file_id",
                    "mime_type",
                    "file_name",
                }
                assert set(file_schema["required"]) == {"download_url", "file_id"}
                assert file_schema["additionalProperties"] is False
                start_annotations = by_name["start_validation"].annotations
                assert start_annotations.read_only_hint is False
                assert start_annotations.idempotent_hint is True

                catalog = await session.call_tool("list_workflows", {})
                assert not catalog.is_error
                workflow_ref = catalog.structured_content["workflows"][0][
                    "workflow_ref"
                ]

                detail = await session.call_tool(
                    "get_workflow",
                    {"workflow_ref": workflow_ref},
                )
                assert not detail.is_error
                assert detail.structured_content["steps"][0]["name"] == ("Schema check")

                launch_args = {
                    "workflow_ref": workflow_ref,
                    "file": {
                        "download_url": "https://files.openai.example/temporary",
                        "file_id": "file-validibot-test",
                        "mime_type": "application/json",
                        "file_name": "payload.json",
                    },
                    "idempotency_key": "official-client-launch-1",
                }
                launched = await session.call_tool(
                    "start_validation",
                    launch_args,
                )
                replayed = await session.call_tool(
                    "start_validation",
                    launch_args,
                )
                assert not launched.is_error
                run_ref = launched.structured_content["run_ref"]
                assert launched.structured_content["idempotency_replayed"] is False
                assert replayed.structured_content["idempotency_replayed"] is True
                assert replayed.structured_content["run_ref"] == run_ref

                status = await session.call_tool(
                    "get_validation_run",
                    {"run_ref": run_ref},
                )
                assert status.structured_content["status"] == "PENDING"

                def complete_run() -> None:
                    """Move the fixture through the normal terminal tool branch."""

                    ValidationRun.objects.filter(workflow=workflow, user=user).update(
                        status=ValidationRunStatus.SUCCEEDED,
                    )

                await sync_to_async(complete_run)()
                findings = await session.call_tool(
                    "list_validation_findings",
                    {"run_ref": run_ref},
                )
                assert not findings.is_error
                assert findings.structured_content["findings"] == []

    anyio.run(exercise_protocol)

    assert ValidationRun.objects.filter(workflow=workflow, user=user).count() == 1
    audit_entries = AuditLogEntry.objects.filter(
        action=AuditAction.MCP_TOOL_CALLED.value,
        actor__user=user,
    )
    assert audit_entries.count() == EXPECTED_AUDIT_ENTRY_COUNT
    sensitive_values = {
        "https://files.openai.example/temporary",
        "file-validibot-test",
        "payload.json",
        "file_content",
    }
    assert all(
        not any(value in str(entry.metadata) for value in sensitive_values)
        for entry in audit_entries
    )
    assert audit_entries.filter(org=org).count() == EXPECTED_AUDIT_ENTRY_COUNT - 1
    assert all(
        entry.metadata["oauth_client_id"] == "validibot-chatgpt-test"
        for entry in audit_entries
    )


def test_streamable_http_requires_bearer_authentication(settings) -> None:
    """An unauthenticated request must receive the OAuth discovery challenge."""

    settings.SITE_URL = ISSUER_URL
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE_URL
    settings.ALLOWED_HOSTS = ["localhost"]

    async def call_without_token() -> None:
        """Make a raw request so the 401 headers remain directly inspectable."""

        app = build_mcp_asgi_application()
        transport = httpx2.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=transport,
                base_url=ISSUER_URL,
            ) as client,
        ):
            response = await client.get("/mcp")
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert (
            'resource_metadata="http://localhost:8000/'
            '.well-known/oauth-protected-resource/mcp"'
            in response.headers["www-authenticate"]
        )

    anyio.run(call_without_token)


def test_start_authorizes_workflow_before_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied reference must not trigger a model-influenced outbound request."""

    user = UserFactory()

    def must_not_download(file: OpenAIFileInput) -> DownloadedFile:
        """Fail loudly if authorization ordering ever regresses."""

        del file
        pytest.fail("The downloader ran before workflow authorization.")

    monkeypatch.setattr(
        "validibot.mcp_server.server.download_openai_file",
        must_not_download,
    )

    with pytest.raises(MCPApplicationError) as denied:
        _start_validation_from_openai_file(
            user=user,
            workflow_ref="wf1.invalid.invalid-reference",
            file=OpenAIFileInput(
                download_url="https://files.openai.example/temporary",
                file_id="file-denied-test",
            ),
            idempotency_key="denied-download-test",
        )

    assert denied.value.code == MCPErrorCode.NOT_FOUND


def test_start_checks_execute_permission_before_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mere workflow visibility must not authorize a model-influenced fetch."""

    org = OrganizationFactory(mcp_allowed=True)
    author = UserFactory(orgs=[org])
    viewer = UserFactory(orgs=[org])
    grant_role(viewer, org, RoleCode.WORKFLOW_VIEWER)
    workflow = WorkflowFactory(
        org=org,
        user=author,
        mcp_enabled=True,
    )
    WorkflowStepFactory(workflow=workflow)

    def must_not_download(file: OpenAIFileInput) -> DownloadedFile:
        """Fail loudly if visibility is ever mistaken for launch permission."""

        del file
        pytest.fail("The downloader ran without workflow launch permission.")

    monkeypatch.setattr(
        "validibot.mcp_server.server.download_openai_file",
        must_not_download,
    )

    with pytest.raises(MCPApplicationError) as denied:
        _start_validation_from_openai_file(
            user=viewer,
            workflow_ref=build_workflow_reference(workflow),
            file=OpenAIFileInput(
                download_url="https://files.openai.example/temporary",
                file_id="file-view-only-test",
            ),
            idempotency_key="view-only-download-test",
        )

    assert denied.value.code == MCPErrorCode.LAUNCH_DENIED


def test_start_checks_workflow_readiness_before_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable validator should produce a truthful error without a fetch."""

    org = OrganizationFactory(mcp_allowed=True)
    user = UserFactory(orgs=[org])
    workflow = WorkflowFactory(
        org=org,
        user=user,
        mcp_enabled=True,
    )
    step = WorkflowStepFactory(workflow=workflow)
    step.validator.availability_state = ValidatorAvailabilityState.MISSING_CONFIG
    step.validator.availability_message = (
        "No registered ValidatorConfig for private.provider.module."
    )
    step.validator.save(
        update_fields=["availability_state", "availability_message"],
    )

    def must_not_download(file: OpenAIFileInput) -> DownloadedFile:
        """Fail loudly if an unrunnable workflow reaches the network boundary."""

        del file
        pytest.fail("The downloader ran for an unavailable workflow.")

    monkeypatch.setattr(
        "validibot.mcp_server.server.download_openai_file",
        must_not_download,
    )

    with pytest.raises(MCPApplicationError) as unavailable:
        _start_validation_from_openai_file(
            user=user,
            workflow_ref=build_workflow_reference(workflow),
            file=OpenAIFileInput(
                download_url="https://files.openai.example/temporary",
                file_id="file-unavailable-workflow-test",
            ),
            idempotency_key="unavailable-workflow-download-test",
        )

    assert unavailable.value.code == MCPErrorCode.WORKFLOW_UNAVAILABLE
    assert "configured validators is unavailable" in unavailable.value.detail
    assert "ValidatorConfig" not in unavailable.value.detail


def test_total_result_guard_rejects_an_oversized_future_projection(settings) -> None:
    """A later schema expansion must not bypass the aggregate response ceiling."""

    settings.MCP_MAX_RESPONSE_BYTES = 8

    with pytest.raises(MCPApplicationError) as rejected:
        _ensure_bounded_result({"untrusted": "long result"})

    assert rejected.value.code == MCPErrorCode.INTERNAL_ERROR


def test_protocol_hides_denied_reference_details(
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    """Tool errors must expose a stable code without IDs or stack details."""

    settings.SITE_URL = ISSUER_URL
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE_URL
    settings.ALLOWED_HOSTS = ["localhost"]
    user = UserFactory(is_superuser=True)
    _accept_test_bearer(monkeypatch, user_id=user.pk)

    async def exercise_denial() -> None:
        """Call a real generated tool with a deliberately invalid reference."""

        app = build_mcp_asgi_application()
        transport = httpx2.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=transport,
                base_url=ISSUER_URL,
                headers={"Authorization": f"Bearer {TEST_BEARER}"},
            ) as http_client,
            streamable_http_client(
                RESOURCE_URL,
                http_client=http_client,
                terminate_on_close=False,
            ) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            denied = await session.call_tool(
                "get_workflow",
                {"workflow_ref": "not-a-valid-reference"},
            )

        assert denied.is_error
        assert isinstance(denied.content[0], TextContent)
        assert "NOT_FOUND: Workflow not found." in denied.content[0].text
        assert "Traceback" not in denied.content[0].text
        assert "workflow_id" not in denied.content[0].text

    anyio.run(exercise_denial)

    audit = AuditLogEntry.objects.get(
        action=AuditAction.MCP_TOOL_CALLED.value,
        actor__user=user,
    )
    assert audit.metadata["outcome"] == "denied"
    assert audit.metadata["error_code"] == "NOT_FOUND"


def test_protocol_audits_the_shared_read_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    """A cross-tool quota denial must travel over MCP and remain auditable."""

    settings.SITE_URL = ISSUER_URL
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE_URL
    settings.ALLOWED_HOSTS = ["localhost"]
    settings.MCP_READS_PER_MINUTE = 1
    org = OrganizationFactory(mcp_allowed=True)
    user = UserFactory(is_superuser=True, orgs=[org])
    WorkflowFactory(org=org, user=user, mcp_enabled=True)
    _accept_test_bearer(monkeypatch, user_id=user.pk)

    async def exercise_limit() -> None:
        """Spend the read budget on discovery, then switch read operations."""

        app = build_mcp_asgi_application()
        transport = httpx2.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=transport,
                base_url=ISSUER_URL,
                headers={"Authorization": f"Bearer {TEST_BEARER}"},
            ) as http_client,
            streamable_http_client(
                RESOURCE_URL,
                http_client=http_client,
                terminate_on_close=False,
            ) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            catalog = await session.call_tool("list_workflows", {})
            workflow_ref = catalog.structured_content["workflows"][0]["workflow_ref"]
            limited = await session.call_tool(
                "get_workflow",
                {"workflow_ref": workflow_ref},
            )

        assert limited.is_error
        assert isinstance(limited.content[0], TextContent)
        assert "RATE_LIMITED" in limited.content[0].text

    anyio.run(exercise_limit)

    audits = list(
        AuditLogEntry.objects.filter(
            action=AuditAction.MCP_TOOL_CALLED.value,
            actor__user=user,
        ).order_by("occurred_at", "pk"),
    )
    assert [entry.metadata["outcome"] for entry in audits] == ["allowed", "denied"]
    assert audits[-1].metadata["error_code"] == "RATE_LIMITED"
