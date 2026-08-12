"""Launch compatibility tests for validators connected through file ports.

The workflow's declared submission type describes only the primary launch
file. These tests protect the distinction between a validator that consumes
that primary file and one that consumes a typed artifact produced by an
earlier step. Without that distinction, valid PDF-to-XML/JSON pipelines are
rejected even though their file-port contracts are compatible.
"""

from __future__ import annotations

import pytest

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import ValidationType
from validibot.validations.services.artifact_bindings import set_artifact_input_binding
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.services.launch_contract import LaunchContract
from validibot.workflows.services.launch_contract import ViolationCode
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _artifact_port(
    *,
    validator,
    contract_key: str,
    direction: str,
    data_format: str,
    media_type: str,
):
    """Create one singleton typed file port for a composition test."""
    allowed_source_scopes = (
        [
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ]
        if direction == StepIODirection.INPUT
        else []
    )
    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key=contract_key,
        native_name=contract_key,
        label=contract_key.replace("_", " ").title(),
        direction=direction,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        data_format=data_format,
        accepted_data_formats=[data_format],
        media_type=media_type,
        accepted_media_types=[media_type],
        allowed_source_scopes=allowed_source_scopes,
        min_items=1,
        max_items=1,
    )


@pytest.mark.parametrize(
    (
        "consumer_validation_type",
        "data_format",
        "media_type",
        "output_key",
        "input_key",
    ),
    [
        (
            ValidationType.XML_SCHEMA,
            SubmissionDataFormat.XML,
            "application/xml",
            "selected_xml",
            "xml_document",
        ),
        (
            ValidationType.JSON_SCHEMA,
            SubmissionDataFormat.JSON,
            "application/json",
            "selected_json",
            "json_document",
        ),
    ],
    ids=["xml", "json"],
)
def test_launch_allows_pdf_workflow_with_typed_validator_bound_upstream(
    consumer_validation_type,
    data_format,
    media_type,
    output_key,
    input_key,
):
    """PDF-produced XML and JSON files should satisfy their later validators."""
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.PDF])
    pdf_validator = ValidatorFactory(validation_type=ValidationType.PDF)
    consumer_validator = ValidatorFactory(validation_type=consumer_validation_type)
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=pdf_validator,
        order=10,
        name="Inspect PDF package",
    )
    consumer = WorkflowStepFactory(
        workflow=workflow,
        validator=consumer_validator,
        order=20,
        name="Validate extracted file",
    )
    output_port = _artifact_port(
        validator=pdf_validator,
        contract_key=output_key,
        direction=StepIODirection.OUTPUT,
        data_format=data_format,
        media_type=media_type,
    )
    input_port = _artifact_port(
        validator=consumer_validator,
        contract_key=input_key,
        direction=StepIODirection.INPUT,
        data_format=data_format,
        media_type=media_type,
    )
    set_artifact_input_binding(
        consumer_step=consumer,
        consumer_port=input_port,
        source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        artifact_reference=f"{producer.step_key}.{output_port.contract_key}",
    )

    violation = LaunchContract.validate(
        workflow=workflow,
        file_type=SubmissionFileType.PDF,
    )

    assert violation is None
    assert workflow.first_incompatible_step(SubmissionFileType.PDF) is None


def test_launch_rejects_xml_validator_bound_to_primary_pdf():
    """An XML file port must not silently accept the original PDF itself."""
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.PDF])
    xml_validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=xml_validator,
        name="Validate XML",
    )
    input_port = _artifact_port(
        validator=xml_validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
    )
    set_artifact_input_binding(
        consumer_step=step,
        consumer_port=input_port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
    )

    violation = LaunchContract.validate(
        workflow=workflow,
        file_type=SubmissionFileType.PDF,
    )

    assert violation is not None
    assert violation.code == ViolationCode.INCOMPATIBLE_STEP
    assert workflow.first_incompatible_step(SubmissionFileType.PDF) == step


def test_launch_keeps_legacy_check_for_validator_without_file_ports():
    """Validators lacking explicit file ports still describe the whole payload."""
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.PDF])
    xml_validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=xml_validator,
        name="Legacy XML validator",
    )

    violation = LaunchContract.validate(
        workflow=workflow,
        file_type=SubmissionFileType.PDF,
    )

    assert violation is not None
    assert violation.code == ViolationCode.INCOMPATIBLE_STEP
    assert workflow.first_incompatible_step(SubmissionFileType.PDF) == step
