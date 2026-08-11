"""
Tests for ExecutionBackend implementations.

This module verifies the execution backend abstraction layer — the bridge
between Django-side validator orchestration and infrastructure-specific
execution targets (Docker Compose, GCP Cloud Run, etc.).

## What's tested

- **ExecutionRequest**: The dataclass that carries all context from Django
  models through to the backend.  Uses real Django models via FactoryBoy
  to verify UUID serialization, case normalization, and relationship
  traversal work correctly against the actual model graph.

- **ExecutionResponse**: The pure dataclass returned by backends.  No
  Django models needed — tests verify field defaults and state flags.

- **Backend registry**: The ``get_execution_backend()`` factory that maps
  ``DEPLOYMENT_TARGET`` / ``VALIDATOR_RUNNER`` settings to backend
  instances.  Tests verify mapping logic and caching.

- **DockerComposeExecutionBackend**: The sync backend used in local dev
  and CI.  Tests verify availability checks, container image resolution,
  and error handling when Docker is unavailable.

- **Envelope building**: The ``build_energyplus_input_envelope()`` helper
  that constructs typed Pydantic envelopes for the validator container.
  Tests verify ``skip_callback`` propagation and dynamic file metadata
  (IDF vs epJSON name/mime_type derivation from URI).

## Testing approach

Earlier versions of this file used hand-rolled mock classes (``MockOrg``,
``MockValidator``, etc.) for Django models.  These have been replaced with
FactoryBoy factories and ``@pytest.mark.django_db`` to ensure tests
exercise real Django model behavior (UUID fields, property accessors,
foreign key traversal, ``get_content()`` methods).  Tests that don't
touch Django models (``TestExecutionResponse``, ``TestBackendFactory``,
most of ``TestDockerComposeExecutionBackend``) remain DB-free for speed.
"""

import json
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from validibot_shared.canonicalization import compute_callback_nonce_commitment
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import SupportedMimeType

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.services.cloud_run.envelope_builder import (
    build_energyplus_input_envelope,
)
from validibot.validations.services.execution.base import ExecutionRequest
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.execution.docker_compose import (
    DockerComposeExecutionBackend,
)
from validibot.validations.services.execution.gcp import CloudRunJobsExecutionBackend
from validibot.validations.services.execution.gcp_service import (
    CloudRunServiceExecutionBackend,
)
from validibot.validations.services.execution.registry import clear_backend_cache
from validibot.validations.services.execution.registry import get_execution_backend
from validibot.validations.services.execution.registry import (
    get_managed_execution_backend_route,
)
from validibot.validations.services.file_identity import FileIdentity
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

TEST_CALLBACK_NONCE = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
TEST_CALLBACK_NONCE_COMMITMENT = compute_callback_nonce_commitment(
    TEST_CALLBACK_NONCE,
)

# ==============================================================================
# Helpers
# ==============================================================================


def _make_execution_request() -> ExecutionRequest:
    """Build an ExecutionRequest from real Django model instances.

    Creates the full model graph (Org → Workflow → ValidationRun → Step)
    using FactoryBoy and returns a ready-to-use ``ExecutionRequest``.

    This replaces the hand-rolled Mock* classes that previously simulated
    Django models with plain Python objects.  Using real models ensures
    UUID serialization, case normalization, and foreign key traversal
    work correctly.
    """
    org = OrganizationFactory()
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    workflow = WorkflowFactory(org=org)
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    submission = SubmissionFactory(
        content="Version,24.1;",
        original_filename="test.idf",
        workflow=workflow,
        org=org,
        project=None,
    )
    validation_run = ValidationRunFactory(
        submission=submission,
        workflow=workflow,
        org=org,
    )
    # Also create a step run so validation_run.current_step_run works
    ValidationStepRunFactory(
        validation_run=validation_run,
        workflow_step=step,
    )
    return ExecutionRequest(
        run=validation_run,
        validator=validator,
        submission=submission,
        step=step,
    )


def _make_weather_resource(
    *, uri: str = "file:///test/weather.epw"
) -> ResourceFileItem:
    """Create a ResourceFileItem for a weather file.

    Used in envelope builder tests where we need at least one resource
    file to build a valid input envelope.
    """
    return ResourceFileItem(
        id="resource-weather-123",
        name="weather.epw",
        type="energyplus_weather",
        port_key="weather_file",
        **_file_identity(uri).envelope_fields(),
    )


