"""
Cloud Run Job launcher service.

This module orchestrates the complete flow of triggering Cloud Run Jobs:
1. Upload submission and envelope to GCS
2. Trigger Cloud Run Job directly via Jobs API
3. Return ValidationResult with pending status

Architecture:
    Web -> Cloud Run Job (this module) -> Callback to worker

Design: Simple function-based orchestration. No complex state management.

Note: Template preprocessing (resolving parameterized IDF templates into
concrete IDF files) happens earlier in the pipeline — in
``AdvancedValidator.validate()`` via ``preprocess_submission()`` — before
the submission reaches this module.  By the time the launcher sees the
submission, ``Submission.get_content()`` already returns a fully resolved
IDF, regardless of whether the original was a template or a direct upload.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone
from validibot_shared.canonicalization import sha256_hex_for_model

from validibot.validations.constants import PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES
from validibot.validations.constants import CloudRunJobStatus
from validibot.validations.constants import Severity
from validibot.validations.services.attempt_paths import attempt_bundle_relpath
from validibot.validations.services.cloud_run.envelope_builder import (
    build_input_envelope,
)
from validibot.validations.services.cloud_run.gcs_client import upload_envelope
from validibot.validations.services.cloud_run.gcs_client import upload_envelope_local
from validibot.validations.services.cloud_run.gcs_client import upload_file
from validibot.validations.services.cloud_run.job_client import (
    get_execution_image_digest,
)
from validibot.validations.services.cloud_run.job_client import get_job_configured_image
from validibot.validations.services.cloud_run.job_client import run_validator_job
from validibot.validations.services.create_only_storage import create_local_bytes
from validibot.validations.services.create_only_storage import create_local_directory
from validibot.validations.services.execution_evidence import (
    build_input_evidence_snapshot,
)
from validibot.validations.services.file_identity import local_bytes_identity
from validibot.validations.services.image_policy import ValidatorBackendImagePolicy
from validibot.validations.services.image_policy import enforce_image_policy
from validibot.validations.services.image_policy import get_current_policy
from validibot.validations.services.submission_file_ports import (
    upload_submitted_input_files_to_gcs,
)
from validibot.validations.validators.base import ValidationIssue
from validibot.validations.validators.base import ValidationResult
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.pdf.validator import PDF_MAX_SUBMISSION_BYTES

if TYPE_CHECKING:
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun
    from validibot.validations.models import Validator
    from validibot.validations.services.execution_attempts import (
        AttemptCallbackCredentials,
    )
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)

VALIDATION_CALLBACK_PATH = "/api/v1/validation-callbacks/"
VALIDATION_STORAGE_CAPABILITY_REFRESH_PATH = (
    "/api/v1/validation-storage-capabilities/refresh/"
)


@dataclass(frozen=True, slots=True)
class AttemptExecutionBundle:
    """Storage locations reserved for one execution attempt.

    ``local_dir`` is populated only for the local asynchronous development
    path. Production Cloud Run dispatch uses the GCS URI and leaves it unset.
    """

    execution_bundle_uri: str
    input_envelope_uri: str
    local_dir: Path | None

    @property
    def is_gcs(self) -> bool:
        """Return whether this bundle uses Google Cloud Storage."""
        return self.execution_bundle_uri.startswith("gs://")


def _active_attempt_for_step(step_run):
    """Return the step's active attempt or fail before storage preparation."""
    from validibot.validations.services.execution_attempts import (
        get_active_execution_attempt,
    )

    attempt = get_active_execution_attempt(step_run)
    if attempt is None:
        msg = f"Validation step {step_run.pk} has no active execution attempt"
        raise RuntimeError(msg)
    return attempt


def _attempt_execution_bundle(
    *,
    run,
    step_run,
    require_gcs: bool = False,
) -> AttemptExecutionBundle:
    """Derive the canonical storage prefix for the step's active attempt.

    Each retry has a new attempt UUID and therefore receives a new physical
    prefix. Run-level retention remains unchanged because all attempt bundles
    stay below ``runs/<org>/<run>/``.
    """
    attempt = _active_attempt_for_step(step_run)
    relpath = attempt_bundle_relpath(
        org_id=str(run.org_id),
        run_id=str(run.pk),
        attempt_id=str(attempt.pk),
    )
    bucket = getattr(settings, "GCS_VALIDATION_BUCKET", "")
    if bucket:
        execution_bundle_uri = f"gs://{bucket}/{relpath.as_posix()}"
        return AttemptExecutionBundle(
            execution_bundle_uri=execution_bundle_uri,
            input_envelope_uri=f"{execution_bundle_uri}/input.json",
            local_dir=None,
        )

    if require_gcs:
        msg = "GCS_VALIDATION_BUCKET is required for this Cloud Run validator"
        raise RuntimeError(msg)

    local_dir = Path(settings.MEDIA_ROOT) / "files" / Path(*relpath.parts)
    create_local_directory(local_dir)
    return AttemptExecutionBundle(
        execution_bundle_uri=str(local_dir),
        input_envelope_uri=str(local_dir / "input.json"),
        local_dir=local_dir,
    )


def _resolve_cloud_run_job_name(validation_type: str) -> str:
    """Return the Cloud Run Job name for a validator.

    Reads from ``ValidatorConfig.cloud_run_job_name`` (which, by
    project convention, equals the validator's container image name
    — see ``ValidatorConfig.image_name`` docstring), then applies the
    deployment's ``GCP_APP_NAME`` prefix and non-prod ``VALIDIBOT_STAGE``
    suffix to match the GCP deploy recipes. Raises a clear ``RuntimeError``
    when the validator isn't registered or its config doesn't yield a job
    name; the caller catches this as a launch-blocking misconfiguration rather
    than letting an empty string reach the GCP Jobs API.

    Replaces the legacy ``settings.GCS_ENERGYPLUS_JOB_NAME`` /
    ``GCS_FMU_JOB_NAME`` env-var reads (removed May 2026 — the env
    vars were unset in production for months and silently broke runs,
    see the post-mortem in this commit). Sourcing the name from the
    validator config means deployers can't get the env var and the
    deploy convention out of sync.
    """
    config = get_config(validation_type)
    if config is None:
        msg = (
            f"No ValidatorConfig registered for validation_type="
            f"{validation_type!r}. Cannot resolve a Cloud Run Job name "
            "without a config — check that the validator's config.py is "
            "imported during app startup (see validators/__init__.py)."
        )
        raise RuntimeError(msg)
    job_name = config.cloud_run_job_name
    if not job_name:
        msg = (
            f"ValidatorConfig for {validation_type!r} resolved to an "
            "empty Cloud Run Job name. Check that the config sets "
            "image_name OR that validation_type is non-empty so the "
            "fallback convention 'validibot-validator-backend-{slug}' "
            "can apply."
        )
        raise RuntimeError(msg)
    app_name = getattr(settings, "GCP_APP_NAME", None) or os.environ.get(
        "GCP_APP_NAME",
        "validibot",
    )
    if app_name != "validibot" and job_name.startswith("validibot-"):
        job_name = f"{app_name}-{job_name.removeprefix('validibot-')}"

    stage = getattr(settings, "VALIDIBOT_STAGE", None) or os.environ.get(
        "VALIDIBOT_STAGE",
        "prod",
    )
    if stage and stage != "prod" and not job_name.endswith(f"-{stage}"):
        job_name = f"{job_name}-{stage}"
    return job_name


