"""Tests for durable validator execution deployment identity.

These tests cover the provider-neutral record introduced before any traffic is
moved from Cloud Run Jobs to Services.  The record must preserve exact image
and provider provenance, validate its JSON contracts, and prevent two routes
from occupying the same validator routing slot.
"""

import json
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.test import override_settings
from django.utils import timezone

from ops.gcp import validator_release_control as release_control
from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditLogEntry
from validibot.core.constants import DeploymentTarget
from validibot.validations.acceptance import BACKENDS_BY_KEY
from validibot.validations.acceptance import AcceptanceReport
from validibot.validations.acceptance import ValidatorAcceptanceRunner
from validibot.validations.constants import ExecutionDeploymentDeactivationCause
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.constants import ExecutionRoutingMode
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorExecutionProfile
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployment_identity import (
    set_deployment_config_digests,
)
from validibot.validations.services.execution.deployments import (
    ExecutionDeploymentResolutionError,
)
from validibot.validations.services.execution.deployments import (
    activate_backend_release,
)
from validibot.validations.services.execution.deployments import (
    activate_backend_release_group,
)
from validibot.validations.services.execution.deployments import (
    effective_execution_budget_seconds,
)
from validibot.validations.services.execution.deployments import (
    effective_execution_profile,
)
from validibot.validations.services.execution.deployments import (
    ensure_backend_release_can_retire,
)
from validibot.validations.services.execution.deployments import (
    ensure_execution_deployment_can_retire,
)
from validibot.validations.services.execution.deployments import (
    mark_execution_deployment_pair_accepted,
)
from validibot.validations.services.execution.deployments import (
    record_execution_deployment_provider_deleted,
)
from validibot.validations.services.execution.deployments import (
    resolve_execution_deployment,
)
from validibot.validations.services.execution.deployments import (
    retire_backend_release_deployments,
)
from validibot.validations.services.execution.deployments import (
    retire_execution_deployment,
)
from validibot.validations.services.execution.deployments import (
    route_execution_deployment_pair,
)
from validibot.validations.services.execution.deployments import (
    set_execution_deployment_block,
)
from validibot.validations.services.execution.deployments import (
    update_execution_deployment_capacity,
)
from validibot.validations.services.execution.deployments import (
    verify_execution_deployment_pair,
)
from validibot.validations.services.execution.gcp_service_dispatch import (
    _prepare_pinned_service_dispatch,
)
from validibot.validations.services.execution.image_retention import (
    build_backend_image_protection_plan,
)
from validibot.validations.services.execution.image_retention import (
    validator_job_update_blockers,
)
from validibot.validations.services.execution_attempts import (
    get_or_create_execution_attempt,
)
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory

DIGEST = "sha256:" + "a" * 64
PROJECT_ID = "validibot-prod"
REGION = "australia-southeast1"
RUNTIME_IDENTITY = "validator-runtime@validibot-prod.iam.gserviceaccount.com"
EXPECTED_SHARED_DEPLOYMENT_COUNT = 2
ATTEMPT_BUDGET_SECONDS = 900
DEADLINE_TOLERANCE_SECONDS = 1
JOB_FINALIZATION_MARGIN_SECONDS = 120
FAST_PROFILE_BUDGET_SECONDS = 900
LONG_PROFILE_BUDGET_SECONDS = 3600
RELEASE_RECORD_SHA256 = "b" * 64


def _job_configuration(job_name="validibot-energyplus"):
    """Return the secret-free provider coordinates for one Cloud Run Job."""
    return {
        "project_id": PROJECT_ID,
        "region": REGION,
        "job_name": job_name,
        "runtime_service_account": RUNTIME_IDENTITY,
    }


def _job_capabilities():
    """Return the initial capability contract for the retained Job route."""
    return {
        "runtime_contract_version": "validibot-execution-v1",
        "maximum_execution_seconds": 1500,
        "execution_shape": "JOB",
        "status_lookup": "SUPPORTED",
        "cancellation": "SUPPORTED",
        "storage_capability": "gcs_downscoped_token",
        "storage_isolation": "attempt_scoped",
        "architectures": ["linux-amd64"],
        "maximum_cpu_millis": 4000,
        "maximum_memory_mib": 8192,
        "callback_authentication": "ATTEMPT_NONCE_AND_OIDC",
    }


def _job_deployment(*, validator, revision="v0.14.0", job_name=None, **overrides):
    """Build a valid unsaved Job deployment with one overridable contract."""
    job_name = job_name or f"validibot-{validator.slug}-{revision.replace('.', '-')}"
    configuration = _job_configuration(job_name)
    values = {
        "validator": validator,
        "provider_type": ExecutionProviderType.GCP,
        "deployment_kind": ExecutionDeploymentKind.CLOUD_RUN_JOB,
        "display_name": f"{validator.name} Job {revision}",
        "deployment_revision": revision,
        "provider_configuration": configuration,
        "provider_resource_name": (
            f"projects/{PROJECT_ID}/locations/{REGION}/jobs/{job_name}"
        ),
        "backend_release_identity": revision,
        "backend_image_ref": f"{REGION}-docker.pkg.dev/{PROJECT_ID}/validibot/"
        f"backend@{DIGEST}",
        "backend_image_digest": DIGEST,
        "expected_runtime_identity": RUNTIME_IDENTITY,
        "declared_capabilities": _job_capabilities(),
        "maximum_execution_seconds": 1500,
        "dispatch_timeout_seconds": 30,
        "minimum_instances": 0,
        "maximum_instances": 10,
        "concurrency": 1,
    }
    values.update(overrides)
    if (
        values.get("readiness_state") == ExecutionDeploymentReadiness.READY
        and "last_verification_details" not in overrides
    ):
        values["last_verification_details"] = {
            "observed_provider_revision": values["deployment_revision"],
            "observed_resource_name": values["provider_resource_name"],
            "observed_image_digest": values["backend_image_digest"],
            "checks": [
                {
                    "code": "provider.resource",
                    "succeeded": True,
                    "summary": "Provider identity matched.",
                }
            ],
        }
    return ValidatorExecutionDeployment(**values)