def _file_identity(uri: str) -> FileIdentity:
    """Create complete immutable metadata for an envelope-builder fixture."""
    return FileIdentity(
        uri=uri,
        size_bytes=17,
        sha256="a" * 64,
        storage_version="sha256:" + "a" * 64,
    )


# ==============================================================================
# ExecutionRequest — verifies Django model field access
# ==============================================================================
# ExecutionRequest is a dataclass with computed properties (run_id, org_id,
# validator_type) that traverse Django model relationships.  Using real
# models catches type mismatches (e.g., UUID vs string) that duck-typed
# mocks would silently accept.
# ==============================================================================


@pytest.mark.django_db
class TestExecutionRequest:
    """Tests for ExecutionRequest dataclass with real Django models."""

    def test_run_id_returns_uuid_string(self):
        """``run_id`` should return the ValidationRun's UUID as a string.

        Django UUIDs are ``uuid.UUID`` objects; the property must call
        ``str()`` to convert them for use in storage paths, envelope
        fields, and logging.
        """
        request = _make_execution_request()
        run_id = request.run_id

        # Should be a string, not a UUID object
        assert isinstance(run_id, str)
        # Should match the actual run's ID
        assert run_id == str(request.run.id)
        # Should look like a UUID (contains hyphens)
        assert "-" in run_id

    def test_org_id_traverses_foreign_key(self):
        """``org_id`` should traverse ``run.org.id`` — a two-level FK chain.

        This verifies the relationship from ValidationRun → Organization
        is correctly loaded.  Hand-rolled mocks can't catch lazy FK
        loading issues or ``select_related`` requirements.
        """
        request = _make_execution_request()
        org_id = request.org_id

        assert isinstance(org_id, str)
        assert org_id == str(request.run.org.id)

    def test_validator_type_is_lowercase(self):
        """``validator_type`` should return the type in lowercase.

        The ``ValidationType`` enum uses uppercase values (``ENERGYPLUS``),
        but the backend uses lowercase for container image names and
        storage paths (``energyplus``).  The property must normalize this.
        """
        request = _make_execution_request()
        assert request.validator_type == "energyplus"


# ==============================================================================
# ExecutionResponse — pure dataclass, no DB needed
# ==============================================================================
# ExecutionResponse carries results back from backends.  It's a plain
# dataclass with no Django dependencies, so these tests run without DB.
# ==============================================================================


class TestExecutionResponse:
    """Tests for ExecutionResponse dataclass."""

    def test_response_complete_with_error(self):
        """A completed response should carry the error message and duration.

        Backends set ``is_complete=True`` and ``error_message`` when
        execution finished but produced an error (e.g., container crashed).
        """
        response = ExecutionResponse(
            execution_id="exec-123",
            is_complete=True,
            output_envelope=None,
            error_message="Test error",
            duration_seconds=10.5,
        )

        assert response.execution_id == "exec-123"
        assert response.is_complete is True
        assert response.error_message == "Test error"
        assert response.duration_seconds == 10.5  # noqa: PLR2004

    def test_response_pending_defaults(self):
        """A pending async response should default optional fields to None.

        Async backends return ``is_complete=False`` immediately after
        launching the job.  The envelope and error arrive later via callback.
        """
        response = ExecutionResponse(
            execution_id="exec-456",
            is_complete=False,
        )

        assert response.execution_id == "exec-456"
        assert response.is_complete is False
        assert response.output_envelope is None
        assert response.error_message is None
        assert response.execution_status is None

    def test_execution_status_preserves_positional_field_order(self):
        """The third positional argument must remain the output envelope.

        Keeping ``execution_status`` after ``output_envelope`` prevents a
        positional envelope argument from being interpreted as a status.
        """
        envelope = object()

        response = ExecutionResponse("exec-positional", True, envelope)  # noqa: FBT003

        assert response.output_envelope is envelope
        assert response.execution_status is None


# ==============================================================================
# Backend registry — maps settings → backend instances
# ==============================================================================
# The registry reads DEPLOYMENT_TARGET and VALIDATOR_RUNNER settings to
# select which ExecutionBackend implementation to use.  It also caches
# the instance for performance (backends are stateless singletons).
# ==============================================================================


