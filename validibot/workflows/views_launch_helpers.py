import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpRequest
from django.urls import reverse
from django.utils.translation import gettext_lazy
from rest_framework.response import Response as APIResponse

from validibot.core.site_settings import MetadataPolicyError
from validibot.core.site_settings import get_site_settings
from validibot.projects.models import Project
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.ingest import prepare_inline_text
from validibot.submissions.ingest import prepare_uploaded_file
from validibot.submissions.models import Submission
from validibot.submissions.models import SubmissionInputFile
from validibot.users.models import User
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import ValidationRunSource
from validibot.validations.exceptions import OrgPolicyDeniedError
from validibot.validations.serializers import ValidationRunSerializer
from validibot.validations.services.validation_run import ValidationRunLaunchResults
from validibot.validations.services.validation_run import ValidationRunService
from validibot.workflows.constants import SUPPORTED_CONTENT_TYPES
from validibot.workflows.constants import WorkflowStartErrorCode
from validibot.workflows.constants import preferred_content_type_for_file
from validibot.workflows.models import Workflow
from validibot.workflows.request_utils import SubmissionRequestMode
from validibot.workflows.request_utils import detect_mode
from validibot.workflows.request_utils import extract_request_basics
from validibot.workflows.services.submitted_file_ports import (
    submitted_file_port_requirements,
)
from validibot.workflows.views_helpers import describe_workflow_file_type_violation
from validibot.workflows.views_helpers import resolve_submission_file_type

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


@dataclass
class SubmissionBuild:
    """Simple container describing a submission ready for launch."""

    submission: Submission
    metadata: dict[str, Any]
    extra: dict[str, Any] | None = None


class LaunchValidationError(Exception):
    """Raised when we cannot build a submission from the incoming request."""

    def __init__(
        self,
        *,
        detail: str,
        code: str,
        status_code: int,
        errors: list[dict[str, Any]] | None = None,
    ):
        super().__init__(detail)
        payload: dict[str, Any] = {"detail": detail, "code": code}
        if errors is not None:
            payload["errors"] = errors
        self.payload = payload
        self.status_code = status_code


def _resolve_submission_retention(workflow: Workflow) -> str:
    """Resolve the submission retention policy from the workflow.

    Every Submission row created by a launch path must snapshot
    the workflow's ``input_retention`` onto its own
    ``retention_policy`` column. The purge job acts on
    ``submission.retention_policy``, not on the workflow's policy,
    so without this snapshot a workflow configured to retain inputs
    would still have its submissions queued for purge.

    This helper is the single source of truth for that mapping.
    Every Submission constructor in launch helpers must pass
    ``retention_policy=_resolve_submission_retention(workflow)``.

    Returns the stringified retention value (TextChoices values are
    strings) so the caller doesn't have to import the enum.
    Empty/null ``input_retention`` falls back to the privacy-
    preserving default (DO_NOT_STORE) — losing data is preferable
    to leaking it.
    """
    raw = workflow.input_retention or ""
    raw = raw.strip()
    if not raw:
        return SubmissionRetention.DO_NOT_STORE
    # Validate against the enum so a typo'd value can't sneak
    # through. If the workflow has a value the enum doesn't
    # recognise, that's a workflow-level configuration bug; we
    # fail closed (DO_NOT_STORE) rather than silently storing.
    try:
        return SubmissionRetention(raw)
    except ValueError:
        return SubmissionRetention.DO_NOT_STORE


def enforce_metadata_policy(metadata, site_settings):
    """Ensure payload metadata abides by the configured policy."""

    metadata = dict(metadata or {})
    site_settings.enforce_metadata_policy(metadata)
    return metadata


