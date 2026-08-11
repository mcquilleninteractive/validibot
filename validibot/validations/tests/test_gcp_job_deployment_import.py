"""Tests for importing existing Cloud Run Jobs as managed deployment routes.

The importer reads one release-specific Job and creates one deployment row per
compatible semantic Validator without redeploying it, changing routes, or
manufacturing provenance for older attempts. These tests cover exact release
metadata, digest validation, idempotency, and backend-scoped command behavior.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command

from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.gcp_job_import import GCPJobImportError
from validibot.validations.services.execution.gcp_job_import import (
    observe_cloud_run_job,
)
from validibot.validations.services.execution.gcp_job_import import (
    register_observed_job_deployment,
)
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidatorFactory

PROJECT_ID = "validibot-prod"
REGION = "australia-southeast1"
RUNTIME_IDENTITY = "validator-runtime@validibot-prod.iam.gserviceaccount.com"
DIGEST = "sha256:" + "d" * 64
REVISION = "3b4ef06"
JOB_TIMEOUT_SECONDS = 3600
JOB_CPU_MILLIS = 2000
JOB_MEMORY_MIB = 4096
RELEASE_VERSION = "0.15.1"
RELEASE_RECORD_SHA256 = "a" * 64


def _job(
    resource_name: str,
    *,
    backend: str = "energyplus",
    image_ref: str | None = None,
):
    """Return the minimal provider-shaped Job object consumed by the importer."""
    container = SimpleNamespace(
        image=image_ref or f"{REGION}-docker.pkg.dev/x/backend@{DIGEST}",
        env=[
            SimpleNamespace(name="VALIDIBOT_BACKEND_SLUG", value=backend),
            SimpleNamespace(
                name="VALIDIBOT_BACKEND_RELEASE",
                value=RELEASE_VERSION,
            ),
            SimpleNamespace(
                name="VALIDIBOT_SOURCE_RELEASE_TAG",
                value=f"{backend}-v{RELEASE_VERSION}",
            ),
            SimpleNamespace(
                name="VALIDIBOT_RELEASE_RECORD_SHA256",
                value=RELEASE_RECORD_SHA256,
            ),
        ],
        resources=SimpleNamespace(limits={"cpu": "2", "memory": "4Gi"}),
    )
    task_template = SimpleNamespace(
        containers=[container],
        service_account=RUNTIME_IDENTITY,
        timeout=SimpleNamespace(seconds=JOB_TIMEOUT_SECONDS),
    )
    return SimpleNamespace(
        name=resource_name,
        reconciling=False,
        labels={"revision": REVISION},
        template=SimpleNamespace(template=task_template),
    )


def _resource(job_name: str) -> str:
    """Return the canonical provider name for a test Job."""
    return f"projects/{PROJECT_ID}/locations/{REGION}/jobs/{job_name}"


def test_observation_extracts_exact_digest_identity_and_resource_limits():
    """Readiness facts must come from the live provider spec, not defaults."""
    resource_name = _resource("validibot-validator-backend-energyplus")

    observation = observe_cloud_run_job(
        _job(resource_name, backend="energyplus"),
        expected_resource_name=resource_name,
    )

    assert observation.resource_name == resource_name
    assert observation.image_digest == DIGEST
    assert observation.maximum_execution_seconds == JOB_TIMEOUT_SECONDS
    assert observation.maximum_cpu_millis == JOB_CPU_MILLIS
    assert observation.maximum_memory_mib == JOB_MEMORY_MIB


def test_observation_rejects_floating_image_tag():
    """A mutable provider image must never be registered as verified provenance."""
    resource_name = _resource("validibot-validator-backend-fmu")

    with pytest.raises(GCPJobImportError, match="not pinned"):
        observe_cloud_run_job(
            _job(
                resource_name,
                backend="fmu",
                image_ref="example.invalid/fmu:latest",
            ),
            expected_resource_name=resource_name,
        )


@pytest.mark.django_db
def test_registration_is_idempotent_and_does_not_rewrite_historical_attempts():
    """Re-running import converges while legacy attempts remain explicitly unknown."""
    validator = ValidatorFactory(
        validation_type=ValidationType.FMU,
        execution_backend_slug="fmu",
        execution_runtime_contract="validibot-execution-v1",
    )
    attempt = ExecutionAttemptFactory()
    resource_name = _resource("validibot-validator-backend-fmu")
    observation = observe_cloud_run_job(
        _job(resource_name, backend="fmu"),
        expected_resource_name=resource_name,
    )

    first, first_created = register_observed_job_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observation,
        activate_primary=False,
    )
    second, second_created = register_observed_job_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observation,
        activate_primary=False,
    )

    assert first_created is True
    assert second_created is False
    assert second.pk == first.pk
    assert second.routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    attempt.refresh_from_db()
    assert attempt.deployment_id is None
    assert attempt.deployment_snapshot == {}


@pytest.mark.django_db
@patch(
    "validibot.validations.management.commands.sync_gcp_validator_deployments."
    "run_v2.JobsClient",
)
def test_command_imports_one_backend_without_changing_routes(
    jobs_client_class,
    settings,
):
    """One backend import must leave every compatible deployment inactive."""
    settings.GCP_PROJECT_ID = PROJECT_ID
    settings.GCP_REGION = REGION
    validator = ValidatorFactory(
        validation_type=ValidationType.FMU,
        execution_backend_slug="fmu",
        execution_runtime_contract="validibot-execution-v1",
    )
    job_name = "vb-vj-fmu-v0-15-1"
    jobs_client_class.return_value.get_job.side_effect = lambda *, name: _job(
        name,
        backend="fmu",
    )

    call_command(
        "sync_gcp_validator_deployments",
        "--backend=fmu",
        f"--job-name={job_name}",
    )

    routes = ValidatorExecutionDeployment.objects.all()
    route = routes.get(validator=validator)
    assert route.validator.validation_type == ValidationType.FMU
    assert set(routes.values_list("validator__execution_backend_slug", flat=True)) == {
        "fmu"
    }
    assert set(routes.values_list("routing_role", flat=True)) == {
        ExecutionDeploymentRoutingRole.INACTIVE
    }
    assert jobs_client_class.return_value.get_job.call_count == routes.count()
    requested = jobs_client_class.return_value.get_job.call_args.kwargs["name"]
    assert requested.endswith(f"/jobs/{job_name}")