class TestBackendFactory:
    """Tests for the execution backend factory / registry."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Clear the backend cache before and after each test.

        The registry caches its backend instance.  Without clearing,
        a test that sets ``DEPLOYMENT_TARGET=test`` would contaminate
        subsequent tests that expect a different backend.
        """
        clear_backend_cache()
        yield
        clear_backend_cache()

    def test_deployment_target_test_uses_docker(self, settings):
        """``DEPLOYMENT_TARGET=test`` should select the Docker Compose backend.

        In CI and local dev, ``test`` is the default deployment target.
        It uses the synchronous Docker Compose backend so tests don't
        need Cloud Run infrastructure.
        """
        settings.VALIDATOR_RUNNER = None
        settings.DEPLOYMENT_TARGET = "test"

        backend = get_execution_backend()

        assert isinstance(backend, DockerComposeExecutionBackend)
        assert backend.is_async is False

    def test_deployment_target_docker_compose_uses_docker(self, settings):
        """``DEPLOYMENT_TARGET=self_hosted`` should also select Docker.

        Self-hosted (renamed from the historical ``docker_compose``)
        is the customer-operated single-VM Compose deployment that
        uses the same Docker-socket validator runner as local dev.
        See ADR-2026-04-27 for the rename rationale.
        """
        settings.VALIDATOR_RUNNER = None
        settings.DEPLOYMENT_TARGET = "self_hosted"

        backend = get_execution_backend()

        assert isinstance(backend, DockerComposeExecutionBackend)

    def test_validator_runner_overrides_deployment_target(self, settings):
        """``VALIDATOR_RUNNER`` should take precedence over ``DEPLOYMENT_TARGET``.

        This escape hatch allows developers to force a specific backend
        regardless of the environment's deployment target — useful for
        testing the Docker backend against a GCP-configured staging env.
        """
        settings.VALIDATOR_RUNNER = "docker"
        settings.DEPLOYMENT_TARGET = "gcp"

        backend = get_execution_backend()

        # Should use docker (from VALIDATOR_RUNNER) not GCP (from DEPLOYMENT_TARGET)
        assert isinstance(backend, DockerComposeExecutionBackend)

    def test_backend_instance_is_cached(self, settings):
        """Repeated calls should return the same backend instance.

        Backends are stateless, so caching avoids unnecessary re-creation
        and settings lookups on every ``validate()`` call.
        """
        settings.VALIDATOR_RUNNER = "docker"

        backend1 = get_execution_backend()
        backend2 = get_execution_backend()

        assert backend1 is backend2

    def test_unknown_validator_runner_raises(self, settings):
        """An unrecognized ``VALIDATOR_RUNNER`` should raise ``ValueError``.

        This catches misconfiguration early rather than silently falling
        back to a default backend that might behave differently.
        """
        settings.VALIDATOR_RUNNER = "unknown_backend"
        clear_backend_cache()

        with pytest.raises(ValueError, match="Unknown VALIDATOR_RUNNER"):
            get_execution_backend()

    def test_pinned_service_route_selects_service_adapter_and_identifier(self):
        """Adapter construction and attempt evidence must share one registry."""
        deployment = MagicMock(
            provider_type=ExecutionProviderType.GCP,
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        )

        route = get_managed_execution_backend_route(deployment)
        backend = get_execution_backend(deployment)

        assert route.backend_class is CloudRunServiceExecutionBackend
        assert route.runner_type == "CloudRunServiceExecutionBackend"
        assert isinstance(backend, CloudRunServiceExecutionBackend)

    def test_pinned_job_route_selects_job_adapter_and_identifier(self):
        """The compatibility route must retain its distinct adapter identity."""
        deployment = MagicMock(
            provider_type=ExecutionProviderType.GCP,
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
        )

        route = get_managed_execution_backend_route(deployment)
        backend = get_execution_backend(deployment)

        assert route.backend_class is CloudRunJobsExecutionBackend
        assert route.runner_type == "CloudRunJobsExecutionBackend"
        assert isinstance(backend, CloudRunJobsExecutionBackend)


# ==============================================================================
# CloudRunJobsExecutionBackend — Cloud Run launcher result conversion
# ==============================================================================
# The GCP launcher returns ValidationResult because it predates the execution
# backend abstraction. These tests keep the adapter honest: a successful launch is
# pending, while a launch-blocking failure must be complete so the processor fails
# the step immediately instead of waiting forever for a callback that cannot come.
# ==============================================================================


