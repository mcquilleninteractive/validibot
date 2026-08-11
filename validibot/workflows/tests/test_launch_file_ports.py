"""Tests for launch-time submitted-file artifact ports.

EnergyPlus is the first workflow-engine vertical slice with declared file
ports. These tests cover the launch-page side of that contract: when an author
wires an artifact input port to "Submitted file", the launch form must collect
that extra file and persist it with the submission so the execution layer can
materialize it later.
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test import override_settings

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.models import SubmissionInputFile
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.forms import WorkflowLaunchForm
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_launch_helpers import build_submission_from_form

pytestmark = pytest.mark.django_db


def _energyplus_workflow_with_submitted_weather():
    """Create an EnergyPlus step whose weather file comes from launch upload."""

    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.TEXT],
    )
    workflow.user.set_current_org(workflow.org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        name="Run simulation",
    )
    primary_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="primary_model",
        native_name="primary_model",
        label="Primary Model",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        metadata={"accepted_extensions": ["idf", "epjson", "json"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="primary-model",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        accepted_data_formats=[
            SubmissionDataFormat.ENERGYPLUS_IDF,
            SubmissionDataFormat.ENERGYPLUS_EPJSON,
        ],
    )
    weather_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="weather_file",
        native_name="weather_file",
        label="Weather File",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
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
        accepted_data_formats=[ResourceFileType.ENERGYPLUS_WEATHER],
    )
    StepInputBindingFactory(
        workflow_step=step,
        io_definition=primary_port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
        is_required=True,
    )
    StepInputBindingFactory(
        workflow_step=step,
        io_definition=weather_port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="weather_file",
        is_required=True,
    )
    return workflow, step


def _shacl_workflow_with_primary_data_graph():
    """Create a SHACL step whose data graph is the main launch submission."""

    validator = ValidatorFactory(validation_type=ValidationType.SHACL)
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.TEXT],
    )
    workflow.user.set_current_org(workflow.org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        name="Validate RDF",
    )
    data_graph_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="data_graph",
        native_name="data_graph",
        label="Data Graph",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        metadata={"accepted_extensions": ["ttl", "rdf", "jsonld", "nt", "nq"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="data-graph",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        accepted_data_formats=[
            SubmissionDataFormat.TEXT,
            SubmissionDataFormat.JSON,
            SubmissionDataFormat.XML,
        ],
    )
    StepInputBindingFactory(
        workflow_step=step,
        io_definition=data_graph_port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
        is_required=True,
    )
    return workflow, step


def _schematron_workflow_with_primary_xml_document():
    """Create a Schematron step whose XML document is the main submission."""

    validator = ValidatorFactory(validation_type=ValidationType.SCHEMATRON)
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.XML],
    )
    workflow.user.set_current_org(workflow.org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        name="Validate XML rules",
    )
    xml_document_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="xml_document",
        native_name="xml_document",
        label="XML Document",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        metadata={"accepted_extensions": ["xml"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="xml-document",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        accepted_data_formats=[SubmissionDataFormat.XML],
    )
    StepInputBindingFactory(
        workflow_step=step,
        io_definition=xml_document_port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
        is_required=True,
    )
    return workflow, step


def _primary_model_upload() -> SimpleUploadedFile:
    """Return a minimal EnergyPlus model upload for launch-form tests."""

    return SimpleUploadedFile(
        "model.idf",
        b"Version,25.1;",
        content_type="text/plain",
    )


def _weather_upload(name: str = "weather.epw") -> SimpleUploadedFile:
    """Return a minimal weather-file upload for launch-form tests."""

    return SimpleUploadedFile(
        name,
        b"LOCATION,Test Weather",
        content_type="application/vnd.energyplus.epw",
    )


def test_launch_form_adds_extra_file_field_for_submitted_weather_port():
    """Submitted-file weather ports should render as launch upload fields."""

    workflow, step = _energyplus_workflow_with_submitted_weather()

    form = WorkflowLaunchForm(workflow=workflow, user=workflow.user)

    field_name = f"submitted_file_port__{step.pk}__weather_file"
    assert field_name in form.fields
    assert form.fields[field_name].label == "Weather File"
    assert "Accepted extensions: .epw." in str(form.fields[field_name].help_text)


def test_launch_form_uses_primary_upload_for_shacl_data_graph_port():
    """SHACL's primary data graph should not render a duplicate upload field."""

    workflow, step = _shacl_workflow_with_primary_data_graph()

    form = WorkflowLaunchForm(workflow=workflow, user=workflow.user)

    field_name = f"submitted_file_port__{step.pk}__data_graph"
    assert field_name not in form.fields


def test_launch_form_uses_primary_upload_for_schematron_xml_document_port():
    """Schematron's primary XML document should not render a duplicate field."""

    workflow, step = _schematron_workflow_with_primary_xml_document()

    form = WorkflowLaunchForm(workflow=workflow, user=workflow.user)

    field_name = f"submitted_file_port__{step.pk}__xml_document"
    assert field_name not in form.fields


def test_launch_form_requires_submitted_weather_file():
    """Required submitted artifact ports should block launch when missing."""

    workflow, step = _energyplus_workflow_with_submitted_weather()
    field_name = f"submitted_file_port__{step.pk}__weather_file"

    form = WorkflowLaunchForm(
        data={"file_type": SubmissionFileType.TEXT},
        files={"attachment": _primary_model_upload()},
        workflow=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert "Weather File is required" in form.errors[field_name][0]


def test_launch_form_accepts_submitted_weather_file():
    """A valid EPW upload should satisfy the weather artifact port."""

    workflow, step = _energyplus_workflow_with_submitted_weather()
    field_name = f"submitted_file_port__{step.pk}__weather_file"

    form = WorkflowLaunchForm(
        data={"file_type": SubmissionFileType.TEXT},
        files={
            "attachment": _primary_model_upload(),
            field_name: _weather_upload(),
        },
        workflow=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data[field_name].name == "weather.epw"


def test_launch_form_rejects_wrong_submitted_weather_extension():
    """Submitted weather uploads should respect the port's declared formats."""

    workflow, step = _energyplus_workflow_with_submitted_weather()
    field_name = f"submitted_file_port__{step.pk}__weather_file"

    form = WorkflowLaunchForm(
        data={"file_type": SubmissionFileType.TEXT},
        files={
            "attachment": _primary_model_upload(),
            field_name: _weather_upload("weather.txt"),
        },
        workflow=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert "Accepted extensions: .epw" in form.errors[field_name][0]


def test_build_submission_from_form_persists_submitted_weather_file(tmp_path):
    """Submission creation should store extra port files transactionally."""

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        workflow, step = _energyplus_workflow_with_submitted_weather()
        field_name = f"submitted_file_port__{step.pk}__weather_file"
        form = WorkflowLaunchForm(
            data={"file_type": SubmissionFileType.TEXT},
            files={
                "attachment": _primary_model_upload(),
                field_name: _weather_upload(),
            },
            workflow=workflow,
            user=workflow.user,
        )
        assert form.is_valid(), form.errors
        request = RequestFactory().post("/launch/")
        request.user = workflow.user

        submission_build = build_submission_from_form(
            request=request,
            workflow=workflow,
            cleaned_data=form.cleaned_data,
        )

        port_file = SubmissionInputFile.objects.get(
            submission=submission_build.submission,
            workflow_step=step,
            port_key="weather_file",
        )
        assert port_file.original_filename == "weather.epw"
        assert port_file.read_bytes() == b"LOCATION,Test Weather"
