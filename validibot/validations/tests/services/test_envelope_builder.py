"""
Tests for ``build_energyplus_input_envelope()`` — the envelope builder service.

The envelope builder constructs typed Pydantic ``EnergyPlusInputEnvelope``
objects that the validator container reads as its primary input.  The envelope
encapsulates all context the container needs:

- **Validator info**: type, version, ID (for logging/tracing)
- **Org/workflow info**: used for storage paths and callback routing
- **Input files**: the primary model file (IDF or epJSON) with correct
  ``name`` and ``mime_type`` so the runner saves it with the right extension
- **Resource files**: weather files (EPW) and any other auxiliary files
- **Execution context**: callback URL, callback ID for idempotency,
  ``skip_callback`` flag for sync backends
- **EnergyPlus inputs**: ``timestep_per_hour`` and future run settings

These tests verify envelope construction with real ``ValidatorFactory``
instances (not hand-rolled mocks), ensuring the builder correctly handles
UUID fields, validation type normalization, and other real model behavior.
"""

import hashlib
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from validibot_shared.canonicalization import compute_callback_nonce_commitment
from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope
from validibot_shared.pdf import PdfInputEnvelope
from validibot_shared.schematron.envelopes import SchematronInputEnvelope
from validibot_shared.shacl.envelopes import SHACLInputEnvelope
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import (
    ValidationArtifact as ValidationArtifactEnvelope,
)
from validibot_shared.validations.envelopes import ValidatorType

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import FMU_MODEL_RESOURCE
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Artifact
from validibot.validations.models import FMUModel
from validibot.validations.models import ResolvedInputTrace
from validibot.validations.services.artifacts import register_output_artifacts
from validibot.validations.services.cloud_run.envelope_builder import (
    build_energyplus_input_envelope,
)
from validibot.validations.services.cloud_run.envelope_builder import (
    build_input_envelope as _build_input_envelope,
)
from validibot.validations.services.file_identity import FileIdentity
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory

pytestmark = pytest.mark.django_db

TEST_CALLBACK_NONCE = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
TEST_CALLBACK_NONCE_COMMITMENT = compute_callback_nonce_commitment(
    TEST_CALLBACK_NONCE,
)
EXPECTED_PDF_MAX_EXECUTION_SECONDS = 300


# ==============================================================================
# Helpers
# ==============================================================================


def _create_step_run_with_attempt(**kwargs):
    """Create the active step run and its immutable execution identity."""
    step_run = ValidationStepRunFactory(**kwargs)
    ExecutionAttemptFactory(step_run=step_run)
    return step_run


def _file_identity(uri: str, content: bytes = b"test file bytes") -> FileIdentity:
    """Create a complete immutable file identity for an envelope fixture."""
    digest = hashlib.sha256(content).hexdigest()
    storage_version = f"sha256:{digest}" if uri.startswith("file://") else "1"
    return FileIdentity(
        uri=uri,
        size_bytes=len(content),
        sha256=digest,
        storage_version=storage_version,
    )


def _validation_artifact(**kwargs) -> ValidationArtifactEnvelope:
    """Create strict backend artifact metadata for registration fixtures."""
    kwargs.setdefault("size_bytes", 0)
    kwargs.setdefault("sha256", "a" * 64)
    kwargs.setdefault("storage_version", "1")
    return ValidationArtifactEnvelope(**kwargs)


def _declare_output_file_port(
    step,
    *,
    contract_key: str,
    role: str,
    data_format: str,
    media_type: str,
    accepted_media_types: list[str] | None = None,
    extensions: list[str] | None = None,
):
    """Declare the producer contract required by a relational artifact binding."""
    return StepIODefinitionFactory(
        validator=step.validator,
        workflow_step=None,
        contract_key=contract_key,
        native_name=contract_key,
        direction=StepIODirection.OUTPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.INTERNAL,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type=media_type,
        data_format=data_format,
        accepted_data_formats=[data_format],
        accepted_media_types=accepted_media_types or [media_type],
        metadata={"accepted_extensions": extensions or []},
        envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
        role=role,
        min_items=0,
        max_items=1,
    )


def _build_test_input_envelope(
    run,
    *,
    input_file_uris=None,
    resource_uri_overrides=None,
    **kwargs,
):
    """Adapt concise URI test data into the strict dispatch identity contract."""
    input_files = {
        key: value if isinstance(value, FileIdentity) else _file_identity(value)
        for key, value in (input_file_uris or {}).items()
    }
    resources = {
        key: value if isinstance(value, FileIdentity) else _file_identity(value)
        for key, value in (resource_uri_overrides or {}).items()
    }
    if kwargs.get("callback_id"):
        kwargs.setdefault("callback_nonce", TEST_CALLBACK_NONCE)
        kwargs.setdefault(
            "callback_nonce_commitment",
            TEST_CALLBACK_NONCE_COMMITMENT,
        )
    else:
        kwargs.setdefault("skip_callback", True)
    return _build_input_envelope(
        run,
        input_file_uris=input_files,
        resource_uri_overrides=resources,
        **kwargs,
    )


def _make_weather_resource(
    uri: str = "gs://test-bucket/weather.epw",
) -> ResourceFileItem:
    """Create a ResourceFileItem for a weather file.

    Weather files are the most common resource type attached to EnergyPlus
    envelopes.  They're passed as ``resource_files`` (not ``input_files``)
    because the runner downloads them separately from the model file.
    """
    return ResourceFileItem(
        id="resource-weather-123",
        type="energyplus_weather",
        port_key="weather_file",
        uri=uri,
        name="weather.epw",
        size_bytes=18,
        sha256="a" * 64,
        storage_version="1",
    )


