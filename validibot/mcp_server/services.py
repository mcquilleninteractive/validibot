"""Transport-neutral application services exposed by the MCP adapter.

The official MCP SDK calls these services directly. Authorization remains in
the canonical Django querysets and workflow launch policy, so adding an MCP
transport does not create a second permission implementation.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from http import HTTPStatus
from typing import TYPE_CHECKING

from django.conf import settings
from django.core import signing
from django.db.models import Count
from django.db.models import Q

from validibot.core.features import CommercialFeature
from validibot.core.features import is_feature_enabled
from validibot.core.idempotency import claim_idempotency_key
from validibot.core.idempotency import complete_idempotency_key
from validibot.mcp_server.constants import MCP_DEFAULT_FILE_MAX_BYTES
from validibot.mcp_server.constants import MCP_DEFAULT_PAGE_SIZE
from validibot.mcp_server.constants import MCP_MAX_FINDING_MESSAGE_LENGTH
from validibot.mcp_server.constants import MCP_MAX_FINDING_PATH_LENGTH
from validibot.mcp_server.constants import MCP_MAX_PAGE_SIZE
from validibot.mcp_server.constants import MCP_MAX_RESULT_CODE_LENGTH
from validibot.mcp_server.constants import MCP_MAX_RESULT_NAME_LENGTH
from validibot.mcp_server.constants import MCP_MAX_STEP_TEXT_LENGTH
from validibot.mcp_server.constants import MCP_MAX_WORKFLOW_DESCRIPTION_LENGTH
from validibot.mcp_server.constants import MCP_MAX_WORKFLOW_STEPS
from validibot.mcp_server.constants import MCPErrorCode
from validibot.mcp_server.exceptions import MCPApplicationError
from validibot.mcp_server.references import build_run_reference
from validibot.mcp_server.references import build_workflow_reference
from validibot.mcp_server.references import parse_run_reference
from validibot.mcp_server.references import parse_workflow_reference
from validibot.mcp_server.schemas import StartValidationInput
from validibot.mcp_server.schemas import StartValidationResult
from validibot.mcp_server.schemas import ValidationFindingListResult
from validibot.mcp_server.schemas import ValidationFindingResult
from validibot.mcp_server.schemas import ValidationRunResult
from validibot.mcp_server.schemas import WorkflowDetailResult
from validibot.mcp_server.schemas import WorkflowListResult
from validibot.mcp_server.schemas import WorkflowStepSummary
from validibot.mcp_server.schemas import WorkflowSummary
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationRunSource
from validibot.validations.exceptions import OrgPolicyDeniedError
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunService
from validibot.workflows.constants import WorkflowStartErrorCode
from validibot.workflows.models import Workflow
from validibot.workflows.version_utils import get_latest_workflow_ids
from validibot.workflows.views_launch_helpers import LaunchValidationError
from validibot.workflows.views_launch_helpers import ensure_launch_preconditions
from validibot.workflows.views_launch_helpers import handle_raw_body_mode

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from validibot.users.models import Organization
    from validibot.users.models import User

_CURSOR_SALT = "validibot.mcp.cursor.v1"

_WORKFLOW_READINESS_ERROR_DETAILS: dict[str, str] = {
    WorkflowStartErrorCode.WORKFLOW_INACTIVE.value: "This workflow is not active.",
    WorkflowStartErrorCode.NO_WORKFLOW_STEPS.value: (
        "This workflow cannot run because it has no validation steps."
    ),
    WorkflowStartErrorCode.VALIDATOR_UNAVAILABLE.value: (
        "This workflow cannot run because one of its configured validators is "
        "unavailable. Update the workflow to use an available validator, or ask "
        "an administrator to restore the required validator version."
    ),
}


def list_workflows(
    *,
    user: User,
    search: str | None = None,
    cursor: str | None = None,
    page_size: int = MCP_DEFAULT_PAGE_SIZE,
) -> WorkflowListResult:
    """Return one bounded page of workflows available to an MCP principal."""

    _ensure_mcp_feature()
    size = _validated_page_size(page_size)
    offset = _decode_cursor(cursor)
    queryset = _accessible_workflows(user=user)
    cleaned_search = (search or "").strip()
    if cleaned_search:
        queryset = queryset.filter(
            Q(name__icontains=cleaned_search)
            | Q(description__icontains=cleaned_search),
        )
    rows = list(
        queryset.order_by("name", "org__slug", "slug", "pk")[
            offset : offset + size + 1
        ],
    )
    has_more = len(rows) > size
    page = rows[:size]
    return WorkflowListResult(
        workflows=[_workflow_summary(workflow) for workflow in page],
        next_cursor=_encode_cursor(offset + size) if has_more else None,
    )


def get_workflow(*, user: User, workflow_ref: str) -> WorkflowDetailResult:
    """Return launch-relevant detail for one authorized workflow."""

    _ensure_mcp_feature()
    workflow = _resolve_workflow(user=user, workflow_ref=workflow_ref)
    step_rows = list(
        workflow.steps.select_related("validator", "action").order_by("order")[
            : MCP_MAX_WORKFLOW_STEPS + 1
        ],
    )
    steps = []
    for step in step_rows[:MCP_MAX_WORKFLOW_STEPS]:
        operation = ""
        if step.validator_id:
            validator = step.validator
            operation = validator.name if validator is not None else ""
        elif step.action_id:
            action = step.action
            operation = action.name if action is not None else ""
        steps.append(
            WorkflowStepSummary(
                order=step.order,
                name=_bounded_untrusted_text(
                    step.name or operation,
                    MCP_MAX_RESULT_NAME_LENGTH,
                ),
                description=_bounded_untrusted_text(
                    step.description,
                    MCP_MAX_STEP_TEXT_LENGTH,
                ),
                operation=_bounded_untrusted_text(
                    operation,
                    MCP_MAX_RESULT_NAME_LENGTH,
                ),
            ),
        )
    summary = _workflow_summary(workflow)
    return WorkflowDetailResult(
        **summary.model_dump(),
        steps=steps,
        steps_truncated=len(step_rows) > MCP_MAX_WORKFLOW_STEPS,
    )


def authorize_validation_start(*, user: User, workflow_ref: str) -> None:
    """Prove launch permission and readiness before any attachment is fetched."""

    _ensure_mcp_feature()
    workflow = _resolve_workflow(user=user, workflow_ref=workflow_ref)
    try:
        ensure_launch_preconditions(workflow=workflow, user=user)
    except LaunchValidationError as exc:
        raise _mcp_error_from_launch_validation_error(exc) from exc
    except (OrgPolicyDeniedError, PermissionError) as exc:
        raise MCPApplicationError(
            MCPErrorCode.LAUNCH_DENIED,
            "This workflow cannot be launched for the current user.",
        ) from exc


def _mcp_error_from_launch_validation_error(
    exc: LaunchValidationError,
) -> MCPApplicationError:
    """Translate canonical launch failures into safe, truthful MCP errors."""

    workflow_error_code = str(exc.payload.get("code") or "")
    if workflow_error_code == WorkflowStartErrorCode.PERMISSION_DENIED.value:
        return MCPApplicationError(
            MCPErrorCode.PERMISSION_DENIED,
            "You do not have permission to run this workflow.",
        )

    readiness_detail = _WORKFLOW_READINESS_ERROR_DETAILS.get(workflow_error_code)
    if readiness_detail is not None:
        return MCPApplicationError(
            MCPErrorCode.WORKFLOW_UNAVAILABLE,
            readiness_detail,
        )

    detail = str(exc.payload.get("detail") or "The validation input was rejected.")
    return MCPApplicationError(MCPErrorCode.INVALID_INPUT, detail)


def resolve_audit_organization(
    *,
    user: User,
    workflow_ref: str = "",
    run_ref: str = "",
) -> Organization | None:
    """Resolve audit scope only through rows the principal may already view."""

    if run_ref:
        try:
            run_id = parse_run_reference(run_ref)
        except ValueError:
            return None
        validation_run = (
            ValidationRun.objects.for_user(user)
            .select_related("org")
            .filter(pk=run_id)
            .first()
        )
        return validation_run.org if validation_run is not None else None
    if workflow_ref:
        try:
            org_slug, workflow_slug = parse_workflow_reference(workflow_ref)
        except ValueError:
            return None
        workflow = (
            Workflow.objects.for_user(user)
            .select_related("org")
            .filter(org__slug=org_slug, slug=workflow_slug)
            .first()
        )
        return workflow.org if workflow is not None else None
    return None


def start_validation(
    *,
    user: User,
    launch: StartValidationInput,
) -> StartValidationResult:
    """Create one retry-safe validation run using the normal launch policy."""

    _ensure_mcp_feature()
    workflow = _resolve_workflow(user=user, workflow_ref=launch.workflow_ref)
    file_bytes = _validated_file_content(launch.file_content)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "content_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "content_type": launch.content_type,
                "file_name": launch.file_name,
                "workflow_ref": launch.workflow_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    ).hexdigest()
    decision = claim_idempotency_key(
        org=workflow.org,
        key=launch.idempotency_key,
        endpoint=f"mcp.start_validation.user.{user.pk}",
        request_hash=fingerprint,
    )
    if decision.action == "replay" and decision.key_record is not None:
        if not isinstance(decision.key_record.response_body, dict):
            raise MCPApplicationError(
                MCPErrorCode.INTERNAL_ERROR,
                "The stored retry result is unavailable.",
            )
        stored = StartValidationResult.model_validate(decision.key_record.response_body)
        return stored.model_copy(update={"idempotency_replayed": True})
    if decision.action == "conflict":
        raise MCPApplicationError(
            MCPErrorCode.IDEMPOTENCY_IN_PROGRESS,
            "A matching validation launch is still being processed. Retry shortly.",
        )
    if decision.action == "hash_mismatch":
        raise MCPApplicationError(
            MCPErrorCode.IDEMPOTENCY_KEY_REUSED,
            "Use a new idempotency key when validation inputs change.",
        )

    key_record = decision.key_record
    try:
        ensure_launch_preconditions(workflow=workflow, user=user)
        submission_build = handle_raw_body_mode(
            workflow=workflow,
            user=user,
            project=workflow.project,
            content_type_header=launch.content_type,
            body_bytes=file_bytes,
            headers={
                "Content-Type": launch.content_type,
                "X-Filename": launch.file_name,
            },
        )
        launch_result = ValidationRunService().launch(
            request=None,
            actor=user,
            org=workflow.org,
            workflow=workflow,
            submission=submission_build.submission,
            metadata=submission_build.metadata,
            extra=submission_build.extra,
            user_id=user.pk,
            source=ValidationRunSource.MCP,
        )
        result = StartValidationResult(
            **_run_result(launch_result.validation_run).model_dump(),
            idempotency_replayed=False,
        )
    except LaunchValidationError as exc:
        if key_record is not None:
            key_record.delete()
        raise _mcp_error_from_launch_validation_error(exc) from exc
    except (OrgPolicyDeniedError, PermissionError) as exc:
        if key_record is not None:
            key_record.delete()
        raise MCPApplicationError(
            MCPErrorCode.LAUNCH_DENIED,
            "This workflow cannot be launched for the current user.",
        ) from exc
    except Exception:
        if key_record is not None:
            key_record.delete()
        raise

    if key_record is not None:
        complete_idempotency_key(
            key_record=key_record,
            response_body=result.model_dump(mode="json"),
            response_status=HTTPStatus.ACCEPTED,
            validation_run=launch_result.validation_run,
        )
    return result


def get_validation_run(*, user: User, run_ref: str) -> ValidationRunResult:
    """Return current status for one run visible to the MCP principal."""

    _ensure_mcp_feature()
    return _run_result(_resolve_run(user=user, run_ref=run_ref))


def list_validation_findings(
    *,
    user: User,
    run_ref: str,
    severity: str | None = None,
    cursor: str | None = None,
    page_size: int = MCP_DEFAULT_PAGE_SIZE,
) -> ValidationFindingListResult:
    """Return one bounded findings page for an authorized validation run."""

    _ensure_mcp_feature()
    validation_run = _resolve_run(user=user, run_ref=run_ref)
    if validation_run.status not in VALIDATION_RUN_TERMINAL_STATUSES:
        raise MCPApplicationError(
            MCPErrorCode.RUN_NOT_COMPLETE,
            "Findings are available after the validation run completes.",
        )
    size = _validated_page_size(page_size)
    offset = _decode_cursor(cursor)
    queryset = ValidationFinding.objects.filter(validation_run=validation_run)
    cleaned_severity = (severity or "").strip().upper()
    if cleaned_severity:
        allowed = {choice.value for choice in Severity}
        if cleaned_severity not in allowed:
            raise MCPApplicationError(
                MCPErrorCode.INVALID_INPUT,
                "Severity must be SUCCESS, INFO, WARNING, or ERROR.",
            )
        queryset = queryset.filter(severity=cleaned_severity)
    rows = list(
        queryset.select_related("validation_step_run__workflow_step").order_by("pk")[
            offset : offset + size + 1
        ],
    )
    has_more = len(rows) > size
    findings = [
        ValidationFindingResult(
            severity=finding.severity,
            code=_bounded_untrusted_text(
                finding.code,
                MCP_MAX_RESULT_CODE_LENGTH,
            ),
            message=_bounded_untrusted_text(
                finding.message,
                MCP_MAX_FINDING_MESSAGE_LENGTH,
            ),
            path=_bounded_untrusted_text(
                finding.path,
                MCP_MAX_FINDING_PATH_LENGTH,
            ),
            step_name=_bounded_untrusted_text(
                finding.validation_step_run.workflow_step.name,
                MCP_MAX_RESULT_NAME_LENGTH,
            ),
        )
        for finding in rows[:size]
    ]
    return ValidationFindingListResult(
        run_ref=build_run_reference(validation_run),
        findings=findings,
        next_cursor=_encode_cursor(offset + size) if has_more else None,
    )


def _ensure_mcp_feature() -> None:
    """Reject direct service use when the installed license lacks MCP."""

    if not is_feature_enabled(CommercialFeature.MCP_SERVER):
        raise MCPApplicationError(
            MCPErrorCode.PERMISSION_DENIED,
            "MCP is unavailable for this deployment.",
        )


def _accessible_workflows(*, user: User) -> QuerySet[Workflow]:
    """Apply canonical identity access plus both MCP channel guardrails."""

    eligible = Workflow.objects.for_user(user).filter(
        is_active=True,
        is_archived=False,
        is_tombstoned=False,
        mcp_enabled=True,
        org__mcp_allowed=True,
    )
    latest_ids = get_latest_workflow_ids(eligible)
    return Workflow.objects.filter(pk__in=latest_ids).select_related("org")


def _resolve_workflow(*, user: User, workflow_ref: str) -> Workflow:
    """Resolve a workflow without disclosing malformed or denied references."""

    try:
        org_slug, workflow_slug = parse_workflow_reference(workflow_ref)
    except ValueError as exc:
        raise MCPApplicationError(
            MCPErrorCode.NOT_FOUND,
            "Workflow not found.",
        ) from exc
    workflow = (
        _accessible_workflows(user=user)
        .filter(
            org__slug=org_slug,
            slug=workflow_slug,
        )
        .first()
    )
    if workflow is None:
        raise MCPApplicationError(MCPErrorCode.NOT_FOUND, "Workflow not found.")
    return workflow


def _resolve_run(*, user: User, run_ref: str) -> ValidationRun:
    """Resolve a run through the canonical row-level visibility policy."""

    try:
        run_id = parse_run_reference(run_ref)
    except ValueError as exc:
        raise MCPApplicationError(MCPErrorCode.NOT_FOUND, "Run not found.") from exc
    validation_run = (
        ValidationRun.objects.for_user(user)
        .select_related("workflow", "workflow__org")
        .filter(
            pk=run_id,
            workflow__mcp_enabled=True,
            workflow__org__mcp_allowed=True,
        )
        .first()
    )
    if validation_run is None:
        raise MCPApplicationError(MCPErrorCode.NOT_FOUND, "Run not found.")
    return validation_run


def _workflow_summary(workflow: Workflow) -> WorkflowSummary:
    """Project a workflow without organization or database identifiers."""

    return WorkflowSummary(
        workflow_ref=build_workflow_reference(workflow),
        name=_bounded_untrusted_text(workflow.name, MCP_MAX_RESULT_NAME_LENGTH),
        description=_bounded_untrusted_text(
            workflow.description,
            MCP_MAX_WORKFLOW_DESCRIPTION_LENGTH,
        ),
        version=workflow.version,
        allowed_file_types=[
            _bounded_untrusted_text(value, MCP_MAX_RESULT_CODE_LENGTH)
            for value in list(workflow.allowed_file_types or [])[:MCP_MAX_PAGE_SIZE]
        ],
    )


def _bounded_untrusted_text(value: object, max_length: int) -> str:
    """Normalize control characters and cap user/validator-authored result text."""

    cleaned = "".join(
        character
        for character in str(value or "")
        if unicodedata.category(character) not in {"Cc", "Cf"}
        or character in {"\n", "\t"}
    )
    if len(cleaned) <= max_length:
        return cleaned
    return f"{cleaned[: max_length - 1]}…"


def _run_result(validation_run: ValidationRun) -> ValidationRunResult:
    """Project a run and bounded aggregate counts without file content."""

    aggregate = ValidationFinding.objects.filter(
        validation_run=validation_run,
    ).aggregate(
        total=Count("pk"),
        errors=Count("pk", filter=Q(severity=Severity.ERROR)),
        warnings=Count("pk", filter=Q(severity=Severity.WARNING)),
        info=Count("pk", filter=Q(severity=Severity.INFO)),
    )
    total_findings = aggregate["total"] or 0
    is_terminal = validation_run.status in VALIDATION_RUN_TERMINAL_STATUSES
    if not is_terminal:
        next_action = "get_validation_run"
    elif total_findings:
        next_action = "list_validation_findings"
    else:
        next_action = "complete"
    return ValidationRunResult(
        run_ref=build_run_reference(validation_run),
        workflow_ref=build_workflow_reference(validation_run.workflow),
        status=validation_run.status,
        created_at=validation_run.created.isoformat(),
        started_at=(
            validation_run.started_at.isoformat() if validation_run.started_at else None
        ),
        ended_at=(
            validation_run.ended_at.isoformat() if validation_run.ended_at else None
        ),
        total_findings=total_findings,
        error_count=aggregate["errors"] or 0,
        warning_count=aggregate["warnings"] or 0,
        info_count=aggregate["info"] or 0,
        findings_available=is_terminal and total_findings > 0,
        next_action=next_action,
    )


def _validated_file_content(file_bytes: bytes) -> bytes:
    """Defensively enforce the file bound at the application-service boundary."""

    limit = int(
        getattr(
            settings,
            "MCP_FILE_MAX_BYTES",
            MCP_DEFAULT_FILE_MAX_BYTES,
        ),
    )
    if not file_bytes:
        raise MCPApplicationError(MCPErrorCode.INVALID_INPUT, "The file is empty.")
    if len(file_bytes) > limit:
        raise MCPApplicationError(
            MCPErrorCode.FILE_TOO_LARGE,
            f"The file exceeds the {limit}-byte MCP limit.",
        )
    return file_bytes


def _validated_page_size(page_size: int) -> int:
    """Reject rather than silently widening a model-controlled query."""

    if page_size < 1 or page_size > MCP_MAX_PAGE_SIZE:
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            f"page_size must be between 1 and {MCP_MAX_PAGE_SIZE}.",
        )
    return page_size


def _encode_cursor(offset: int) -> str:
    """Return a tamper-evident pagination cursor."""

    return signing.dumps({"offset": offset}, salt=_CURSOR_SALT, compress=True)


def _decode_cursor(cursor: str | None) -> int:
    """Validate a pagination cursor and return its non-negative offset."""

    if cursor is None:
        return 0
    try:
        payload = signing.loads(cursor, salt=_CURSOR_SALT)
        offset = int(payload["offset"])
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
        ) from exc
    if offset < 0:
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
        )
    return offset