def build_validation_callback_url() -> str:
    """
    Build the fully-qualified validation callback URL.

    In Cloud Run, validator containers POST their results back to Django via the
    worker service's internal API. We keep this separate from `SITE_URL` so that
    production can use a custom public domain (e.g., `validibot.com`) while
    callbacks still route to the IAM-protected worker `*.run.app` URL.

    Returns:
        Fully-qualified callback URL, e.g.
        `https://validibot-worker-xyz.a.run.app/api/v1/validation-callbacks/`.

    Raises:
        ValueError: If neither WORKER_URL nor SITE_URL is set.
    """
    worker_url = (getattr(settings, "WORKER_URL", "") or "").strip()
    if worker_url:
        base_url = worker_url
    else:
        base_url = (getattr(settings, "SITE_URL", "") or "").strip()
        logger.warning(
            (
                "WORKER_URL is not set; falling back to SITE_URL=%s for validation "
                "callbacks. In Cloud Run multi-service deployments this is usually "
                "incorrect."
            ),
            base_url,
        )

    if not base_url:
        msg = (
            "WORKER_URL (preferred) or SITE_URL must be set to build validation "
            "callback URL."
        )
        raise ValueError(msg)

    return f"{base_url.rstrip('/')}{VALIDATION_CALLBACK_PATH}"


def build_validation_storage_capability_refresh_url() -> str:
    """Build the worker-only endpoint used to renew a bounded GCS token."""
    callback_url = build_validation_callback_url()
    return callback_url.removesuffix(VALIDATION_CALLBACK_PATH) + (
        VALIDATION_STORAGE_CAPABILITY_REFRESH_PATH
    )


def _prepare_attempt_capability_envelope(envelope, *, execution_bundle_uri: str):
    """Stage all GCS reads into the one prefix authorized for this attempt."""
    from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
        prepare_envelope_for_attempt_capability,
    )

    return prepare_envelope_for_attempt_capability(
        envelope,
        execution_bundle_uri=execution_bundle_uri,
    )


def _check_already_launched(step_run) -> ValidationResult | None:
    """Return a pending result if a job was already launched, else None."""
    existing_output = step_run.output or {}
    if existing_output.get("job_name"):
        logger.info(
            "Job already launched for step run %s (job=%s), skipping relaunch",
            step_run.id,
            existing_output.get("job_name"),
        )
        return ValidationResult(
            passed=None,
            issues=[],
            stats=existing_output,
        )
    return None


def _mark_step_run_running(
    step_run,
    *,
    image_digest: str | None = None,
    provider_execution_id: str = "",
) -> None:
    """Mark a step run as RUNNING with the current timestamp.

    Trust ADR Phase 5 Session A — when ``image_digest`` is provided,
    the validator backend image reference is persisted at launch time
    on the same save. ``None`` leaves the field at its default empty
    string (e.g. when digest resolution failed; we never want capture
    failures to break a run).
    """
    from datetime import UTC
    from datetime import datetime

    from validibot.validations.constants import StepStatus

    step_run.status = StepStatus.RUNNING
    step_run.started_at = datetime.now(UTC)
    update_fields = ["status", "started_at"]
    if image_digest:
        step_run.validator_backend_image_digest = image_digest
        update_fields.append("validator_backend_image_digest")
    step_run.save(update_fields=update_fields)
    if image_digest and provider_execution_id:
        from validibot.validations.services.execution_attempts import (
            record_execution_attempt_backend_image_digest,
        )

        record_execution_attempt_backend_image_digest(
            step_run_id=step_run.pk,
            provider_execution_id=provider_execution_id,
            backend_image_digest=image_digest,
        )


class ProviderDispatchAmbiguousError(RuntimeError):
    """The provider call may have been accepted but returned no identity."""


def _issue_callback_credentials_for_step(
    step_run: ValidationStepRun,
) -> AttemptCallbackCredentials:
    """Issue callback credentials for the step's one active attempt."""
    from validibot.validations.services.execution_attempts import (
        issue_attempt_callback_credentials,
    )

    attempt = _active_attempt_for_step(step_run)
    return issue_attempt_callback_credentials(attempt)


