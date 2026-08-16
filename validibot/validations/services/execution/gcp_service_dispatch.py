"""Dispatch one pinned validator Service attempt through its backend queue.

This is the only code allowed to turn staged attempt data into a provider task.
The task name is the attempt UUID, so application-task redelivery and Cloud
Tasks ``AlreadyExists`` converge on one provider delivery identity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError
from validibot_shared.canonicalization import sha256_hex_for_model

from validibot.core.tasks.dispatch.http_task_client import create_http_task
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
    issue_attempt_gcs_runtime_capability,
)
from validibot.validations.services.cloud_run.launcher import (
    ProviderDispatchAmbiguousError,
)
from validibot.validations.services.cloud_run.launcher import _mark_step_run_running
from validibot.validations.services.cloud_run.launcher import (
    build_validation_storage_capability_refresh_url,
)
from validibot.validations.services.execution.deployment_schemas import (
    DeploymentRouteSnapshot,
)
from validibot.validations.services.execution_attempts import (
    get_active_execution_attempt,
)
from validibot.validations.services.execution_attempts import (
    transition_execution_attempt,
)
from validibot.validations.services.execution_evidence import (
    build_input_evidence_snapshot,
)

logger = logging.getLogger(__name__)
_SERVICE_RESOURCE_PART_COUNT = 6

if TYPE_CHECKING:
    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidatorExecutionDeployment


@dataclass(frozen=True, slots=True)
class _ServiceDispatchClaim:
    """Locked attempt and immutable route facts authorized for provider contact."""

    attempt: ExecutionAttempt
    deployment: ValidatorExecutionDeployment
    snapshot: DeploymentRouteSnapshot
    project_id: str
    region: str
    service_name: str


@dataclass(frozen=True, slots=True)
class _PendingAttemptInputs:
    """Evidence fields committed atomically when a pending attempt is claimed."""

    execution_bundle_uri: str
    input_envelope_uri: str
    input_envelope_sha256: str
    input_evidence_snapshot: dict[str, Any]
    output_envelope_uri: str


def _service_coordinates(provider_resource_name: str) -> tuple[str, str, str]:
    """Parse one canonical Cloud Run Service resource without mutable JSON."""
    parts = provider_resource_name.split("/")
    if (
        len(parts) != _SERVICE_RESOURCE_PART_COUNT
        or parts[0] != "projects"
        or parts[2] != "locations"
        or parts[4] != "services"
        or not all((parts[1], parts[3], parts[5]))
    ):
        raise RuntimeError("Pinned Service resource name is not canonical.")
    return parts[1], parts[3], parts[5]


def _validated_service_snapshot(
    *,
    attempt: ExecutionAttempt,
    deployment: ValidatorExecutionDeployment,
    expected_service_name: str,
    expected_image_digest: str | None,
) -> tuple[DeploymentRouteSnapshot, str, str, str]:
    """Reconcile the live FK with the attempt's immutable dispatch authority."""
    if deployment.readiness_state != ExecutionDeploymentReadiness.READY:
        raise RuntimeError("Pinned Service deployment is no longer ready.")
    if deployment.emergency_blocked:
        raise RuntimeError("Pinned Service deployment is emergency blocked.")
    try:
        snapshot = DeploymentRouteSnapshot.model_validate(attempt.deployment_snapshot)
    except PydanticValidationError as exc:
        raise RuntimeError("Pinned Service deployment snapshot is invalid.") from exc
    if snapshot.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise RuntimeError("Pinned deployment snapshot is not a Cloud Run Service.")
    project_id, region, service_name = _service_coordinates(
        snapshot.provider_resource_name
    )
    if service_name != expected_service_name:
        raise RuntimeError("Staged Service target does not match attempt snapshot.")
    if snapshot.backend_image_digest != expected_image_digest:
        raise RuntimeError("Staged image digest does not match attempt snapshot.")

    snapshot_facts = {
        "deployment_id": snapshot.deployment_id,
        "validator_id": snapshot.validator_id,
        "provider_type": snapshot.provider_type,
        "deployment_kind": snapshot.deployment_kind,
        "deployment_revision": snapshot.deployment_revision,
        "provider_resource_name": snapshot.provider_resource_name,
        "route": snapshot.route,
        "authentication_audience": snapshot.authentication_audience,
        "backend_release_identity": snapshot.backend_release_identity,
        "backend_image_ref": snapshot.backend_image_ref,
        "backend_image_digest": snapshot.backend_image_digest,
        "expected_runtime_identity": snapshot.expected_runtime_identity,
        "declared_capabilities": snapshot.declared_capabilities.model_dump(mode="json"),
        "maximum_execution_seconds": snapshot.maximum_execution_seconds,
        "request_timeout_seconds": snapshot.request_timeout_seconds,
        "dispatch_timeout_seconds": snapshot.dispatch_timeout_seconds,
        "concurrency": snapshot.concurrency,
    }
    live_facts = {
        "deployment_id": deployment.pk,
        "validator_id": deployment.validator_id,
        "provider_type": deployment.provider_type,
        "deployment_kind": deployment.deployment_kind,
        "deployment_revision": deployment.deployment_revision,
        "provider_resource_name": deployment.provider_resource_name,
        "route": deployment.route,
        "authentication_audience": deployment.authentication_audience,
        "backend_release_identity": deployment.backend_release_identity,
        "backend_image_ref": deployment.backend_image_ref,
        "backend_image_digest": deployment.backend_image_digest,
        "expected_runtime_identity": deployment.expected_runtime_identity,
        "declared_capabilities": deployment.declared_capabilities,
        "maximum_execution_seconds": deployment.maximum_execution_seconds,
        "request_timeout_seconds": deployment.request_timeout_seconds,
        "dispatch_timeout_seconds": deployment.dispatch_timeout_seconds,
        "concurrency": deployment.concurrency,
    }
    mismatches = sorted(
        field for field, value in snapshot_facts.items() if live_facts[field] != value
    )
    if mismatches:
        raise RuntimeError(
            "Pinned Service deployment no longer matches its attempt snapshot: "
            + ", ".join(mismatches)
        )
    return snapshot, project_id, region, service_name


