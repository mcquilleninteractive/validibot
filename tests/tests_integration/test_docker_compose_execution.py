"""
Docker Compose execution backend integration tests.

These tests verify the complete execution flow for Docker Compose deployments
using Docker containers for advanced validators (EnergyPlus, FMU).

## Test Categories

1. **Built-in validators (JSON Schema, XML, etc.)**
   - Run in-process, no Docker required
   - Test via normal workflow execution

2. **Advanced validators (EnergyPlus, FMU)**
   - Run in Docker containers via DockerComposeExecutionBackend
   - Requires Docker daemon and validator images

## Prerequisites

For Docker-based tests:
- Docker-compatible daemon running (rootful or rootless Docker)
- Validator images available locally:
  - `validibot-validator-backend-energyplus:latest`
  - `validibot-validator-backend-fmu:latest`

## Running These Tests

```bash
# Run all Docker Compose integration tests
pytest tests/tests_integration/test_docker_compose_execution.py -v

# Run only non-Docker tests (fast)
pytest tests/tests_integration/test_docker_compose_execution.py -v -k "not docker"

# Run with verbose logging
pytest tests/tests_integration/test_docker_compose_execution.py -v --log-cli-level=INFO

# Rootless acceptance: first confirm doctor reports VB322=ok, then run the
# real EnergyPlus launch/mount/wait/result/cleanup path
pytest tests/tests_integration/test_docker_compose_execution.py \
  -v -k test_energyplus_execution_via_docker
```
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.status import HTTP_200_OK

from tests.helpers.assets import load_json_test_asset
from tests.helpers.assets import load_test_asset
from tests.helpers.payloads import invalid_product_payload
from tests.helpers.payloads import valid_product_payload
from tests.helpers.polling import extract_issues
from tests.helpers.polling import normalize_poll_url
from tests.helpers.polling import poll_until_complete
from tests.helpers.polling import start_workflow_url
from tests.helpers.workflows import create_workflow_step_with_default_bindings
from validibot.core.storage.registry import clear_storage_cache
from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.services.execution.registry import clear_backend_cache
from validibot.validations.tests.factories import RulesetFactory
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory

logger = logging.getLogger(__name__)


# =============================================================================
# Docker Availability Check
# =============================================================================


def docker_available() -> bool:
    """Check if Docker is available and validator images exist."""
    try:
        from validibot.validations.services.runners import get_validator_runner

        runner = get_validator_runner()
        return runner.is_available()
    except Exception:
        return False


def energyplus_image_available() -> bool:
    """Check if the EnergyPlus validator image is available."""
    if not docker_available():
        return False
    try:
        import docker

        client = docker.from_env()
        images = client.images.list(name="validibot-validator-backend-energyplus")
        return len(images) > 0
    except Exception:
        return False


skip_if_no_docker = pytest.mark.skipif(
    not docker_available(),
    reason="Docker not available",
)

skip_if_no_energyplus_image = pytest.mark.skipif(
    not energyplus_image_available(),
    reason="EnergyPlus validator image not available",
)


def fmu_image_available() -> bool:
    """Check if the FMU validator image is available."""
    if not docker_available():
        return False
    try:
        import docker

        client = docker.from_env()
        images = client.images.list(name="validibot-validator-backend-fmu")
        return len(images) > 0
    except Exception:
        return False


skip_if_no_fmu_image = pytest.mark.skipif(
    not fmu_image_available(),
    reason="FMU validator image not available",
)


# =============================================================================
# Built-in Validator Tests (No Docker Required)
# =============================================================================


@pytest.mark.django_db(transaction=True)
class TestBuiltInValidators:
    """
    Tests for built-in validators that run in-process.

    These tests verify that the validation workflow works correctly for
    validators that don't require Docker (JSON Schema, XML Schema, etc.).
    """

    @pytest.fixture
    def json_schema_workflow(self, api_client, system_validator_for):
        """Create a workflow with a JSON Schema validator."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org])
        user.set_current_org(org)
        grant_role(user, org, RoleCode.EXECUTOR)

        validator = system_validator_for(ValidationType.JSON_SCHEMA)

        schema = load_json_test_asset("assets/json/example_product_schema.json")
        ruleset = RulesetFactory(
            org=org,
            user=user,
            ruleset_type=ValidationType.JSON_SCHEMA,
            rules_text=json.dumps(schema),
            metadata={
                "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            },
        )

        workflow = WorkflowFactory(org=org, user=user)
        create_workflow_step_with_default_bindings(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=1,
        )

        api_client.force_authenticate(user=user)

        return {
            "org": org,
            "user": user,
            "validator": validator,
            "ruleset": ruleset,
            "workflow": workflow,
            "client": api_client,
        }

    def test_json_schema_valid_payload_succeeds(self, json_schema_workflow):
        """Valid JSON payload should pass validation."""
        client = json_schema_workflow["client"]
        workflow = json_schema_workflow["workflow"]
        org = json_schema_workflow["org"]

        url = start_workflow_url(workflow)
        payload = valid_product_payload()

        resp = client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), resp.content

        # Get polling URL
        loc = resp.headers.get("Location") or ""
        poll_url = normalize_poll_url(loc)
        if not poll_url:
            data = resp.json()
            run_id = data.get("id")
            if run_id:
                poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        # Poll until complete
        data, status = poll_until_complete(client, poll_url)
        assert status == HTTP_200_OK, f"Polling failed: {status} {data}"

        run_status = (data.get("status") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Expected SUCCEEDED, got {run_status}: {data}"
        )

        issues = extract_issues(data)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_json_schema_invalid_payload_fails(self, json_schema_workflow):
        """Invalid JSON payload should fail validation with issues."""
        client = json_schema_workflow["client"]
        workflow = json_schema_workflow["workflow"]
        org = json_schema_workflow["org"]

        url = start_workflow_url(workflow)
        payload = invalid_product_payload()

        resp = client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), resp.content

        # Get polling URL
        loc = resp.headers.get("Location") or ""
        poll_url = normalize_poll_url(loc)
        if not poll_url:
            data = resp.json()
            run_id = data.get("id")
            if run_id:
                poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        # Poll until complete
        data, status = poll_until_complete(client, poll_url)
        assert status == HTTP_200_OK, f"Polling failed: {status} {data}"

        run_status = (data.get("status") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Expected FAILED, got {run_status}: {data}"
        )

        issues = extract_issues(data)
        assert len(issues) >= 1, "Expected at least one issue"

        # Check that rating/max error is mentioned
        joined = " | ".join(str(issue) for issue in issues)
        assert ("rating" in joined) or ("maximum" in joined), (
            f"Expected rating/max error, got: {issues}"
        )