class TestCloudRunJobsExecutionBackendLaunchResultConversion:
    """Tests for converting Cloud Run launcher results into ExecutionResponse."""

    def test_pending_launch_result_stays_async(self):
        """``passed=None`` from the launcher means the Cloud Run Job dispatched."""
        backend = CloudRunJobsExecutionBackend()
        result = ValidationResult(
            passed=None,
            issues=[],
            stats={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "input_uri": "gs://bucket/input.json",
                "result_uri": "gs://bucket/output.json",
                "execution_bundle_uri": "gs://bucket/bundle/",
            },
        )

        response = backend._launch_result_to_response(result)

        assert response.is_complete is False
        assert response.execution_id.endswith("/executions/e")
        assert response.error_message is None
        assert response.input_uri == "gs://bucket/input.json"

    def test_failed_launch_result_completes_with_error_message(self):
        """Launch failures must fail immediately, not become async pending runs.

        ``launch_shacl_validation`` catches GCP/image-policy/storage failures and
        returns ``ValidationResult(passed=False)``. If the GCP adapter discards
        that and returns ``is_complete=False``, the step is marked RUNNING and no
        callback will ever arrive.
        """
        backend = CloudRunJobsExecutionBackend()
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    path="",
                    message="Failed to launch SHACL validation.",
                    severity=Severity.ERROR,
                ),
            ],
            stats={},
        )

        response = backend._launch_result_to_response(result)

        assert response.is_complete is True
        assert response.execution_id == ""
        assert "Failed to launch SHACL validation" in response.error_message


# ==============================================================================
# DockerComposeExecutionBackend — sync local/CI backend
# ==============================================================================
# The Docker Compose backend runs validator containers locally via the
# Docker socket.  It's synchronous — ``execute()`` blocks until the
# container exits and returns the output envelope directly.
# ==============================================================================