@transaction.atomic
def _prepare_pinned_service_dispatch(
    *,
    attempt: ExecutionAttempt,
    expected_service_name: str,
    expected_image_digest: str | None,
    pending_inputs: _PendingAttemptInputs | None,
) -> _ServiceDispatchClaim:
    """Lock, revalidate, and claim a pinned attempt before provider contact.

    The deployment lock serializes this readiness/block check with the audited
    emergency-block operation. Once the transaction commits ``DISPATCHING`` is
    the linearization point: a later block affects new or still-pending work,
    while deterministic redelivery can only recover this same provider task.
    """
    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidatorExecutionDeployment

    locked_attempt = ExecutionAttempt.objects.select_for_update().get(pk=attempt.pk)
    if locked_attempt.deployment_id is None:
        raise RuntimeError("Managed Service dispatch requires a pinned deployment.")
    deployment = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=locked_attempt.deployment_id
    )
    snapshot, project_id, region, service_name = _validated_service_snapshot(
        attempt=locked_attempt,
        deployment=deployment,
        expected_service_name=expected_service_name,
        expected_image_digest=expected_image_digest,
    )
    if locked_attempt.state == ExecutionAttemptState.PENDING:
        if pending_inputs is None:
            raise RuntimeError("Pending Service dispatch requires staged inputs.")
        locked_attempt, claimed = transition_execution_attempt(
            locked_attempt.pk,
            ExecutionAttemptState.DISPATCHING,
            provider_resource_name=snapshot.provider_resource_name,
            execution_bundle_uri=pending_inputs.execution_bundle_uri,
            input_envelope_uri=pending_inputs.input_envelope_uri,
            input_envelope_sha256=pending_inputs.input_envelope_sha256,
            input_evidence_snapshot=pending_inputs.input_evidence_snapshot,
            output_envelope_uri=pending_inputs.output_envelope_uri,
        )
        if not claimed:
            raise ProviderDispatchAmbiguousError(
                f"Execution attempt {locked_attempt.pk} was already claimed"
            )
    elif locked_attempt.state not in {
        ExecutionAttemptState.DISPATCHING,
        ExecutionAttemptState.RUNNING,
        ExecutionAttemptState.UNKNOWN,
    }:
        raise ProviderDispatchAmbiguousError(
            f"Execution attempt {locked_attempt.pk} was already claimed"
        )
    return _ServiceDispatchClaim(
        attempt=locked_attempt,
        deployment=deployment,
        snapshot=snapshot,
        project_id=project_id,
        region=region,
        service_name=service_name,
    )