def _run_validator_job_safely(
    *,
    step_run,
    project_id: str,
    region: str,
    job_name: str,
    input_uri: str,
    execution_bundle_uri: str,
    input_envelope_sha256: str,
    input_evidence_snapshot: dict,
    output_envelope_uri: str,
    gcs_capability,
) -> str:
    """Launch once and preserve provider-acceptance ambiguity explicitly.

    The durable row is claimed immediately before the external API. If the call
    raises, the attempt becomes UNKNOWN and no delivery may launch it again.
    """
    from validibot.validations.constants import ExecutionAttemptState
    from validibot.validations.services.execution_attempts import (
        get_active_execution_attempt,
    )
    from validibot.validations.services.execution_attempts import (
        transition_execution_attempt,
    )

    attempt = get_active_execution_attempt(step_run)
    if attempt is None:
        msg = f"Validation step {step_run.pk} has no active execution attempt"
        raise RuntimeError(msg)

    if attempt.state != ExecutionAttemptState.PENDING:
        if attempt.state == ExecutionAttemptState.RUNNING and (
            attempt.provider_execution_id
        ):
            return attempt.provider_execution_id
        raise ProviderDispatchAmbiguousError(
            f"Execution attempt {attempt.pk} was already claimed"
        )

    attempt, claimed = transition_execution_attempt(
        attempt.pk,
        ExecutionAttemptState.DISPATCHING,
        provider_resource_name=(
            attempt.deployment.provider_resource_name
            if attempt.deployment is not None
            else job_name
        ),
        execution_bundle_uri=execution_bundle_uri,
        input_envelope_uri=input_uri,
        input_envelope_sha256=input_envelope_sha256,
        input_evidence_snapshot=input_evidence_snapshot,
        output_envelope_uri=output_envelope_uri,
    )
    if not claimed:
        if attempt.state == ExecutionAttemptState.RUNNING and (
            attempt.provider_execution_id
        ):
            return attempt.provider_execution_id
        raise ProviderDispatchAmbiguousError(
            f"Execution attempt {attempt.pk} was already claimed"
        )

    try:
        execution_name = run_validator_job(
            project_id=project_id,
            region=region,
            job_name=job_name,
            input_uri=input_uri,
            gcs_capability=gcs_capability,
        )
    except Exception as exc:
        transition_execution_attempt(
            attempt.pk,
            ExecutionAttemptState.UNKNOWN,
            last_error_code="provider_acceptance_unknown",
            last_error="Provider dispatch raised before returning execution identity.",
        )
        raise ProviderDispatchAmbiguousError(
            f"Provider acceptance is unknown for execution attempt {attempt.pk}"
        ) from exc

    transition_execution_attempt(
        attempt.pk,
        ExecutionAttemptState.RUNNING,
        provider_execution_id=execution_name,
        provider_accepted_at=timezone.now(),
    )
    return execution_name


def _enforce_cloud_run_job_image_policy(
    job_name: str,
    *,
    expected_image_digest: str | None = None,
) -> None:
    """Fail closed when a Cloud Run Job image violates deployment policy.

    GCP operators retain the same tag/digest/signed-digest choice as other
    deployments. Strict policies require both provider discovery and an
    immutable configured image before dispatch.
    """
    policy = get_current_policy()
    configured_image = get_job_configured_image(
        project_id=settings.GCP_PROJECT_ID,
        region=settings.GCP_REGION,
        job_name=job_name,
    )
    if policy != ValidatorBackendImagePolicy.TAG and configured_image is None:
        logger.warning(
            "Refusing to trigger Cloud Run Job %s: image lookup failed "
            "under strict policy '%s'",
            job_name,
            policy.value,
        )
        msg = (
            f"Validator image policy '{policy.value}' requires verifying the "
            f"Cloud Run Job's configured image, but the lookup for "
            f"'{job_name}' returned no image. Refusing to launch under strict "
            "policy. Check that the job exists, its service account can inspect "
            "it, and GCP_PROJECT_ID / GCP_REGION are correct."
        )
        raise RuntimeError(msg)

    if not configured_image:
        return

    if expected_image_digest and not configured_image.endswith(
        f"@{expected_image_digest}"
    ):
        msg = (
            f"Cloud Run Job '{job_name}' does not match its pinned deployment "
            "image digest. Refusing to dispatch."
        )
        raise RuntimeError(msg)

    policy_result = enforce_image_policy(configured_image)
    if policy_result.should_proceed:
        return

    logger.warning(
        "Refusing to trigger Cloud Run Job %s: %s",
        job_name,
        policy_result.message,
    )
    msg = (
        f"Cloud Run Job '{job_name}' violates validator image policy: "
        f"{policy_result.message}"
    )
    raise RuntimeError(msg)


def _managed_cloud_run_job_target(attempt) -> tuple[str, str, str, str] | None:
    """Read an exact managed Job target only from its immutable attempt snapshot."""
    if attempt is None or not getattr(attempt, "deployment_id", None):
        return None
    snapshot_data = getattr(attempt, "deployment_snapshot", None)
    if not isinstance(snapshot_data, dict) or not snapshot_data:
        raise RuntimeError(
            "Managed Cloud Run Job attempt has no immutable deployment snapshot."
        )
    from validibot.validations.constants import ExecutionDeploymentKind
    from validibot.validations.services.execution.deployment_schemas import (
        DeploymentRouteSnapshot,
    )

    try:
        snapshot = DeploymentRouteSnapshot.model_validate(snapshot_data)
    except ValueError as exc:
        raise RuntimeError(
            "Managed Cloud Run Job attempt has an invalid deployment snapshot."
        ) from exc
    if snapshot.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_JOB:
        raise RuntimeError(
            "Managed Cloud Run Job dispatch received a non-Job deployment snapshot."
        )
    parts = snapshot.provider_resource_name.split("/")
    if (
        len(parts) != 6  # noqa: PLR2004
        or parts[0] != "projects"
        or parts[2] != "locations"
        or parts[4] != "jobs"
        or not parts[1]
        or not parts[3]
        or not parts[5]
    ):
        raise RuntimeError(
            "Managed Cloud Run Job snapshot has an invalid provider resource name."
        )
    return (
        parts[1],
        parts[3],
        snapshot.provider_resource_name,
        snapshot.backend_image_digest,
    )


def _dispatch_cloud_run_validation(
    *,
    step_run: ValidationStepRun,
    job_name: str,
    input_envelope_uri: str,
    execution_bundle_uri: str,
    envelope,
    submission: Submission,
    step: WorkflowStep,
    expected_image_digest: str | None = None,
) -> tuple[str, str | None]:
    """Apply common launch policy, durable dispatch, and digest capture.

    Validator-specific launchers remain responsible for materializing inputs,
    building their typed envelope, and mapping domain errors. This helper owns
    the shared trust-boundary mechanics that must evolve in lockstep.
    """
    from validibot.validations.services.execution_attempts import (
        get_active_execution_attempt,
    )

    attempt = get_active_execution_attempt(step_run)
    managed_target = _managed_cloud_run_job_target(attempt)
    if managed_target is None:
        project_id = settings.GCP_PROJECT_ID
        region = settings.GCP_REGION
        pinned_digest = expected_image_digest
    else:
        project_id, region, job_name, pinned_digest = managed_target
    if pinned_digest:
        _enforce_cloud_run_job_image_policy(
            job_name,
            expected_image_digest=pinned_digest,
        )
    else:
        _enforce_cloud_run_job_image_policy(job_name)
    logger.info("Triggering Cloud Run Job: %s", job_name)
    from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
        issue_attempt_gcs_runtime_capability,
    )

    gcs_capability = issue_attempt_gcs_runtime_capability(
        execution_bundle_uri=execution_bundle_uri,
        project_id=project_id,
        refresh_url=build_validation_storage_capability_refresh_url(),
    )

    execution_name = _run_validator_job_safely(
        step_run=step_run,
        project_id=project_id,
        region=region,
        job_name=job_name,
        input_uri=input_envelope_uri,
        execution_bundle_uri=execution_bundle_uri,
        input_envelope_sha256=sha256_hex_for_model(envelope),
        input_evidence_snapshot=build_input_evidence_snapshot(
            envelope,
            submission=submission,
            step=step,
        ),
        output_envelope_uri=str(envelope.context.expected_output_uri),
        gcs_capability=gcs_capability,
    )
    backend_image_digest = get_execution_image_digest(execution_name)
    _mark_step_run_running(
        step_run,
        image_digest=backend_image_digest,
        provider_execution_id=execution_name,
    )
    return execution_name, backend_image_digest