class TestDockerComposeExecutionBackend:
    """Tests for the Docker Compose execution backend."""

    def test_is_async_false(self):
        """Docker Compose backend should be synchronous.

        Unlike the GCP backend (which returns ``is_async=True`` and
        delivers results via callback), Docker blocks until the
        container exits.
        """
        backend = DockerComposeExecutionBackend()
        assert backend.is_async is False

    def test_backend_name(self):
        """``backend_name`` should return the class name for logging."""
        backend = DockerComposeExecutionBackend()
        assert backend.backend_name == "DockerComposeExecutionBackend"

    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    def test_is_available_true(self, mock_get_runner):
        """``is_available()`` should return True when Docker daemon is reachable.

        The backend delegates to the validator runner's availability check,
        which pings the Docker socket.
        """
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = True
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        assert backend.is_available() is True

    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    def test_is_available_false(self, mock_get_runner):
        """``is_available()`` should return False when Docker daemon is unreachable.

        This is common in CI environments without Docker or when the
        daemon is stopped.  The validator gracefully reports backend
        unavailability rather than crashing.
        """
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = False
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        assert backend.is_available() is False

    def test_get_container_image_default(self, settings):
        """Default image name should follow the naming convention.

        Format: ``validibot-validator-backend-{type}:{tag}`` — the convention
        used in ``docker-compose.yml`` and local builds.
        """
        settings.VALIDATOR_IMAGE_TAG = "latest"
        settings.VALIDATOR_IMAGE_REGISTRY = ""
        settings.VALIDATOR_IMAGES = {}

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("energyplus")

        assert image == "validibot-validator-backend-energyplus:latest"

    def test_get_container_image_with_registry(self, settings):
        """When a registry is configured, it should prefix the image name.

        In production, images live in a container registry (GCR, ECR).
        The registry prefix is prepended to the default naming convention.
        """
        settings.VALIDATOR_IMAGE_TAG = "v1.0.0"
        settings.VALIDATOR_IMAGE_REGISTRY = "gcr.io/my-project"
        settings.VALIDATOR_IMAGES = {}

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("fmu")

        assert image == "gcr.io/my-project/validibot-validator-backend-fmu:v1.0.0"

    def test_get_container_image_explicit_mapping(self, settings):
        """Explicit ``VALIDATOR_IMAGES`` mapping should override defaults.

        This allows using custom image names (e.g., pre-built images
        from a different registry) without changing the naming convention.
        """
        settings.VALIDATOR_IMAGES = {
            "energyplus": "my-custom-image:custom-tag",
        }

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("energyplus")

        assert image == "my-custom-image:custom-tag"

    def test_get_container_image_reads_from_validator_config(
        self, settings, monkeypatch
    ):
        """``ValidatorConfig.image_name`` is the canonical source for the
        Docker image base name.

        System validators declare ``image_name`` in their ``config.py``.
        This test patches ``get_config()`` to return a config whose
        ``image_name`` differs from the convention, proving the backend
        actually reads it (rather than coincidentally producing the same
        value via the convention fallback). This is what makes the
        validator definition — not an env var or settings dict — the
        source of truth for which container to launch.
        """
        from types import SimpleNamespace

        settings.VALIDATOR_IMAGE_TAG = "latest"
        settings.VALIDATOR_IMAGE_REGISTRY = ""
        settings.VALIDATOR_IMAGES = {}

        fake_config = SimpleNamespace(image_name="my-org/explicit-energyplus")
        monkeypatch.setattr(
            "validibot.validations.validators.base.config.get_config",
            lambda vtype: fake_config if vtype == "ENERGYPLUS" else None,
        )

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("energyplus")

        assert image == "my-org/explicit-energyplus:latest"

    def test_get_container_image_falls_back_when_no_config(self, settings, monkeypatch):
        """Convention fallback applies when the validator has no
        registered ``ValidatorConfig``.

        Custom org validators (created in the admin, not via a
        ``config.py``) won't have an entry in the config registry.
        Rather than crashing, the backend falls back to the convention
        ``validibot-validator-backend-{slug}`` so dispatch still works
        for sane custom-validator naming. This contract matters for
        org-defined validators added through the UI.
        """
        settings.VALIDATOR_IMAGE_TAG = "latest"
        settings.VALIDATOR_IMAGE_REGISTRY = ""
        settings.VALIDATOR_IMAGES = {}

        monkeypatch.setattr(
            "validibot.validations.validators.base.config.get_config",
            lambda vtype: None,
        )

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("custom-validator")

        assert image == "validibot-validator-backend-custom-validator:latest"

    @pytest.mark.django_db
    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    @patch("validibot.validations.services.execution.docker_compose.get_data_storage")
    def test_execute_returns_error_when_not_available(
        self, mock_get_storage, mock_get_runner
    ):
        """When Docker is unavailable, ``execute()`` should return an error response.

        Rather than raising an exception, the backend returns a completed
        ``ExecutionResponse`` with an error message — the processor handles
        this gracefully as a step failure.

        Uses real Django models (via ``_make_execution_request()``) to
        verify the full model traversal works during error handling.
        """
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = False
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        request = _make_execution_request()

        response = backend.execute(request)

        assert response.is_complete is True
        assert "not available" in response.error_message

    @pytest.mark.django_db
    def test_exception_after_dispatch_becomes_unknown_not_failed(self, tmp_path):
        """A lost Docker response must not authorize automatic container replay.

        Even though self-hosted execution is normally synchronous, the worker
        can lose contact after Docker accepted the container. Returning a
        pending response keeps the run available to the watchdog while the
        durable attempt prevents task redelivery from starting another copy.
        """
        request = _make_execution_request()
        attempt = ExecutionAttemptFactory(
            step_run=request.run.current_step_run,
            state=ExecutionAttemptState.PENDING,
        )
        workspace = MagicMock()
        workspace.execution_bundle_container_uri = "file:///bundle"
        workspace.input_envelope_container_uri = "file:///bundle/input.json"
        workspace.output_envelope_container_uri = "file:///bundle/output.json"
        workspace.host_input_envelope_path = tmp_path / "input" / "input.json"
        workspace.host_input_envelope_path.parent.mkdir()
        envelope = MagicMock()
        envelope.model_dump_json.return_value = "{}"
        envelope.model_dump.return_value = {}

        backend = DockerComposeExecutionBackend()
        backend._runner = MagicMock()
        backend._runner.is_available.return_value = True
        backend._runner.run.side_effect = ConnectionError("docker response lost")

        with (
            patch.object(
                backend,
                "_build_workspace_and_envelope_kwargs",
                return_value=(workspace, {}, {}),
            ),
            patch.object(backend, "build_input_envelope", return_value=envelope),
        ):
            response = backend.execute(request)

        attempt.refresh_from_db()
        assert response.is_complete is False
        assert response.error_message is None
        assert attempt.state == ExecutionAttemptState.UNKNOWN

    @pytest.mark.django_db
    def test_unverified_output_fails_the_claimed_attempt(self, tmp_path):
        """A zero-exit container cannot complete without a verified envelope.

        Container completion is only transport status.  Treating a missing or
        mismatched ``output.json`` as a completed attempt would let untrusted
        runtime output bypass the ADR's fail-closed result boundary.
        """
        request = _make_execution_request()
        attempt = ExecutionAttemptFactory(
            step_run=request.run.current_step_run,
            state=ExecutionAttemptState.PENDING,
        )
        workspace = MagicMock()
        workspace.execution_bundle_container_uri = "file:///validibot/output"
        workspace.input_envelope_container_uri = "file:///validibot/input/input.json"
        workspace.output_envelope_container_uri = "file:///validibot/output/output.json"
        workspace.host_input_envelope_path = tmp_path / "input" / "input.json"
        workspace.host_input_envelope_path.parent.mkdir()
        envelope = MagicMock()
        envelope.model_dump_json.return_value = "{}"
        envelope.model_dump.return_value = {}
        runner_result = MagicMock(
            succeeded=True,
            execution_id="container-123",
            duration_seconds=1.5,
            validator_backend_image_digest="sha256:abc",
        )

        backend = DockerComposeExecutionBackend()
        backend._runner = MagicMock()
        backend._runner.is_available.return_value = True
        backend._runner.run.return_value = runner_result

        with (
            patch.object(
                backend,
                "_build_workspace_and_envelope_kwargs",
                return_value=(workspace, {}, {}),
            ),
            patch.object(backend, "build_input_envelope", return_value=envelope),
            patch.object(
                backend,
                "_read_output_envelope_from_host",
                return_value=None,
            ),
        ):
            response = backend.execute(request)

        attempt.refresh_from_db()
        assert response.is_complete is True
        assert response.output_envelope is None
        assert "failed trusted" in (response.error_message or "")
        assert attempt.state == ExecutionAttemptState.FAILED
        assert attempt.last_error_code == "output_verification_failed"

    def test_check_status_exists(self):
        """Docker Compose backend should expose a ``check_status`` method.

        This is part of the ExecutionBackend interface contract — even
        sync backends implement it (returning immediately) so the
        processor can use a uniform polling API.
        """
        backend = DockerComposeExecutionBackend()
        assert hasattr(backend, "check_status")
        assert callable(backend.check_status)

    def test_get_execution_status_removed(self):
        """``get_execution_status`` should NOT exist on the base class.

        This was removed during the backend refactor in favor of
        ``check_status``.  Ensures no accidental re-introduction.
        """
        from validibot.validations.services.execution.base import ExecutionBackend

        assert not hasattr(ExecutionBackend, "get_execution_status")


