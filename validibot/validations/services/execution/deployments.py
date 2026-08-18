"""Resolve, snapshot, and activate managed validator execution deployments.

Resolution is deliberately small and fail-closed.  It runs while the logical
step row is locked by attempt allocation, selects only an explicitly activated
and verified route for the exact Validator row, and never treats provider
failure as permission to choose another deployment after contact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from validibot.audit.constants import AuditAction
from validibot.audit.services import ActorSpec
from validibot.audit.services import AuditLogService
from validibot.validations.constants import CallbackAuthenticationMethod
from validibot.validations.constants import ExecutionDeploymentDeactivationCause
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.constants import ExecutionRoutingMode
from validibot.validations.constants import RuntimeStorageIsolation
from validibot.validations.constants import StorageCapabilityMode
from validibot.validations.constants import ValidatorExecutionProfile
from validibot.validations.services.execution.deployment_schemas import (
    DeploymentRouteSnapshot,
)
from validibot.validations.services.execution.deployment_schemas import (
    parse_deployment_capabilities,
)

if TYPE_CHECKING:
    from validibot.validations.models import Validator
    from validibot.validations.models import ValidatorExecutionDeployment


class ExecutionDeploymentResolutionError(RuntimeError):
    """No explicitly activated deployment can safely execute the attempt."""


DEPLOYMENT_PAIR_MEMBER_COUNT = 2
DEFAULT_PROVIDER_DRAIN_DAYS = 7
MAX_OPERATOR_REASON_LENGTH = 1000


@dataclass(frozen=True, slots=True)
class VerifiedDeploymentPair:
    """One verified Service/Job pair for one semantic Validator."""

    service: ValidatorExecutionDeployment
    job: ValidatorExecutionDeployment
    runtime_contract: str


def _require_current_verification(
    deployment: ValidatorExecutionDeployment,
) -> None:
    """Require stored provider observations to match immutable database facts."""
    from validibot.validations.services.execution.deployment_identity import (
        execution_config_sha256,
    )
    from validibot.validations.services.execution.deployment_identity import (
        provider_spec_sha256,
    )

    if (
        deployment.readiness_state != ExecutionDeploymentReadiness.READY
        or deployment.last_verification_succeeded is not True
        or deployment.last_verified_at is None
        or not deployment.last_verification_details
    ):
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} has no successful provider observation."
        )
    details = deployment.last_verification_details
    expected_observations = {
        "observed_provider_revision": deployment.deployment_revision,
        "observed_resource_name": deployment.provider_resource_name,
        "observed_image_digest": deployment.backend_image_digest,
    }
    mismatches = [
        key
        for key, expected in expected_observations.items()
        if details.get(key) != expected
    ]
    if mismatches:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} provider observation differs in: "
            + ", ".join(sorted(mismatches))
        )
    if (
        not deployment.provider_spec_sha256
        or deployment.provider_spec_sha256 != provider_spec_sha256(deployment)
    ):
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} provider specification digest is missing "
            "or incorrect."
        )
    if (
        not deployment.execution_config_sha256
        or deployment.execution_config_sha256 != execution_config_sha256(deployment)
    ):
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} execution configuration digest is missing "
            "or incorrect."
        )


def verify_execution_deployment_pair(
    *,
    service: ValidatorExecutionDeployment,
    job: ValidatorExecutionDeployment,
) -> VerifiedDeploymentPair:
    """Verify one same-release, same-image Service and Job pair.

    Provider import commands must re-read both live resources immediately
    before calling an activation service. This function then checks those
    stored observations, all immutable release facts, runtime identity, and
    execution limits without contacting a provider inside the DB transaction.
    """
    if service.pk == job.pk:
        raise ExecutionDeploymentResolutionError(
            "A deployment cannot serve as both pair members."
        )
    if service.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise ExecutionDeploymentResolutionError(
            "The Service pair member is not a Cloud Run Service deployment."
        )
    if job.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_JOB:
        raise ExecutionDeploymentResolutionError(
            "The Job pair member is not a Cloud Run Job deployment."
        )
    if service.validator_id != job.validator_id:
        raise ExecutionDeploymentResolutionError(
            "Service and Job pair members use different semantic Validators."
        )
    if service.provider_type != job.provider_type:
        raise ExecutionDeploymentResolutionError(
            "Service and Job pair members use different providers."
        )
    if not service.backend_slug or service.backend_slug != job.backend_slug:
        raise ExecutionDeploymentResolutionError(
            "Service and Job pair members do not have the same backend slug."
        )
    validator_backend = service.validator.execution_backend_slug
    if validator_backend and service.backend_slug != validator_backend:
        raise ExecutionDeploymentResolutionError(
            "Deployment backend does not match the semantic Validator declaration."
        )
    if (
        not service.backend_release_identity
        or service.backend_release_identity != job.backend_release_identity
    ):
        raise ExecutionDeploymentResolutionError(
            "Service and Job pair members do not have the same backend version."
        )
    expected_source_tag = f"{service.backend_slug}-v{service.backend_release_identity}"
    if (
        service.source_release_tag != expected_source_tag
        or job.source_release_tag != expected_source_tag
    ):
        raise ExecutionDeploymentResolutionError(
            "Service and Job source tags do not match their backend and version."
        )
    if (
        not service.release_record_sha256
        or service.release_record_sha256 != job.release_record_sha256
    ):
        raise ExecutionDeploymentResolutionError(
            "Service and Job pair members do not have the same release-record "
            "SHA-256 digest."
        )
    if (
        not service.backend_image_digest
        or service.backend_image_digest != job.backend_image_digest
    ):
        raise ExecutionDeploymentResolutionError(
            "Service and Job pair members do not have the same image digest."
        )
    if service.emergency_blocked or job.emergency_blocked:
        raise ExecutionDeploymentResolutionError(
            "An emergency-blocked deployment pair cannot be activated."
        )
    _require_current_verification(service)
    _require_current_verification(job)
    service_capabilities = _validated_capabilities(service)
    job_capabilities = _validated_capabilities(job)
    if (
        service_capabilities.runtime_contract_version
        != job_capabilities.runtime_contract_version
    ):
        raise ExecutionDeploymentResolutionError(
            "Service and Job runtime contracts differ."
        )
    validator_contract = service.validator.execution_runtime_contract
    if (
        validator_contract
        and service_capabilities.runtime_contract_version != validator_contract
    ):
        raise ExecutionDeploymentResolutionError(
            "Deployment runtime contract does not match the semantic Validator."
        )
    if service.expected_runtime_identity != job.expected_runtime_identity:
        raise ExecutionDeploymentResolutionError(
            "Service and Job runtime identities differ."
        )
    service_configuration = service.provider_configuration
    job_configuration = job.provider_configuration
    for coordinate in ("project_id", "region", "runtime_service_account"):
        if service_configuration.get(coordinate) != job_configuration.get(coordinate):
            raise ExecutionDeploymentResolutionError(
                f"Service and Job provider coordinate differs: {coordinate}."
            )
    if service.concurrency != 1:
        raise ExecutionDeploymentResolutionError(
            "Cloud Run validator Service concurrency must be one."
        )
    if service.maximum_instances is None:
        raise ExecutionDeploymentResolutionError(
            "Cloud Run validator Service requires a maximum instance limit."
        )
    if job.maximum_execution_seconds < service.maximum_execution_seconds:
        raise ExecutionDeploymentResolutionError(
            "Cloud Run Job execution limit cannot be shorter than the Service limit."
        )
    return VerifiedDeploymentPair(
        service=service,
        job=job,
        runtime_contract=service_capabilities.runtime_contract_version,
    )


def routing_mode_for_pair(
    *,
    service: ValidatorExecutionDeployment,
    job: ValidatorExecutionDeployment,
) -> ExecutionRoutingMode:
    """Calculate routing mode from the two routing slots; persist no mode."""
    roles = (service.routing_role, job.routing_role)
    if roles == (
        ExecutionDeploymentRoutingRole.PRIMARY,
        ExecutionDeploymentRoutingRole.LONG_RUNNING,
    ):
        return ExecutionRoutingMode.NORMAL
    if roles == (
        ExecutionDeploymentRoutingRole.INACTIVE,
        ExecutionDeploymentRoutingRole.PRIMARY,
    ):
        return ExecutionRoutingMode.JOB_ONLY
    if roles == (
        ExecutionDeploymentRoutingRole.INACTIVE,
        ExecutionDeploymentRoutingRole.INACTIVE,
    ):
        return ExecutionRoutingMode.INACTIVE
    return ExecutionRoutingMode.INCONSISTENT


def _record_operator_audit(
    deployment: ValidatorExecutionDeployment,
    *,
    action: AuditAction,
    changes: dict[str, object],
    metadata: dict[str, object] | None = None,
) -> None:
    """Record a secret-free system actor event for an operator route change."""
    AuditLogService.record(
        action=action,
        actor=ActorSpec(email="validibot-operator@system.local"),
        target=deployment,
        changes=changes,
        metadata={
            "validator_id": str(deployment.validator_id),
            "provider_type": deployment.provider_type,
            "deployment_kind": deployment.deployment_kind,
            "deployment_revision": deployment.deployment_revision,
            "routing_role": deployment.routing_role,
            "provider_resource_name": deployment.provider_resource_name,
            **(metadata or {}),
        },
    )


def _normalize_operator_reason(reason: str) -> str:
    """Return bounded audit text without creating another lifecycle authority."""
    normalized = reason.strip()
    if len(normalized) > MAX_OPERATOR_REASON_LENGTH:
        raise ValueError(
            f"Operator reason cannot exceed {MAX_OPERATOR_REASON_LENGTH} characters."
        )
    return normalized


def _record_displaced_route_audits(
    deployments: list[ValidatorExecutionDeployment],
    *,
    replacement: ValidatorExecutionDeployment,
    modified_at,
) -> None:
    """Record why every route displaced by an activation became inactive."""
    for deployment in deployments:
        previous_role = deployment.routing_role
        deployment.routing_role = ExecutionDeploymentRoutingRole.INACTIVE
        deployment.activated_at = None
        deployment.modified = modified_at
        _record_operator_audit(
            deployment,
            action=AuditAction.VALIDATOR_DEPLOYMENT_DEACTIVATED,
            changes={
                "routing_role": [
                    previous_role,
                    ExecutionDeploymentRoutingRole.INACTIVE,
                ]
            },
            metadata={
                "replacement_deployment_id": str(replacement.pk),
                "replacement_routing_role": replacement.routing_role,
            },
        )


def record_execution_deployment_verification(
    deployment: ValidatorExecutionDeployment,
    *,
    created: bool,
) -> None:
    """Audit one explicit operator import/readiness verification."""
    _record_operator_audit(
        deployment,
        action=(
            AuditAction.VALIDATOR_DEPLOYMENT_REGISTERED
            if created
            else AuditAction.VALIDATOR_DEPLOYMENT_VERIFIED
        ),
        changes={},
        metadata={
            "readiness_state": deployment.readiness_state,
            "verification_succeeded": deployment.last_verification_succeeded,
            "verified_at": (
                deployment.last_verified_at.isoformat()
                if deployment.last_verified_at
                else None
            ),
        },
    )


@transaction.atomic
def update_execution_deployment_capacity(
    deployment: ValidatorExecutionDeployment,
    *,
    minimum_instances: int,
    maximum_instances: int,
) -> ValidatorExecutionDeployment:
    """Record verified Service-level scaling without changing revision identity."""
    from validibot.validations.models import ValidatorExecutionDeployment

    if minimum_instances < 0 or maximum_instances < 1:
        raise ValueError("Service capacity requires minimum >= 0 and maximum >= 1.")
    if minimum_instances > maximum_instances:
        raise ValueError("Service minimum instances cannot exceed maximum instances.")
    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    if selected.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise ValueError("Only Cloud Run Service deployments expose instance capacity.")
    if selected.readiness_state != ExecutionDeploymentReadiness.READY:
        raise ValueError("Only ready Cloud Run Service capacity may be updated.")
    previous = (selected.minimum_instances, selected.maximum_instances)
    current = (minimum_instances, maximum_instances)
    if previous == current:
        return selected
    selected.minimum_instances = minimum_instances
    selected.maximum_instances = maximum_instances
    selected.save(update_fields=["minimum_instances", "maximum_instances", "modified"])
    _record_operator_audit(
        selected,
        action=AuditAction.VALIDATOR_DEPLOYMENT_CAPACITY_UPDATED,
        changes={
            "minimum_instances": [previous[0], minimum_instances],
            "maximum_instances": [previous[1], maximum_instances],
        },
    )
    return selected


def _latest_verified_deployment(
    deployments: list[ValidatorExecutionDeployment],
) -> ValidatorExecutionDeployment:
    """Return the deployment observed most recently by the provider importer."""
    return max(
        deployments,
        key=lambda item: (
            item.last_verified_at or item.created,
            item.created,
            str(item.pk),
        ),
    )


def _resolve_backend_release_pair_from_deployments(
    deployments: list[ValidatorExecutionDeployment],
    *,
    validator: Validator,
    backend_slug: str,
    backend_release_identity: str,
    require_accepted: bool,
) -> VerifiedDeploymentPair:
    """Resolve one release pair while preserving immutable revision history."""
    candidates = [
        deployment
        for deployment in deployments
        if deployment.readiness_state == ExecutionDeploymentReadiness.READY
        and deployment.backend_slug == backend_slug
        and deployment.backend_release_identity == backend_release_identity
    ]
    services = [
        deployment
        for deployment in candidates
        if deployment.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_SERVICE
    ]
    jobs = [
        deployment
        for deployment in candidates
        if deployment.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_JOB
    ]
    if not services or not jobs:
        raise ExecutionDeploymentResolutionError(
            f"Validator {validator.pk} requires a Service and Job row for "
            f"{backend_slug} {backend_release_identity}."
        )
    if require_accepted:
        services = [deployment for deployment in services if deployment.accepted_at]
        jobs = [deployment for deployment in jobs if deployment.accepted_at]
        if not services or not jobs:
            raise ExecutionDeploymentResolutionError(
                f"Validator {validator.pk} pair has not completed private acceptance."
            )
    return verify_execution_deployment_pair(
        service=_latest_verified_deployment(services),
        job=_latest_verified_deployment(jobs),
    )


def resolve_backend_release_pair(
    *,
    validator: Validator,
    backend_slug: str,
    backend_release_identity: str,
    require_accepted: bool = False,
) -> VerifiedDeploymentPair:
    """Resolve and verify one backend release pair without changing its route.

    Provider reconciliation may retain several immutable Service revisions for
    the same backend release. The current candidate is the revision observed
    most recently by the provider importer; accepted recovery ignores newer
    revisions that have not completed private acceptance.
    """
    from validibot.validations.models import ValidatorExecutionDeployment

    deployments = list(
        ValidatorExecutionDeployment.objects.select_related("validator").filter(
            validator=validator,
            backend_slug=backend_slug,
            backend_release_identity=backend_release_identity,
            readiness_state=ExecutionDeploymentReadiness.READY,
        )
    )
    return _resolve_backend_release_pair_from_deployments(
        deployments,
        validator=validator,
        backend_slug=backend_slug,
        backend_release_identity=backend_release_identity,
        require_accepted=require_accepted,
    )


def ensure_execution_deployment_can_retire(
    deployment: ValidatorExecutionDeployment,
) -> None:
    """Fail unless an inactive, cold Service has no nonterminal attempts."""
    from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
    from validibot.validations.models import ExecutionAttempt

    if deployment.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise ValueError("Only Cloud Run Service deployments can use this cleanup.")
    if deployment.routing_role != ExecutionDeploymentRoutingRole.INACTIVE:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} still occupies a routing slot."
        )
    if deployment.minimum_instances != 0:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} must have minimum instances zero."
        )
    if (
        ExecutionAttempt.objects.filter(deployment=deployment)
        .exclude(state__in=EXECUTION_ATTEMPT_TERMINAL_STATES)
        .exists()
    ):
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} still has a nonterminal attempt."
        )


def ensure_backend_release_can_retire(
    deployments: list[ValidatorExecutionDeployment],
    *,
    now=None,
    drain_days: int = DEFAULT_PROVIDER_DRAIN_DAYS,
    allow_immediate: bool = False,
    allow_unaccepted_candidate: bool = False,
) -> None:
    """Require a complete release to be inactive and fully drained.

    The normal production lifecycle keeps a seven-day drain period. The
    explicit ``allow_immediate`` escape hatch is reserved for a no-user
    bootstrap reconciliation that has already proved there are no
    nonterminal attempts; it is never implied by ``drain_days=0`` alone.

    ``allow_unaccepted_candidate`` handles a failed private-acceptance
    candidate whose providers have already been removed. It accepts only a
    wholly unaccepted Service/Job pair. A partially accepted pair remains an
    error because its inconsistent evidence requires operator investigation.
    """
    from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
    from validibot.validations.models import ExecutionAttempt

    if drain_days < 0:
        raise ValueError("Provider drain period cannot be negative.")
    if drain_days < DEFAULT_PROVIDER_DRAIN_DAYS and not allow_immediate:
        raise ValueError(
            f"Routine drain period cannot be below {DEFAULT_PROVIDER_DRAIN_DAYS} days."
        )
    if allow_unaccepted_candidate and not allow_immediate:
        raise ValueError(
            "Unaccepted candidate retirement requires the explicit immediate "
            "empty-installation path."
        )
    if not deployments:
        raise ExecutionDeploymentResolutionError(
            "No deployment rows exist for the selected backend release."
        )
    by_validator: dict[object, list[ValidatorExecutionDeployment]] = {}
    for deployment in deployments:
        by_validator.setdefault(deployment.validator_id, []).append(deployment)
    for validator_id, rows in by_validator.items():
        kinds = {row.deployment_kind for row in rows}
        if kinds != {
            ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
            ExecutionDeploymentKind.CLOUD_RUN_JOB,
        }:
            raise ExecutionDeploymentResolutionError(
                f"Validator {validator_id} does not have one complete provider pair."
            )
        accepted_kinds = {
            row.deployment_kind for row in rows if row.accepted_at is not None
        }
        complete_pair = {
            ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
            ExecutionDeploymentKind.CLOUD_RUN_JOB,
        }
        if accepted_kinds == complete_pair:
            continue
        if allow_unaccepted_candidate and not accepted_kinds:
            continue
        if accepted_kinds:
            raise ExecutionDeploymentResolutionError(
                f"Validator {validator_id} has a partially accepted provider pair."
            )
        raise ExecutionDeploymentResolutionError(
            f"Validator {validator_id} has no accepted provider pair."
        )
    deadline = (now or timezone.now()) - timedelta(days=drain_days)
    for deployment in deployments:
        if deployment.routing_role != ExecutionDeploymentRoutingRole.INACTIVE:
            raise ExecutionDeploymentResolutionError(
                f"Deployment {deployment.pk} still occupies a routing slot."
            )
        if deployment.deactivated_at is None or deployment.deactivated_at > deadline:
            raise ExecutionDeploymentResolutionError(
                f"Deployment {deployment.pk} has not completed the "
                f"{drain_days}-day drain period."
            )
        if (
            deployment.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_SERVICE
            and deployment.minimum_instances != 0
        ):
            raise ExecutionDeploymentResolutionError(
                f"Service deployment {deployment.pk} must have minimum instances zero."
            )
    if (
        ExecutionAttempt.objects.filter(
            deployment_id__in=[deployment.pk for deployment in deployments]
        )
        .exclude(state__in=EXECUTION_ATTEMPT_TERMINAL_STATES)
        .exists()
    ):
        raise ExecutionDeploymentResolutionError(
            "The backend release still has a nonterminal attempt."
        )


@transaction.atomic
def record_execution_deployment_provider_deleted(
    deployment: ValidatorExecutionDeployment,
    *,
    deleted_at=None,
    deactivate_superseded: bool = False,
) -> ValidatorExecutionDeployment:
    """Record confirmed provider absence during resumable release cleanup.

    Normal drained-release cleanup requires the deployment to be inactive
    before provider deletion is checkpointed. The explicit
    ``deactivate_superseded`` path is reserved for latest-only reconciliation:
    it repairs a historical semantic Validator route that remained active after
    its provider release was superseded and removed. That repair remains
    fail-closed when the deployment has nonterminal attempts.
    """
    from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidatorExecutionDeployment

    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    previous_role = selected.routing_role
    if (
        previous_role != ExecutionDeploymentRoutingRole.INACTIVE
        and not deactivate_superseded
    ):
        raise ExecutionDeploymentResolutionError(
            f"Deployment {selected.pk} still occupies a routing slot."
        )
    if previous_role != ExecutionDeploymentRoutingRole.INACTIVE and (
        ExecutionAttempt.objects.filter(deployment=selected)
        .exclude(state__in=EXECUTION_ATTEMPT_TERMINAL_STATES)
        .exists()
    ):
        raise ExecutionDeploymentResolutionError(
            f"Deployment {selected.pk} still has a nonterminal attempt."
        )

    checkpoint_time = deleted_at or timezone.now()
    previous_deactivated_at = selected.deactivated_at
    previous_deactivation_cause = selected.deactivation_cause
    previous_provider_deleted_at = selected.provider_deleted_at
    update_fields = []
    if previous_role != ExecutionDeploymentRoutingRole.INACTIVE:
        selected.routing_role = ExecutionDeploymentRoutingRole.INACTIVE
        selected.activated_at = None
        selected.deactivated_at = checkpoint_time
        selected.deactivation_cause = (
            ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
        )
        update_fields.extend(
            [
                "routing_role",
                "activated_at",
                "deactivated_at",
                "deactivation_cause",
            ]
        )
    if selected.provider_deleted_at is None:
        selected.provider_deleted_at = checkpoint_time
        update_fields.append("provider_deleted_at")
    if not update_fields:
        return selected
    selected.save(update_fields=[*update_fields, "modified"])

    if previous_role != ExecutionDeploymentRoutingRole.INACTIVE:
        changes = {
            "routing_role": [previous_role, selected.routing_role],
            "deactivated_at": [
                (
                    previous_deactivated_at.isoformat()
                    if previous_deactivated_at
                    else None
                ),
                checkpoint_time.isoformat(),
            ],
            "deactivation_cause": [
                previous_deactivation_cause,
                selected.deactivation_cause,
            ],
        }
        if previous_provider_deleted_at != selected.provider_deleted_at:
            changes["provider_deleted_at"] = [
                (
                    previous_provider_deleted_at.isoformat()
                    if previous_provider_deleted_at
                    else None
                ),
                selected.provider_deleted_at.isoformat(),
            ]
        _record_operator_audit(
            selected,
            action=AuditAction.VALIDATOR_DEPLOYMENT_DEACTIVATED,
            changes=changes,
            metadata={
                "backend_slug": selected.backend_slug,
                "backend_release": selected.backend_release_identity,
                "provider_resource_deleted": True,
                "latest_only_reconciliation": True,
            },
        )
    return selected


@transaction.atomic
def retire_backend_release_deployments(
    *,
    backend_slug: str,
    backend_release_identity: str,
    reason: str,
    drain_days: int = DEFAULT_PROVIDER_DRAIN_DAYS,
    allow_immediate: bool = False,
    allow_unaccepted_candidate: bool = False,
) -> tuple[ValidatorExecutionDeployment, ...]:
    """Retire every semantic deployment row after both provider members vanish.

    ``allow_immediate`` is intentionally explicit because deleting a provider
    pair without its normal drain period is appropriate only for a confirmed
    empty installation reset. ``allow_unaccepted_candidate`` additionally
    permits a complete, wholly unaccepted pair from a failed acceptance gate;
    all normal inactivity, attempt, and provider-deletion checks still apply.
    """
    from validibot.validations.models import ValidatorExecutionDeployment

    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("Deployment retirement requires a reason.")
    deployments = list(
        ValidatorExecutionDeployment.objects.select_for_update()
        .select_related("validator")
        .filter(
            backend_slug=backend_slug,
            backend_release_identity=backend_release_identity,
        )
        .order_by("validator_id", "deployment_kind")
    )
    ensure_backend_release_can_retire(
        deployments,
        allow_immediate=allow_immediate,
        drain_days=drain_days,
        allow_unaccepted_candidate=allow_unaccepted_candidate,
    )
    missing_deletion = [
        deployment.pk
        for deployment in deployments
        if deployment.provider_deleted_at is None
    ]
    if missing_deletion:
        raise ExecutionDeploymentResolutionError(
            "Provider deletion has not been confirmed for deployment rows: "
            + ", ".join(str(value) for value in missing_deletion)
        )
    retired_at = timezone.now()
    for deployment in deployments:
        if deployment.readiness_state == ExecutionDeploymentReadiness.RETIRED:
            continue
        provider_deleted_at = deployment.provider_deleted_at
        if provider_deleted_at is None:
            raise ExecutionDeploymentResolutionError(
                f"Provider deletion has not been confirmed for deployment "
                f"{deployment.pk}."
            )
        previous_state = deployment.readiness_state
        deployment.readiness_state = ExecutionDeploymentReadiness.RETIRED
        deployment.retired_at = retired_at
        deployment.retirement_reason = normalized_reason
        deployment.save(
            update_fields=[
                "readiness_state",
                "retired_at",
                "retirement_reason",
                "modified",
            ]
        )
        _record_operator_audit(
            deployment,
            action=AuditAction.VALIDATOR_DEPLOYMENT_RETIRED,
            changes={"readiness_state": [previous_state, deployment.readiness_state]},
            metadata={
                "backend_slug": backend_slug,
                "backend_release": backend_release_identity,
                "provider_deleted_at": provider_deleted_at.isoformat(),
                "retirement_reason": normalized_reason,
            },
        )
    return tuple(deployments)


@transaction.atomic
def retire_execution_deployment(
    deployment: ValidatorExecutionDeployment,
    *,
    provider_deleted_at=None,
    reason: str = "Provider resource deleted after verified drain.",
) -> ValidatorExecutionDeployment:
    """Retire an inactive Service after its provider resource was deleted."""
    from validibot.validations.models import ValidatorExecutionDeployment

    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    ensure_execution_deployment_can_retire(selected)
    if selected.readiness_state == ExecutionDeploymentReadiness.RETIRED:
        return selected
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("Deployment retirement requires a reason.")
    previous_state = selected.readiness_state
    retired_at = timezone.now()
    selected.provider_deleted_at = provider_deleted_at or retired_at
    selected.retired_at = retired_at
    selected.retirement_reason = normalized_reason
    selected.readiness_state = ExecutionDeploymentReadiness.RETIRED
    selected.save(
        update_fields=[
            "provider_deleted_at",
            "retired_at",
            "retirement_reason",
            "readiness_state",
            "modified",
        ]
    )
    _record_operator_audit(
        selected,
        action=AuditAction.VALIDATOR_DEPLOYMENT_RETIRED,
        changes={"readiness_state": [previous_state, selected.readiness_state]},
        metadata={
            "provider_resource_deleted": True,
            "provider_deleted_at": selected.provider_deleted_at.isoformat(),
            "retirement_reason": normalized_reason,
        },
    )
    return selected


def effective_execution_profile(*, step) -> ValidatorExecutionProfile:
    """Return the validated workload profile requested by a workflow step."""
    raw_value = (getattr(step, "config", None) or {}).get(
        "execution_profile",
        ValidatorExecutionProfile.FAST_RESPONSE,
    )
    try:
        return ValidatorExecutionProfile(raw_value)
    except ValueError as exc:
        allowed = ", ".join(ValidatorExecutionProfile.values)
        raise ExecutionDeploymentResolutionError(
            f"execution_profile must be one of: {allowed}."
        ) from exc


def effective_execution_budget_seconds(*, step) -> int:
    """Return the operator-bounded domain budget for one authored profile.

    Fast-response steps use the Service-eligible default. Long-running steps
    receive the site-wide validator ceiling without asking a solo operator or
    workflow author to coordinate a second timeout field. Deployment targets
    with only one local execution route use the site-wide ceiling directly;
    they do not inherit GCP's HTTP transport limit. Machine-authored workflow
    imports may still request a narrower explicit timeout.
    """
    from django.conf import settings

    from validibot.core.deployment import (
        supports_author_selectable_validator_execution_profiles,
    )

    profile = effective_execution_profile(step=step)
    configured = (getattr(step, "config", None) or {}).get("execution_timeout_seconds")
    value = (
        configured
        if configured is not None
        else (
            getattr(settings, "VALIDATOR_DEFAULT_EXECUTION_SECONDS", 1500)
            if (
                supports_author_selectable_validator_execution_profiles()
                and profile == ValidatorExecutionProfile.FAST_RESPONSE
            )
            else getattr(settings, "VALIDATOR_TIMEOUT_SECONDS", 3600)
        )
    )
    if isinstance(value, bool):
        raise ExecutionDeploymentResolutionError(
            "execution_timeout_seconds must be a positive integer."
        )
    try:
        budget = int(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionDeploymentResolutionError(
            "execution_timeout_seconds must be a positive integer."
        ) from exc
    maximum = int(getattr(settings, "VALIDATOR_TIMEOUT_SECONDS", 3600))
    if budget < 1 or budget > maximum:
        raise ExecutionDeploymentResolutionError(
            f"execution_timeout_seconds must be between 1 and {maximum}."
        )
    return budget


def _validated_capabilities(deployment: ValidatorExecutionDeployment):
    """Return verified capabilities after enforcing baseline runtime needs."""
    if not deployment.verified_capabilities:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} has no verified capabilities."
        )
    capabilities = parse_deployment_capabilities(
        deployment_kind=deployment.deployment_kind,
        capabilities=deployment.verified_capabilities,
    )
    unsupported: list[str] = []
    if capabilities.runtime_contract_version != "validibot-execution-v1":
        unsupported.append("runtime contract validibot-execution-v1")
    if capabilities.storage_capability != StorageCapabilityMode.GCS_DOWNSCOPED_TOKEN:
        unsupported.append("downscoped GCS storage")
    if capabilities.storage_isolation != RuntimeStorageIsolation.ATTEMPT_SCOPED:
        unsupported.append("attempt-scoped storage isolation")
    if (
        capabilities.callback_authentication
        != CallbackAuthenticationMethod.ATTEMPT_NONCE_AND_OIDC
    ):
        unsupported.append("attempt nonce plus OIDC callback authentication")
    if "linux-amd64" not in capabilities.architectures:
        unsupported.append("linux-amd64")
    if unsupported:
        joined = ", ".join(unsupported)
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} lacks required capabilities: {joined}."
        )
    return capabilities


def resolve_execution_deployment(
    *,
    validator: Validator,
    effective_budget_seconds: int,
    execution_profile: ValidatorExecutionProfile | str = (
        ValidatorExecutionProfile.FAST_RESPONSE
    ),
    for_update: bool = False,
) -> ValidatorExecutionDeployment:
    """Select the exact active route for a managed attempt before dispatch.

    The workflow's profile makes route selection explicit before provider
    contact. Fast-response work uses the primary route. Long-running work uses
    the compatibility route, or a primary Job while an operator rollback is in
    effect. Missing, blocked, unready, drifted, or capability-incompatible
    deployments fail closed; runtime failure never authorizes route switching.
    """
    from validibot.validations.models import ValidatorExecutionDeployment

    if effective_budget_seconds < 1:
        raise ExecutionDeploymentResolutionError(
            "The effective execution budget must be at least one second."
        )
    try:
        profile = ValidatorExecutionProfile(execution_profile)
    except ValueError as exc:
        raise ExecutionDeploymentResolutionError(
            f"Unknown validator execution profile: {execution_profile!r}."
        ) from exc
    queryset = ValidatorExecutionDeployment.objects.filter(validator=validator)
    if for_update:
        queryset = queryset.select_for_update()
    deployments = {
        deployment.routing_role: deployment
        for deployment in queryset.filter(
            routing_role__in=(
                ExecutionDeploymentRoutingRole.PRIMARY,
                ExecutionDeploymentRoutingRole.LONG_RUNNING,
            )
        )
    }
    primary = deployments.get(ExecutionDeploymentRoutingRole.PRIMARY)
    compatibility = deployments.get(ExecutionDeploymentRoutingRole.LONG_RUNNING)

    if profile == ValidatorExecutionProfile.LONG_RUNNING:
        selected = compatibility
        route_label = "Long-running"
        if (
            selected is None
            and primary is not None
            and primary.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_JOB
        ):
            # Operator rollback makes the retained Job primary and clears the
            # compatibility slot. It remains the truthful long-running route.
            selected = primary
            route_label = "Primary Job"
    else:
        selected = primary
        route_label = "Primary"

    if selected is None:
        missing_role = (
            "long-running"
            if profile == ValidatorExecutionProfile.LONG_RUNNING
            else "primary"
        )
        raise ExecutionDeploymentResolutionError(
            f"Validator {validator.pk} has no activated {missing_role} deployment."
        )
    if selected.readiness_state != ExecutionDeploymentReadiness.READY:
        raise ExecutionDeploymentResolutionError(
            f"{route_label} deployment {selected.pk} is not ready."
        )
    if selected.emergency_blocked:
        raise ExecutionDeploymentResolutionError(
            f"{route_label} deployment {selected.pk} is emergency blocked."
        )
    capabilities = _validated_capabilities(selected)
    if effective_budget_seconds > capabilities.maximum_execution_seconds:
        guidance = (
            " Choose the Long-running profile for larger work."
            if profile == ValidatorExecutionProfile.FAST_RESPONSE
            and selected.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_SERVICE
            else ""
        )
        raise ExecutionDeploymentResolutionError(
            f"The {effective_budget_seconds}-second attempt budget exceeds "
            f"{route_label.lower()} deployment {selected.pk}'s verified maximum."
            f"{guidance}"
        )
    return selected


def build_deployment_snapshot(
    deployment: ValidatorExecutionDeployment,
) -> dict[str, object]:
    """Return the typed JSON-safe evidence snapshot for one selected route."""
    snapshot = DeploymentRouteSnapshot(
        deployment_id=deployment.pk,
        validator_id=deployment.validator_id,
        validator_slug=deployment.validator.slug,
        validator_version=str(deployment.validator.version),
        validator_semantic_digest=deployment.validator.semantic_digest,
        selected_at=timezone.now(),
        provider_type=ExecutionProviderType(deployment.provider_type),
        deployment_kind=ExecutionDeploymentKind(deployment.deployment_kind),
        deployment_revision=deployment.deployment_revision,
        provider_resource_name=deployment.provider_resource_name,
        route=deployment.route,
        authentication_audience=deployment.authentication_audience,
        backend_slug=deployment.backend_slug,
        backend_release_identity=deployment.backend_release_identity,
        source_release_tag=deployment.source_release_tag,
        release_record_sha256=deployment.release_record_sha256,
        backend_image_ref=deployment.backend_image_ref,
        backend_image_digest=deployment.backend_image_digest,
        provider_spec_sha256=deployment.provider_spec_sha256,
        execution_config_sha256=deployment.execution_config_sha256,
        expected_runtime_identity=deployment.expected_runtime_identity,
        routing_role=ExecutionDeploymentRoutingRole(deployment.routing_role),
        declared_capabilities=deployment.declared_capabilities,
        verified_capabilities=deployment.verified_capabilities,
        maximum_execution_seconds=deployment.maximum_execution_seconds,
        request_timeout_seconds=deployment.request_timeout_seconds,
        dispatch_timeout_seconds=deployment.dispatch_timeout_seconds,
        minimum_instances=deployment.minimum_instances,
        maximum_instances=deployment.maximum_instances,
        concurrency=deployment.concurrency,
    )
    return snapshot.model_dump(mode="json")


@transaction.atomic
def mark_execution_deployment_pair_accepted(
    *,
    service: ValidatorExecutionDeployment,
    job: ValidatorExecutionDeployment,
    accepted_at=None,
) -> VerifiedDeploymentPair:
    """Record successful private acceptance on both verified pair members."""
    from validibot.validations.models import ValidatorExecutionDeployment

    locked = {
        item.pk: item
        for item in ValidatorExecutionDeployment.objects.select_for_update()
        .select_related("validator")
        .filter(pk__in=(service.pk, job.pk))
    }
    if len(locked) != DEPLOYMENT_PAIR_MEMBER_COUNT:
        raise ExecutionDeploymentResolutionError(
            "Both deployment pair members must exist before acceptance."
        )
    pair = verify_execution_deployment_pair(
        service=locked[service.pk],
        job=locked[job.pk],
    )
    existing_acceptance_times = {
        deployment.accepted_at
        for deployment in (pair.service, pair.job)
        if deployment.accepted_at is not None
    }
    if len(existing_acceptance_times) > 1:
        raise ExecutionDeploymentResolutionError(
            "Deployment pair members have different acceptance times."
        )
    if existing_acceptance_times:
        existing_time = existing_acceptance_times.pop()
        if accepted_at is not None and accepted_at != existing_time:
            raise ExecutionDeploymentResolutionError(
                "Deployment pair already has a different acceptance time."
            )
        moment = existing_time
    else:
        moment = accepted_at or timezone.now()
    for deployment in (pair.service, pair.job):
        if deployment.accepted_at is None:
            deployment.accepted_at = moment
            deployment.save(update_fields=["accepted_at", "modified"])
    return pair


def _route_locked_pair(
    *,
    pair: VerifiedDeploymentPair,
    all_routes: list[ValidatorExecutionDeployment],
    mode: ExecutionRoutingMode,
    deactivation_cause: ExecutionDeploymentDeactivationCause,
    operator_reason: str,
    changed_at,
) -> VerifiedDeploymentPair:
    """Apply one already-verified pair transition inside the caller's lock."""
    from validibot.validations.models import ValidatorExecutionDeployment

    target_roles = {
        pair.service.pk: (
            ExecutionDeploymentRoutingRole.PRIMARY
            if mode == ExecutionRoutingMode.NORMAL
            else ExecutionDeploymentRoutingRole.INACTIVE
        ),
        pair.job.pk: (
            ExecutionDeploymentRoutingRole.LONG_RUNNING
            if mode == ExecutionRoutingMode.NORMAL
            else (
                ExecutionDeploymentRoutingRole.PRIMARY
                if mode == ExecutionRoutingMode.JOB_ONLY
                else ExecutionDeploymentRoutingRole.INACTIVE
            )
        ),
    }
    previous_roles = {item.pk: item.routing_role for item in all_routes}
    previous_deactivation = {
        item.pk: (item.deactivated_at, item.deactivation_cause) for item in all_routes
    }
    active_routes = [
        item
        for item in all_routes
        if item.routing_role != ExecutionDeploymentRoutingRole.INACTIVE
    ]
    if active_routes:
        ValidatorExecutionDeployment.objects.filter(
            pk__in=[item.pk for item in active_routes]
        ).update(
            routing_role=ExecutionDeploymentRoutingRole.INACTIVE,
            activated_at=None,
            deactivated_at=changed_at,
            deactivation_cause=deactivation_cause,
            modified=changed_at,
        )

    for deployment in all_routes:
        target_role = target_roles.get(
            deployment.pk,
            ExecutionDeploymentRoutingRole.INACTIVE,
        )
        previous_role = previous_roles[deployment.pk]
        deployment.routing_role = target_role
        if target_role == ExecutionDeploymentRoutingRole.INACTIVE:
            deployment.activated_at = None
            if previous_role != ExecutionDeploymentRoutingRole.INACTIVE:
                deployment.deactivated_at = changed_at
                deployment.deactivation_cause = deactivation_cause
            elif deployment.pk in target_roles and deployment.deactivated_at is None:
                # A newly imported Service can enter Job-only mode before it
                # has ever occupied PRIMARY. This explicit transition starts
                # its first accountable continuous inactivity period.
                deployment.deactivated_at = changed_at
                deployment.deactivation_cause = deactivation_cause
        else:
            deployment.activated_at = changed_at
            deployment.deactivated_at = None
            deployment.deactivation_cause = ""

    # Active slots were cleared above, so these saves cannot temporarily
    # violate the per-Validator unique routing-slot constraints.
    for deployment in (pair.service, pair.job):
        deployment.save(
            update_fields=[
                "routing_role",
                "activated_at",
                "deactivated_at",
                "deactivation_cause",
                "modified",
            ]
        )

    changed = [
        deployment
        for deployment in all_routes
        if previous_roles[deployment.pk] != deployment.routing_role
    ]
    for deployment in changed:
        previous_role = previous_roles[deployment.pk]
        previous_deactivated_at, previous_deactivation_cause = previous_deactivation[
            deployment.pk
        ]
        action = (
            AuditAction.VALIDATOR_DEPLOYMENT_DEACTIVATED
            if deployment.routing_role == ExecutionDeploymentRoutingRole.INACTIVE
            else AuditAction.VALIDATOR_DEPLOYMENT_ACTIVATED
        )
        _record_operator_audit(
            deployment,
            action=action,
            changes={
                "routing_role": [previous_role, deployment.routing_role],
                "deactivated_at": [
                    (
                        previous_deactivated_at.isoformat()
                        if previous_deactivated_at
                        else None
                    ),
                    (
                        deployment.deactivated_at.isoformat()
                        if deployment.deactivated_at
                        else None
                    ),
                ],
                "deactivation_cause": [
                    previous_deactivation_cause,
                    deployment.deactivation_cause,
                ],
            },
            metadata={
                "backend_slug": pair.service.backend_slug,
                "backend_release": deployment.backend_release_identity,
                "routing_mode": mode.value,
                "pair_service_deployment_id": str(pair.service.pk),
                "pair_job_deployment_id": str(pair.job.pk),
                **({"operator_reason": operator_reason} if operator_reason else {}),
            },
        )
    return pair