# =============================================================================
# Docker-Based Advanced Validator Tests
# =============================================================================


@pytest.fixture
def local_docker_execution_settings(tmp_path, settings):
    """Configure real filesystem storage for local container integration.

    The normal test settings use Django's in-memory media backend. Local
    containers require host paths for both managed resources and attempt data,
    so the media backend, data-storage root, and cached adapter instances must
    all agree for the fixture's complete lifetime.
    """
    storage_root = tmp_path / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    settings.DATA_STORAGE_ROOT = storage_root
    settings.DATA_STORAGE_OPTIONS = {"root": str(storage_root)}
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(settings.MEDIA_ROOT)},
        },
    }
    settings.VALIDATOR_RUNNER = "docker"
    settings.DATA_STORAGE_BACKEND = "local"
    clear_backend_cache()
    clear_storage_cache()

    yield storage_root

    clear_backend_cache()
    clear_storage_cache()


@pytest.mark.django_db(transaction=True)
@skip_if_no_docker
@skip_if_no_energyplus_image
class TestDockerEnergyPlusExecution:
    """
    Tests for EnergyPlus validation via Docker containers.

    These tests verify the DockerComposeExecutionBackend correctly:
    1. Uploads input envelope to local storage
    2. Runs the Docker container
    3. Reads the output envelope
    4. Returns results to the workflow
    """

    @pytest.fixture
    def energyplus_workflow(
        self,
        api_client,
        local_docker_execution_settings,
        system_validator_for,
    ):
        """Create a production-shaped EnergyPlus workflow and file bindings."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org])
        user.set_current_org(org)
        grant_role(user, org, RoleCode.EXECUTOR)

        validator = system_validator_for(ValidationType.ENERGYPLUS)

        # EnergyPlus doesn't use rules_text the same way, but needs a ruleset
        ruleset = RulesetFactory(
            org=org,
            user=user,
            ruleset_type=ValidationType.ENERGYPLUS,
            rules_text="{}",
        )

        workflow = WorkflowFactory(
            org=org,
            user=user,
            allowed_file_types=["json"],
        )

        # Register the weather file as a workflow resource. The binding service
        # resolves this relation into the named ``weather_file`` port; runtime
        # configuration never carries an untracked weather URI.
        weather_data = load_test_asset("data/energyplus/test_weather.epw")
        weather_file = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="EnergyPlus integration weather",
            filename="test_weather.epw",
            file=SimpleUploadedFile(
                "test_weather.epw",
                weather_data,
                content_type="application/vnd.energyplus.epw",
            ),
        )
        step = create_workflow_step_with_default_bindings(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=1,
        )
        WorkflowStepResourceFactory(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=weather_file,
        )

        api_client.force_authenticate(user=user)

        return {
            "org": org,
            "user": user,
            "validator": validator,
            "ruleset": ruleset,
            "workflow": workflow,
            "client": api_client,
            "storage_root": local_docker_execution_settings,
            "weather_resource": weather_file,
        }

    def test_energyplus_execution_via_docker(self, energyplus_workflow):
        """
        Test EnergyPlus validation executes via Docker container.

        This is an integration test that:
        1. Submits an EnergyPlus model via the API
        2. Waits for the Docker container to run
        3. Verifies the validation completes with results

        Why this matters: after VB322 confirms the selected engine is
        rootless, this same test is the acceptance path for the complete
        rootless lifecycle rather than a synthetic daemon-info check.
        """
        client = energyplus_workflow["client"]
        workflow = energyplus_workflow["workflow"]
        org = energyplus_workflow["org"]
        weather_resource = energyplus_workflow["weather_resource"]

        weather_path = Path(weather_resource.file.path)
        assert weather_path.is_file(), (
            "The workflow's bound EnergyPlus weather resource must exist before "
            f"dispatch: {weather_path}"
        )

        # Load the test model
        model_data = load_test_asset("data/energyplus/example_epjson.json")

        url = start_workflow_url(workflow)

        logger.info("Starting EnergyPlus validation via Docker")
        logger.info("URL: %s", url)

        resp = client.post(
            url,
            data=model_data,
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), (
            f"Failed to start workflow: {resp.status_code} {resp.content}"
        )

        # Get polling URL
        loc = resp.headers.get("Location") or ""
        poll_url = normalize_poll_url(loc)
        if not poll_url:
            data = resp.json()
            run_id = data.get("id")
            if run_id:
                poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        logger.info("Polling for completion: %s", poll_url)

        # Poll with longer timeout for EnergyPlus (container startup + simulation)
        data, status = poll_until_complete(
            client,
            poll_url,
            timeout_s=120.0,  # EnergyPlus can take time
            interval_s=2.0,
        )

        assert status == HTTP_200_OK, f"Polling failed: {status} {data}"

        run_status = (data.get("status") or "").upper()
        logger.info("Validation completed with status: %s", run_status)

        assert run_status == ValidationRunStatus.SUCCEEDED, (
            "Expected the real EnergyPlus contract to succeed, "
            f"got {run_status}: {data}"
        )

        # Log the steps for debugging
        steps = data.get("steps", [])
        for step in steps:
            logger.info(
                "Step %s: status=%s, issues=%d",
                step.get("name", "unknown"),
                step.get("status", "unknown"),
                len(step.get("issues", [])),
            )


# =============================================================================
# FMU Docker-Based Validator Tests
# =============================================================================


def load_fmu_asset() -> bytes:
    """Load the test FMU file from test assets."""
    asset_path = Path(__file__).parents[1] / "assets" / "fmu" / "Feedthrough.fmu"
    if not asset_path.exists():
        pytest.skip(f"Test FMU asset not found: {asset_path}")
    return asset_path.read_bytes()


@pytest.mark.django_db(transaction=True)
@skip_if_no_docker
@skip_if_no_fmu_image
class TestDockerFMUExecution:
    """
    Tests for FMU (Functional Mock-up Unit) validation via Docker containers.

    These tests verify the DockerComposeExecutionBackend correctly:
    1. Creates an FMU validator with an attached FMU file
    2. Uploads input envelope with FMU URI to local storage
    3. Runs the FMU Docker container
    4. Reads the output envelope
    5. Returns simulation results to the workflow
    """

    @pytest.fixture
    def fmu_workflow(self, api_client, local_docker_execution_settings):
        """Create a workflow with an FMU validator using the Feedthrough FMU."""
        from validibot.submissions.constants import SubmissionFileType
        from validibot.validations.constants import RulesetType
        from validibot.validations.models import Ruleset
        from validibot.validations.services.fmu import create_fmu_validator

        org = OrganizationFactory()
        user = UserFactory(orgs=[org])
        user.set_current_org(org)
        grant_role(user, org, RoleCode.EXECUTOR)

        # Load the FMU and create a SimpleUploadedFile
        fmu_data = load_fmu_asset()
        fmu_upload = SimpleUploadedFile(
            "Feedthrough.fmu",
            fmu_data,
            content_type="application/octet-stream",
        )

        # Create a project for the workflow
        from validibot.projects.tests.factories import ProjectFactory

        project = ProjectFactory(org=org)

        # Create the FMU validator using the service function
        # This handles FMU introspection and catalog seeding
        validator = create_fmu_validator(
            org=org,
            project=project,
            name="Feedthrough FMU Validator",
            upload=fmu_upload,
        )

        logger.info(
            "Created FMU validator: id=%s, fmu_model=%s",
            validator.id,
            validator.fmu_model,
        )

        # Create a ruleset for FMU
        ruleset = Ruleset.objects.create(
            org=org,
            user=user,
            name="FMU Test Rules",
            ruleset_type=RulesetType.FMU,
            version="1",
            rules_text="{}",
        )

        # The FMU is a validator resource; submissions carry typed simulation
        # inputs as JSON and never masquerade as the FMU executable itself.
        workflow = WorkflowFactory(
            org=org,
            user=user,
            project=project,
            allowed_file_types=[SubmissionFileType.JSON],
        )

        create_workflow_step_with_default_bindings(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=1,
            config={},
        )

        api_client.force_authenticate(user=user)

        return {
            "org": org,
            "user": user,
            "validator": validator,
            "ruleset": ruleset,
            "workflow": workflow,
            "project": project,
            "client": api_client,
            "storage_root": local_docker_execution_settings,
        }

    def test_fmu_execution_via_docker(self, fmu_workflow):
        """
        Test FMU validation executes via Docker container.

        This is an integration test that:
        1. Submits a binary file (FMU input parameters) via the API
        2. Waits for the Docker container to run the FMU simulation
        3. Verifies the validation completes with results
        """
        client = fmu_workflow["client"]
        workflow = fmu_workflow["workflow"]
        org = fmu_workflow["org"]

        # FMU submissions contain input parameter values as JSON. The bound
        # ``fmu_model`` file comes from the validator's immutable resource.
        submission_data = {
            "input_parameters": {
                "real_in": 1.5,
            },
        }

        url = start_workflow_url(workflow)

        logger.info("Starting FMU validation via Docker")
        logger.info("URL: %s", url)

        resp = client.post(
            url,
            data=json.dumps(submission_data),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), (
            f"Failed to start workflow: {resp.status_code} {resp.content}"
        )

        # Get polling URL
        loc = resp.headers.get("Location") or ""
        poll_url = normalize_poll_url(loc)
        if not poll_url:
            data = resp.json()
            run_id = data.get("id")
            if run_id:
                poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        logger.info("Polling for completion: %s", poll_url)

        # Poll with timeout for FMU (container startup + simulation)
        data, status = poll_until_complete(
            client,
            poll_url,
            timeout_s=60.0,
            interval_s=2.0,
        )

        assert status == HTTP_200_OK, f"Polling failed: {status} {data}"

        run_status = (data.get("status") or "").upper()
        logger.info("Validation completed with status: %s", run_status)

        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Expected the real FMU contract to succeed, got {run_status}: {data}"
        )

        # Log the steps for debugging
        steps = data.get("steps", [])
        for step in steps:
            logger.info(
                "Step %s: status=%s, issues=%d",
                step.get("name", "unknown"),
                step.get("status", "unknown"),
                len(step.get("issues", [])),
            )


# =============================================================================
# Direct Backend Tests (Lower Level)
# =============================================================================


@pytest.mark.django_db
@skip_if_no_docker
class TestDockerComposeBackendDirect:
    """
    Direct tests of the DockerComposeExecutionBackend without going through API.

    These tests verify the backend's internal behavior at a lower level.
    """

    def test_backend_is_available_when_docker_running(self):
        """Backend should report available when Docker is running."""
        from validibot.validations.services.execution.docker_compose import (
            DockerComposeExecutionBackend,
        )

        backend = DockerComposeExecutionBackend()
        assert backend.is_available() is True

    def test_backend_is_sync(self):
        """Docker Compose backend should be synchronous."""
        from validibot.validations.services.execution.docker_compose import (
            DockerComposeExecutionBackend,
        )

        backend = DockerComposeExecutionBackend()
        assert backend.is_async is False

    def test_get_container_image_default_naming(self):
        """Backend should generate correct image names."""
        from validibot.validations.services.execution.docker_compose import (
            DockerComposeExecutionBackend,
        )

        backend = DockerComposeExecutionBackend()

        # Default naming convention
        with override_settings(
            VALIDATOR_IMAGE_TAG="latest",
            VALIDATOR_IMAGE_REGISTRY="",
        ):
            image = backend.get_container_image("energyplus")
            assert image == "validibot-validator-backend-energyplus:latest"

        # With registry
        with override_settings(
            VALIDATOR_IMAGE_TAG="v1.0",
            VALIDATOR_IMAGE_REGISTRY="gcr.io/my-project",
        ):
            image = backend.get_container_image("fmu")
            assert image == "gcr.io/my-project/validibot-validator-backend-fmu:v1.0"

    @skip_if_no_energyplus_image
    def test_get_container_image_for_energyplus(self):
        """Backend should find the EnergyPlus image."""
        from validibot.validations.services.execution.docker_compose import (
            DockerComposeExecutionBackend,
        )

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("energyplus")

        # Verify image exists in Docker
        import docker

        client = docker.from_env()
        images = client.images.list(name=image.split(":")[0])
        assert len(images) > 0, f"Image {image} not found in Docker"