def _service_deployment(*, validator, revision="service-r1", **overrides):
    """Build a valid unsaved private Service deployment contract."""
    service_name = f"validibot-{validator.pk}-service"
    service_url = f"https://{service_name}-{REGION}.a.run.app"
    capabilities = _job_capabilities()
    capabilities.update(
        {
            "execution_shape": "REQUEST",
            "status_lookup": "UNSUPPORTED",
        }
    )
    values = {
        "validator": validator,
        "provider_type": ExecutionProviderType.GCP,
        "deployment_kind": ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        "display_name": f"{validator.name} Service {revision}",
        "deployment_revision": revision,
        "provider_configuration": {
            "project_id": PROJECT_ID,
            "region": REGION,
            "service_name": service_name,
            "service_url": service_url,
            "authentication_audience": service_url,
            "runtime_service_account": RUNTIME_IDENTITY,
            "invoker_service_account": (
                "validator-invoker@validibot-prod.iam.gserviceaccount.com"
            ),
        },
        "provider_resource_name": (
            f"projects/{PROJECT_ID}/locations/{REGION}/services/{service_name}"
        ),
        "route": service_url,
        "authentication_audience": service_url,
        "backend_release_identity": revision,
        "backend_image_ref": (
            f"{REGION}-docker.pkg.dev/{PROJECT_ID}/validibot/backend@{DIGEST}"
        ),
        "backend_image_digest": DIGEST,
        "expected_runtime_identity": RUNTIME_IDENTITY,
        "declared_capabilities": capabilities,
        "maximum_execution_seconds": 1500,
        "request_timeout_seconds": 1649,
        "dispatch_timeout_seconds": 1800,
        "minimum_instances": 0,
        "maximum_instances": 10,
        "concurrency": 1,
    }
    values.update(overrides)
    if (
        values.get("readiness_state") == ExecutionDeploymentReadiness.READY
        and "last_verification_details" not in overrides
    ):
        values["last_verification_details"] = {
            "observed_provider_revision": values["deployment_revision"],
            "observed_resource_name": values["provider_resource_name"],
            "observed_image_digest": values["backend_image_digest"],
            "checks": [
                {
                    "code": "provider.resource",
                    "succeeded": True,
                    "summary": "Provider identity matched.",
                }
            ],
        }
    return ValidatorExecutionDeployment(**values)


def _save_ready(deployment, *, role):
    """Persist a deployment with complete successful readiness evidence."""
    deployment.readiness_state = ExecutionDeploymentReadiness.READY
    deployment.routing_role = role
    deployment.verified_capabilities = deepcopy(deployment.declared_capabilities)
    deployment.last_verification_succeeded = True
    deployment.last_verified_at = timezone.now()
    deployment.last_verification_details = {
        "observed_provider_revision": deployment.deployment_revision,
        "observed_resource_name": deployment.provider_resource_name,
        "observed_image_digest": deployment.backend_image_digest,
        "checks": [
            {
                "code": "provider.resource",
                "succeeded": True,
                "summary": "Provider identity matched.",
            }
        ],
    }
    deployment.save()
    return deployment


def _release_pair(*, validator, backend: str, version: str, suffix: str = "1"):
    """Persist one fully verified inactive release-specific provider pair."""
    provider_backend = backend.replace("_", "-")
    common = {
        "backend_slug": backend,
        "backend_release_identity": version,
        "source_release_tag": f"{backend}-v{version}",
        "release_record_sha256": RELEASE_RECORD_SHA256,
    }
    service = _service_deployment(
        validator=validator,
        revision=f"{provider_backend}-service-{version}-{suffix}",
        **common,
    )
    job = _job_deployment(
        validator=validator,
        revision=f"{provider_backend}-job-{version}-{suffix}",
        **common,
    )
    for deployment in (service, job):
        deployment.readiness_state = ExecutionDeploymentReadiness.READY
        deployment.routing_role = ExecutionDeploymentRoutingRole.INACTIVE
        deployment.verified_capabilities = deepcopy(
            deployment.declared_capabilities,
        )
        deployment.last_verification_succeeded = True
        deployment.last_verified_at = timezone.now()
        deployment.last_verification_details = {
            "observed_provider_revision": deployment.deployment_revision,
            "observed_resource_name": deployment.provider_resource_name,
            "observed_image_digest": deployment.backend_image_digest,
            "checks": [
                {
                    "code": "provider.resource",
                    "succeeded": True,
                    "summary": "Provider identity matched.",
                }
            ],
        }
        set_deployment_config_digests(deployment)
        deployment.save()
    return service, job


@pytest.mark.django_db
def test_job_deployment_accepts_exact_provider_and_image_identity():
    """A complete digest-pinned Job route is valid before runtime behavior changes."""
    deployment = _job_deployment(validator=ValidatorFactory())

    deployment.full_clean()

    assert deployment.provider_resource_name.endswith(
        f"/jobs/{deployment.provider_configuration['job_name']}"
    )


@pytest.mark.django_db
def test_deployment_rejects_resource_name_not_derived_from_configuration():
    """Provider JSON and the indexed canonical resource cannot drift apart."""
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        provider_resource_name="projects/other/locations/elsewhere/jobs/drifted",
    )

    with pytest.raises(ValidationError) as exc_info:
        deployment.full_clean()

    assert "provider_resource_name" in exc_info.value.message_dict


@pytest.mark.django_db
def test_deployment_rejects_image_reference_not_pinned_to_its_digest():
    """A floating or mismatched image cannot become durable route provenance."""
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        backend_image_ref=(
            f"{REGION}-docker.pkg.dev/{PROJECT_ID}/validibot/backend:latest"
        ),
    )

    with pytest.raises(ValidationError) as exc_info:
        deployment.full_clean()

    assert "backend_image_ref" in exc_info.value.message_dict


@pytest.mark.django_db
def test_ready_deployment_requires_successful_timestamped_verification():
    """Routing must not treat an unverified database declaration as deployable."""
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        readiness_state=ExecutionDeploymentReadiness.READY,
    )

    with pytest.raises(ValidationError) as exc_info:
        deployment.full_clean()

    assert "readiness_state" in exc_info.value.message_dict


@pytest.mark.django_db(transaction=True)
def test_validator_has_at_most_one_deployment_in_each_active_routing_slot():
    """A database constraint protects activation from split-brain routing."""
    validator = ValidatorFactory()
    verified = _job_capabilities()
    first = _job_deployment(
        validator=validator,
        revision="r1",
        readiness_state=ExecutionDeploymentReadiness.READY,
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        verified_capabilities=deepcopy(verified),
        last_verification_succeeded=True,
        last_verified_at=timezone.now(),
    )
    first.full_clean()
    first.save()
    second = _job_deployment(
        validator=validator,
        revision="r2",
        readiness_state=ExecutionDeploymentReadiness.READY,
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        verified_capabilities=deepcopy(verified),
        last_verification_succeeded=True,
        last_verified_at=timezone.now(),
    )
    second.full_clean(exclude={"routing_role"}, validate_constraints=False)

    with pytest.raises(IntegrityError):
        ValidatorExecutionDeployment.objects.bulk_create([second])


@pytest.mark.django_db
def test_ready_deployment_requires_a_new_revision_for_identity_changes():
    """An accepted route cannot silently rewrite the provenance of later attempts."""
    verified = _job_capabilities()
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        readiness_state=ExecutionDeploymentReadiness.READY,
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        verified_capabilities=deepcopy(verified),
        last_verification_succeeded=True,
        last_verified_at=timezone.now(),
    )
    deployment.save()
    deployment.backend_image_digest = "sha256:" + "b" * 64
    deployment.backend_image_ref = deployment.backend_image_ref.replace(
        DIGEST,
        deployment.backend_image_digest,
    )

    with pytest.raises(ValidationError, match="create a new revision"):
        deployment.save()