def launch_web_validation_run(
    *,
    submission_build: SubmissionBuild,
    request: HttpRequest,
    workflow: Workflow,
) -> ValidationRunLaunchResults:
    """Launches a workflow run initiated through the HTML form.

    Args:
        request: Django request used to build redirect URLs and capture the actor.
        workflow: Workflow that should be executed.
        submission_build: Pre-built submission and metadata for the run.

    Returns:
        ValidationRunLaunchResults: Launch metadata, including the ValidationRun.
    """

    service = ValidationRunService()
    return service.launch(
        request=request,
        org=workflow.org,
        workflow=workflow,
        submission=submission_build.submission,
        metadata=submission_build.metadata,
        extra=submission_build.extra,
        user_id=getattr(request.user, "id", None),
        source=ValidationRunSource.LAUNCH_PAGE,
    )


def launch_api_validation_run(
    *,
    request: HttpRequest,
    workflow: Workflow,
    submission_build: SubmissionBuild,
    source: ValidationRunSource = ValidationRunSource.API,
) -> APIResponse:
    """Launches a workflow run initiated by the REST API.

    ``source`` must be passed explicitly by the *route* that called
    this helper.  Each entry point (regular REST API, MCP helper API,
    future CLI helper) knows its own provenance and passes it here.
    We do NOT derive source from a request header, because headers
    are caller-controlled and a generic API caller could otherwise
    mint runs that look MCP-originated.

    Args:
        request: DRF/Django request received by the API endpoint.
        workflow: Workflow requested by the caller.
        submission_build: Submission and metadata prepared for launch.
        source: The trusted source for this launch. Routes that
            handle the regular REST API pass ``API`` (the default);
            the MCP helper API route passes ``MCP``; the CLI passes
            ``CLI`` once that distinction is wired through;
            scheduled tasks pass ``SCHEDULE``. The ``LAUNCH_PAGE``
            source is reserved for the web form's helper
            (``launch_web_validation_run``) and is not accepted here.

    Returns:
        APIResponse: Serializer payload, headers, and status for the run request.
    """

    # Defensive guard: reject the LAUNCH_PAGE source on the API path.
    # Routes can't accidentally pass it, but mistakes happen.
    if source == ValidationRunSource.LAUNCH_PAGE:
        msg = (
            "launch_api_validation_run does not accept LAUNCH_PAGE as a source; "
            "the web form should call launch_web_validation_run instead."
        )
        raise ValueError(msg)

    service = ValidationRunService()
    try:
        launch_result: ValidationRunLaunchResults = service.launch(
            request=request,
            org=workflow.org,
            workflow=workflow,
            submission=submission_build.submission,
            metadata=submission_build.metadata,
            # Carry run-level extras (e.g. short_description) through the API
            # path too — previously only the web path forwarded these, so an
            # API-supplied short_description was silently dropped (ADR-2026-06-03b).
            extra=submission_build.extra,
            user_id=getattr(request.user, "id", None),
            source=source,
        )
    except OrgPolicyDeniedError as exc:
        # The caller IS permitted to run this workflow (so we don't hide it
        # behind a 404 like a true permission failure), but an org policy
        # blocked the launch — billing not set up, quota/credits exhausted,
        # rate limited. Return 403 with the policy's own reason and a
        # machine-readable code so API clients can react. The reason is a
        # developer-authored gettext string, never user input.
        detail = str(exc) or gettext_lazy(
            "Your organization can't run this workflow right now.",
        )
        payload = {
            "detail": detail,
            "code": WorkflowStartErrorCode.ORG_POLICY_DENIED.value,
        }
        return APIResponse(payload, status=HTTPStatus.FORBIDDEN)
    except PermissionError:
        payload = {
            "detail": gettext_lazy("You do not have permission to run this workflow."),
        }
        return APIResponse(payload, status=HTTPStatus.NOT_FOUND)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Run service errored for workflow %s", workflow.pk)
        payload = {
            "detail": gettext_lazy("Could not run the workflow. Please try again."),
        }
        return APIResponse(payload, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    validation_run: ValidationRun = launch_result.validation_run
    data = ValidationRunSerializer(validation_run).data
    per_attempt = int(getattr(settings, "VALIDATION_START_ATTEMPT_TIMEOUT", 5))
    # Use org-scoped route for the Location header.
    location = request.build_absolute_uri(
        reverse(
            "api:org-runs-detail",
            kwargs={"org_slug": workflow.org.slug, "pk": validation_run.id},
        ),
    )
    headers = {"Location": location}
    if validation_run.status not in VALIDATION_RUN_TERMINAL_STATUSES:
        data["url"] = location
        data["poll"] = location
        headers["Retry-After"] = str(per_attempt)

    status_code = launch_result.status or HTTPStatus.CREATED
    return APIResponse(data=data, status=status_code, headers=headers)


def handle_raw_body_mode(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    content_type_header: str,
    body_bytes: bytes,
    headers: dict[str, str],
) -> SubmissionBuild:
    """
    Process Mode 1 (raw body) submission.
    """
    encoding = headers.get("Content-Encoding")
    full_ct = headers.get("Content-Type", "")
    filename = headers.get("X-Filename") or ""
    raw = body_bytes
    max_inline = getattr(settings, "SUBMISSION_INLINE_MAX_BYTES", 10_000_000)
    if len(raw) > max_inline:
        err_msg = gettext_lazy("Payload too large.")
        raise LaunchValidationError(
            detail=err_msg,
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=413,
        )

    if encoding:
        if encoding.lower() != "base64":
            raise LaunchValidationError(
                detail=gettext_lazy("Unsupported Content-Encoding (only base64)."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=400,
            )
        try:
            raw = base64.b64decode(raw, validate=True)
        except Exception as e:
            raise LaunchValidationError(
                detail=gettext_lazy("Invalid base64 payload."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=400,
            ) from e

    ct_norm = (content_type_header or "").split(";")[0].strip().lower()
    file_type = SUPPORTED_CONTENT_TYPES.get(ct_norm)
    if not file_type:
        err_msg = gettext_lazy("Unsupported Content-Type '%s'. ") % full_ct
        err_msg += gettext_lazy("Supported : %s") % ", ".join(
            SUPPORTED_CONTENT_TYPES.keys(),
        )
        raise LaunchValidationError(
            detail=err_msg,
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=400,
        )

    filename_file_type = resolve_submission_file_type(
        requested=file_type,
        filename=filename,
    )
    is_binary_body = filename_file_type in {
        SubmissionFileType.PDF,
        SubmissionFileType.BINARY,
    }
    text_content: str | None = None
    if not is_binary_body:
        charset = "utf-8"
        if ";" in full_ct:
            for part in full_ct.split(";")[1:]:
                k, _, v = part.partition("=")
                if k.strip().lower() == "charset" and v.strip():
                    charset = v.strip()
                    break
        try:
            text_content = raw.decode(charset)
        except UnicodeDecodeError:
            text_content = raw.decode("utf-8", errors="replace")

    resolved_file_type = resolve_submission_file_type(
        requested=file_type,
        filename=filename,
        inline_text=text_content,
    )
    is_binary_body = resolved_file_type in {
        SubmissionFileType.PDF,
        SubmissionFileType.BINARY,
    }
    violation = describe_workflow_file_type_violation(
        workflow=workflow,
        file_type=resolved_file_type,
    )
    if violation:
        raise LaunchValidationError(
            detail=violation,
            code=WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED,
            status_code=400,
            errors=[],
        )
    ingest_content_type = ct_norm
    if resolved_file_type != file_type:
        ingest_content_type = preferred_content_type_for_file(
            resolved_file_type,
            filename=filename,
        )

    binary_upload: ContentFile | None = None
    if is_binary_body:
        binary_upload = ContentFile(raw, name=filename or "document")
        ingest = prepare_uploaded_file(
            uploaded_file=binary_upload,
            filename=filename,
            content_type=ingest_content_type,
            max_bytes=max_inline,
        )
        safe_filename = ingest.filename
    else:
        assert text_content is not None
        safe_filename, ingest = prepare_inline_text(
            text=text_content,
            filename=filename,
            content_type=ingest_content_type,
            deny_magic_on_text=True,
        )

    submission = Submission(
        org=workflow.org,
        workflow=workflow,
        user=user,
        project=project,
        name=safe_filename,
        checksum_sha256=ingest.sha256,
        metadata={},
        # Snapshot workflow.input_retention onto
        # submission.retention_policy so the purge job (which acts
        # on the submission's policy) honours what the workflow
        # promised.
        retention_policy=_resolve_submission_retention(workflow),
    )
    if is_binary_body:
        assert binary_upload is not None
        submission.set_content(
            uploaded_file=binary_upload,
            filename=safe_filename,
            file_type=resolved_file_type,
        )
        submission.checksum_sha256 = ingest.sha256
    else:
        submission.set_content(
            inline_text=text_content,
            filename=safe_filename,
            file_type=resolved_file_type,
        )

    with transaction.atomic():
        submission.full_clean()
        submission.save()
        return SubmissionBuild(submission=submission, metadata={})


def process_structured_payload(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    payload: Any,
    serializer_factory: Callable[..., Any],
    submission_settings=None,
) -> SubmissionBuild:
    """
    Shared serializer handling for JSON envelope and multipart submissions.
    """
    if hasattr(payload, "copy"):  # noqa : SIM108
        payload = payload.copy()
    else:
        payload = dict(payload or {})
    payload["workflow"] = workflow.pk
    serializer = serializer_factory(data=payload)
    serializer.is_valid(raise_exception=True)

    vd = serializer.validated_data
    file_obj = vd.get("file", None)
    if file_obj is not None:
        max_file = getattr(settings, "SUBMISSION_FILE_MAX_BYTES", 1_000_000_000)
        if getattr(file_obj, "size", 0) > max_file:
            raise LaunchValidationError(
                detail=gettext_lazy("File too large."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=413,
            )

    submission_settings = submission_settings or get_site_settings()
    metadata = vd.get("metadata") or {}
    try:
        metadata = enforce_metadata_policy(metadata, submission_settings)
    except MetadataPolicyError as exc:
        raise LaunchValidationError(
            detail=gettext_lazy("Invalid request payload."),
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=400,
            errors=[
                {
                    "field": "metadata",
                    "message": str(exc),
                },
            ],
        ) from exc

    if vd.get("file") is not None:
        file_obj = vd["file"]
        ct = vd["content_type"]
        filename_value = vd.get("filename") or getattr(file_obj, "name", "") or ""
        resolved_file_type = resolve_submission_file_type(
            requested=vd["file_type"],
            filename=filename_value,
        )
        violation = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type=resolved_file_type,
        )
        if violation:
            raise LaunchValidationError(
                detail=violation,
                code=WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED,
                status_code=400,
                errors=[],
            )
        if resolved_file_type != vd["file_type"]:
            ct = preferred_content_type_for_file(
                resolved_file_type,
                filename=filename_value,
            )
        max_file = getattr(settings, "SUBMISSION_FILE_MAX_BYTES", 1_000_000_000)
        ingest = prepare_uploaded_file(
            uploaded_file=file_obj,
            filename=filename_value,
            content_type=ct,
            max_bytes=max_file,
        )
        safe_filename = ingest.filename

        submission = Submission(
            org=workflow.org,
            workflow=workflow,
            user=user if getattr(user, "is_authenticated", False) else None,
            project=project,
            name=safe_filename,
            # Persist the submitter-supplied metadata (already policy-enforced
            # above). This branch previously hard-coded ``{}``, silently
            # dropping metadata on every multipart file upload — which broke
            # SUBMISSION_METADATA → ``i.*`` input bindings (e.g. EnergyPlus's
            # ``expected_floor_area_m2``) for file uploads, even though the
            # inline-content branch below persisted it correctly. The file
            # branch stores the checksum in its own ``checksum_sha256`` column,
            # so unlike the inline branch we do NOT fold ``sha256`` into here.
            metadata=metadata,
            checksum_sha256=ingest.sha256,
            # Snapshot retention from the workflow — see
            # _resolve_submission_retention for rationale.
            retention_policy=_resolve_submission_retention(workflow),
        )
        submission.set_content(
            uploaded_file=file_obj,
            filename=safe_filename,
            file_type=resolved_file_type,
        )

    elif vd.get("normalized_content") is not None:
        ct = vd["content_type"]
        filename_value = vd.get("filename") or ""
        resolved_file_type = resolve_submission_file_type(
            requested=vd["file_type"],
            filename=filename_value,
            inline_text=vd["normalized_content"],
        )
        violation = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type=resolved_file_type,
        )
        if violation:
            raise LaunchValidationError(
                detail=violation,
                code=WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED,
                status_code=400,
                errors=[],
            )
        if resolved_file_type != vd["file_type"]:
            ct = preferred_content_type_for_file(
                resolved_file_type,
                filename=filename_value,
            )
        safe_filename, ingest = prepare_inline_text(
            text=vd["normalized_content"],
            filename=filename_value,
            content_type=ct,
            deny_magic_on_text=True,
        )
        metadata = {**metadata, "sha256": ingest.sha256}
        try:
            metadata = enforce_metadata_policy(metadata, submission_settings)
        except MetadataPolicyError as exc:
            raise LaunchValidationError(
                detail=gettext_lazy("Invalid request payload."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=400,
                errors=[
                    {
                        "field": "metadata",
                        "message": str(exc),
                    },
                ],
            ) from exc

        submission = Submission(
            org=workflow.org,
            workflow=workflow,
            user=user if getattr(user, "is_authenticated", False) else None,
            project=project,
            name=safe_filename,
            metadata=metadata,
            # Snapshot retention from the workflow — see
            # _resolve_submission_retention for rationale.
            retention_policy=_resolve_submission_retention(workflow),
        )
        submission.set_content(
            inline_text=vd["normalized_content"],
            filename=safe_filename,
            file_type=resolved_file_type,
        )
    else:
        raise LaunchValidationError(
            detail=gettext_lazy("No content provided."),
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=400,
        )

    # short_description is surfaced to assertions as
    # ``submission.short_description`` (ADR-2026-06-03b). On the API it is a
    # TRUSTED-SETTER field: the caller holds a token scoped to a user permitted
    # to launch this workflow, so it is persisted ungated — unlike the web form,
    # which gates it behind ``allow_submission_short_description``. It lands on
    # the ValidationRun (not the Submission) via the launch service's ``extra``.
    run_extra: dict[str, Any] = {}
    short_description = (vd.get("short_description") or "").strip()
    if short_description:
        run_extra["short_description"] = short_description

    with transaction.atomic():
        submission.full_clean()
        submission.save()
        return SubmissionBuild(
            submission=submission,
            metadata=metadata,
            extra=run_extra or None,
        )


def handle_json_envelope(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    envelope: dict[str, Any],
    submission_settings,
    serializer_factory: Callable[..., Any],
) -> SubmissionBuild:
    """Handle Mode 2 (JSON envelope) submission."""
    payload = dict(envelope or {})
    return process_structured_payload(
        workflow=workflow,
        user=user,
        project=project,
        payload=payload,
        serializer_factory=serializer_factory,
        submission_settings=submission_settings,
    )


def handle_multipart_mode(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    payload,
    submission_settings,
    serializer_factory: Callable[..., Any],
) -> SubmissionBuild:
    """Handle Mode 3 (multipart/form-data) submissions.

    Args:
        workflow: Workflow accepting the submission.
        user: Authenticated user (may be anonymous for API token uses).
        project: Optional project the submission should be associated with.
        payload: Raw multipart payload dict (request.data).
        submission_settings: System submission policy settings.
        serializer_factory: Callable returning the DRF serializer for validation.

    Returns:
        SubmissionBuild: Persisted submission and metadata bundle.
    """
    return process_structured_payload(
        workflow=workflow,
        user=user,
        project=project,
        payload=payload,
        serializer_factory=serializer_factory,
        submission_settings=submission_settings,
    )


def build_submission_from_form(
    *,
    request: HttpRequest,
    workflow: Workflow,
    cleaned_data: dict[str, Any],
) -> SubmissionBuild:
    """Persist a submission from validated WorkflowLaunchForm data."""

    ensure_launch_preconditions(workflow=workflow, user=request.user)

    payload = cleaned_data.get("payload")
    attachment = cleaned_data.get("attachment")
    requested_file_type = cleaned_data["file_type"]
    filename = cleaned_data.get("filename") or ""
    metadata = cleaned_data.get("metadata") or {}
    short_description = cleaned_data.get("short_description") or ""
    attachment_name = getattr(attachment, "name", "") if attachment else ""
    detection_input_name = filename or attachment_name or "document"
    final_file_type = resolve_submission_file_type(
        requested=requested_file_type,
        filename=detection_input_name,
        inline_text=payload,
    )
    violation = describe_workflow_file_type_violation(
        workflow=workflow,
        file_type=final_file_type,
    )
    if violation:
        raise ValidationError(violation)
    content_type = preferred_content_type_for_file(
        final_file_type,
        filename=detection_input_name,
    )

    submission = Submission(
        org=workflow.org,
        workflow=workflow,
        user=request.user if getattr(request.user, "is_authenticated", False) else None,
        project=workflow.project,
        name="",
        metadata=metadata,
        checksum_sha256="",
        # Snapshot retention from the workflow — see
        # _resolve_submission_retention for rationale.
        retention_policy=_resolve_submission_retention(workflow),
    )

    run_kwargs: dict[str, Any] = {}
    if workflow.allow_submission_short_description and short_description:
        run_kwargs["short_description"] = short_description

    if attachment:
        max_file = int(settings.SUBMISSION_FILE_MAX_BYTES)
        ingest = prepare_uploaded_file(
            uploaded_file=attachment,
            filename=filename,
            content_type=content_type,
            max_bytes=max_file,
        )
        safe_filename = ingest.filename
        submission.name = safe_filename
        submission.checksum_sha256 = ingest.sha256
        submission.set_content(
            uploaded_file=attachment,
            filename=safe_filename,
            file_type=final_file_type,
        )
    else:
        safe_filename, ingest = prepare_inline_text(
            text=payload,
            filename=filename,
            content_type=content_type,
            deny_magic_on_text=True,
        )
        submission.name = safe_filename
        submission.checksum_sha256 = ingest.sha256
        submission.set_content(
            inline_text=payload,
            filename=safe_filename,
            file_type=final_file_type,
        )

    with transaction.atomic():
        submission.full_clean()
        submission.save()
        for req in submitted_file_port_requirements(workflow):
            uploaded = cleaned_data.get(req.field_name)
            if not uploaded:
                continue
            port_file = SubmissionInputFile(
                submission=submission,
                workflow_step_id=req.workflow_step_id,
                port_key=req.port_key,
            )
            port_file.set_file(
                uploaded_file=uploaded,
                filename=getattr(uploaded, "name", "") or req.label,
            )
            port_file.full_clean()
            port_file.save()
    return SubmissionBuild(submission=submission, metadata=metadata, extra=run_kwargs)


def build_submission_from_api(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    request: HttpRequest,
    serializer_factory: Callable[..., Any],
    multipart_payload=None,
    submission_settings=None,
) -> SubmissionBuild:
    """Builds a Submission object for API-driven workflow launches.

    The helper normalizes the incoming payload (raw body, JSON envelope, or
    multipart) and persists the resulting Submission for the launch service.

    Args:
        workflow: Workflow targeted by the request.
        user: User initiating the run.
        project: Project context resolved for the workflow.
        request: DRF/Django request carrying the submission payload.
        serializer_factory: Factory that yields the DRF serializer used to
            validate structured payloads (required for API invocations).
        multipart_payload: Callable returning multipart data when request is not
            available (mainly for tests).
        submission_settings: Optional overrides for metadata policy enforcement.

    Returns:
        SubmissionBuild: Saved submission and metadata ready for launch.
    """

    ensure_launch_preconditions(workflow=workflow, user=user)

    headers = {key: value for key, value in request.headers.items()}
    content_type_header, body_bytes = extract_request_basics(request)

    detection_result = detect_mode(
        request=None,
        content_type_header=content_type_header,
        body_bytes=body_bytes,
    )
    if detection_result.has_error:
        error_message = detection_result.error or gettext_lazy(
            "Invalid request payload.",
        )
        logger.warning(
            "Submission mode detection failed",
            extra={
                "workflow_id": workflow.pk,
                "content_type": detection_result.content_type,
                "error": detection_result.error,
            },
        )
        raise LaunchValidationError(
            detail=gettext_lazy("Invalid request payload."),
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=400,
            errors=[
                {
                    "field": "content",
                    "message": error_message,
                },
            ],
        )

    submission_settings = submission_settings or get_site_settings()

    if detection_result.mode == SubmissionRequestMode.RAW_BODY:
        return handle_raw_body_mode(
            workflow=workflow,
            user=user,
            project=project,
            content_type_header=content_type_header,
            body_bytes=body_bytes,
            headers=headers,
        )

    if detection_result.mode == SubmissionRequestMode.JSON_ENVELOPE:
        return handle_json_envelope(
            workflow=workflow,
            user=user,
            project=project,
            envelope=detection_result.parsed_envelope or {},
            submission_settings=submission_settings,
            serializer_factory=serializer_factory,
        )

    if detection_result.mode == SubmissionRequestMode.MULTIPART:
        payload_value = (
            multipart_payload() if callable(multipart_payload) else multipart_payload
        )
        return handle_multipart_mode(
            workflow=workflow,
            user=user,
            project=project,
            payload=payload_value or {},
            submission_settings=submission_settings,
            serializer_factory=serializer_factory,
        )

    logger.warning(
        "Unsupported submission mode detected",
        extra={
            "workflow_id": workflow.pk,
            "content_type": detection_result.content_type,
        },
    )
    raise LaunchValidationError(
        detail=gettext_lazy("Unsupported request content type."),
        code=WorkflowStartErrorCode.INVALID_PAYLOAD,
        status_code=400,
    )


def ensure_launch_preconditions(*, workflow: Workflow, user: User) -> None:
    """Shared entry point for workflow readiness + permission checks."""

    ensure_workflow_ready_for_launch(workflow)
    ensure_user_can_launch_workflow(workflow=workflow, user=user)


def ensure_workflow_ready_for_launch(workflow: Workflow) -> None:
    """
    Guard conditions that prevent a workflow from running.
    """

    if not workflow.is_active:
        raise LaunchValidationError(
            detail="",
            code=WorkflowStartErrorCode.WORKFLOW_INACTIVE,
            status_code=HTTPStatus.CONFLICT,
        )
    if not workflow.steps.exists():
        raise LaunchValidationError(
            detail=gettext_lazy(
                "This workflow has no steps defined and cannot be executed."
            ),
            code=WorkflowStartErrorCode.NO_WORKFLOW_STEPS,
            status_code=HTTPStatus.BAD_REQUEST,
        )
    unavailable_step = workflow.first_unavailable_validator_step()
    if unavailable_step is not None:
        validator = unavailable_step.validator
        detail = gettext_lazy(
            "Step %(step)s (%(validator)s) uses a validator that is not "
            "available in this deployment. %(reason)s"
        ) % {
            "step": unavailable_step.step_number_display,
            "validator": validator.name,
            "reason": validator.runtime_unavailable_reason(),
        }
        raise LaunchValidationError(
            detail=detail,
            code=WorkflowStartErrorCode.VALIDATOR_UNAVAILABLE,
            status_code=HTTPStatus.CONFLICT,
        )


def ensure_user_can_launch_workflow(*, workflow: Workflow, user: User) -> None:
    """
    Ensure the user can execute the workflow before creating submissions.

    Uses workflow.can_execute() which checks:
    - Public workflows (any authenticated user)
    - Org membership with WORKFLOW_LAUNCH permission
    - Active WorkflowAccessGrant (guest access)
    """
    if not workflow.can_execute(user=user):
        raise LaunchValidationError(
            detail=gettext_lazy("You do not have permission to run this workflow."),
            code=WorkflowStartErrorCode.PERMISSION_DENIED,
            status_code=HTTPStatus.FORBIDDEN,
        )