@transaction.atomic
def route_execution_deployment_pair(
    *,
    service: ValidatorExecutionDeployment,
    job: ValidatorExecutionDeployment,
    mode: ExecutionRoutingMode | str,
    deactivation_cause: ExecutionDeploymentDeactivationCause | str,
    require_accepted: bool = True,
    operator_reason: str = "",
) -> VerifiedDeploymentPair:
    """Change normal or Job-only routing for one pair in one transaction."""
    from validibot.validations.models import ValidatorExecutionDeployment

    try:
        selected_mode = ExecutionRoutingMode(mode)
    except ValueError as exc:
        raise ValueError(
            "Pair routing mode must be normal, job-only, or inactive."
        ) from exc
    if selected_mode not in {
        ExecutionRoutingMode.NORMAL,
        ExecutionRoutingMode.JOB_ONLY,
        ExecutionRoutingMode.INACTIVE,
    }:
        raise ValueError("Pair routing mode must be normal, job-only, or inactive.")
    try:
        cause = ExecutionDeploymentDeactivationCause(deactivation_cause)
    except ValueError as exc:
        raise ValueError("Unknown deployment deactivation cause.") from exc
    reason = _normalize_operator_reason(operator_reason)

    routes = list(
        ValidatorExecutionDeployment.objects.select_for_update()
        .select_related("validator")
        .filter(validator_id=service.validator_id)
    )
    selected_by_id = {item.pk: item for item in routes}
    if service.pk not in selected_by_id or job.pk not in selected_by_id:
        raise ExecutionDeploymentResolutionError(
            "Both pair members must belong to the locked semantic Validator."
        )
    pair = verify_execution_deployment_pair(
        service=selected_by_id[service.pk],
        job=selected_by_id[job.pk],
    )
    if require_accepted and (
        pair.service.accepted_at is None or pair.job.accepted_at is None
    ):
        raise ExecutionDeploymentResolutionError(
            "Both pair members require successful private acceptance."
        )
    return _route_locked_pair(
        pair=pair,
        all_routes=routes,
        mode=selected_mode,
        deactivation_cause=cause,
        operator_reason=reason,
        changed_at=timezone.now(),
    )