@pytest.mark.django_db
class TestDockerOutputEnvelopeReader:
    """Tests for trusted output-envelope parsing in the sync Docker path."""

    def _write_energyplus_output(
        self,
        tmp_path,
        request: ExecutionRequest,
        attempt,
        *,
        validator_type: str = "ENERGYPLUS",
        validator_id: str | None = None,
        run_id: str | None = None,
    ):
        """Write a minimal EnergyPlus output envelope JSON file."""
        output_path = tmp_path / "output.json"
        output_path.write_text(
            json.dumps(
                {
                    "schema_version": "validibot.output.v1",
                    "run_id": run_id or str(request.run.id),
                    "step_run_id": str(attempt.step_run_id),
                    "execution_attempt_id": str(attempt.pk),
                    "attempt_contract_version": "validibot.attempt.v2",
                    "input_envelope_sha256": attempt.input_envelope_sha256,
                    "output_uri": attempt.output_envelope_uri,
                    "validator": {
                        "id": validator_id or str(request.validator.id),
                        "type": validator_type,
                        "version": str(request.validator.version),
                    },
                    "status": "success",
                    "timing": {},
                    "outputs": {
                        "energyplus_returncode": 0,
                        "execution_seconds": 1.0,
                        "invocation_mode": "cli",
                    },
                },
            ),
            encoding="utf-8",
        )
        return output_path

    def test_reads_output_with_trusted_validator_class(self, tmp_path):
        """Valid output should parse through the validator's configured class."""
        request = _make_execution_request()
        attempt = ExecutionAttemptFactory(
            step_run=request.run.current_step_run,
            input_envelope_sha256="a" * 64,
            output_envelope_uri="file:///validibot/output/output.json",
        )
        output_path = self._write_energyplus_output(tmp_path, request, attempt)

        envelope = DockerComposeExecutionBackend()._read_output_envelope_from_host(
            output_path,
            expected_run=request.run,
            expected_validator=request.validator,
            expected_attempt=attempt,
        )

        assert envelope is not None
        assert envelope.validator.id == str(request.validator.id)
        assert envelope.outputs.energyplus_returncode == 0

    def test_rejects_output_validator_type_mismatch(self, tmp_path):
        """The output document must not choose its own parser via validator.type."""
        request = _make_execution_request()
        attempt = ExecutionAttemptFactory(
            step_run=request.run.current_step_run,
            input_envelope_sha256="a" * 64,
            output_envelope_uri="file:///validibot/output/output.json",
        )
        output_path = self._write_energyplus_output(
            tmp_path,
            request,
            attempt,
            validator_type="FMU",
        )

        envelope = DockerComposeExecutionBackend()._read_output_envelope_from_host(
            output_path,
            expected_run=request.run,
            expected_validator=request.validator,
            expected_attempt=attempt,
        )

        assert envelope is None

    def test_rejects_oversized_output_before_reading(self, tmp_path, settings):
        """Local Docker output parsing must enforce the result-size cap too."""
        request = _make_execution_request()
        attempt = ExecutionAttemptFactory(
            step_run=request.run.current_step_run,
            input_envelope_sha256="a" * 64,
            output_envelope_uri="file:///validibot/output/output.json",
        )
        output_path = self._write_energyplus_output(tmp_path, request, attempt)
        output_path.write_bytes(b"x" * 128)
        settings.VALIDATION_RESULT_MAX_BYTES = 64

        envelope = DockerComposeExecutionBackend()._read_output_envelope_from_host(
            output_path,
            expected_run=request.run,
            expected_validator=request.validator,
            expected_attempt=attempt,
        )

        assert envelope is None