def _ambiguous_dispatch_result() -> ValidationResult:
    """Keep the workflow pending until reconciliation or operator action."""
    return ValidationResult(
        passed=None,
        issues=[],
        stats={
            "job_status": CloudRunJobStatus.PENDING,
            "dispatch_state": "UNKNOWN",
        },
    )


def _attempt_requires_reconciliation(step_run) -> bool:
    """Return whether provider work was claimed before a later local error."""
    if step_run is None:
        return False
    from validibot.validations.constants import ExecutionAttemptState
    from validibot.validations.services.execution_attempts import (
        get_active_execution_attempt,
    )

    attempt = get_active_execution_attempt(step_run)
    return bool(
        attempt
        and attempt.state
        in {
            ExecutionAttemptState.DISPATCHING,
            ExecutionAttemptState.RUNNING,
            ExecutionAttemptState.UNKNOWN,
        }
    )


def launch_energyplus_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission: Submission,
    ruleset: Ruleset | None,
    step: WorkflowStep,
    job_name: str | None = None,
    expected_image_digest: str | None = None,
    provider_dispatch=None,
) -> ValidationResult:
    """
    Launch an EnergyPlus validation via Cloud Run Jobs.

    Uploads the submission (IDF or epJSON) to GCS, builds an input envelope,
    triggers the Cloud Run Job, and returns a pending ValidationResult.

    .. note::

       Template preprocessing (resolving ``$VARIABLE`` placeholders) happens
       earlier in ``AdvancedValidator.validate()`` via
       ``EnergyPlusValidator.preprocess_submission()``.  By the time this
       function is called, ``submission.get_content()`` already returns a
       fully resolved IDF — this function does not need to distinguish
       between template and direct-upload submissions.

    Args:
        run: ValidationRun instance (already created in PENDING status)
        validator: Validator instance
        submission: Submission instance with IDF/epJSON content
        ruleset: Ruleset instance (may be None for EnergyPlus)
        step: WorkflowStep instance with config including weather_file

    Returns:
        ValidationResult with passed=None (pending), issues=[], stats
        with job info.

    Raises:
        ValueError: If required config is missing (e.g., weather_file)
        Exception: If GCS upload or job trigger fails
    """
    current_step_run = None
    try:
        # 0. Idempotency check
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run found for ValidationRun {run.id}"
            raise ValueError(msg)  # noqa: TRY301

        already_launched = _check_already_launched(current_step_run)
        if already_launched:
            return already_launched

        # 1. Derive the GCS prefix for this exact execution attempt. Retries
        # receive a different prefix even though they belong to the same run.
        bundle = _attempt_execution_bundle(
            run=run,
            step_run=current_step_run,
            require_gcs=True,
        )
        execution_bundle_uri = bundle.execution_bundle_uri
        input_envelope_uri = bundle.input_envelope_uri

        # 2. Upload submission file to GCS.
        # After preprocessing, submission.get_content() returns the resolved
        # IDF (for template mode) or the original IDF/epJSON (for direct mode).
        # Determine file extension from the submission's filename.
        filename = submission.original_filename or "model.epjson"
        if filename.lower().endswith(".idf"):
            model_file_uri = f"{execution_bundle_uri}/model.idf"
            content_type = "text/plain"
        else:
            model_file_uri = f"{execution_bundle_uri}/model.epjson"
            content_type = "application/json"

        logger.info("Uploading submission to %s", model_file_uri)
        model_file = upload_file(
            content=submission.get_content().encode("utf-8"),
            uri=model_file_uri,
            content_type=content_type,
        )

        # 3. Issue one callback credential set for this attempt.
        callback_url = build_validation_callback_url()
        callback_credentials = _issue_callback_credentials_for_step(current_step_run)

        # 4. Build the typed input envelope exclusively through the validator's
        # declared file ports. ``primary_file_uri`` is the internal identity
        # supplied to the submission-file resolver for ``primary_model``.
        input_file_uris = {"primary_file_uri": model_file}
        input_file_uris.update(
            upload_submitted_input_files_to_gcs(
                submission=submission,
                step=step,
                execution_bundle_uri=execution_bundle_uri,
            )
        )
        envelope = build_input_envelope(
            run=run,
            callback_url=callback_url,
            callback_id=callback_credentials.callback_id,
            callback_nonce=callback_credentials.callback_nonce,
            callback_nonce_commitment=(callback_credentials.callback_nonce_commitment),
            execution_bundle_uri=execution_bundle_uri,
            input_file_uris=input_file_uris,
        )
        envelope = _prepare_attempt_capability_envelope(
            envelope,
            execution_bundle_uri=execution_bundle_uri,
        )

        # 5. Upload envelope to GCS
        logger.info("Uploading input envelope to %s", input_envelope_uri)
        upload_envelope(envelope, input_envelope_uri)

        # 6. Trigger Cloud Run Job directly via Jobs API.
        # Job name comes from ValidatorConfig (not from a per-validator
        # env var) so the deploy convention and the runtime lookup
        # can't drift — see _resolve_cloud_run_job_name.
        job_name = job_name or _resolve_cloud_run_job_name("ENERGYPLUS")

        dispatch = provider_dispatch or _dispatch_cloud_run_validation
        execution_name, backend_image_digest = dispatch(
            step_run=current_step_run,
            job_name=job_name,
            input_envelope_uri=input_envelope_uri,
            execution_bundle_uri=execution_bundle_uri,
            envelope=envelope,
            submission=submission,
            step=step,
            expected_image_digest=expected_image_digest,
        )
        logger.info(
            "Marked step run %s as RUNNING for run %s (image digest: %s)",
            current_step_run.id,
            run.id,
            backend_image_digest or "unresolved",
        )

        # 7. Return pending ValidationResult
        stats: dict[str, object] = {
            "job_status": CloudRunJobStatus.PENDING,
            "job_name": job_name,
            "execution_name": execution_name,
            "input_uri": input_envelope_uri,
            "execution_bundle_uri": execution_bundle_uri,
        }

        return ValidationResult(
            passed=None,  # Pending - will be updated by callback
            issues=[],
            stats=stats,
        )

    except ProviderDispatchAmbiguousError:
        logger.warning(
            "Cloud Run acceptance is unknown for EnergyPlus run %s; "
            "waiting for reconciliation or callback",
            run.id,
        )
        return _ambiguous_dispatch_result()
    except Exception:
        if _attempt_requires_reconciliation(current_step_run):
            logger.exception(
                "EnergyPlus launch follow-up failed after provider claim; "
                "preserving attempt for reconciliation"
            )
            return _ambiguous_dispatch_result()
        logger.exception("Failed to launch EnergyPlus Cloud Run Job for run %s", run.id)
        issues = [
            ValidationIssue(
                path="",
                message=(
                    "Failed to launch EnergyPlus validation. "
                    "The error has been logged for investigation."
                ),
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})