@transaction.atomic
def activate_backend_release(
    *,
    backend_slug: str,
    backend_release_identity: str,
    mode: ExecutionRoutingMode | str = ExecutionRoutingMode.NORMAL,
    deactivation_cause: ExecutionDeploymentDeactivationCause
    | str = ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE,
    require_accepted: bool = True,
    operator_reason: str = "",
) -> tuple[VerifiedDeploymentPair, ...]:
    """Activate every compatible semantic Validator row for one backend release.

    Every candidate pair is verified before any routing row changes. Only
    Validators declaring ``backend_slug`` are locked, so updating EnergyPlus
    never locks or changes another backend.
    """
    from validibot.validations.constants import ValidatorAvailabilityState
    from validibot.validations.constants import ValidatorReleaseState
    from validibot.validations.models import Validator
    from validibot.validations.models import ValidatorExecutionDeployment

    try:
        selected_mode = ExecutionRoutingMode(mode)
    except ValueError as exc:
        raise ValueError(
            "Backend routing mode must be normal, job-only, or inactive."
        ) from exc
    if selected_mode not in {
        ExecutionRoutingMode.NORMAL,
        ExecutionRoutingMode.JOB_ONLY,
        ExecutionRoutingMode.INACTIVE,
    }:
        raise ValueError("Backend routing mode must be normal, job-only, or inactive.")
    try:
        cause = ExecutionDeploymentDeactivationCause(deactivation_cause)
    except ValueError as exc:
        raise ValueError("Unknown deployment deactivation cause.") from exc
    reason = _normalize_operator_reason(operator_reason)
    validators = list(
        Validator.objects.filter(
            execution_backend_slug=backend_slug,
            release_state=ValidatorReleaseState.PUBLISHED,
            is_system=True,
            is_enabled=True,
            availability_state=ValidatorAvailabilityState.AVAILABLE,
        ).order_by("pk")
    )
    if not validators:
        raise ExecutionDeploymentResolutionError(
            f"No published semantic Validator declares backend {backend_slug!r}."
        )
    validator_ids = [validator.pk for validator in validators]
    deployments = list(
        ValidatorExecutionDeployment.objects.select_for_update()
        .select_related("validator")
        .filter(
            validator_id__in=validator_ids,
            readiness_state=ExecutionDeploymentReadiness.READY,
        )
        .order_by("validator_id", "pk")
    )
    by_validator: dict[int, list[ValidatorExecutionDeployment]] = {
        validator_id: [] for validator_id in validator_ids
    }
    for deployment in deployments:
        by_validator[deployment.validator_id].append(deployment)

    pairs: list[VerifiedDeploymentPair] = []
    for validator in validators:
        # A failed provider reconciliation can leave an older immutable Cloud
        # Run revision in deployment history. Acceptance-only routing selects
        # the revision just observed by the importer; production routing
        # selects the latest such revision that completed private acceptance.
        pair = _resolve_backend_release_pair_from_deployments(
            by_validator[validator.pk],
            validator=validator,
            backend_slug=backend_slug,
            backend_release_identity=backend_release_identity,
            require_accepted=require_accepted,
        )
        pairs.append(pair)

    now = timezone.now()
    for pair in pairs:
        _route_locked_pair(
            pair=pair,
            all_routes=by_validator[pair.service.validator_id],
            mode=selected_mode,
            deactivation_cause=cause,
            operator_reason=reason,
            changed_at=now,
        )
    return tuple(pairs)