# ==============================================================================
# Envelope building — skip_callback and file metadata
# ==============================================================================
# The envelope builder constructs typed Pydantic envelopes that the
# validator container reads as input.  Key concerns:
# - ``skip_callback``: True for sync backends (Docker) where the
#   processor reads the output directly; False for async (GCP) where
#   the container POSTs results back.
# - File metadata: The ``name`` and ``mime_type`` of the primary model
#   file must match the actual file type (IDF vs epJSON).  The runner
#   uses ``name`` as the local filename, and EnergyPlus uses the
#   extension to decide its parsing mode.
# ==============================================================================


@pytest.mark.django_db
class TestEnvelopeSkipCallback:
    """Tests for ``skip_callback`` in envelope building."""

    def test_skip_callback_true_for_sync_backends(self):
        """Sync backends (Docker Compose) should set ``skip_callback=True``.

        When the backend blocks until the container exits, the output
        envelope is read directly from the shared volume — no HTTP
        callback is needed.  Setting ``skip_callback=True`` tells the
        container to skip the POST request.
        """
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Organization",
            workflow_id="workflow-789",
            step_id="step-012",
            step_name="Test Step",
            model_file=_file_identity("file:///test/model.idf"),
            resource_files=[_make_weather_resource()],
            callback_url="http://localhost:8000/callbacks/",
            callback_id="cb-123",
            execution_bundle_uri="file:///test/runs/123/",
            execution_attempt_id="attempt-123",
            step_run_id="step-run-123",
            expected_output_uri="file:///test/runs/123/output.json",
            skip_callback=True,
        )

        assert envelope.context.skip_callback is True

    def test_skip_callback_false_for_async_backends(self):
        """Async backends (GCP Cloud Run) should set ``skip_callback=False``.

        The container must POST its results back to the callback URL
        because the launcher returns immediately without waiting.
        """
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Organization",
            workflow_id="workflow-789",
            step_id="step-012",
            step_name="Test Step",
            model_file=_file_identity("gs://bucket/model.idf"),
            resource_files=[
                _make_weather_resource(uri="gs://bucket/weather.epw"),
            ],
            callback_url="https://api.example.com/callbacks/",
            callback_id="cb-123",
            callback_nonce=TEST_CALLBACK_NONCE,
            callback_nonce_commitment=TEST_CALLBACK_NONCE_COMMITMENT,
            execution_bundle_uri="gs://bucket/runs/123/",
            execution_attempt_id="attempt-123",
            step_run_id="step-run-123",
            expected_output_uri="gs://bucket/runs/123/output.json",
            skip_callback=False,
        )

        assert envelope.context.skip_callback is False