def launch_fmu_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission,
    ruleset,
    step: WorkflowStep,
    job_name: str | None = None,
    expected_image_digest: str | None = None,
    provider_dispatch=None,
) -> ValidationResult:
    """
    Launch an FMU validation via Cloud Run Jobs.

    Resolves inputs using catalog binding paths (or slug-name defaults), builds
    an FMU input envelope, uploads it, triggers the Cloud Run Job, and returns
    a pending ValidationResult.
    """
    current_step_run = None
    try:
        # 0. Idempotency check
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run for run {run.id}"
            raise ValueError(msg)  # noqa: TRY301

        already_launched = _check_already_launched(current_step_run)
        if already_launched:
            return already_launched

        # FMU URI resolution is handled by build_input_envelope(), which
        # checks for step-level FMU resources first, then falls back to
        # the library validator's FMU model.

        bundle = _attempt_execution_bundle(
            run=run,
            step_run=current_step_run,
        )
        execution_bundle_uri = bundle.execution_bundle_uri
        input_envelope_uri = bundle.input_envelope_uri

        callback_url = build_validation_callback_url()

        callback_credentials = _issue_callback_credentials_for_step(current_step_run)

        # Build envelope via the shared builder. This gets binding-aware
        # input resolution (StepInputBinding + resolve_path), proper
        # output_variables extraction from StepIODefinition, and audit
        # tracing via ResolvedInputTrace.
        from validibot.validations.services.cloud_run.envelope_builder import (
            build_input_envelope,
        )

        envelope = build_input_envelope(
            run=run,
            callback_url=callback_url,
            callback_id=callback_credentials.callback_id,
            callback_nonce=callback_credentials.callback_nonce,
            callback_nonce_commitment=(callback_credentials.callback_nonce_commitment),
            execution_bundle_uri=execution_bundle_uri,
        )
        envelope = _prepare_attempt_capability_envelope(
            envelope,
            execution_bundle_uri=execution_bundle_uri,
        )

        # Upload envelope
        if input_envelope_uri.startswith("gs://"):
            upload_envelope(envelope, input_envelope_uri)
        else:
            upload_envelope_local(envelope, Path(input_envelope_uri))

        # Trigger Cloud Run Job directly via Jobs API.
        # Job name comes from ValidatorConfig (not from a per-validator
        # env var) so the deploy convention and the runtime lookup
        # can't drift — see _resolve_cloud_run_job_name.
        job_name = job_name or _resolve_cloud_run_job_name("FMU")

        dispatch = provider_dispatch or _dispatch_cloud_run_validation
        execution_name, _ = dispatch(
            step_run=current_step_run,
            job_name=job_name,
            input_envelope_uri=input_envelope_uri,
            execution_bundle_uri=execution_bundle_uri,
            envelope=envelope,
            submission=submission,
            step=step,
            expected_image_digest=expected_image_digest,
        )

        stats: dict[str, object] = {
            "job_status": CloudRunJobStatus.PENDING,
            "job_name": job_name,
            "execution_name": execution_name,
            "input_uri": input_envelope_uri,
            "execution_bundle_uri": execution_bundle_uri,
            "output_values": {},  # available after callback processing
        }
        return ValidationResult(passed=None, issues=[], stats=stats)

    except ProviderDispatchAmbiguousError:
        logger.warning(
            "Cloud Run acceptance is unknown for FMU run %s; "
            "waiting for reconciliation or callback",
            run.id,
        )
        return _ambiguous_dispatch_result()
    except Exception:
        if _attempt_requires_reconciliation(current_step_run):
            logger.exception(
                "FMU launch follow-up failed after provider claim; "
                "preserving attempt for reconciliation"
            )
            return _ambiguous_dispatch_result()
        logger.exception("Failed to launch FMU Cloud Run Job for run %s", run.id)
        issues = [
            ValidationIssue(
                path="",
                message=(
                    "Failed to launch FMU validation. "
                    "The error has been logged for investigation."
                ),
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})