def _build_envelope(validator=None, **overrides) -> EnergyPlusInputEnvelope:
    """Build an envelope with sensible defaults, allowing per-test overrides.

    Reduces boilerplate across tests — each test only specifies the
    parameters it cares about.
    """
    if validator is None:
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

    defaults = {
        "run_id": "run-123",
        "validator": validator,
        "org_id": "org-456",
        "org_name": "Test Organization",
        "workflow_id": "workflow-789",
        "step_id": "step-012",
        "step_name": "EnergyPlus Simulation",
        "model_file": _file_identity("gs://test-bucket/model.idf"),
        "resource_files": [_make_weather_resource()],
        "callback_url": "https://api.example.com/callbacks/",
        "callback_id": "cb-test-123",
        "callback_nonce": TEST_CALLBACK_NONCE,
        "callback_nonce_commitment": TEST_CALLBACK_NONCE_COMMITMENT,
        "execution_bundle_uri": "gs://test-bucket/runs/run-123/",
        "execution_attempt_id": "attempt-123",
        "step_run_id": "step-run-123",
        "expected_output_uri": "gs://test-bucket/runs/run-123/output.json",
    }
    defaults.update(overrides)
    if defaults["callback_id"] is None:
        defaults["callback_nonce"] = None
        defaults["callback_nonce_commitment"] = None
        defaults["skip_callback"] = True
    return build_energyplus_input_envelope(**defaults)


def _build_fmu_run(*, submission_content: str = "{}"):
    """Create a runnable FMU step graph for envelope-builder tests."""
    validator = ValidatorFactory(validation_type=ValidationType.FMU)
    step = WorkflowStepFactory(validator=validator)
    WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.FMU_MODEL,
        validator_resource_file=None,
        step_resource_file=SimpleUploadedFile("model.fmu", b"fmu-bytes"),
        filename="model.fmu",
        resource_type="fmu",
    )
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        content=submission_content,
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    _create_step_run_with_attempt(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
    )
    return run, step


def _make_fmu_model_port(validator):
    """Create the declared FMU model artifact input port for envelope tests."""

    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="fmu_model",
        native_name="fmu_model",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.fmi.fmu",
        data_format=SubmissionDataFormat.FMU,
        accepted_data_formats=[SubmissionDataFormat.FMU],
        accepted_media_types=["application/vnd.fmi.fmu"],
        metadata={"accepted_extensions": ["fmu"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        resource_type=FMU_MODEL_RESOURCE,
        role="fmu",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.WORKFLOW_RESOURCE,
            BindingSourceScope.SYSTEM,
        ],
    )


def _make_shacl_data_graph_port(validator):
    """Create the declared SHACL data graph artifact input port for tests."""

    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="data_graph",
        native_name="data_graph",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="text/turtle",
        data_format=SubmissionDataFormat.TEXT,
        accepted_data_formats=[
            SubmissionDataFormat.TEXT,
            SubmissionDataFormat.JSON,
            SubmissionDataFormat.XML,
        ],
        accepted_media_types=[
            "text/turtle",
            "application/rdf+xml",
            "application/ld+json",
            "application/n-triples",
            "application/n-quads",
        ],
        metadata={"accepted_extensions": ["ttl", "rdf", "jsonld", "nt", "nq"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="data-graph",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )


def _build_shacl_data_graph_run(
    *,
    original_filename: str = "submission.ttl",
    file_type: str = SubmissionFileType.TEXT,
):
    """Create a SHACL run with a declared ``data_graph`` artifact input port."""

    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        version=3,
    )
    ruleset = RulesetFactory(
        org=validator.org,
        ruleset_type=RulesetType.SHACL,
        rules_text="@prefix sh: <http://www.w3.org/ns/shacl#> .",
        metadata={"submission_format": "auto"},
    )
    step = WorkflowStepFactory(
        validator=validator,
        name="Validate RDF",
        ruleset=ruleset,
        order=10,
    )
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        content="@prefix ex: <http://example.org/> . ex:a a ex:Thing .",
        file_type=file_type,
        original_filename=original_filename,
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    _create_step_run_with_attempt(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        status=StepStatus.PENDING,
    )
    data_graph_port = _make_shacl_data_graph_port(validator)
    return run, step, data_graph_port


def _make_schematron_xml_document_port(validator):
    """Create the declared Schematron XML document artifact input port."""

    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="xml_document",
        native_name="xml_document",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/xml",
        data_format=SubmissionDataFormat.XML,
        accepted_data_formats=[SubmissionDataFormat.XML],
        accepted_media_types=["application/xml", "text/xml"],
        metadata={"accepted_extensions": ["xml"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="xml-document",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )


def _build_schematron_xml_document_run():
    """Create a Schematron run with an ``xml_document`` artifact input port."""

    validator = ValidatorFactory(
        validation_type=ValidationType.SCHEMATRON,
        version=2,
    )
    ruleset = RulesetFactory(
        org=validator.org,
        ruleset_type=RulesetType.SCHEMATRON,
        rules_text=(
            "<schema xmlns='http://purl.oclc.org/dsdl/schematron'>"
            "<pattern id='p'/></schema>"
        ),
    )
    step = WorkflowStepFactory(
        validator=validator,
        name="Validate XML Rules",
        ruleset=ruleset,
        order=10,
    )
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        content="<invoice/>",
        file_type=SubmissionFileType.XML,
        original_filename="invoice.xml",
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    _create_step_run_with_attempt(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        status=StepStatus.PENDING,
    )
    xml_document_port = _make_schematron_xml_document_port(validator)
    return run, step, xml_document_port


def _build_energyplus_file_port_run():
    """Create an EnergyPlus run with declared model/weather artifact ports.

    The helper mirrors the post-``sync_validators`` shape: file ports are
    validator-owned ``StepIODefinition`` rows and per-step bindings decide where
    each file comes from.
    """
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    step = WorkflowStepFactory(
        validator=validator,
        name="Run Simulation",
        config={"timestep_per_hour": 6},
        order=10,
    )
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        content="Version,25.1;",
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    _create_step_run_with_attempt(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        status=StepStatus.PENDING,
    )

    primary_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="primary_model",
        native_name="primary_model",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.energyplus.idf",
        data_format=SubmissionDataFormat.ENERGYPLUS_IDF,
        accepted_data_formats=[
            SubmissionDataFormat.ENERGYPLUS_IDF,
            SubmissionDataFormat.ENERGYPLUS_EPJSON,
        ],
        accepted_media_types=[
            "application/vnd.energyplus.idf",
            "application/vnd.energyplus.epjson",
        ],
        metadata={"accepted_extensions": ["idf", "epjson", "json"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="primary-model",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )
    weather_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="weather_file",
        native_name="weather_file",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.energyplus.epw",
        data_format=ResourceFileType.ENERGYPLUS_WEATHER,
        accepted_data_formats=[ResourceFileType.ENERGYPLUS_WEATHER],
        accepted_media_types=["application/vnd.energyplus.epw"],
        metadata={"accepted_extensions": ["epw"]},
        envelope_channel=EnvelopeChannel.RESOURCE_FILES,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        role="weather",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.WORKFLOW_RESOURCE,
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )
    weather_resource = WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=ValidatorResourceFileFactory(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        ),
    )
    return run, step, primary_port, weather_port, weather_resource