@pytest.mark.django_db
def test_ready_deployment_cannot_downgrade_to_reopen_immutable_fields():
    """Readiness cannot be laundered through DRAFT to rewrite provenance."""
    deployment = _save_ready(
        _job_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    deployment.readiness_state = ExecutionDeploymentReadiness.DRAFT

    with pytest.raises(ValidationError, match="cannot return"):
        deployment.save(update_fields=["readiness_state", "modified"])

    deployment.refresh_from_db()
    assert deployment.readiness_state == ExecutionDeploymentReadiness.READY


@pytest.mark.django_db
def test_ready_deployment_allows_fresh_verification_observations():
    """Operators may refresh readiness evidence without revising route identity."""
    verified = _job_capabilities()
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        readiness_state=ExecutionDeploymentReadiness.READY,
        verified_capabilities=deepcopy(verified),
        last_verification_succeeded=True,
        last_verified_at=timezone.now(),
    )
    deployment.save()
    refreshed_details = deepcopy(deployment.last_verification_details)
    refreshed_details["checks"][0]["summary"] = "Provider identity re-verified."
    deployment.last_verification_details = refreshed_details
    deployment.last_verified_at = timezone.now()

    deployment.save()

    deployment.refresh_from_db()
    assert deployment.last_verification_details == refreshed_details