def launch_shacl_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission: Submission,
    ruleset: Ruleset | None,
    step: WorkflowStep,
    job_name: str | None = None,
    expected_image_digest: str | None = None,
    provider_dispatch=None,
) -> ValidationResult:
    """
    Launch a SHACL validation via Cloud Run Jobs.

    Uploads the RDF submission to GCS (the submission IS the primary file, as for
    EnergyPlus), builds a ``SHACLInputEnvelope`` via the shared envelope builder
    (which resolves shapes/ontology/settings/SPARQL-ASK assertions from the DB),
    triggers the Cloud Run Job, and returns a pending ValidationResult.
    """
    current_step_run = None
    try:
        # 0. Idempotency check.
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run for run {run.id}"
            raise ValueError(msg)  # noqa: TRY301

        already_launched = _check_already_launched(current_step_run)
        if already_launched:
            return already_launched

        bundle = _attempt_execution_bundle(
            run=run,
            step_run=current_step_run,
        )
        execution_bundle_uri = bundle.execution_bundle_uri
        input_envelope_uri = bundle.input_envelope_uri
        if bundle.is_gcs:
            submission_uri = f"{execution_bundle_uri}/submission.rdf"
            is_gcs = True
            local_submission_path = None
        else:
            # The local-async development path uses the same attempt layout as
            # production GCS. Normal self-hosting uses the synchronous Docker
            # backend instead of this launcher.
            if bundle.local_dir is None:
                msg = "Local attempt bundle did not provide a filesystem path"
                raise RuntimeError(msg)  # noqa: TRY301
            local_submission_path = bundle.local_dir / "submission.rdf"
            submission_uri = f"file://{local_submission_path}"
            is_gcs = False

        # 1. Upload the RDF submission. The container parses it using the
        # rdf_format resolved Django-side, so the on-disk name is cosmetic.
        content = submission.get_content()
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        if is_gcs:
            logger.info("Uploading SHACL submission to %s", submission_uri)
            submission_file = upload_file(
                content=content_bytes,
                uri=submission_uri,
                content_type="text/plain",
            )
        else:
            create_local_bytes(local_submission_path, content_bytes)
            submission_file = local_bytes_identity(
                content=content_bytes,
                uri=submission_uri,
            )

        # 2. Attempt-bound callback credentials.
        callback_url = build_validation_callback_url()
        callback_credentials = _issue_callback_credentials_for_step(current_step_run)

        # 3. Build the typed envelope via the shared builder (SHACL branch reads
        # primary_file_uri from input_file_uris and resolves rulesets/assertions).
        from validibot.validations.services.cloud_run.envelope_builder import (
            build_input_envelope,
        )

        envelope = build_input_envelope(
            run=run,
            callback_url=callback_url,
            callback_id=callback_credentials.callback_id,
            callback_nonce=callback_credentials.callback_nonce,
            callback_nonce_commitment=(callback_credentials.callback_nonce_commitment),
            execution_bundle_uri=execution_bundle_uri,
            input_file_uris={
                "data_graph": submission_file,
                "primary_file_uri": submission_file,
            },
        )
        envelope = _prepare_attempt_capability_envelope(
            envelope,
            execution_bundle_uri=execution_bundle_uri,
        )

        # 4. Upload the input envelope.
        if is_gcs:
            upload_envelope(envelope, input_envelope_uri)
        else:
            upload_envelope_local(envelope, Path(input_envelope_uri))

        # 5. Trigger the Cloud Run Job. Job name comes from ValidatorConfig.
        job_name = job_name or _resolve_cloud_run_job_name("SHACL")

        dispatch = provider_dispatch or _dispatch_cloud_run_validation
        execution_name, _ = dispatch(
            step_run=current_step_run,
            job_name=job_name,
            input_envelope_uri=input_envelope_uri,
            execution_bundle_uri=execution_bundle_uri,
            envelope=envelope,
            submission=submission,
            step=step,
            expected_image_digest=expected_image_digest,
        )

        stats: dict[str, object] = {
            "job_status": CloudRunJobStatus.PENDING,
            "job_name": job_name,
            "execution_name": execution_name,
            "input_uri": input_envelope_uri,
            "execution_bundle_uri": execution_bundle_uri,
            "output_values": {},  # available after callback processing
        }
        return ValidationResult(passed=None, issues=[], stats=stats)

    except ProviderDispatchAmbiguousError:
        logger.warning(
            "Cloud Run acceptance is unknown for SHACL run %s; "
            "waiting for reconciliation or callback",
            run.id,
        )
        return _ambiguous_dispatch_result()
    except Exception:
        if _attempt_requires_reconciliation(current_step_run):
            logger.exception(
                "SHACL launch follow-up failed after provider claim; "
                "preserving attempt for reconciliation"
            )
            return _ambiguous_dispatch_result()
        logger.exception("Failed to launch SHACL Cloud Run Job for run %s", run.id)
        issues = [
            ValidationIssue(
                path="",
                message=(
                    "Failed to launch SHACL validation. "
                    "The error has been logged for investigation."
                ),
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})