def _build_pdf_file_port_run():
    """Create a PDF run whose complete selector config crosses the boundary."""
    validator = ValidatorFactory(validation_type=ValidationType.PDF, version=2)
    step = WorkflowStepFactory(
        validator=validator,
        name="Inspect PDF package",
        order=10,
        config={
            "profile": "safe_static_package_v1",
            "emit_extracted_files_bundle": True,
            "selected_xml": {
                "required": True,
                "original_filename": "handover.xml",
            },
            "selected_json": {
                "required": False,
                "rich_media_asset_name": "asset-index",
            },
            "selected_step_p21": {
                "required": True,
                "step_file_schema": ["AP242_FIXTURE"],
            },
        },
    )
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        content="%PDF-2.0\n% fixture",
        file_type=SubmissionFileType.BINARY,
        original_filename="package.pdf",
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    _create_step_run_with_attempt(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        status=StepStatus.PENDING,
    )
    port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="pdf_document",
        native_name="pdf_document",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/pdf",
        data_format=SubmissionDataFormat.PDF,
        accepted_data_formats=[SubmissionDataFormat.PDF],
        accepted_media_types=["application/pdf"],
        metadata={"accepted_extensions": ["pdf"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="pdf-document",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )
    return run, step, port


# ==============================================================================
# Envelope structure — verifies all sections are populated correctly
# ==============================================================================
# The envelope is the contract between Django and the validator container.
# If any section is missing or malformed, the runner will fail to parse it
# and the validation job will crash without producing results.
# ==============================================================================


class TestEnvelopeStructure:
    """Tests verifying the overall envelope structure and field mapping."""

    def test_creates_correct_envelope_type(self):
        """The builder should return an ``EnergyPlusInputEnvelope`` instance.

        The shared library defines multiple envelope types (EnergyPlus, FMU).
        Using the wrong type would cause the runner's deserializer to fail.
        """
        envelope = _build_envelope()
        assert isinstance(envelope, EnergyPlusInputEnvelope)

    def test_run_id_preserved(self):
        """The ``run_id`` field should be passed through unchanged.

        The container uses this for logging and as part of the callback
        payload so Django can match results to the originating run.
        """
        envelope = _build_envelope(run_id="run-abc-123")
        assert envelope.run_id == "run-abc-123"

    def test_validator_info_from_real_model(self):
        """Validator info should be populated from the real Django model.

        The builder reads ``.id``, ``.validation_type``, and ``.version``
        from the model instance.  Using a real ``ValidatorFactory`` ensures
        UUID serialization and enum-to-string conversion work correctly.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            version=3,
        )
        envelope = _build_envelope(validator=validator)

        assert envelope.validator.id == str(validator.id)
        assert envelope.validator.type == ValidatorType.ENERGYPLUS
        assert envelope.validator.version == "3"

    def test_org_info(self):
        """Organization fields should be populated in the envelope.

        The container uses org info for storage path construction and
        logging — it needs to know which org's data it's processing.
        """
        envelope = _build_envelope(org_id="org-456", org_name="Test Organization")
        assert envelope.org.id == "org-456"
        assert envelope.org.name == "Test Organization"

    def test_workflow_info(self):
        """Workflow and step info should be populated in the envelope.

        The step name is shown in container logs for debugging which step
        of a multi-step workflow is running.
        """
        envelope = _build_envelope(
            workflow_id="workflow-789",
            step_id="step-012",
            step_name="EnergyPlus Simulation",
        )
        assert envelope.workflow.id == "workflow-789"
        assert envelope.workflow.step_id == "step-012"
        assert envelope.workflow.step_name == "EnergyPlus Simulation"

    def test_model_file_in_input_files(self):
        """The primary model file should appear in ``input_files``.

        Only the model file goes in ``input_files``; weather and other
        auxiliary files go in ``resource_files``.  The runner treats
        ``input_files[0]`` as the primary model to simulate.
        """
        envelope = _build_envelope(
            model_file=_file_identity("gs://test-bucket/model.idf"),
        )
        assert len(envelope.input_files) == 1
        model_file = envelope.input_files[0]
        assert model_file.uri == "gs://test-bucket/model.idf"
        assert model_file.role == "primary-model"

    def test_weather_resource_in_resource_files(self):
        """Weather files should appear in ``resource_files``.

        The runner downloads resource files to a working directory alongside
        the model.  Weather file URIs may be ``gs://`` (GCP) or ``file://``
        (Docker Compose local dev).
        """
        weather = _make_weather_resource(uri="gs://test-bucket/weather.epw")
        envelope = _build_envelope(resource_files=[weather])

        assert len(envelope.resource_files) == 1
        assert envelope.resource_files[0].type == "energyplus_weather"
        assert envelope.resource_files[0].uri == "gs://test-bucket/weather.epw"

    def test_execution_context(self):
        """The execution context should carry callback info and bundle URI.

        The callback URL is where the container POSTs its output envelope
        when done.  The execution bundle URI is the directory where all
        run artifacts (input, output, logs) are stored.
        """
        envelope = _build_envelope(
            callback_url="https://api.example.com/callbacks/",
            execution_bundle_uri="gs://test-bucket/runs/run-123/",
        )
        assert (
            str(envelope.context.callback_url) == "https://api.example.com/callbacks/"
        )
        assert envelope.context.execution_bundle_uri == "gs://test-bucket/runs/run-123/"

    def test_timestep_per_hour_default(self):
        """The default ``timestep_per_hour`` should be 4.

        EnergyPlus defaults to 6, but we use 4 for faster simulations
        in the common case.  Authors can override via step config.
        """
        envelope = _build_envelope()  # No timestep_per_hour override
        assert envelope.inputs.timestep_per_hour == 4  # noqa: PLR2004

    def test_timestep_per_hour_custom(self):
        """Custom ``timestep_per_hour`` values should be passed through."""
        envelope = _build_envelope(timestep_per_hour=12)
        assert envelope.inputs.timestep_per_hour == 12  # noqa: PLR2004

    def test_review_readiness_settings_are_forwarded(self):
        """Checks, run mode, and profile must reach the strict backend contract."""
        envelope = _build_envelope(
            idf_checks=["duplicate-names", "schedule-coverage"],
            run_simulation=False,
            review_profile="leed_review",
        )

        assert envelope.inputs.idf_checks == [
            "duplicate-names",
            "schedule-coverage",
        ]
        assert envelope.inputs.run_simulation is False
        assert envelope.inputs.review_profile == "leed_review"


# ==============================================================================
# Callback ID — idempotency support for async backends
# ==============================================================================
# The callback_id enables idempotent callback processing.  When a container
# retries its POST (e.g., due to network timeout), the callback handler
# uses the ID to detect duplicates and skip reprocessing.
# ==============================================================================


class TestCallbackId:
    """Tests for callback ID handling in the envelope builder."""

    def test_callback_id_included_when_provided(self):
        """When a callback ID is provided, it should appear in the context.

        Async backends (GCP Cloud Run) always provide a callback ID
        for idempotent processing.  The container includes it in the
        callback POST so Django can detect duplicate deliveries.
        """
        envelope = _build_envelope(callback_id="cb-uuid-12345")
        assert envelope.context.callback_id == "cb-uuid-12345"

    def test_callback_id_none_for_sync_backends(self):
        """When callback_id is None, the context should accept it.

        Sync backends (Docker Compose) don't use callbacks — the processor
        reads the output envelope directly.  Passing ``None`` should not
        raise an error.
        """
        envelope = _build_envelope(callback_id=None)
        assert envelope.context.callback_id is None


# ==============================================================================
# Multiple resource files
# ==============================================================================


class TestMultipleResourceFiles:
    """Tests for envelopes with multiple resource files."""

    def test_multiple_resource_files_preserved(self):
        """All resource files should appear in the envelope, in order.

        While weather files are the most common, some validators need
        additional auxiliary files (e.g., library data, schedule files).
        The builder should pass them through without filtering.
        """
        weather = _make_weather_resource()
        library = ResourceFileItem(
            id="resource-lib-456",
            type="energyplus_library",
            port_key="library_file",
            uri="gs://test-bucket/library.dat",
            name="library.dat",
            size_bytes=18,
            sha256="b" * 64,
            storage_version="2",
        )
        envelope = _build_envelope(resource_files=[weather, library])

        assert len(envelope.resource_files) == 2  # noqa: PLR2004
        assert envelope.resource_files[0].type == "energyplus_weather"
        assert envelope.resource_files[1].type == "energyplus_library"


# ==============================================================================
# EnergyPlus file-port materialization
# ==============================================================================
# Declared artifact ports are the workflow-engine contract; the backend envelope
# remains the wire protocol.  These tests prove that the launch builder bridges
# those layers without reintroducing hard-coded config-only file handling.
# ==============================================================================


class TestEnergyPlusFilePortMaterialization:
    """Tests for declared EnergyPlus artifact ports in strict input envelopes."""

    def test_submitted_model_and_workflow_weather_resource_materialize(self):
        """Default file-port bindings should produce backend envelope items.

        The primary model is a submitted runtime file and the weather file is a
        workflow resource.  The envelope keeps the existing backend shape while
        adding ``port_key`` so the item is traceable to the declared contract.
        """
        run, _step, primary_port, weather_port, weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=_step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=_step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "primary_file_uri": "file:///validibot/input/model.idf",
            },
            resource_uri_overrides={
                str(weather_resource.validator_resource_file_id): (
                    "file:///validibot/input/resources/weather.epw"
                ),
            },
        )

        assert len(envelope.input_files) == 1
        assert envelope.input_files[0].port_key == "primary_model"
        assert envelope.input_files[0].role == "primary-model"
        assert envelope.input_files[0].uri == "file:///validibot/input/model.idf"
        assert len(envelope.resource_files) == 1
        assert envelope.resource_files[0].port_key == "weather_file"
        assert envelope.resource_files[0].type == ResourceFileType.ENERGYPLUS_WEATHER
        assert envelope.resource_files[0].uri.endswith("/weather.epw")
        assert envelope.inputs.timestep_per_hour == 6  # noqa: PLR2004

    def test_preflight_file_ports_do_not_require_or_materialize_weather(self):
        """Conversion-only direct validation must work without an EPW binding."""
        run, step, primary_port, _weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        step.config = {
            "run_simulation": False,
            "idf_checks": ["duplicate-names"],
            "review_profile": "standard",
            "timestep_per_hour": 4,
        }
        step.save(update_fields=["config"])
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "primary_file_uri": "file:///validibot/input/model.idf",
            },
        )

        assert envelope.resource_files == []
        assert envelope.inputs.run_simulation is False
        assert envelope.inputs.idf_checks == ["duplicate-names"]

    def test_upstream_model_artifact_materializes_as_primary_input_file(self):
        """An upstream ArtifactRef can satisfy the primary model file port.

        This guards the handoff between artifact references and file-port
        materialization, which is the core compatibility point with the
        cross-step data binding ADR.
        """
        run, step, primary_port, weather_port, weather_resource = (
            _build_energyplus_file_port_run()
        )
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Build Model",
            order=step.order - 5,
        )
        _declare_output_file_port(
            upstream_step,
            contract_key="generated_model",
            role="generated-model",
            data_format=SubmissionDataFormat.ENERGYPLUS_EPJSON,
            media_type="application/json",
            extensions=["epjson", "json"],
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="model.epjson",
                        type="generated-model",
                        mime_type="application/json",
                        uri="gs://validibot/runs/run-1/model.epjson",
                        size_bytes=456,
                    ),
                ],
                raw_outputs=None,
            ),
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            resource_uri_overrides={
                str(weather_resource.validator_resource_file_id): (
                    "file:///validibot/input/resources/weather.epw"
                ),
            },
        )

        assert envelope.input_files[0].port_key == "primary_model"
        assert envelope.input_files[0].name == "model.epjson"
        assert envelope.input_files[0].uri == "gs://validibot/runs/run-1/model.epjson"
        assert envelope.resource_files[0].port_key == "weather_file"

    def test_upstream_model_rejects_unaccepted_data_format_with_trace(self):
        """Upstream ArtifactRefs must satisfy the consumer port data format.

        The binding path proves where the artifact came from. The artifact
        metadata still has to match the file-port contract before dispatch.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Build Model",
            order=step.order - 5,
        )
        _declare_output_file_port(
            upstream_step,
            contract_key="generated_model",
            role="generated-model",
            data_format=SubmissionDataFormat.ENERGYPLUS_EPJSON,
            media_type="application/json",
            extensions=["epjson", "json"],
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="model.epjson",
                        type="generated-model",
                        mime_type="application/json",
                        uri="gs://validibot/runs/run-1/model.epjson",
                    ),
                ],
                raw_outputs=None,
            ),
        )
        Artifact.objects.filter(step_run=upstream_run).update(
            data_format=SubmissionDataFormat.CSV,
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not accept data format"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "csv" in trace.error_message

    def test_upstream_model_rejects_wrong_media_type_with_trace(self):
        """Explicitly wrong upstream artifact media types are not ignored.

        Generic JSON is accepted for legacy epJSON artifacts, but a clearly
        unrelated MIME type should fail even when the filename extension looks
        plausible.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Build Model",
            order=step.order - 5,
        )
        _declare_output_file_port(
            upstream_step,
            contract_key="generated_model",
            role="generated-model",
            data_format=SubmissionDataFormat.ENERGYPLUS_EPJSON,
            media_type="application/json",
            extensions=["epjson", "json"],
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="model.epjson",
                        type="generated-model",
                        mime_type="application/json",
                        uri="gs://validibot/runs/run-1/model.epjson",
                    ),
                ],
                raw_outputs=None,
            ),
        )
        Artifact.objects.filter(step_run=upstream_run).update(
            content_type="application/pdf",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not accept media type"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "application/pdf" in trace.error_message

    def test_missing_weather_resource_fails_with_port_specific_error(self):
        """A declared weather port should fail before launching without a file."""
        run, step, primary_port, weather_port, weather_resource = (
            _build_energyplus_file_port_run()
        )
        weather_resource.delete()
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="weather_file"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
            )

    def test_submitted_weather_file_materializes_as_input_file(self):
        """Submitted EPW files should populate the weather artifact port.

        Managed weather resources stay in ``resource_files``. When the author
        chooses "Submitted file" for the weather port, the EPW is a launch-time
        input and must ride in ``input_files`` with the declared port key.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="",
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "primary_file_uri": "file:///validibot/input/model.idf",
                "weather_file": "file:///validibot/input/resources/weather.epw",
            },
        )

        assert [item.port_key for item in envelope.input_files] == [
            "primary_model",
            "weather_file",
        ]
        weather_item = envelope.input_files[1]
        assert weather_item.role == "weather"
        assert weather_item.name == "weather.epw"
        assert weather_item.uri == "file:///validibot/input/resources/weather.epw"
        assert envelope.resource_files == []

    def test_submitted_weather_file_records_artifact_input_traces(self):
        """File-port resolution should leave auditable input trace rows.

        The envelope proves what the backend receives; the trace table proves
        why that file was selected for this step. This is the bridge evidence
        and credentials need later.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="",
        )

        weather_identity = _file_identity(
            "file:///validibot/input/resources/weather.epw",
        )
        _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "primary_file_uri": "file:///validibot/input/model.idf",
                "weather_file": weather_identity,
            },
        )

        traces = {
            trace.input_contract_key: trace
            for trace in ResolvedInputTrace.objects.filter(
                step_run=run.current_step_run,
            )
        }
        assert traces["primary_model"].resolved is True
        assert traces["primary_model"].value_snapshot["uri"].endswith("model.idf")
        assert traces["weather_file"].resolved is True
        assert traces["weather_file"].source_scope_used == (
            BindingSourceScope.SUBMISSION_FILE
        )
        assert traces["weather_file"].value_snapshot == {
            "source": BindingSourceScope.SUBMISSION_FILE,
            "port_key": "weather_file",
            "role": "weather",
            **weather_identity.envelope_fields(),
        }

    def test_missing_weather_file_records_failed_artifact_input_trace(self):
        """Missing artifact files should fail with a persisted port trace."""
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="",
        )

        with pytest.raises(ValueError, match="weather_file"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="weather_file",
        )
        assert trace.resolved is False
        assert "submitted file identity" in trace.error_message

    def test_wrong_weather_extension_fails_before_dispatch_with_trace(self):
        """Wrong file formats should fail before the backend is launched."""
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="",
        )

        with pytest.raises(ValueError, match=r"expected one of \.epw"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                    "weather_file": "file:///validibot/input/resources/weather.txt",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="weather_file",
        )
        assert trace.resolved is False
        assert "expected one of .epw" in trace.error_message

    def test_disallowed_artifact_source_scope_fails_with_trace(self):
        """Bindings must use a source scope declared by the artifact port.

        This protects the workflow contract from accidental UI/API drift: a
        submitted file cannot satisfy a port that was narrowed to upstream
        artifacts only.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        primary_port.allowed_source_scopes = [BindingSourceScope.UPSTREAM_ARTIFACT]
        primary_port.save(update_fields=["allowed_source_scopes"])
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not allow source scope"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "does not allow source scope" in trace.error_message

    def test_submitted_model_rejects_unaccepted_data_format_with_trace(self):
        """Accepted data formats are enforced after URI resolution.

        Extension checks alone are not enough once ports declare semantic data
        formats. An epJSON file should fail when the port is narrowed to IDF.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        primary_port.accepted_data_formats = [SubmissionDataFormat.ENERGYPLUS_IDF]
        primary_port.save(update_fields=["accepted_data_formats"])
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not accept data format"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.epjson",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "energyplus_epjson" in trace.error_message

    def test_submitted_model_rejects_unaccepted_media_type_with_trace(self):
        """Accepted media types are enforced separately from data formats.

        A port may accept the epJSON data format only when it is carried with
        an accepted MIME type. This prevents generic file acceptance from
        bypassing the declared backend contract.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        primary_port.accepted_media_types = ["application/vnd.energyplus.idf"]
        primary_port.save(update_fields=["accepted_media_types"])
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not accept media type"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.epjson",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "application/vnd.energyplus.epjson" in trace.error_message

    def test_duplicate_weather_resources_fail_cardinality_with_trace(self):
        """Workflow resources must satisfy the port's declared cardinality.

        EnergyPlus weather accepts one EPW file. If two matching resources are
        attached, launch should fail before the backend has to guess.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        WorkflowStepResourceFactory(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=ValidatorResourceFileFactory(
                validator=step.validator,
                resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            ),
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="accepts at most 1"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="weather_file",
        )
        assert trace.resolved is False
        assert "accepts at most 1" in trace.error_message

    def test_workflow_weather_resource_rejects_wrong_extension_with_trace(self):
        """Managed workflow resources are validated like submitted files.

        The resource table tells us the intended type, but the dispatch URI
        still needs to match the concrete backend file contract.
        """
        run, step, primary_port, weather_port, weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match=r"expected one of \.epw"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
                resource_uri_overrides={
                    str(weather_resource.validator_resource_file_id): (
                        "file:///validibot/input/resources/weather.txt"
                    ),
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="weather_file",
        )
        assert trace.resolved is False
        assert "expected one of .epw" in trace.error_message


# ==============================================================================
# PDF package file-port and selector materialization
# ==============================================================================
# PDF has three independent optional typed outputs. This boundary test protects
# them from being silently dropped while the submitted carrier is resolved by
# its required file-port key.
# ==============================================================================


class TestPdfFilePortMaterialization:
    """Tests for the complete application-to-PDF-backend input contract."""

    def test_all_selectors_and_bounded_deadline_reach_the_pdf_envelope(self):
        """No fixed selector may disappear when Django builds the attempt."""
        run, step, port = _build_pdf_file_port_run()
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary",
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "primary_file_uri": "file:///validibot/input/package.pdf",
            },
        )

        assert isinstance(envelope, PdfInputEnvelope)
        assert envelope.input_files[0].port_key == "pdf_document"
        assert envelope.inputs.selected_xml.original_filename == "handover.xml"
        assert envelope.inputs.selected_json.rich_media_asset_name == "asset-index"
        assert envelope.inputs.selected_step_p21.step_file_schema == ["AP242_FIXTURE"]
        assert envelope.inputs.emit_extracted_files_bundle is True
        assert (
            envelope.inputs.limits.max_execution_seconds
            == EXPECTED_PDF_MAX_EXECUTION_SECONDS
        )


# ==============================================================================
# SHACL data-graph file-port materialization
# ==============================================================================
# The artifact-port contract names the RDF ``data_graph`` explicitly and keeps
# its envelope identity independent of list position or backend role labels.
# ==============================================================================


class TestSHACLDataGraphFilePortMaterialization:
    """Tests for resolving SHACL's RDF data graph through artifact ports."""

    def test_submitted_rdf_data_graph_materializes_with_port_key(self):
        """A submitted Turtle file should populate SHACL ``input_files``.

        The backend still receives the first input file URI, but the envelope
        now carries ``port_key=data_graph`` so traces, evidence, and future
        binding UIs can identify the semantic input instead of relying on an
        implicit SHACL-only convention.
        """
        run, step, data_graph_port = _build_shacl_data_graph_run()
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=data_graph_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="data_graph",
        )

        data_graph_identity = _file_identity(
            "file:///validibot/input/submission.ttl",
        )
        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "data_graph": data_graph_identity,
            },
        )

        assert isinstance(envelope, SHACLInputEnvelope)
        assert envelope.input_files[0].port_key == "data_graph"
        assert envelope.input_files[0].role == "data-graph"
        assert envelope.input_files[0].name == "submission.ttl"
        assert envelope.input_files[0].uri == "file:///validibot/input/submission.ttl"
        assert envelope.input_files[0].mime_type == "text/turtle"
        assert envelope.inputs.rdf_format == "turtle"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="data_graph",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.SUBMISSION_FILE
        assert trace.value_snapshot == {
            "source": BindingSourceScope.SUBMISSION_FILE,
            "port_key": "data_graph",
            "role": "data-graph",
            **data_graph_identity.envelope_fields(),
        }

    def test_upstream_rdf_artifact_sets_auto_detected_format_from_artifact_uri(self):
        """Upstream ArtifactRefs should drive SHACL auto-format detection.

        If the original submission was Turtle but a previous step produced
        JSON-LD, the SHACL backend must parse the upstream artifact as JSON-LD.
        This pins the handoff to the artifact's URI rather than the original
        submission filename.
        """
        run, step, data_graph_port = _build_shacl_data_graph_run(
            original_filename="original.ttl",
        )
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Build RDF",
            order=step.order - 5,
        )
        _declare_output_file_port(
            upstream_step,
            contract_key="data_graph",
            role="data_graph",
            data_format=SubmissionDataFormat.JSON,
            media_type="application/ld+json",
            accepted_media_types=["application/ld+json", "application/json"],
            extensions=["jsonld"],
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="graph.jsonld",
                        type="data_graph",
                        mime_type="application/json",
                        uri="gs://validibot/runs/run-1/graph.jsonld",
                        size_bytes=789,
                    ),
                ],
                raw_outputs=None,
            ),
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=data_graph_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.data_graph",
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
        )

        assert envelope.input_files[0].port_key == "data_graph"
        assert envelope.input_files[0].uri == "gs://validibot/runs/run-1/graph.jsonld"
        assert envelope.input_files[0].mime_type == "application/ld+json"
        assert envelope.inputs.rdf_format == "json-ld"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="data_graph",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.UPSTREAM_ARTIFACT
        assert trace.upstream_step_key == upstream_step.step_key

    def test_wrong_data_graph_extension_fails_before_dispatch_with_trace(self):
        """SHACL should reject files outside the declared RDF extension set."""
        run, step, data_graph_port = _build_shacl_data_graph_run()
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=data_graph_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="data_graph",
        )

        with pytest.raises(ValueError, match=r"expected one of \.ttl"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "data_graph": "file:///validibot/input/submission.txt",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="data_graph",
        )
        assert trace.resolved is False
        assert "expected one of .ttl" in trace.error_message


