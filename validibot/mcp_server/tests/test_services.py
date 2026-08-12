"""Integration tests for direct Django services behind the MCP tools.

The protocol adapter is intentionally thin. These tests exercise canonical
workflow and run authorization, privacy-minimizing projections, pagination,
finding filters, file validation, and database-backed launch idempotency using
real Django models and the normal validation launch service.
"""

import pytest

from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import get_license
from validibot.core.license import set_license
from validibot.mcp_server.constants import MCPErrorCode
from validibot.mcp_server.exceptions import MCPApplicationError
from validibot.mcp_server.references import build_run_reference
from validibot.mcp_server.references import build_workflow_reference
from validibot.mcp_server.schemas import StartValidationInput
from validibot.mcp_server.services import get_validation_run
from validibot.mcp_server.services import get_workflow
from validibot.mcp_server.services import list_validation_findings
from validibot.mcp_server.services import list_workflows
from validibot.mcp_server.services import start_validation
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRun
from validibot.validations.tests.factories import ValidationFindingFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db

EXPECTED_FINDING_COUNT = 2


@pytest.fixture(autouse=True)
def _licensed_mcp_services():
    """Exercise service behavior behind the same Pro feature as production."""

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


def _accessible_workflow(*, user=None, name="MCP Workflow"):
    """Build a workflow satisfying identity and both MCP channel gates."""

    org = OrganizationFactory(mcp_allowed=True)
    principal = user or UserFactory(orgs=[org])
    grant_role(principal, org, RoleCode.EXECUTOR)
    workflow = WorkflowFactory(
        org=org,
        user=principal,
        name=name,
        description="Validate a JSON document.",
        mcp_enabled=True,
    )
    return principal, workflow


def test_direct_service_invocation_repeats_the_pro_feature_gate() -> None:
    """An internal caller must not bypass the route-level commercial gate."""

    user = UserFactory()
    original_license = get_license()
    try:
        set_license(License(edition=Edition.COMMUNITY))
        with pytest.raises(MCPApplicationError) as denied:
            list_workflows(user=user)
    finally:
        set_license(original_license)

    assert denied.value.code == MCPErrorCode.PERMISSION_DENIED


def test_workflow_discovery_applies_gates_search_and_pagination() -> None:
    """Discovery must be useful yet never widen canonical MCP access."""

    user, first = _accessible_workflow(name="Alpha validator")
    second = WorkflowFactory(
        org=first.org,
        user=user,
        name="Beta validator",
        mcp_enabled=True,
    )
    WorkflowFactory(
        org=first.org,
        user=user,
        name="Hidden validator",
        mcp_enabled=False,
    )

    first_page = list_workflows(user=user, page_size=1)
    second_page = list_workflows(
        user=user,
        page_size=1,
        cursor=first_page.next_cursor,
    )
    searched = list_workflows(user=user, search="Beta")

    assert len(first_page.workflows) == 1
    assert first_page.next_cursor is not None
    assert len(second_page.workflows) == 1
    assert second_page.next_cursor is None
    assert {item.name for item in [*first_page.workflows, *second_page.workflows]} == {
        first.name,
        second.name,
    }
    assert [item.name for item in searched.workflows] == [second.name]


def test_workflow_detail_returns_bounded_steps_without_tenant_identity() -> None:
    """Detail should guide a launch without exposing organization routing data."""

    user, workflow = _accessible_workflow()
    step = WorkflowStepFactory(
        workflow=workflow,
        order=10,
        name="Schema check",
        description="Check required fields.",
    )

    result = get_workflow(
        user=user,
        workflow_ref=build_workflow_reference(workflow),
    )

    assert result.name == workflow.name
    assert result.steps[0].name == step.name
    assert result.steps[0].operation == step.validator.name
    assert workflow.org.slug not in result.model_dump_json()
    assert set(result.model_dump()) == {
        "workflow_ref",
        "name",
        "description",
        "version",
        "allowed_file_types",
        "steps",
    }