@pytest.mark.django_db
class TestEnvelopeFileMetadata:
    """Tests for dynamic file metadata in envelope building.

    The envelope's ``input_files[0].name`` and ``mime_type`` must match
    the actual file type.  The EnergyPlus runner uses ``name`` as the
    local filename when downloading from GCS/local storage, and
    EnergyPlus uses the file extension to decide whether to parse the
    file as IDF text or epJSON.

    A bug previously hardcoded these to ``model.idf`` / ``ENERGYPLUS_IDF``
    for all submissions, breaking direct epJSON execution.  These tests
    verify the fix that derives metadata from the ``model_file_uri``.
    """

    def test_idf_uri_sets_idf_metadata(self):
        """An IDF model URI should produce ``model.idf`` / ``ENERGYPLUS_IDF``.

        This is the traditional EnergyPlus format — plain text with
        object-field-semicolon syntax.
        """
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Org",
            workflow_id="wf-789",
            step_id="step-012",
            step_name="Test Step",
            model_file=_file_identity("file:///test/model.idf"),
            resource_files=[_make_weather_resource()],
            callback_url="http://localhost/cb/",
            callback_id="cb-1",
            skip_callback=True,
            execution_bundle_uri="file:///test/runs/1/",
            execution_attempt_id="attempt-1",
            step_run_id="step-run-1",
            expected_output_uri="file:///test/runs/1/output.json",
        )

        model_file = envelope.input_files[0]
        assert model_file.name == "model.idf"
        assert model_file.mime_type == SupportedMimeType.ENERGYPLUS_IDF

    def test_epjson_uri_sets_epjson_metadata(self):
        """An epJSON model URI should produce ``model.epjson`` / ``ENERGYPLUS_EPJSON``.

        epJSON is the newer JSON-based EnergyPlus format.  Using the
        wrong filename/mime_type would cause the runner to save the file
        with the wrong extension, making EnergyPlus try to parse JSON
        as IDF text (or vice versa).
        """
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Org",
            workflow_id="wf-789",
            step_id="step-012",
            step_name="Test Step",
            model_file=_file_identity("file:///test/model.epjson"),
            resource_files=[_make_weather_resource()],
            callback_url="http://localhost/cb/",
            callback_id="cb-1",
            skip_callback=True,
            execution_bundle_uri="file:///test/runs/1/",
            execution_attempt_id="attempt-1",
            step_run_id="step-run-1",
            expected_output_uri="file:///test/runs/1/output.json",
        )

        model_file = envelope.input_files[0]
        assert model_file.name == "model.epjson"
        assert model_file.mime_type == SupportedMimeType.ENERGYPLUS_EPJSON

    def test_gs_uri_epjson_sets_epjson_metadata(self):
        """GCS URIs ending in ``.epjson`` should also get epJSON metadata.

        The URI scheme (``gs://`` vs ``file://``) should not affect
        file metadata derivation — only the file extension matters.
        """
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Org",
            workflow_id="wf-789",
            step_id="step-012",
            step_name="Test Step",
            model_file=_file_identity("gs://my-bucket/runs/org-1/run-2/model.epjson"),
            resource_files=[
                _make_weather_resource(uri="gs://my-bucket/weather.epw"),
            ],
            callback_url="https://api.example.com/cb/",
            callback_id="cb-1",
            skip_callback=True,
            execution_bundle_uri="gs://my-bucket/runs/org-1/run-2/",
            execution_attempt_id="attempt-1",
            step_run_id="step-run-1",
            expected_output_uri="gs://my-bucket/runs/org-1/run-2/output.json",
        )

        model_file = envelope.input_files[0]
        assert model_file.name == "model.epjson"
        assert model_file.mime_type == SupportedMimeType.ENERGYPLUS_EPJSON

    def test_uppercase_epjson_extension_handled(self):
        """File extension matching should be case-insensitive.

        Although uncommon, ``model.EPJSON`` or ``model.EpJSON`` should
        still produce epJSON metadata.
        """
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Org",
            workflow_id="wf-789",
            step_id="step-012",
            step_name="Test Step",
            model_file=_file_identity("file:///test/model.EPJSON"),
            resource_files=[_make_weather_resource()],
            callback_url="http://localhost/cb/",
            callback_id="cb-1",
            skip_callback=True,
            execution_bundle_uri="file:///test/runs/1/",
            execution_attempt_id="attempt-1",
            step_run_id="step-run-1",
            expected_output_uri="file:///test/runs/1/output.json",
        )

        model_file = envelope.input_files[0]
        assert model_file.name == "model.epjson"
        assert model_file.mime_type == SupportedMimeType.ENERGYPLUS_EPJSON