# ==============================================================================
# Schematron XML document file-port materialization
# ==============================================================================
# Schematron rules intentionally remain inline in SchematronInputs for this
# slice. The submitted XML document is the artifact port because it is the
# data-plane object the backend downloads and validates.
# ==============================================================================


class TestSchematronXmlDocumentFilePortMaterialization:
    """Tests for resolving Schematron's XML document through artifact ports."""

    def test_submitted_xml_document_materializes_with_port_key(self):
        """A submitted XML file should populate Schematron ``input_files``."""
        run, step, xml_document_port = _build_schematron_xml_document_run()
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=xml_document_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="xml_document",
        )

        xml_identity = _file_identity("file:///validibot/input/invoice.xml")
        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "xml_document": xml_identity,
            },
        )

        assert isinstance(envelope, SchematronInputEnvelope)
        assert envelope.input_files[0].port_key == "xml_document"
        assert envelope.input_files[0].role == "xml-document"
        assert envelope.input_files[0].name == "invoice.xml"
        assert envelope.input_files[0].uri == "file:///validibot/input/invoice.xml"
        assert envelope.input_files[0].mime_type == "application/xml"
        assert "schematron" in envelope.inputs.schematron_text

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="xml_document",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.SUBMISSION_FILE
        assert trace.value_snapshot == {
            "source": BindingSourceScope.SUBMISSION_FILE,
            "port_key": "xml_document",
            "role": "xml-document",
            **xml_identity.envelope_fields(),
        }

    def test_upstream_xml_artifact_materializes_as_schematron_input_file(self):
        """An upstream XML ArtifactRef can satisfy the XML document port."""
        run, step, xml_document_port = _build_schematron_xml_document_run()
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Normalize XML",
            order=step.order - 5,
        )
        _declare_output_file_port(
            upstream_step,
            contract_key="xml_document",
            role="xml_document",
            data_format=SubmissionDataFormat.XML,
            media_type="application/xml",
            extensions=["xml"],
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="normalized.xml",
                        type="xml_document",
                        mime_type="application/xml",
                        uri="gs://validibot/runs/run-1/normalized.xml",
                        size_bytes=321,
                    ),
                ],
                raw_outputs=None,
            ),
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=xml_document_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.xml_document",
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
        )

        assert envelope.input_files[0].port_key == "xml_document"
        assert envelope.input_files[0].role == "xml-document"
        assert envelope.input_files[0].uri == (
            "gs://validibot/runs/run-1/normalized.xml"
        )
        assert envelope.input_files[0].mime_type == "application/xml"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="xml_document",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.UPSTREAM_ARTIFACT
        assert trace.upstream_step_key == upstream_step.step_key

    def test_wrong_xml_document_extension_fails_before_dispatch_with_trace(self):
        """Schematron should reject non-XML files before backend dispatch."""
        run, step, xml_document_port = _build_schematron_xml_document_run()
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=xml_document_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="xml_document",
        )

        with pytest.raises(ValueError, match=r"expected one of \.xml"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "xml_document": "file:///validibot/input/invoice.txt",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="xml_document",
        )
        assert trace.resolved is False
        assert "expected one of .xml" in trace.error_message