@transaction.atomic
def activate_backend_release_group(
    *,
    releases: dict[str, str],
    mode: ExecutionRoutingMode | str = ExecutionRoutingMode.NORMAL,
    deactivation_cause: ExecutionDeploymentDeactivationCause
    | str = ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE,
    operator_reason: str = "",
) -> dict[str, tuple[VerifiedDeploymentPair, ...]]:
    """Activate several accepted backends as one database transaction.

    Setup and explicit multi-backend updates stage and accept every candidate
    before calling this service. Nested backend activations share this outer
    transaction, so a failure in the final backend rolls back every earlier
    route change in the group.
    """
    if not releases:
        raise ValueError("At least one backend release is required.")
    activated: dict[str, tuple[VerifiedDeploymentPair, ...]] = {}
    for backend_slug in sorted(releases):
        activated[backend_slug] = activate_backend_release(
            backend_slug=backend_slug,
            backend_release_identity=releases[backend_slug],
            mode=mode,
            deactivation_cause=deactivation_cause,
            operator_reason=operator_reason,
        )
    return activated


@transaction.atomic
def activate_execution_deployment(
    deployment: ValidatorExecutionDeployment,
) -> ValidatorExecutionDeployment:
    """Compatibility wrapper that enters Job-only mode through pair routing."""
    from validibot.validations.models import ValidatorExecutionDeployment

    selected = ValidatorExecutionDeployment.objects.get(pk=deployment.pk)
    if selected.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_JOB:
        raise ExecutionDeploymentResolutionError(
            "Legacy single-deployment activation is disabled; select a pair."
        )
    service = (
        ValidatorExecutionDeployment.objects.filter(
            validator_id=selected.validator_id,
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
            backend_slug=selected.backend_slug,
            backend_release_identity=selected.backend_release_identity,
            backend_image_digest=selected.backend_image_digest,
            release_record_sha256=selected.release_record_sha256,
        )
        .order_by("-created")
        .first()
    )
    if service is None:
        raise ExecutionDeploymentResolutionError(
            "Job-only activation requires its same-release Service pair member."
        )
    pair = route_execution_deployment_pair(
        service=service,
        job=selected,
        mode=ExecutionRoutingMode.JOB_ONLY,
        deactivation_cause=ExecutionDeploymentDeactivationCause.SHAPE_ROLLBACK,
        require_accepted=False,
    )
    return pair.job