def launch_schematron_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission: Submission,
    ruleset: Ruleset | None,
    step: WorkflowStep,
    job_name: str | None = None,
    expected_image_digest: str | None = None,
    provider_dispatch=None,
) -> ValidationResult:
    """
    Launch a Schematron validation via Cloud Run Jobs.

    Mirrors :func:`launch_shacl_validation`: the XML submission is uploaded
    to the run bundle, and the author's Schematron rules travel **inline**
    in the typed envelope (resolved from the step's ruleset — the SHACL
    shapes_text pattern, ADR-2026-07-01 D4b). Launch-time failures map to
    the D9 taxonomy (reserved ``schematron.*`` codes + ``meta.infra_error``)
    so "we couldn't run the rules" never renders as a rule failure.
    """
    from validibot.validations.validators.schematron.launch import (
        SchematronRulesResolutionError,
    )
    from validibot.validations.validators.schematron.validator import (
        CODE_BACKEND_UNAVAILABLE,
    )
    from validibot.validations.validators.schematron.validator import CODE_RULES_INVALID

    current_step_run = None
    try:
        # 0. Idempotency check.
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run for run {run.id}"
            raise ValueError(msg)  # noqa: TRY301

        already_launched = _check_already_launched(current_step_run)
        if already_launched:
            return already_launched

        bundle = _attempt_execution_bundle(
            run=run,
            step_run=current_step_run,
        )
        execution_bundle_uri = bundle.execution_bundle_uri
        input_envelope_uri = bundle.input_envelope_uri
        if bundle.is_gcs:
            submission_uri = f"{execution_bundle_uri}/submission.xml"
            is_gcs = True
            local_submission_path = None
        else:
            # Local asynchronous development mirrors the production
            # attempt-specific layout under MEDIA_ROOT.
            if bundle.local_dir is None:
                msg = "Local attempt bundle did not provide a filesystem path"
                raise RuntimeError(msg)  # noqa: TRY301
            local_submission_path = bundle.local_dir / "submission.xml"
            submission_uri = f"file://{local_submission_path}"
            is_gcs = False

        # 1. Upload the XML submission (the rules need no staging — they
        # ship inline in the envelope below).
        content = submission.get_content()
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        if is_gcs:
            logger.info("Uploading Schematron submission to %s", submission_uri)
            submission_file = upload_file(
                content=content_bytes,
                uri=submission_uri,
                content_type="application/xml",
            )
        else:
            create_local_bytes(local_submission_path, content_bytes)
            submission_file = local_bytes_identity(
                content=content_bytes,
                uri=submission_uri,
            )

        # 2. Attempt-bound callback credentials.
        callback_url = build_validation_callback_url()
        callback_credentials = _issue_callback_credentials_for_step(current_step_run)

        # 3. Build the typed envelope via the shared builder (the SCHEMATRON
        # branch resolves the rules text from the step's ruleset and ships
        # it inline in SchematronInputs).
        from validibot.validations.services.cloud_run.envelope_builder import (
            build_input_envelope,
        )

        envelope = build_input_envelope(
            run=run,
            callback_url=callback_url,
            callback_id=callback_credentials.callback_id,
            callback_nonce=callback_credentials.callback_nonce,
            callback_nonce_commitment=(callback_credentials.callback_nonce_commitment),
            execution_bundle_uri=execution_bundle_uri,
            input_file_uris={
                "xml_document": submission_file,
                "primary_file_uri": submission_file,
            },
        )
        envelope = _prepare_attempt_capability_envelope(
            envelope,
            execution_bundle_uri=execution_bundle_uri,
        )

        # 4. Upload the input envelope.
        if is_gcs:
            upload_envelope(envelope, input_envelope_uri)
        else:
            upload_envelope_local(envelope, Path(input_envelope_uri))

        # 5. Trigger the Cloud Run Job. Job name comes from ValidatorConfig,
        # gated by the same image policy as the other advanced launchers.
        job_name = job_name or _resolve_cloud_run_job_name("SCHEMATRON")

        dispatch = provider_dispatch or _dispatch_cloud_run_validation
        execution_name, _ = dispatch(
            step_run=current_step_run,
            job_name=job_name,
            input_envelope_uri=input_envelope_uri,
            execution_bundle_uri=execution_bundle_uri,
            envelope=envelope,
            submission=submission,
            step=step,
            expected_image_digest=expected_image_digest,
        )

        stats: dict[str, object] = {
            "job_status": CloudRunJobStatus.PENDING,
            "job_name": job_name,
            "execution_name": execution_name,
            "input_uri": input_envelope_uri,
            "execution_bundle_uri": execution_bundle_uri,
            # Provenance of the executed rules (D5) — recorded at launch so
            # the run is auditable even if the callback never arrives.
            "schematron_sha256": envelope.inputs.schematron_sha256,
            "output_values": {},  # available after callback processing
        }
        return ValidationResult(passed=None, issues=[], stats=stats)

    except ProviderDispatchAmbiguousError:
        logger.warning(
            "Cloud Run acceptance is unknown for Schematron run %s; "
            "waiting for reconciliation or callback",
            run.id,
        )
        return _ambiguous_dispatch_result()
    except SchematronRulesResolutionError as exc:
        # D9: a step with no resolvable rules is an authoring problem — the
        # rules were never run, and this must not read as "your document
        # failed the rules".
        logger.exception(
            "Schematron rules resolution failed for run %s",
            run.id,
        )
        issues = [
            ValidationIssue(
                path="",
                message=str(exc),
                severity=Severity.ERROR,
                code=CODE_RULES_INVALID,
                meta={"infra_error": True},
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})
    except Exception:
        if _attempt_requires_reconciliation(current_step_run):
            logger.exception(
                "Schematron launch follow-up failed after provider claim; "
                "preserving attempt for reconciliation"
            )
            return _ambiguous_dispatch_result()
        logger.exception(
            "Failed to launch Schematron Cloud Run Job for run %s",
            run.id,
        )
        issues = [
            ValidationIssue(
                path="",
                message=(
                    "The Schematron validation backend could not be "
                    "launched. This says nothing about whether your "
                    "document satisfies the rules; the error has been "
                    "logged for investigation."
                ),
                severity=Severity.ERROR,
                code=CODE_BACKEND_UNAVAILABLE,
                meta={"infra_error": True},
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})