def dispatch_cloud_run_service_validation(
    *,
    step_run,
    job_name: str,
    input_envelope_uri: str,
    execution_bundle_uri: str,
    envelope,
    submission,
    step,
    expected_image_digest: str | None = None,
) -> tuple[str, str]:
    """Create or recover the deterministic provider task for one Service attempt."""
    attempt = get_active_execution_attempt(step_run)
    if attempt is None:
        raise RuntimeError("Managed Service dispatch requires a pinned deployment.")
    queue_name = str(getattr(settings, "GCP_VALIDATOR_TASK_QUEUE_NAME", ""))
    invoker = str(getattr(settings, "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT", ""))
    if not queue_name or not invoker:
        raise RuntimeError("Validator backend queue and invoker must be configured.")
    if attempt.timeout_at is None:
        raise RuntimeError("Managed Service attempt has no absolute deadline.")
    if attempt.state == ExecutionAttemptState.RUNNING and attempt.provider_execution_id:
        try:
            snapshot = DeploymentRouteSnapshot.model_validate(
                attempt.deployment_snapshot
            )
        except PydanticValidationError as exc:
            raise RuntimeError(
                "Pinned Service deployment snapshot is invalid."
            ) from exc
        return attempt.provider_execution_id, snapshot.backend_image_digest

    pending_inputs = None
    if attempt.state == ExecutionAttemptState.PENDING:
        pending_inputs = _PendingAttemptInputs(
            execution_bundle_uri=execution_bundle_uri,
            input_envelope_uri=input_envelope_uri,
            input_envelope_sha256=sha256_hex_for_model(envelope),
            input_evidence_snapshot=build_input_evidence_snapshot(
                envelope,
                submission=submission,
                step=step,
            ),
            output_envelope_uri=str(envelope.context.expected_output_uri),
        )
    claim = _prepare_pinned_service_dispatch(
        attempt=attempt,
        expected_service_name=job_name,
        expected_image_digest=expected_image_digest,
        pending_inputs=pending_inputs,
    )
    attempt = claim.attempt
    deployment = claim.deployment
    snapshot = claim.snapshot
    if attempt.state == ExecutionAttemptState.RUNNING and attempt.provider_execution_id:
        return attempt.provider_execution_id, snapshot.backend_image_digest
    if attempt.timeout_at is None:
        raise RuntimeError("Managed Service attempt has no absolute deadline.")
    if attempt.state not in {
        ExecutionAttemptState.DISPATCHING,
        ExecutionAttemptState.UNKNOWN,
    }:
        raise ProviderDispatchAmbiguousError(
            f"Execution attempt {attempt.pk} was already claimed"
        )
    task_id = str(attempt.pk)
    project_id = claim.project_id
    region = claim.region
    task_name = (
        f"projects/{project_id}/locations/{region}/queues/{queue_name}/tasks/{task_id}"
    )
    try:
        capability = issue_attempt_gcs_runtime_capability(
            execution_bundle_uri=execution_bundle_uri,
            project_id=project_id,
            refresh_url=build_validation_storage_capability_refresh_url(),
        )
    except Exception:
        # No Cloud Task request has been made yet. A freshly claimed attempt is
        # therefore definitively failed, not ambiguous. Leaving it in
        # DISPATCHING makes acceptance wait until its full timeout even though
        # the provider could never have seen it. Preserve UNKNOWN redeliveries,
        # which may represent an earlier ambiguous provider handoff.
        if attempt.state == ExecutionAttemptState.DISPATCHING:
            transition_execution_attempt(
                attempt.pk,
                ExecutionAttemptState.FAILED,
                last_error_code="runtime_capability_preparation_failed",
                last_error=(
                    "Runtime storage capability preparation failed before "
                    "provider contact."
                ),
            )
        logger.warning(
            "Validator runtime capability preparation failed before provider contact",
            extra={
                "attempt_id": str(attempt.pk),
                "deployment_id": str(deployment.pk),
                "provider_resource_name": snapshot.provider_resource_name,
            },
            exc_info=True,
        )
        raise
    payload = {
        "schema_version": 1,
        "attempt_id": str(attempt.pk),
        "deployment_id": str(deployment.pk),
        "deployment_revision": snapshot.deployment_revision,
        "provider_resource_name": snapshot.provider_resource_name,
        "provider_task_name": task_name,
        "service_name": claim.service_name,
        "service_revision": snapshot.deployment_revision,
        "backend_image_digest": snapshot.backend_image_digest,
        "input_uri": input_envelope_uri,
        "timeout_at": attempt.timeout_at.isoformat(),
        "domain_timeout_seconds": snapshot.maximum_execution_seconds,
        "gcs_capability": {
            "access_token": capability.access_token,
            "expires_at": capability.expires_at.isoformat(),
            "allowed_prefix": capability.allowed_prefix,
            "project_id": capability.project_id,
            "refresh_url": capability.refresh_url,
        },
    }
    try:
        created_task = create_http_task(
            project_id=project_id,
            region=region,
            queue_name=queue_name,
            task_id=task_id,
            endpoint_url=f"{snapshot.route.rstrip('/')}/v1/execute",
            payload=payload,
            oidc_service_account=invoker,
            oidc_audience=snapshot.authentication_audience,
            dispatch_deadline_seconds=int(
                settings.GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS
            ),
        )
    except Exception as exc:
        transition_execution_attempt(
            attempt.pk,
            ExecutionAttemptState.UNKNOWN,
            last_error_code="provider_task_acceptance_unknown",
            last_error="Provider task creation raised before acceptance was known.",
        )
        logger.warning(
            "Validator provider task acceptance is unknown",
            extra={
                "attempt_id": str(attempt.pk),
                "deployment_id": str(deployment.pk),
                "provider_resource_name": snapshot.provider_resource_name,
                "provider_task_name": task_name,
            },
            exc_info=True,
        )
        raise ProviderDispatchAmbiguousError(
            f"Provider task acceptance is unknown for attempt {attempt.pk}"
        ) from exc
    transition_execution_attempt(
        attempt.pk,
        ExecutionAttemptState.RUNNING,
        provider_execution_id=created_task.task_name,
        provider_accepted_at=timezone.now(),
    )
    _mark_step_run_running(
        step_run,
        image_digest=snapshot.backend_image_digest,
        provider_execution_id=created_task.task_name,
    )
    logger.info(
        "Validator provider task accepted",
        extra={
            "attempt_id": str(attempt.pk),
            "deployment_id": str(deployment.pk),
            "deployment_kind": snapshot.deployment_kind,
            "deployment_revision": snapshot.deployment_revision,
            "provider_resource_name": snapshot.provider_resource_name,
            "provider_task_name": created_task.task_name,
        },
    )
    return created_task.task_name, snapshot.backend_image_digest
