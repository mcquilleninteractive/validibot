"""Tests for source-aware validator input contracts.

These tests protect the boundary between permissive workflow authoring,
workflow-level submission admission, and concrete runtime source validation.
They also pin the special JSON representation of the whole submission metadata
object so it remains a real binding scope rather than an ad-hoc JSONPath token.
"""

from __future__ import annotations

import json

import pytest
from django.core.exceptions import ValidationError

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import ValidationType
from validibot.validations.models import StepInputBinding
from validibot.validations.services.artifact_bindings import set_artifact_input_binding
from validibot.validations.services.input_contracts import InputCompatibilityLevel
from validibot.validations.services.input_contracts import analyze_primary_source
from validibot.validations.services.input_contracts import primary_source_advisory
from validibot.validations.services.resolved_files import resolve_file_inputs
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _input_port(
    *,
    validator,
    contract_key: str,
    data_format: str,
    file_type: str,
    extension: str,
    scopes: list[str],
):
    """Create one required singleton document port for contract tests."""

    media_type = {
        SubmissionDataFormat.JSON: "application/json",
        SubmissionDataFormat.XML: "application/xml",
    }[data_format]
    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key=contract_key,
        direction=StepIODirection.INPUT,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        data_format=data_format,
        accepted_data_formats=[data_format],
        media_type=media_type,
        accepted_media_types=[media_type],
        accepted_file_types=[file_type],
        accepted_extensions=[extension],
        allowed_source_scopes=scopes,
        min_items=1,
        max_items=1,
        is_path_editable=False,
    )


def test_primary_source_advisory_explains_both_author_fixes():
    """A static mismatch should explain admission and upstream composition."""

    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.PDF])
    validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    port = _input_port(
        validator=validator,
        contract_key="xml_document",
        data_format=SubmissionDataFormat.XML,
        file_type=SubmissionFileType.XML,
        extension="xml",
        scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )

    message = primary_source_advisory(workflow=workflow, port=port)

    assert message == (
        "This validator requires XML. Add XML to the allowed file types or "
        "select an earlier step output that produces it."
    )


def test_mixed_admission_contract_does_not_claim_every_run_is_compatible():
    """Partial primary coverage warns without blocking the workflow definition."""

    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.PDF, SubmissionFileType.XML]
    )
    validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    port = _input_port(
        validator=validator,
        contract_key="xml_document",
        data_format=SubmissionDataFormat.XML,
        file_type=SubmissionFileType.XML,
        extension="xml",
        scopes=[BindingSourceScope.SUBMISSION_FILE],
    )

    diagnostic = analyze_primary_source(workflow=workflow, port=port)

    assert diagnostic.level == InputCompatibilityLevel.POSSIBLY_COMPATIBLE
    assert diagnostic.unsupported_file_types == (SubmissionFileType.PDF,)
    assert primary_source_advisory(workflow=workflow, port=port) == (
        "This step expects XML from Primary submission, but this workflow also "
        "accepts PDF. PDF submissions will fail at this step."
    )


def test_json_schema_resolves_whole_submission_metadata_without_jsonpath():
    """The metadata scope with an empty subpath materializes canonical JSON."""

    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.PDF])
    validator = ValidatorFactory(validation_type=ValidationType.JSON_SCHEMA)
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    port = _input_port(
        validator=validator,
        contract_key="json_document",
        data_format=SubmissionDataFormat.JSON,
        file_type=SubmissionFileType.JSON,
        extension="json",
        scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.SUBMISSION_METADATA,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )
    binding = set_artifact_input_binding(
        consumer_step=step,
        consumer_port=port,
        source_scope=BindingSourceScope.SUBMISSION_METADATA,
        source_data_path="",
    )
    submission = SubmissionFactory(
        workflow=workflow,
        file_type=SubmissionFileType.PDF,
        content="%PDF-1.7",
        original_filename="carrier.pdf",
        metadata={"z": 2, "a": {"name": "example"}},
    )
    run = ValidationRunFactory(submission=submission)

    resolved = resolve_file_inputs(run=run, step=step)["json_document"]

    assert binding.source_data_path == ""
    assert resolved.source_scope == BindingSourceScope.SUBMISSION_METADATA
    assert resolved.name == "submission-metadata.json"
    assert resolved.identity.uri == f"submission-metadata:{submission.pk}"
    assert resolved.content == b'{"a":{"name":"example"},"z":2}'
    assert json.loads(resolved.content) == submission.metadata


def test_submission_metadata_model_invariant_rejects_json_arrays():
    """Metadata must remain an object so the binding contract is predictable."""

    with pytest.raises(ValidationError, match="must be a JSON object"):
        SubmissionFactory(metadata=[{"name": "not-an-object"}])


def test_binding_model_rejects_metadata_scope_not_declared_by_port():
    """Imports and non-form writes cannot bypass a port's source vocabulary."""

    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.XML])
    validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    port = _input_port(
        validator=validator,
        contract_key="xml_document",
        data_format=SubmissionDataFormat.XML,
        file_type=SubmissionFileType.XML,
        extension="xml",
        scopes=[BindingSourceScope.SUBMISSION_FILE],
    )
    binding = StepInputBinding(
        workflow_step=step,
        io_definition=port,
        source_scope=BindingSourceScope.SUBMISSION_METADATA,
        source_data_path="",
    )

    with pytest.raises(ValidationError, match="not allowed"):
        binding.full_clean()