@transaction.atomic
def activate_service_with_job_compatibility(
    deployment: ValidatorExecutionDeployment,
) -> ValidatorExecutionDeployment:
    """Compatibility wrapper that activates a verified normal pair."""
    from validibot.validations.models import ValidatorExecutionDeployment

    selected = ValidatorExecutionDeployment.objects.get(pk=deployment.pk)
    if selected.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise ExecutionDeploymentResolutionError(
            "Service activation requires a Cloud Run Service deployment."
        )
    job = (
        ValidatorExecutionDeployment.objects.filter(
            validator_id=selected.validator_id,
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
            backend_slug=selected.backend_slug,
            backend_release_identity=selected.backend_release_identity,
            backend_image_digest=selected.backend_image_digest,
            release_record_sha256=selected.release_record_sha256,
        )
        .order_by("-created")
        .first()
    )
    if job is None:
        raise ExecutionDeploymentResolutionError(
            "Normal activation requires its same-release Job pair member."
        )
    pair = route_execution_deployment_pair(
        service=selected,
        job=job,
        mode=ExecutionRoutingMode.NORMAL,
        deactivation_cause=(
            ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
        ),
        require_accepted=False,
    )
    return pair.service


@transaction.atomic
def set_execution_deployment_block(
    deployment: ValidatorExecutionDeployment,
    *,
    blocked: bool,
    reason: str = "",
) -> ValidatorExecutionDeployment:
    """Set or clear a route block through one audited, locked operation."""
    from validibot.validations.models import ValidatorExecutionDeployment

    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    normalized_reason = reason.strip()
    if blocked and not normalized_reason:
        raise ValueError("Blocking a deployment requires an operator reason.")
    if selected.emergency_blocked == blocked and selected.emergency_block_reason == (
        normalized_reason if blocked else ""
    ):
        return selected
    previous_blocked = selected.emergency_blocked
    previous_reason = selected.emergency_block_reason
    selected.emergency_blocked = blocked
    selected.emergency_block_reason = normalized_reason if blocked else ""
    selected.save(
        update_fields=[
            "emergency_blocked",
            "emergency_block_reason",
            "modified",
        ]
    )
    _record_operator_audit(
        selected,
        action=(
            AuditAction.VALIDATOR_DEPLOYMENT_BLOCKED
            if blocked
            else AuditAction.VALIDATOR_DEPLOYMENT_UNBLOCKED
        ),
        changes={
            "emergency_blocked": [previous_blocked, blocked],
            "emergency_block_reason": [
                previous_reason,
                selected.emergency_block_reason,
            ],
        },
    )
    return selected