def launch_portfolio_manager_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission: Submission,
    ruleset: Ruleset | None,
    step: WorkflowStep,
    job_name: str | None = None,
    expected_image_digest: str | None = None,
    provider_dispatch=None,
) -> ValidationResult:
    """Stage exact report bytes and launch the isolated Portfolio Manager backend."""
    del ruleset
    current_step_run = None
    try:
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run for run {run.id}"
            raise ValueError(msg)  # noqa: TRY301
        already_launched = _check_already_launched(current_step_run)
        if already_launched:
            return already_launched

        bundle = _attempt_execution_bundle(run=run, step_run=current_step_run)
        execution_bundle_uri = bundle.execution_bundle_uri
        input_envelope_uri = bundle.input_envelope_uri
        suffix = Path(submission.original_filename or "").suffix.casefold()
        if suffix not in {".xls", ".xlsx", ".xml", ".zip"}:
            msg = "Portfolio Manager submission has an unsupported extension"
            raise ValueError(msg)  # noqa: TRY301
        staged_name = f"portfolio-manager-report{suffix}"
        submission_uri = f"{execution_bundle_uri}/{staged_name}"
        local_submission_path = (
            bundle.local_dir / staged_name if bundle.local_dir is not None else None
        )
        content_bytes = submission.read_bytes(
            max_bytes=PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES
        )
        content_type = {
            ".xls": "application/vnd.ms-excel",
            ".xlsx": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            ".xml": "application/xml",
            ".zip": "application/zip",
        }[suffix]
        if bundle.is_gcs:
            submission_file = upload_file(
                content=content_bytes,
                uri=submission_uri,
                content_type=content_type,
            )
        else:
            if local_submission_path is None:
                msg = "Local attempt bundle did not provide a filesystem path"
                raise RuntimeError(msg)  # noqa: TRY301
            create_local_bytes(local_submission_path, content_bytes)
            submission_file = local_bytes_identity(
                content=content_bytes,
                uri=submission_uri,
            )

        callback_url = build_validation_callback_url()
        callback_credentials = _issue_callback_credentials_for_step(current_step_run)
        envelope = build_input_envelope(
            run=run,
            callback_url=callback_url,
            callback_id=callback_credentials.callback_id,
            callback_nonce=callback_credentials.callback_nonce,
            callback_nonce_commitment=(callback_credentials.callback_nonce_commitment),
            execution_bundle_uri=execution_bundle_uri,
            input_file_uris={
                "portfolio_manager_report": submission_file,
                "primary_file_uri": submission_file,
            },
        )
        envelope = _prepare_attempt_capability_envelope(
            envelope,
            execution_bundle_uri=execution_bundle_uri,
        )
        if bundle.is_gcs:
            upload_envelope(envelope, input_envelope_uri)
        else:
            upload_envelope_local(envelope, Path(input_envelope_uri))

        job_name = job_name or _resolve_cloud_run_job_name("PORTFOLIO_MANAGER")
        dispatch = provider_dispatch or _dispatch_cloud_run_validation
        execution_name, _ = dispatch(
            step_run=current_step_run,
            job_name=job_name,
            input_envelope_uri=input_envelope_uri,
            execution_bundle_uri=execution_bundle_uri,
            envelope=envelope,
            submission=submission,
            step=step,
            expected_image_digest=expected_image_digest,
        )
        return ValidationResult(
            passed=None,
            issues=[],
            stats={
                "job_status": CloudRunJobStatus.PENDING,
                "job_name": job_name,
                "execution_name": execution_name,
                "input_uri": input_envelope_uri,
                "execution_bundle_uri": execution_bundle_uri,
                "output_values": {},
            },
        )
    except ProviderDispatchAmbiguousError:
        logger.warning(
            "Cloud Run acceptance is unknown for Portfolio Manager run %s",
            run.id,
        )
        return _ambiguous_dispatch_result()
    except Exception:
        if _attempt_requires_reconciliation(current_step_run):
            logger.exception(
                "Portfolio Manager launch follow-up failed after provider claim"
            )
            return _ambiguous_dispatch_result()
        logger.exception(
            "Failed to launch Portfolio Manager backend for run %s",
            run.id,
        )
        return ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    path="",
                    message=(
                        "The Portfolio Manager validation backend could not be "
                        "launched. The error has been logged for investigation."
                    ),
                    severity=Severity.ERROR,
                    code="portfolio_manager.backend_unavailable",
                    meta={"infra_error": True},
                )
            ],
            stats={},
        )


def launch_pdf_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission: Submission,
    ruleset: Ruleset | None,
    step: WorkflowStep,
    job_name: str | None = None,
    expected_image_digest: str | None = None,
    provider_dispatch=None,
) -> ValidationResult:
    """Stage exact PDF bytes and launch the isolated package backend."""
    del ruleset
    current_step_run = None
    try:
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run for run {run.id}"
            raise ValueError(msg)  # noqa: TRY301
        already_launched = _check_already_launched(current_step_run)
        if already_launched:
            return already_launched

        bundle = _attempt_execution_bundle(run=run, step_run=current_step_run)
        execution_bundle_uri = bundle.execution_bundle_uri
        input_envelope_uri = bundle.input_envelope_uri
        staged_name = "document.pdf"
        submission_uri = f"{execution_bundle_uri}/{staged_name}"
        local_submission_path = (
            bundle.local_dir / staged_name if bundle.local_dir is not None else None
        )
        content_bytes = submission.read_bytes(max_bytes=PDF_MAX_SUBMISSION_BYTES)
        if bundle.is_gcs:
            submission_file = upload_file(
                content=content_bytes,
                uri=submission_uri,
                content_type="application/pdf",
            )
        else:
            if local_submission_path is None:
                msg = "Local attempt bundle did not provide a filesystem path"
                raise RuntimeError(msg)  # noqa: TRY301
            create_local_bytes(local_submission_path, content_bytes)
            submission_file = local_bytes_identity(
                content=content_bytes,
                uri=submission_uri,
            )

        callback_url = build_validation_callback_url()
        callback_credentials = _issue_callback_credentials_for_step(current_step_run)
        envelope = build_input_envelope(
            run=run,
            callback_url=callback_url,
            callback_id=callback_credentials.callback_id,
            callback_nonce=callback_credentials.callback_nonce,
            callback_nonce_commitment=(callback_credentials.callback_nonce_commitment),
            execution_bundle_uri=execution_bundle_uri,
            input_file_uris={
                "pdf_document": submission_file,
                "primary_file_uri": submission_file,
            },
        )
        envelope = _prepare_attempt_capability_envelope(
            envelope,
            execution_bundle_uri=execution_bundle_uri,
        )
        if bundle.is_gcs:
            upload_envelope(envelope, input_envelope_uri)
        else:
            upload_envelope_local(envelope, Path(input_envelope_uri))

        job_name = job_name or _resolve_cloud_run_job_name("PDF")
        dispatch = provider_dispatch or _dispatch_cloud_run_validation
        execution_name, _ = dispatch(
            step_run=current_step_run,
            job_name=job_name,
            input_envelope_uri=input_envelope_uri,
            execution_bundle_uri=execution_bundle_uri,
            envelope=envelope,
            submission=submission,
            step=step,
            expected_image_digest=expected_image_digest,
        )
        return ValidationResult(
            passed=None,
            issues=[],
            stats={
                "job_status": CloudRunJobStatus.PENDING,
                "job_name": job_name,
                "execution_name": execution_name,
                "input_uri": input_envelope_uri,
                "execution_bundle_uri": execution_bundle_uri,
                "output_values": {},
            },
        )
    except ProviderDispatchAmbiguousError:
        logger.warning("Cloud Run acceptance is unknown for PDF run %s", run.id)
        return _ambiguous_dispatch_result()
    except Exception:
        if _attempt_requires_reconciliation(current_step_run):
            logger.exception("PDF launch follow-up failed after provider claim")
            return _ambiguous_dispatch_result()
        logger.exception("Failed to launch PDF package backend for run %s", run.id)
        return ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    path="",
                    message=(
                        "The PDF package validation backend could not be "
                        "launched. The error has been logged for investigation."
                    ),
                    severity=Severity.ERROR,
                    code="pdf.backend_unavailable",
                    meta={"infra_error": True},
                )
            ],
            stats={},
        )
