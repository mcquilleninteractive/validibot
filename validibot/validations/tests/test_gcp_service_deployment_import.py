"""Tests for verifying and registering private Cloud Run Services.

Service activation is a routing change, not merely a deployment operation.
These tests prove that only a ready, digest-pinned, privately invokable Service
can be registered, that repeated observation converges, and that activation
uses the verified same-release Cloud Run Job as an atomic pair.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditLogEntry
from validibot.validations.constants import ExecutionDeploymentDeactivationCause
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionRoutingMode
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import (
    route_execution_deployment_pair,
)
from validibot.validations.services.execution.gcp_job_import import GCPJobObservation
from validibot.validations.services.execution.gcp_job_import import (
    register_observed_job_deployment,
)
from validibot.validations.services.execution.gcp_service_import import (
    GCPServiceImportError,
)
from validibot.validations.services.execution.gcp_service_import import (
    observe_cloud_run_service,
)
from validibot.validations.services.execution.gcp_service_import import (
    register_observed_service_deployment,
)
from validibot.validations.services.execution.gcp_service_import import (
    registered_service_observation_mismatches,
)
from validibot.validations.tests.factories import ValidatorFactory

PROJECT_ID = "validibot-prod"
REGION = "australia-southeast1"
RUNTIME_IDENTITY = "validator-runtime@validibot-prod.iam.gserviceaccount.com"
INVOKER_IDENTITY = "validator-invoker@validibot-prod.iam.gserviceaccount.com"
DIGEST = "sha256:" + "e" * 64
REVISION = "validibot-validator-service-shacl-00001-abc"
SERVICE_TIMEOUT_SECONDS = 1649
SERVICE_CPU_MILLIS = 2000
SERVICE_MEMORY_MIB = 4096
RELEASE_VERSION = "0.15.1"
RELEASE_RECORD_SHA256 = "b" * 64


def _resource(service_name: str) -> str:
    """Return the canonical provider name for a test Service."""
    return f"projects/{PROJECT_ID}/locations/{REGION}/services/{service_name}"


def _service(
    resource_name: str,
    *,
    backend: str = "shacl",
    image_ref: str | None = None,
    minimum_instances: int = 0,
    maximum_instances: int = 8,
):
    """Return the provider-shaped Service object consumed by the verifier."""
    service_name = resource_name.rsplit("/", 1)[-1]
    revision = f"projects/x/locations/{REGION}/services/x/revisions/{REVISION}"
    container = SimpleNamespace(
        image=image_ref or f"{REGION}-docker.pkg.dev/x/backend@{DIGEST}",
        env=[
            SimpleNamespace(name="VALIDIBOT_EXECUTION_SHAPE", value="service"),
            SimpleNamespace(name="VALIDIBOT_BACKEND_IMAGE_DIGEST", value=DIGEST),
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
        resources=SimpleNamespace(
            limits={"cpu": "2", "memory": "4Gi"},
            startup_cpu_boost=True,
        ),
    )
    return SimpleNamespace(
        name=resource_name,
        reconciling=False,
        latest_ready_revision=revision,
        latest_created_revision=revision,
        uri=f"https://{service_name}-{REGION}.a.run.app",
        template=SimpleNamespace(
            containers=[container],
            service_account=RUNTIME_IDENTITY,
            timeout=SimpleNamespace(seconds=SERVICE_TIMEOUT_SECONDS),
            max_instance_request_concurrency=1,
        ),
        scaling=SimpleNamespace(
            min_instance_count=minimum_instances,
            max_instance_count=maximum_instances,
        ),
    )


def _policy(*, broad_access: bool = False, unexpected_identity: bool = False):
    """Return an IAM policy granting only the dedicated task identity by default."""
    members = [f"serviceAccount:{INVOKER_IDENTITY}"]
    if broad_access:
        members.append("allUsers")
    if unexpected_identity:
        members.append("serviceAccount:unexpected@example.iam.gserviceaccount.com")
    return SimpleNamespace(
        bindings=[SimpleNamespace(role="roles/run.invoker", members=members)]
    )


def _observe(service_name: str):
    """Observe a valid private test Service."""
    resource_name = _resource(service_name)
    return observe_cloud_run_service(
        _service(resource_name),
        policy=_policy(),
        expected_resource_name=resource_name,
        invoker_service_account=INVOKER_IDENTITY,
    )


def _register_primary_job(validator):
    """Create the verified compatibility Job required before Service activation."""
    observation = GCPJobObservation(
        resource_name=(
            f"projects/{PROJECT_ID}/locations/{REGION}/jobs/"
            "validibot-validator-backend-shacl"
        ),
        job_name="validibot-validator-backend-shacl",
        revision="0.14.0",
        image_ref=f"{REGION}-docker.pkg.dev/x/job@{DIGEST}",
        image_digest=DIGEST,
        backend_slug="shacl",
        backend_release_version=RELEASE_VERSION,
        source_release_tag=f"shacl-v{RELEASE_VERSION}",
        release_record_sha256=RELEASE_RECORD_SHA256,
        runtime_service_account=RUNTIME_IDENTITY,
        maximum_execution_seconds=3600,
        maximum_cpu_millis=SERVICE_CPU_MILLIS,
        maximum_memory_mib=SERVICE_MEMORY_MIB,
    )
    deployment, _ = register_observed_job_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observation,
        activate_primary=False,
    )
    return deployment


def test_observation_records_exact_ready_revision_resources_and_private_iam():
    """Registration evidence must be derived from the live ready revision."""
    observation = _observe("validibot-validator-service-shacl")

    assert observation.revision == REVISION
    assert observation.image_digest == DIGEST
    assert observation.maximum_cpu_millis == SERVICE_CPU_MILLIS
    assert observation.maximum_memory_mib == SERVICE_MEMORY_MIB
    assert observation.invoker_service_account == INVOKER_IDENTITY


def test_observation_rejects_broad_invocation_even_with_dedicated_invoker():
    """An allUsers grant must fail readiness rather than weaken private execution."""
    resource_name = _resource("validibot-validator-service-fmu")

    with pytest.raises(GCPServiceImportError, match="broad invoker"):
        observe_cloud_run_service(
            _service(resource_name),
            policy=_policy(broad_access=True),
            expected_resource_name=resource_name,
            invoker_service_account=INVOKER_IDENTITY,
        )


def test_observation_rejects_any_additional_invoker_identity():
    """A second narrow principal must not weaken the dedicated task boundary."""
    resource_name = _resource("validibot-validator-service-shacl")

    with pytest.raises(GCPServiceImportError, match="unexpected invoker"):
        observe_cloud_run_service(
            _service(resource_name),
            policy=_policy(unexpected_identity=True),
            expected_resource_name=resource_name,
            invoker_service_account=INVOKER_IDENTITY,
        )


@pytest.mark.django_db
@patch(
    "validibot.validations.management.commands.verify_gcp_validator_deployments."
    "run_v2.ServicesClient"
)
def test_drift_verifier_fails_when_live_capacity_changes(
    services_client_class,
    settings,
):
    """An unaudited provider capacity edit must trigger scheduled drift alerting."""
    settings.GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT = INVOKER_IDENTITY
    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        execution_backend_slug="shacl",
        execution_runtime_contract="validibot-execution-v1",
    )
    _register_primary_job(validator)
    observation = _observe("validibot-validator-service-shacl-v0-15-0")
    deployment, _ = register_observed_service_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observation,
        maximum_execution_seconds=1500,
        activate_primary=False,
    )
    assert not registered_service_observation_mismatches(deployment, observation)
    job = ValidatorExecutionDeployment.objects.get(
        validator=validator,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
    )
    route_execution_deployment_pair(
        service=deployment,
        job=job,
        mode=ExecutionRoutingMode.NORMAL,
        deactivation_cause=(
            ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
        ),
        require_accepted=False,
    )

    client = services_client_class.return_value
    client.get_service.return_value = _service(
        deployment.provider_resource_name,
        minimum_instances=1,
    )
    client.get_iam_policy.return_value = _policy()

    with pytest.raises(CommandError, match=r"deployment\(s\) drifted"):
        call_command("verify_gcp_validator_deployments", "--json")


@pytest.mark.django_db
def test_pair_routing_switches_between_normal_and_job_only_atomically():
    """Normal and Job-only modes must move both pair members together."""
    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        execution_backend_slug="shacl",
        execution_runtime_contract="validibot-execution-v1",
    )
    job = _register_primary_job(validator)
    observation = _observe("validibot-validator-service-shacl")

    service, created = register_observed_service_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observation,
        maximum_execution_seconds=1500,
        activate_primary=False,
    )
    repeated, repeated_created = register_observed_service_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observation,
        maximum_execution_seconds=1500,
        activate_primary=False,
    )

    assert created is True
    assert repeated_created is False
    assert repeated.pk == service.pk
    pair = route_execution_deployment_pair(
        service=service,
        job=job,
        mode=ExecutionRoutingMode.NORMAL,
        deactivation_cause=(
            ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
        ),
        require_accepted=False,
    )
    service = pair.service
    job = pair.job
    assert service.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert job.routing_role == ExecutionDeploymentRoutingRole.LONG_RUNNING

    route_execution_deployment_pair(
        service=service,
        job=job,
        mode=ExecutionRoutingMode.JOB_ONLY,
        deactivation_cause=ExecutionDeploymentDeactivationCause.SHAPE_ROLLBACK,
        require_accepted=False,
    )

    service.refresh_from_db()
    job.refresh_from_db()
    assert service.routing_role == ExecutionDeploymentRoutingRole.INACTIVE
    assert job.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    assert service.deactivated_at is not None
    assert job.deactivated_at is None


@pytest.mark.django_db
def test_reverification_audits_mutable_service_level_capacity():
    """Warming changes must be visible without rewriting immutable revision facts."""
    validator = ValidatorFactory(validation_type=ValidationType.SHACL)
    service_name = "validibot-validator-service-shacl-v0-15-0"
    initial = _observe(service_name)
    deployment, _ = register_observed_service_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=initial,
        maximum_execution_seconds=1500,
        activate_primary=False,
    )
    resource_name = _resource(service_name)
    warmed = observe_cloud_run_service(
        _service(resource_name, minimum_instances=1),
        policy=_policy(),
        expected_resource_name=resource_name,
        invoker_service_account=INVOKER_IDENTITY,
    )

    register_observed_service_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=warmed,
        maximum_execution_seconds=1500,
        activate_primary=False,
    )

    deployment.refresh_from_db()
    assert deployment.minimum_instances == 1
    assert AuditLogEntry.objects.filter(
        action=AuditAction.VALIDATOR_DEPLOYMENT_CAPACITY_UPDATED,
        target_id=str(deployment.pk),
    ).exists()


@pytest.mark.django_db
@patch(
    "validibot.validations.management.commands."
    "sync_registered_gcp_validator_services.run_v2.ServicesClient"
)
def test_registered_service_sync_audits_provider_capacity_convergence(
    services_client_class,
    settings,
):
    """Cooling an inactive release must update and audit its stored capacity."""
    settings.GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT = INVOKER_IDENTITY
    validator = ValidatorFactory(validation_type=ValidationType.SHACL)
    service_name = "validibot-validator-service-shacl-v0-14-0"
    resource_name = _resource(service_name)
    deployment, _ = register_observed_service_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observe_cloud_run_service(
            _service(resource_name, minimum_instances=1),
            policy=_policy(),
            expected_resource_name=resource_name,
            invoker_service_account=INVOKER_IDENTITY,
        ),
        maximum_execution_seconds=1500,
        activate_primary=False,
    )
    AuditLogEntry.objects.filter(
        action=AuditAction.VALIDATOR_DEPLOYMENT_CAPACITY_UPDATED,
        target_id=str(deployment.pk),
    ).delete()
    client = services_client_class.return_value
    client.get_service.return_value = _service(
        deployment.provider_resource_name,
        minimum_instances=0,
    )
    client.get_iam_policy.return_value = _policy()

    call_command("sync_registered_gcp_validator_services")

    deployment.refresh_from_db()
    assert deployment.minimum_instances == 0
    assert AuditLogEntry.objects.filter(
        action=AuditAction.VALIDATOR_DEPLOYMENT_CAPACITY_UPDATED,
        target_id=str(deployment.pk),
    ).exists()


@pytest.mark.django_db
@patch(
    "validibot.validations.management.commands.sync_gcp_validator_services."
    "run_v2.ServicesClient"
)
def test_command_verifies_one_backend_without_activating(
    services_client_class,
    settings,
):
    """The operator command imports only the explicitly selected backend."""
    settings.GCP_PROJECT_ID = PROJECT_ID
    settings.GCP_REGION = REGION
    settings.GCP_APP_NAME = "validibot"
    settings.VALIDIBOT_STAGE = "prod"
    settings.GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT = INVOKER_IDENTITY
    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        execution_backend_slug="shacl",
        execution_runtime_contract="validibot-execution-v1",
    )
    client = services_client_class.return_value
    client.get_service.side_effect = lambda *, name: _service(name)
    client.get_iam_policy.return_value = _policy()

    call_command(
        "sync_gcp_validator_services",
        "--backend=shacl",
        "--service-name=vb-vs-shacl-v0-15-1",
    )

    routes = ValidatorExecutionDeployment.objects.all()
    assert routes.filter(validator=validator).count() == 1
    assert set(routes.values_list("validator__execution_backend_slug", flat=True)) == {
        "shacl"
    }
    assert set(routes.values_list("routing_role", flat=True)) == {
        ExecutionDeploymentRoutingRole.INACTIVE
    }
    assert client.get_service.call_count == routes.count()
    assert client.get_service.call_args.kwargs["name"].endswith(
        "/services/vb-vs-shacl-v0-15-1"
    )