@pytest.mark.django_db
def test_retired_deployment_keeps_immutable_provider_provenance():
    """Cleanup must not make a historical deployment identity editable again."""
    deployment = _save_ready(
        _service_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    deployment = retire_execution_deployment(deployment)
    deployment.backend_image_digest = "sha256:" + "b" * 64
    deployment.backend_image_ref = deployment.backend_image_ref.replace(
        DIGEST,
        deployment.backend_image_digest,
    )

    with pytest.raises(ValidationError, match="create a new revision"):
        deployment.save()


@pytest.mark.django_db
def test_retired_deployment_rejects_capacity_mutation():
    """A deleted provider resource cannot acquire new stored warm capacity."""
    deployment = _save_ready(
        _service_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    deployment = retire_execution_deployment(deployment)

    with pytest.raises(ValueError, match="Only ready"):
        update_execution_deployment_capacity(
            deployment,
            minimum_instances=1,
            maximum_instances=10,
        )


@pytest.mark.django_db
def test_shared_job_resource_can_route_multiple_validator_contracts():
    """One backend Job may execute multiple FMU/library validator records."""
    first = ValidatorFactory()
    second = ValidatorFactory()
    job_name = "validibot-validator-backend-fmu"

    _job_deployment(validator=first, job_name=job_name).save()
    _job_deployment(validator=second, job_name=job_name).save()

    assert (
        ValidatorExecutionDeployment.objects.filter(
            provider_resource_name=(
                f"projects/{PROJECT_ID}/locations/{REGION}/jobs/{job_name}"
            )
        ).count()
        == EXPECTED_SHARED_DEPLOYMENT_COUNT
    )


@pytest.mark.django_db
def test_resolver_selects_primary_service_when_attempt_budget_fits():
    """Normal bounded work must use the explicitly activated Service route."""
    validator = ValidatorFactory()
    primary = _save_ready(
        _service_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    selected = resolve_execution_deployment(
        validator=validator,
        effective_budget_seconds=1200,
    )

    assert selected == primary


@pytest.mark.django_db
def test_long_running_profile_selects_job_before_dispatch():
    """An author's large-work choice must pin the retained Job explicitly."""
    validator = ValidatorFactory()
    _save_ready(
        _service_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    job_capabilities = _job_capabilities()
    job_capabilities["maximum_execution_seconds"] = 3600
    compatibility = _save_ready(
        _job_deployment(
            validator=validator,
            declared_capabilities=job_capabilities,
            maximum_execution_seconds=3600,
        ),
        role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
    )

    selected = resolve_execution_deployment(
        validator=validator,
        effective_budget_seconds=1800,
        execution_profile=ValidatorExecutionProfile.LONG_RUNNING,
    )

    assert selected == compatibility


@pytest.mark.django_db
def test_fast_response_profile_never_silently_falls_back_to_job():
    """An oversized fast attempt must fail before contact, not change systems."""
    validator = ValidatorFactory()
    _save_ready(
        _service_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    job_capabilities = _job_capabilities()
    job_capabilities["maximum_execution_seconds"] = 3600
    _save_ready(
        _job_deployment(
            validator=validator,
            declared_capabilities=job_capabilities,
            maximum_execution_seconds=3600,
        ),
        role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
    )

    with pytest.raises(
        ExecutionDeploymentResolutionError,
        match="Choose the Long-running profile",
    ):
        resolve_execution_deployment(
            validator=validator,
            effective_budget_seconds=1800,
            execution_profile=ValidatorExecutionProfile.FAST_RESPONSE,
        )


@pytest.mark.django_db
def test_long_running_profile_uses_primary_job_during_operator_rollback():
    """Rollback must keep large authored work usable without a fake Job slot."""
    validator = ValidatorFactory()
    job_capabilities = _job_capabilities()
    job_capabilities["maximum_execution_seconds"] = 3600
    primary_job = _save_ready(
        _job_deployment(
            validator=validator,
            declared_capabilities=job_capabilities,
            maximum_execution_seconds=3600,
        ),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    selected = resolve_execution_deployment(
        validator=validator,
        effective_budget_seconds=3600,
        execution_profile=ValidatorExecutionProfile.LONG_RUNNING,
    )

    assert selected == primary_job


@override_settings(
    DEPLOYMENT_TARGET=DeploymentTarget.GCP,
    VALIDATOR_DEFAULT_EXECUTION_SECONDS=FAST_PROFILE_BUDGET_SECONDS,
    VALIDATOR_TIMEOUT_SECONDS=LONG_PROFILE_BUDGET_SECONDS,
)
def test_execution_profiles_supply_simple_stable_default_budgets():
    """Authors should choose one profile without coordinating timeout values."""
    fast_step = SimpleNamespace(config={})
    long_step = SimpleNamespace(
        config={"execution_profile": ValidatorExecutionProfile.LONG_RUNNING}
    )

    assert effective_execution_profile(step=fast_step) == (
        ValidatorExecutionProfile.FAST_RESPONSE
    )
    assert (
        effective_execution_budget_seconds(step=fast_step)
        == FAST_PROFILE_BUDGET_SECONDS
    )
    assert (
        effective_execution_budget_seconds(step=long_step)
        == LONG_PROFILE_BUDGET_SECONDS
    )


@override_settings(
    DEPLOYMENT_TARGET=DeploymentTarget.SELF_HOSTED,
    VALIDATOR_DEFAULT_EXECUTION_SECONDS=FAST_PROFILE_BUDGET_SECONDS,
    VALIDATOR_TIMEOUT_SECONDS=LONG_PROFILE_BUDGET_SECONDS,
)
def test_single_route_self_hosted_uses_the_site_wide_validator_budget():
    """Local Docker work must not inherit GCP's shorter HTTP task ceiling."""
    imported_long_step = SimpleNamespace(
        config={"execution_profile": ValidatorExecutionProfile.LONG_RUNNING}
    )
    ordinary_step = SimpleNamespace(config={})

    assert (
        effective_execution_budget_seconds(step=ordinary_step)
        == LONG_PROFILE_BUDGET_SECONDS
    )
    assert (
        effective_execution_budget_seconds(step=imported_long_step)
        == LONG_PROFILE_BUDGET_SECONDS
    )


def test_unknown_execution_profile_fails_before_attempt_allocation():
    """Malformed imported workflow config must not guess a provider route."""
    step = SimpleNamespace(config={"execution_profile": "BURSTY"})

    with pytest.raises(ExecutionDeploymentResolutionError, match="must be one of"):
        effective_execution_profile(step=step)


@pytest.mark.django_db
def test_blocked_primary_fails_closed_without_using_compatibility_job():
    """An operator block is authoritative and cannot become runtime failover."""
    validator = ValidatorFactory()
    primary = _save_ready(
        _service_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    primary.emergency_blocked = True
    primary.emergency_block_reason = "Operator investigation"
    primary.save(update_fields=["emergency_blocked", "emergency_block_reason"])
    _save_ready(
        _job_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
    )

    with pytest.raises(ExecutionDeploymentResolutionError, match="blocked"):
        resolve_execution_deployment(
            validator=validator,
            effective_budget_seconds=1200,
        )


@pytest.mark.django_db
def test_emergency_block_service_requires_reason_and_writes_audit_event():
    """A route must not be silently disabled outside an accountable operation."""
    deployment = _save_ready(
        _job_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    with pytest.raises(ValueError, match="requires an operator reason"):
        set_execution_deployment_block(deployment, blocked=True)

    blocked = set_execution_deployment_block(
        deployment,
        blocked=True,
        reason="Provider incident under investigation",
    )

    assert blocked.emergency_blocked is True
    audit_entry = AuditLogEntry.objects.get(
        action=AuditAction.VALIDATOR_DEPLOYMENT_BLOCKED,
        target_id=str(deployment.pk),
    )
    assert audit_entry.metadata["provider_type"] == ExecutionProviderType.GCP
    assert audit_entry.metadata["deployment_kind"] == (
        ExecutionDeploymentKind.CLOUD_RUN_JOB
    )
    assert audit_entry.metadata["routing_role"] == (
        ExecutionDeploymentRoutingRole.PRIMARY
    )


@pytest.mark.django_db
def test_dispatch_claim_rechecks_a_block_applied_after_attempt_pinning():
    """A locked pre-contact check must keep blocked pinned work at PENDING."""
    validator = ValidatorFactory()
    deployment = _save_ready(
        _service_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    attempt, _created = get_or_create_execution_attempt(
        ValidationStepRunFactory(),
        validator=validator,
        managed=True,
        effective_budget_seconds=ATTEMPT_BUDGET_SECONDS,
    )
    set_execution_deployment_block(
        deployment,
        blocked=True,
        reason="Provider incident",
    )

    with pytest.raises(RuntimeError, match="emergency blocked"):
        _prepare_pinned_service_dispatch(
            attempt=attempt,
            expected_service_name=deployment.provider_configuration["service_name"],
            expected_image_digest=DIGEST,
            pending_inputs=None,
        )

    attempt.refresh_from_db()
    assert attempt.state == "PENDING"


@pytest.mark.django_db
def test_service_retirement_requires_inactive_cold_and_drained_deployment():
    """Cleanup must never delete a route that can still launch or callback."""
    deployment = _save_ready(
        _service_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    with pytest.raises(ExecutionDeploymentResolutionError, match="routing slot"):
        ensure_execution_deployment_can_retire(deployment)

    deployment.routing_role = ExecutionDeploymentRoutingRole.INACTIVE
    deployment.save(update_fields=["routing_role", "modified"])
    attempt = ExecutionAttemptFactory(deployment=deployment, state="RUNNING")
    with pytest.raises(ExecutionDeploymentResolutionError, match="nonterminal"):
        ensure_execution_deployment_can_retire(deployment)

    attempt.state = "COMPLETED"
    attempt.save(update_fields=["state", "modified"])
    retired = retire_execution_deployment(deployment)

    assert retired.readiness_state == ExecutionDeploymentReadiness.RETIRED
    assert AuditLogEntry.objects.filter(
        action=AuditAction.VALIDATOR_DEPLOYMENT_RETIRED,
        target_id=str(deployment.pk),
    ).exists()


@pytest.mark.django_db
def test_provider_deletion_checkpoint_requires_explicit_stale_route_repair():
    """Routine cleanup must not silently deactivate a deployment route."""
    deployment = _save_ready(
        _service_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    with pytest.raises(ExecutionDeploymentResolutionError, match="routing slot"):
        record_execution_deployment_provider_deleted(deployment)

    deployment.refresh_from_db()
    assert deployment.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert deployment.provider_deleted_at is None


@pytest.mark.django_db
def test_latest_only_checkpoint_deactivates_deleted_superseded_route():
    """Provider absence must repair stale historical routing before retirement.

    A semantic Validator version can leave its accepted route marked primary
    after the current version moves forward. Latest-only reconciliation must
    atomically make that historical row inactive and checkpoint provider
    deletion so the existing retirement phase can finish it.
    """
    deployment = _save_ready(
        _service_deployment(
            validator=ValidatorFactory(),
            backend_slug="energyplus",
            backend_release_identity="0.16.1",
        ),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    checkpointed = record_execution_deployment_provider_deleted(
        deployment,
        deactivate_superseded=True,
    )

    assert checkpointed.routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert checkpointed.activated_at is None
    assert checkpointed.deactivated_at == checkpointed.provider_deleted_at
    assert checkpointed.deactivation_cause == (
        ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
    )
    audit_entry = AuditLogEntry.objects.get(
        action=AuditAction.VALIDATOR_DEPLOYMENT_DEACTIVATED,
        target_id=str(deployment.pk),
    )
    assert audit_entry.changes["routing_role"] == [
        ExecutionDeploymentRoutingRole.PRIMARY,
        ExecutionDeploymentRoutingRole.INACTIVE,
    ]
    assert audit_entry.metadata["provider_resource_deleted"] is True
    assert audit_entry.metadata["latest_only_reconciliation"] is True


@pytest.mark.django_db
def test_latest_only_checkpoint_refuses_stale_route_with_unfinished_attempt():
    """Repair must fail closed when historical work still pins the deployment."""
    deployment = _save_ready(
        _service_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    ExecutionAttemptFactory(deployment=deployment, state="RUNNING")

    with pytest.raises(ExecutionDeploymentResolutionError, match="nonterminal"):
        record_execution_deployment_provider_deleted(
            deployment,
            deactivate_superseded=True,
        )

    deployment.refresh_from_db()
    assert deployment.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert deployment.provider_deleted_at is None


@pytest.mark.django_db
def test_managed_attempt_pins_route_snapshot_and_absolute_deadline():
    """Dispatch evidence must be complete before the first provider API call."""
    validator = ValidatorFactory()
    deployment = _save_ready(
        _job_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    step_run = ValidationStepRunFactory()
    before = timezone.now()

    attempt, created = get_or_create_execution_attempt(
        step_run,
        validator=validator,
        managed=True,
        effective_budget_seconds=ATTEMPT_BUDGET_SECONDS,
    )

    assert created is True
    assert attempt.deployment == deployment
    assert attempt.deployment_snapshot["deployment_id"] == str(deployment.pk)
    assert attempt.deployment_snapshot["backend_image_digest"] == DIGEST
    assert attempt.provider_resource_name == deployment.provider_resource_name
    assert attempt.backend_image_digest == DIGEST
    assert attempt.timeout_at is not None
    elapsed_seconds = (attempt.timeout_at - before).total_seconds()
    expected_deadline_seconds = ATTEMPT_BUDGET_SECONDS + JOB_FINALIZATION_MARGIN_SECONDS
    assert (
        expected_deadline_seconds - DEADLINE_TOLERANCE_SECONDS
        <= elapsed_seconds
        <= expected_deadline_seconds + DEADLINE_TOLERANCE_SECONDS
    )
    assert attempt.retry_policy_snapshot["schema_version"] == 2  # noqa: PLR2004
    assert attempt.retry_policy_snapshot["maximum_provider_dispatches"] == 1
    assert attempt.retry_policy_snapshot["requested_execution_profile"] == (
        ValidatorExecutionProfile.FAST_RESPONSE
    )


@pytest.mark.django_db
def test_backend_image_plan_unions_services_attempts_and_retirement_grace():
    """Cleanup protects deployments, active attempts, and Service grace images."""
    now = timezone.now()
    validator = ValidatorFactory()
    service_digest = "sha256:" + "b" * 64
    grace_digest = "sha256:" + "c" * 64
    job_digest = "sha256:" + "d" * 64
    active_service = _save_ready(
        _service_deployment(
            validator=validator,
            backend_image_digest=service_digest,
            backend_image_ref=f"registry/service@{service_digest}",
        ),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    retired_service = _save_ready(
        _service_deployment(
            validator=validator,
            revision="service-r2",
            backend_image_digest=grace_digest,
            backend_image_ref=f"registry/service@{grace_digest}",
        ),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    ValidatorExecutionDeployment.objects.filter(pk=retired_service.pk).update(
        readiness_state=ExecutionDeploymentReadiness.RETIRED,
        modified=now - timedelta(days=6),
    )
    job = _save_ready(
        _job_deployment(
            validator=validator,
            revision="v0.15.0",
            backend_image_digest=job_digest,
            backend_image_ref=f"registry/job@{job_digest}",
        ),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    idle_job_digest = "sha256:" + "e" * 64
    idle_job = _save_ready(
        _job_deployment(
            validator=ValidatorFactory(),
            revision="v0.15.1",
            backend_image_digest=idle_job_digest,
            backend_image_ref=f"registry/job@{idle_job_digest}",
        ),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    ExecutionAttemptFactory(
        deployment=job,
        state="RUNNING",
        backend_image_digest=job_digest,
        provider_resource_name=job.provider_resource_name,
    )

    plan = build_backend_image_protection_plan(grace_days=7, now=now)
    protected = {item.digest for item in plan.protected}

    assert active_service.backend_image_digest in protected
    assert grace_digest in protected
    assert job_digest in protected
    assert idle_job.backend_image_digest in protected
    assert plan.blockers == ()

    ValidatorExecutionDeployment.objects.filter(pk=retired_service.pk).update(
        modified=now - timedelta(days=8),
    )
    expired_plan = build_backend_image_protection_plan(grace_days=7, now=now)
    assert grace_digest not in {item.digest for item in expired_plan.protected}


@pytest.mark.django_db
def test_backend_image_plan_json_marker_returns_blockers_for_private_cleanup():
    """Cloud Run automation must recover a fail-closed inventory from logs."""
    deployment = _save_ready(
        _job_deployment(
            validator=ValidatorFactory(),
        ),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    ValidatorExecutionDeployment.objects.filter(pk=deployment.pk).update(
        backend_image_digest="",
        backend_image_ref="registry/job:mutable",
    )
    output = StringIO()

    call_command(
        "list_protected_validator_backend_digests",
        "--json-marker",
        stdout=output,
    )

    marker = "VALIDIBOT_BACKEND_IMAGE_PROTECTION_JSON="
    payload_line = output.getvalue().strip()
    assert payload_line.startswith(marker)
    assert f"non-retired Job deployment {deployment.pk}" in payload_line


@pytest.mark.django_db
def test_fixed_job_update_preflight_requires_application_attempt_drain():
    """A fixed Job cannot change image while a pinned attempt is nonterminal."""
    job_name = "validibot-validator-backend-energyplus"
    deployment = _save_ready(
        _job_deployment(
            validator=ValidatorFactory(),
            job_name=job_name,
        ),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    attempt = ExecutionAttemptFactory(
        deployment=deployment,
        state="PENDING",
        backend_image_digest=deployment.backend_image_digest,
        provider_resource_name=deployment.provider_resource_name,
    )

    blockers = validator_job_update_blockers(job_name=job_name)
    assert len(blockers) == 1
    assert str(attempt.pk) in blockers[0]

    attempt.state = "COMPLETED"
    attempt.save(update_fields=["state", "modified"])
    assert validator_job_update_blockers(job_name=job_name) == ()


# ──────────────────────────────────────────────────────────────────────
# Release-specific pair routing and provider lifecycle
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_pair_verification_rejects_a_different_release_record_digest():
    """A same-version image is not a pair when its signed release files differ."""
    validator = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    service, job = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.15.1",
    )
    ValidatorExecutionDeployment.objects.filter(pk=job.pk).update(
        release_record_sha256="c" * 64,
    )
    job.refresh_from_db()

    with pytest.raises(
        ExecutionDeploymentResolutionError,
        match="release-record",
    ):
        verify_execution_deployment_pair(service=service, job=job)


@pytest.mark.django_db
def test_pair_routing_tracks_continuous_inactivity_in_job_only_mode():
    """Only the unrouted Service starts inactivity during a shape rollback."""
    validator = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    service, job = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.15.1",
    )
    mark_execution_deployment_pair_accepted(service=service, job=job)

    normal = route_execution_deployment_pair(
        service=service,
        job=job,
        mode=ExecutionRoutingMode.NORMAL,
        deactivation_cause=(
            ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
        ),
    )
    job_only = route_execution_deployment_pair(
        service=normal.service,
        job=normal.job,
        mode=ExecutionRoutingMode.JOB_ONLY,
        deactivation_cause=ExecutionDeploymentDeactivationCause.SHAPE_ROLLBACK,
    )

    assert job_only.service.routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert job_only.service.deactivated_at is not None
    assert job_only.service.deactivation_cause == (
        ExecutionDeploymentDeactivationCause.SHAPE_ROLLBACK
    )
    assert job_only.job.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert job_only.job.deactivated_at is None
    assert not ValidatorExecutionDeployment.objects.filter(
        validator=validator,
        routing_role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
    ).exists()

    restored = route_execution_deployment_pair(
        service=job_only.service,
        job=job_only.job,
        mode=ExecutionRoutingMode.NORMAL,
        deactivation_cause=ExecutionDeploymentDeactivationCause.SHAPE_ROLLBACK,
    )

    assert restored.service.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert restored.service.deactivated_at is None
    assert restored.job.routing_role == (ExecutionDeploymentRoutingRole.LONG_RUNNING)


@pytest.mark.django_db
def test_backend_activation_does_not_change_another_backend_route():
    """Updating EnergyPlus must leave SHACL's selected provider pair unchanged."""
    energyplus = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    energyplus_pair = _release_pair(
        validator=energyplus,
        backend="energyplus",
        version="0.15.1",
    )
    mark_execution_deployment_pair_accepted(
        service=energyplus_pair[0],
        job=energyplus_pair[1],
    )
    shacl = ValidatorFactory(
        execution_backend_slug="shacl",
        execution_runtime_contract="validibot-execution-v1",
    )
    shacl_pair = _release_pair(
        validator=shacl,
        backend="shacl",
        version="0.15.1",
    )
    mark_execution_deployment_pair_accepted(
        service=shacl_pair[0],
        job=shacl_pair[1],
    )
    route_execution_deployment_pair(
        service=shacl_pair[0],
        job=shacl_pair[1],
        mode=ExecutionRoutingMode.JOB_ONLY,
        deactivation_cause=ExecutionDeploymentDeactivationCause.SHAPE_ROLLBACK,
    )

    activated = activate_backend_release(
        backend_slug="energyplus",
        backend_release_identity="0.15.1",
        mode=ExecutionRoutingMode.NORMAL,
    )

    assert len(activated) == 1
    shacl_pair[0].refresh_from_db()
    shacl_pair[1].refresh_from_db()
    assert shacl_pair[0].routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert shacl_pair[1].routing_role == ExecutionDeploymentRoutingRole.PRIMARY


@pytest.mark.django_db
def test_backend_activation_selects_the_most_recently_verified_service_revision():
    """Reconciliation may retain old revisions without making routing ambiguous.

    Cloud Run creates a new immutable revision when Service-level settings
    change. The importer keeps the old row for pinned attempts and audit
    history, while activation must route the revision that the immediately
    preceding provider observation verified.
    """
    validator = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    old_service, job = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.15.4",
        suffix="old",
    )
    activate_backend_release(
        backend_slug="energyplus",
        backend_release_identity="0.15.4",
        require_accepted=False,
    )
    replacement = _service_deployment(
        validator=validator,
        revision="energyplus-service-0.15.4-current",
        backend_slug="energyplus",
        backend_release_identity="0.15.4",
        source_release_tag="energyplus-v0.15.4",
        release_record_sha256=RELEASE_RECORD_SHA256,
    )
    set_deployment_config_digests(replacement)
    replacement = _save_ready(
        replacement,
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    ValidatorExecutionDeployment.objects.filter(pk=replacement.pk).update(
        last_verified_at=timezone.now() + timedelta(minutes=1),
    )

    activated = activate_backend_release(
        backend_slug="energyplus",
        backend_release_identity="0.15.4",
        require_accepted=False,
    )

    assert activated[0].service.pk == replacement.pk
    old_service.refresh_from_db()
    job.refresh_from_db()
    replacement.refresh_from_db()
    assert old_service.routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert replacement.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert job.routing_role == ExecutionDeploymentRoutingRole.LONG_RUNNING


@pytest.mark.django_db
@pytest.mark.parametrize(
    "backend_slug",
    ["energyplus", "fmu", "shacl", "schematron", "portfolio_manager", "pdf"],
)
def test_acceptance_certifies_current_service_revision_with_retained_history(
    backend_slug,
):
    """Rehearse candidate acceptance while old Service revisions remain.

    Provider reconciliation deliberately retains immutable deployment history.
    For every managed backend, this follows the production route sequence from
    normal preflight through Job-only acceptance recording and proves the exact
    current pair is certified without touching the historical Service row.
    """
    validator = ValidatorFactory(
        slug=f"v-{backend_slug[0]}-{len(backend_slug)}",
        execution_backend_slug=backend_slug,
        execution_runtime_contract="validibot-execution-v1",
    )
    old_service, job = _release_pair(
        validator=validator,
        backend=backend_slug,
        version="0.15.4",
        suffix="old",
    )
    current_service = _service_deployment(
        validator=validator,
        revision=f"{backend_slug.replace('_', '-')}-service-0.15.4-current",
        backend_slug=backend_slug,
        backend_release_identity="0.15.4",
        source_release_tag=f"{backend_slug}-v0.15.4",
        release_record_sha256=RELEASE_RECORD_SHA256,
    )
    set_deployment_config_digests(current_service)
    current_service = _save_ready(
        current_service,
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    ValidatorExecutionDeployment.objects.filter(pk=current_service.pk).update(
        last_verified_at=timezone.now() + timedelta(minutes=1),
    )

    activate_backend_release(
        backend_slug=backend_slug,
        backend_release_identity="0.15.4",
        mode=ExecutionRoutingMode.NORMAL,
        require_accepted=False,
    )
    normal_runner = ValidatorAcceptanceRunner(
        backend=backend_slug,
        release_tag=f"{backend_slug}-v0.15.4",
        run_storage_probe=False,
    )

    _, selected_service, selected_job = normal_runner._accepted_routes(
        BACKENDS_BY_KEY[backend_slug],
        "0.15.4",
        validator=validator,
    )

    assert selected_service.pk == current_service.pk
    assert selected_job.pk == job.pk

    activate_backend_release(
        backend_slug=backend_slug,
        backend_release_identity="0.15.4",
        mode=ExecutionRoutingMode.JOB_ONLY,
        require_accepted=False,
    )
    job_runner = ValidatorAcceptanceRunner(
        backend=backend_slug,
        release_tag=f"{backend_slug}-v0.15.4",
        routing_mode=ExecutionRoutingMode.JOB_ONLY,
        record_acceptance=True,
        run_storage_probe=False,
    )
    scenario = SimpleNamespace(
        workflow=SimpleNamespace(
            steps=SimpleNamespace(
                get=lambda: SimpleNamespace(validator=validator),
            )
        )
    )
    report = AcceptanceReport(
        backend=backend_slug,
        release_tag=f"{backend_slug}-v0.15.4",
        attempts_per_backend=1,
    )
    job_runner._record_pair_acceptance(report, (scenario,))

    old_service.refresh_from_db()
    current_service.refresh_from_db()
    job.refresh_from_db()
    assert old_service.routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert current_service.routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert job.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert old_service.accepted_at is None
    assert current_service.accepted_at is not None
    assert job.accepted_at == current_service.accepted_at
    assert report.checks[-1].status == "passed"


@pytest.mark.django_db
def test_backend_activation_restores_the_latest_accepted_service_revision():
    """Failed candidate acceptance must leave a known-good route recoverable.

    An interrupted same-release reconciliation can leave a newer verified
    Service revision that has not passed private acceptance. Normal routing
    must ignore that candidate, while the explicit acceptance-only path tested
    above may still select it for certification.
    """
    validator = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    accepted_service, job = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.15.4",
        suffix="a",
    )
    mark_execution_deployment_pair_accepted(service=accepted_service, job=job)
    activate_backend_release(
        backend_slug="energyplus",
        backend_release_identity="0.15.4",
    )
    candidate_service = _service_deployment(
        validator=validator,
        revision="energyplus-service-0.15.4-candidate",
        backend_slug="energyplus",
        backend_release_identity="0.15.4",
        source_release_tag="energyplus-v0.15.4",
        release_record_sha256=RELEASE_RECORD_SHA256,
    )
    set_deployment_config_digests(candidate_service)
    candidate_service = _save_ready(
        candidate_service,
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    ValidatorExecutionDeployment.objects.filter(pk=candidate_service.pk).update(
        last_verified_at=timezone.now() + timedelta(minutes=1),
    )

    restored = activate_backend_release(
        backend_slug="energyplus",
        backend_release_identity="0.15.4",
    )

    assert restored[0].service.pk == accepted_service.pk
    accepted_service.refresh_from_db()
    candidate_service.refresh_from_db()
    assert accepted_service.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert candidate_service.routing_role == ExecutionDeploymentRoutingRole.INACTIVE


@pytest.mark.django_db
def test_release_rollback_exports_its_own_version_and_reason_to_status():
    """Audit production and status reconstruction must agree on the old release."""
    validator = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    outgoing = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.18.0",
        suffix="out",
    )
    rollback = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.17.0",
        suffix="rb",
    )
    for service, job in (outgoing, rollback):
        mark_execution_deployment_pair_accepted(service=service, job=job)
    activate_backend_release(
        backend_slug="energyplus",
        backend_release_identity="0.18.0",
    )

    activate_backend_release(
        backend_slug="energyplus",
        backend_release_identity="0.17.0",
        deactivation_cause=(ExecutionDeploymentDeactivationCause.RELEASE_ROLLBACK_FROM),
        operator_reason="Repaired callback routing before exact recovery.",
    )

    event = AuditLogEntry.objects.filter(
        action=AuditAction.VALIDATOR_DEPLOYMENT_DEACTIVATED,
        target_id=str(outgoing[0].pk),
    ).latest("occurred_at")
    assert event.metadata["backend_release"] == "0.18.0"
    assert event.metadata["operator_reason"] == (
        "Repaired callback routing before exact recovery."
    )

    output = StringIO()
    call_command("export_validator_release_state", stdout=output)
    database = json.loads(output.getvalue())
    intent = release_control.BackendIntent(
        "energyplus",
        "energyplus",
        "0.18.0",
        "validibot-validator-backend-energyplus",
    )

    status = release_control.calculate_status((intent,), database)
    rolled_back = status["backends"][0]["rolled_back_from"]

    assert any(
        row["deployment_revision"] == outgoing[0].deployment_revision
        for row in database["deployments"]
    )
    assert [fact["version"] for fact in rolled_back] == ["0.18.0"]
    assert rolled_back[0]["reason"] == (
        "Repaired callback routing before exact recovery."
    )


@pytest.mark.django_db
def test_exported_release_state_separates_historical_managed_deployments():
    """Latest-only retries need history without offering obsolete rollbacks.

    A semantic Validator whose runtime config disappeared is not a current
    routing target, but its provider rows still require deletion checkpoints
    and retirement. The export therefore keeps status-facing ``deployments``
    current-only while exposing every managed row through
    ``deployment_history`` for cleanup.
    """
    historical_validator = ValidatorFactory(
        slug="ep-history",
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
        availability_state=ValidatorAvailabilityState.MISSING_CONFIG,
    )
    historical_pair = _release_pair(
        validator=historical_validator,
        backend="energyplus",
        version="0.14.0",
        suffix="h",
    )

    output = StringIO()
    call_command("export_validator_release_state", stdout=output)
    database = json.loads(output.getvalue())

    historical_ids = {str(item.pk) for item in historical_pair}
    assert not historical_ids & {
        row["deployment_id"] for row in database["deployments"]
    }
    assert historical_ids <= {
        row["deployment_id"] for row in database["deployment_history"]
    }


@pytest.mark.django_db
def test_backend_activation_validates_every_pair_before_writing_routes():
    """One bad semantic Validator pair must leave the whole backend inactive."""
    first_validator = ValidatorFactory(
        slug="ep-semantic-a",
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    first_pair = _release_pair(
        validator=first_validator,
        backend="energyplus",
        version="0.15.1",
        suffix="first",
    )
    mark_execution_deployment_pair_accepted(
        service=first_pair[0],
        job=first_pair[1],
    )
    second_validator = ValidatorFactory(
        slug="ep-semantic-b",
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    second_pair = _release_pair(
        validator=second_validator,
        backend="energyplus",
        version="0.15.1",
        suffix="second",
    )
    mark_execution_deployment_pair_accepted(
        service=second_pair[0],
        job=second_pair[1],
    )
    ValidatorExecutionDeployment.objects.filter(pk=second_pair[1].pk).update(
        release_record_sha256="c" * 64,
    )

    with pytest.raises(
        ExecutionDeploymentResolutionError,
        match="release-record",
    ):
        activate_backend_release(
            backend_slug="energyplus",
            backend_release_identity="0.15.1",
        )

    first_pair[0].refresh_from_db()
    first_pair[1].refresh_from_db()
    assert first_pair[0].routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert first_pair[1].routing_role == ExecutionDeploymentRoutingRole.INACTIVE


@pytest.mark.django_db
def test_group_activation_rolls_back_every_backend_when_one_backend_is_invalid():
    """Initial setup must never leave a partially active managed backend set."""
    energyplus_validator = ValidatorFactory(
        slug="ep-group",
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    energyplus_pair = _release_pair(
        validator=energyplus_validator,
        backend="energyplus",
        version="0.15.1",
        suffix="g",
    )
    mark_execution_deployment_pair_accepted(
        service=energyplus_pair[0],
        job=energyplus_pair[1],
    )
    shacl_validator = ValidatorFactory(
        slug="shacl-group",
        execution_backend_slug="shacl",
        execution_runtime_contract="validibot-execution-v1",
    )
    shacl_pair = _release_pair(
        validator=shacl_validator,
        backend="shacl",
        version="0.15.1",
        suffix="g",
    )
    mark_execution_deployment_pair_accepted(
        service=shacl_pair[0],
        job=shacl_pair[1],
    )
    ValidatorExecutionDeployment.objects.filter(pk=shacl_pair[1].pk).update(
        release_record_sha256="c" * 64,
    )

    with pytest.raises(
        ExecutionDeploymentResolutionError,
        match="release-record",
    ):
        activate_backend_release_group(
            releases={
                "energyplus": "0.15.1",
                "shacl": "0.15.1",
            }
        )

    energyplus_pair[0].refresh_from_db()
    energyplus_pair[1].refresh_from_db()
    assert energyplus_pair[0].routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert energyplus_pair[1].routing_role == ExecutionDeploymentRoutingRole.INACTIVE


@pytest.mark.django_db
def test_retirement_allows_unaccepted_older_service_revision_history():
    """Immutable history must not prevent cleanup of an accepted current pair.

    Provider imports retain superseded Cloud Run Service revisions because
    attempts and audit evidence may still reference them. Once one complete
    pair was accepted, an older revision that never received traffic must not
    make the entire drained release unretirable.
    """
    validator = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    service, job = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.14.0",
        suffix="c",
    )
    accepted = mark_execution_deployment_pair_accepted(service=service, job=job)
    historical_service = _service_deployment(
        validator=validator,
        revision="energyplus-service-0.14.0-history",
        backend_slug="energyplus",
        backend_release_identity="0.14.0",
        source_release_tag="energyplus-v0.14.0",
        release_record_sha256=RELEASE_RECORD_SHA256,
    )
    set_deployment_config_digests(historical_service)
    historical_service = _save_ready(
        historical_service,
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    now = timezone.now()
    drained_at = now - timedelta(days=8)
    ValidatorExecutionDeployment.objects.filter(
        pk__in=[accepted.service.pk, accepted.job.pk, historical_service.pk],
    ).update(
        deactivated_at=drained_at,
        deactivation_cause=(
            ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
        ),
    )
    ValidatorExecutionDeployment.objects.filter(pk=historical_service.pk).update(
        last_verified_at=now - timedelta(days=1),
    )
    service.refresh_from_db()
    job.refresh_from_db()
    historical_service.refresh_from_db()

    ensure_backend_release_can_retire(
        [historical_service, service, job],
        now=now,
    )


@pytest.mark.django_db
def test_retirement_waits_for_drain_and_keeps_rows_and_attempts():
    """Provider deletion checkpoints retire no history and block unfinished work."""
    validator = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    service, job = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.14.0",
    )
    accepted = mark_execution_deployment_pair_accepted(service=service, job=job)
    drained_at = timezone.now() - timedelta(days=8)
    ValidatorExecutionDeployment.objects.filter(
        pk__in=[accepted.service.pk, accepted.job.pk],
    ).update(
        deactivated_at=drained_at,
        deactivation_cause=(
            ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
        ),
    )
    service.refresh_from_db()
    job.refresh_from_db()
    attempt = ExecutionAttemptFactory(deployment=job, state="RUNNING")

    with pytest.raises(
        ExecutionDeploymentResolutionError,
        match="nonterminal",
    ):
        ensure_backend_release_can_retire([service, job])

    attempt.state = "COMPLETED"
    attempt.save(update_fields=["state", "modified"])
    record_execution_deployment_provider_deleted(service)
    record_execution_deployment_provider_deleted(job)
    retired = retire_backend_release_deployments(
        backend_slug="energyplus",
        backend_release_identity="0.14.0",
        reason="Provider pair deleted after the verified drain.",
    )

    assert len(retired) == EXPECTED_SHARED_DEPLOYMENT_COUNT
    assert all(
        item.readiness_state == ExecutionDeploymentReadiness.RETIRED for item in retired
    )
    assert all(item.provider_deleted_at is not None for item in retired)
    assert (
        ValidatorExecutionDeployment.objects.filter(
            pk__in=[service.pk, job.pk],
        ).count()
        == EXPECTED_SHARED_DEPLOYMENT_COUNT
    )
    assert type(attempt).objects.filter(pk=attempt.pk).exists()


@pytest.mark.django_db
def test_latest_only_bootstrap_can_retire_wholly_unaccepted_candidate():
    """Failed private acceptance must not strand deleted candidate providers.

    The no-user latest-only path may retire a complete candidate pair that
    never passed acceptance, but only after both rows are inactive, every
    attempt is terminal, and provider absence has been checkpointed.
    """
    validator = ValidatorFactory(
        slug="pm-failed",
        execution_backend_slug="portfolio_manager",
        execution_runtime_contract="validibot-execution-v1",
    )
    service, job = _release_pair(
        validator=validator,
        backend="portfolio_manager",
        version="0.16.4",
        suffix="f",
    )
    deactivated_at = timezone.now()
    ValidatorExecutionDeployment.objects.filter(pk__in=[service.pk, job.pk]).update(
        deactivated_at=deactivated_at,
        deactivation_cause=ExecutionDeploymentDeactivationCause.ACCEPTANCE_FAILURE,
    )
    record_execution_deployment_provider_deleted(service)
    record_execution_deployment_provider_deleted(job)

    retired = retire_backend_release_deployments(
        backend_slug="portfolio_manager",
        backend_release_identity="0.16.4",
        reason="Empty-installation failed-candidate reconciliation.",
        drain_days=0,
        allow_immediate=True,
        allow_unaccepted_candidate=True,
    )

    assert len(retired) == EXPECTED_SHARED_DEPLOYMENT_COUNT
    assert all(item.accepted_at is None for item in retired)
    assert all(
        item.readiness_state == ExecutionDeploymentReadiness.RETIRED for item in retired
    )


@pytest.mark.django_db
def test_latest_only_rejects_partially_accepted_candidate_pair():
    """One-sided acceptance evidence must remain a fail-closed cleanup blocker."""
    validator = ValidatorFactory(
        slug="pm-partial",
        execution_backend_slug="portfolio_manager",
        execution_runtime_contract="validibot-execution-v1",
    )
    service, job = _release_pair(
        validator=validator,
        backend="portfolio_manager",
        version="0.16.4",
        suffix="p",
    )
    now = timezone.now()
    ValidatorExecutionDeployment.objects.filter(pk=service.pk).update(accepted_at=now)
    ValidatorExecutionDeployment.objects.filter(pk__in=[service.pk, job.pk]).update(
        deactivated_at=now,
        deactivation_cause=ExecutionDeploymentDeactivationCause.ACCEPTANCE_FAILURE,
    )
    record_execution_deployment_provider_deleted(service)
    record_execution_deployment_provider_deleted(job)

    with pytest.raises(
        ExecutionDeploymentResolutionError,
        match="partially accepted provider pair",
    ):
        retire_backend_release_deployments(
            backend_slug="portfolio_manager",
            backend_release_identity="0.16.4",
            reason="Must not erase inconsistent acceptance evidence.",
            drain_days=0,
            allow_immediate=True,
            allow_unaccepted_candidate=True,
        )


@pytest.mark.django_db
def test_latest_only_bootstrap_can_retire_a_terminal_pair_immediately():
    """The explicit empty-installation path may skip time, not safety checks."""
    validator = ValidatorFactory(
        execution_backend_slug="energyplus",
        execution_runtime_contract="validibot-execution-v1",
    )
    service, job = _release_pair(
        validator=validator,
        backend="energyplus",
        version="0.14.0",
        suffix="lo",
    )
    accepted = mark_execution_deployment_pair_accepted(service=service, job=job)
    now = timezone.now()
    ValidatorExecutionDeployment.objects.filter(
        pk__in=[accepted.service.pk, accepted.job.pk],
    ).update(
        deactivated_at=now,
        deactivation_cause=(
            ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
        ),
    )
    service.refresh_from_db()
    job.refresh_from_db()
    record_execution_deployment_provider_deleted(service)
    record_execution_deployment_provider_deleted(job)

    retired = retire_backend_release_deployments(
        backend_slug="energyplus",
        backend_release_identity="0.14.0",
        reason="Empty-installation latest-only reconciliation.",
        drain_days=0,
        allow_immediate=True,
    )

    assert len(retired) == EXPECTED_SHARED_DEPLOYMENT_COUNT
    assert all(
        item.readiness_state == ExecutionDeploymentReadiness.RETIRED for item in retired
    )