# ==============================================================================
# FMU input bindings
# ==============================================================================
# FMU envelopes must receive values through explicit StepInputBinding rows.
# Passing the whole submission JSON when bindings are missing would reintroduce
# a second execution contract and hide missing author wiring.
# ==============================================================================


class TestFMUFilePortMaterialization:
    """Tests for resolving the FMU model file through artifact-port bindings."""

    def test_step_owned_fmu_model_resolves_through_artifact_port(self):
        """Step-level FMU uploads should materialize as ``fmu_model`` input files."""
        run, step = _build_fmu_run()
        fmu_port = _make_fmu_model_port(step.validator)
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={"fmu_model_uri": "file:///validibot/input/model.fmu"},
        )

        assert envelope.input_files[0].port_key == "fmu_model"
        assert envelope.input_files[0].role == "fmu"
        assert envelope.input_files[0].name == "model.fmu"
        assert envelope.input_files[0].uri == "file:///validibot/input/model.fmu"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="fmu_model",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.WORKFLOW_RESOURCE
        assert trace.value_snapshot["type"] == FMU_MODEL_RESOURCE

    def test_library_fmu_model_resolves_through_system_artifact_port(self):
        """Library FMU validators should also use the same ``fmu_model`` port."""
        fmu_model = FMUModel.objects.create(
            org=None,
            name="Library FMU",
            file=SimpleUploadedFile("library.fmu", b"fmu-bytes"),
            checksum="abc123",
            gcs_uri="gs://validibot/fmus/library.fmu",
        )
        validator = ValidatorFactory(
            validation_type=ValidationType.FMU,
            fmu_model=fmu_model,
        )
        step = WorkflowStepFactory(validator=validator)
        submission = SubmissionFactory(
            workflow=step.workflow,
            org=step.workflow.org,
            content="{}",
        )
        run = ValidationRunFactory(
            workflow=step.workflow,
            org=step.workflow.org,
            submission=submission,
        )
        _create_step_run_with_attempt(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
        )
        fmu_port = _make_fmu_model_port(validator)
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=fmu_port,
            source_scope=BindingSourceScope.SYSTEM,
            source_data_path="fmu_model",
        )

        fmu_identity = _file_identity("gs://validibot/fmus/library.fmu")
        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "fmu_model_uri": fmu_identity,
            },
        )

        assert envelope.input_files[0].port_key == "fmu_model"
        assert envelope.input_files[0].uri == "gs://validibot/fmus/library.fmu"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="fmu_model",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.SYSTEM
        assert trace.value_snapshot["fmu_model_id"] == str(fmu_model.id)
        assert trace.value_snapshot["sha256"] == fmu_identity.sha256

    def test_missing_step_owned_fmu_model_fails_with_port_trace(self):
        """Missing FMU resources should fail before dispatch with a port trace."""
        validator = ValidatorFactory(validation_type=ValidationType.FMU)
        step = WorkflowStepFactory(validator=validator)
        submission = SubmissionFactory(
            workflow=step.workflow,
            org=step.workflow.org,
            content="{}",
        )
        run = ValidationRunFactory(
            workflow=step.workflow,
            org=step.workflow.org,
            submission=submission,
        )
        _create_step_run_with_attempt(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
        )
        fmu_port = _make_fmu_model_port(validator)
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )

        with pytest.raises(ValueError, match="fmu_model"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="fmu_model",
        )
        assert trace.resolved is False
        assert "expected at least 1" in trace.error_message

    def test_step_owned_fmu_model_rejects_wrong_extension_with_trace(self):
        """The FMU file-port contract should reject non-FMU filenames."""
        run, step = _build_fmu_run()
        fmu_port = _make_fmu_model_port(step.validator)
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )

        with pytest.raises(ValueError, match=r"expected one of \.fmu"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={"fmu_model_uri": "file:///validibot/input/model.txt"},
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            input_contract_key="fmu_model",
        )
        assert trace.resolved is False
        assert "expected one of .fmu" in trace.error_message