def test_run_status_and_findings_follow_row_visibility_and_cursor_contract() -> None:
    """Run tools must authorize first and return only bounded finding fields."""

    user, workflow = _accessible_workflow()
    validation_run = ValidationRunFactory(
        workflow=workflow,
        org=workflow.org,
        project=workflow.project,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )
    step_run = ValidationStepRunFactory(
        validation_run=validation_run,
        workflow_step__workflow=workflow,
        workflow_step__name="Schema check",
    )
    ValidationFindingFactory(
        validation_step_run=step_run,
        severity=Severity.ERROR,
        code="required",
        message="A required field is missing.",
        meta={"private": "must not be projected"},
    )
    ValidationFindingFactory(
        validation_step_run=step_run,
        severity=Severity.WARNING,
        code="format",
        message="A value has an unusual format.",
    )
    run_ref = build_run_reference(validation_run)

    status = get_validation_run(user=user, run_ref=run_ref)
    first_page = list_validation_findings(user=user, run_ref=run_ref, page_size=1)
    second_page = list_validation_findings(
        user=user,
        run_ref=run_ref,
        page_size=1,
        cursor=first_page.next_cursor,
    )
    errors = list_validation_findings(
        user=user,
        run_ref=run_ref,
        severity="error",
    )

    assert status.total_findings == EXPECTED_FINDING_COUNT
    assert status.error_count == 1
    assert status.warning_count == 1
    assert first_page.next_cursor is not None
    assert second_page.next_cursor is None
    assert len(errors.findings) == 1
    assert errors.findings[0].code == "required"
    assert "private" not in errors.model_dump_json()

    outsider = UserFactory()
    with pytest.raises(MCPApplicationError) as denied:
        get_validation_run(user=outsider, run_ref=run_ref)
    assert denied.value.code == MCPErrorCode.NOT_FOUND


def test_start_validation_is_database_idempotent_for_exact_retries(monkeypatch) -> None:
    """A client retry must return one run and consume launch policy only once."""

    user, workflow = _accessible_workflow()
    user.is_superuser = True
    user.save(update_fields=["is_superuser"])
    WorkflowStepFactory(workflow=workflow)

    def do_not_dispatch(*, validation_run_id, user_id) -> None:
        """Leave the admitted run pending while testing launch semantics."""

        assert validation_run_id
        assert user_id == user.pk

    monkeypatch.setattr(
        "validibot.core.tasks.enqueue_validation_run",
        do_not_dispatch,
    )
    launch = StartValidationInput(
        workflow_ref=build_workflow_reference(workflow),
        file_name="payload.json",
        content_type="application/json",
        file_content=b"{}",
        idempotency_key="chatgpt-request-1",
    )

    first = start_validation(user=user, launch=launch)
    second = start_validation(user=user, launch=launch)

    assert first.idempotency_replayed is False
    assert second.idempotency_replayed is True
    assert second.run_ref == first.run_ref
    assert (
        ValidationRun.objects.filter(
            workflow=workflow,
            user=user,
            source=ValidationRunSource.MCP,
        ).count()
        == 1
    )

    changed = launch.model_copy(
        update={"file_content": b'{"changed":true}'},
    )
    with pytest.raises(MCPApplicationError) as reused:
        start_validation(user=user, launch=changed)
    assert reused.value.code == MCPErrorCode.IDEMPOTENCY_KEY_REUSED


def test_start_validation_rejects_empty_and_oversized_files(settings) -> None:
    """Downloaded bytes must remain bounded at the application-service boundary."""

    user, workflow = _accessible_workflow()
    workflow_ref = build_workflow_reference(workflow)
    empty = StartValidationInput.model_construct(
        workflow_ref=workflow_ref,
        file_name="payload.json",
        content_type="application/json",
        file_content=b"",
        idempotency_key="invalid-file",
    )

    with pytest.raises(MCPApplicationError) as empty_error:
        start_validation(user=user, launch=empty)
    assert empty_error.value.code == MCPErrorCode.INVALID_INPUT

    settings.MCP_FILE_MAX_BYTES = 2
    oversized = empty.model_copy(
        update={
            "file_content": b"three",
            "idempotency_key": "oversized-file",
        },
    )
    with pytest.raises(MCPApplicationError) as oversized_error:
        start_validation(user=user, launch=oversized)
    assert oversized_error.value.code == MCPErrorCode.FILE_TOO_LARGE


def test_findings_wait_for_a_terminal_run() -> None:
    """Clients must poll run status instead of reading partial findings."""

    user, workflow = _accessible_workflow()
    validation_run = ValidationRunFactory(
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.RUNNING,
    )

    with pytest.raises(MCPApplicationError) as incomplete:
        list_validation_findings(
            user=user,
            run_ref=build_run_reference(validation_run),
        )

    assert incomplete.value.code == MCPErrorCode.RUN_NOT_COMPLETE