class TestFMUInputBindings:
    """Tests for FMU input-value construction in ``_build_test_input_envelope()``."""

    def test_no_declared_fmu_inputs_produces_empty_input_values(self):
        """A step with no declared FMU inputs should launch with an empty map."""
        run, step = _build_fmu_run(
            submission_content='{"accidental": "must-not-enter-envelope"}',
        )
        fmu_port = _make_fmu_model_port(step.validator)
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={"fmu_model_uri": "file:///validibot/input/model.fmu"},
        )

        assert envelope.inputs.input_values == {}

    def test_declared_fmu_input_without_binding_fails_closed(self):
        """Declared inputs require bindings; raw submission JSON is not a fallback."""
        run, step = _build_fmu_run(submission_content='{"panel_area": 150.0}')
        fmu_port = _make_fmu_model_port(step.validator)
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )
        StepIODefinitionFactory(
            workflow_step=step,
            validator=None,
            contract_key="panel_area",
            native_name="Panel.Area",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.FMU,
        )

        with pytest.raises(ValueError, match="StepInputBinding"):
            _build_test_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={"fmu_model_uri": "file:///validibot/input/model.fmu"},
            )

    def test_declared_fmu_input_uses_binding_not_entire_submission(self):
        """Only bound values should reach envelope and canonical step state."""
        run, step = _build_fmu_run(
            submission_content=(
                '{"building": {"panel_area": 150.0}, '
                '"accidental": "must-not-enter-envelope"}'
            ),
        )
        fmu_port = _make_fmu_model_port(step.validator)
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )
        io_definition = StepIODefinitionFactory(
            workflow_step=step,
            validator=None,
            contract_key="panel_area",
            native_name="Panel.Area",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.FMU,
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=io_definition,
            source_data_path="building.panel_area",
        )

        envelope = _build_test_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={"fmu_model_uri": "file:///validibot/input/model.fmu"},
        )

        assert envelope.inputs.input_values == {"Panel.Area": 150.0}
        step_run = run.step_runs.get(workflow_step=step)
        assert step_run.input_values == {"panel_area": 150.0}
        assert "resolved_inputs" not in step_run.output
