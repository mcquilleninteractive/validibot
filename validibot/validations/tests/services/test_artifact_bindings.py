"""Test generic singleton cross-step artifact dependency edges.

The suite protects the authoring compatibility filter, relational producer
identity, workflow ordering invariant, and execution-time byte verification
used by PDF-to-XML composition.
"""

from __future__ import annotations

import hashlib

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db.models.deletion import RestrictedError

from validibot.actions.protocols import RunContext
from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import RulesetType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Artifact
from validibot.validations.services.artifact_bindings import compatible_artifact_choices
from validibot.validations.services.artifact_bindings import set_artifact_input_binding
from validibot.validations.services.artifact_bindings import (
    validate_workflow_dependencies,
)
from validibot.validations.services.resolved_files import resolve_file_inputs
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.xml_schema.validator import XmlSchemaValidator
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
    allow_upstream: bool = False,
):
    """Create one singleton file port with the fields compatibility needs."""
    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key=contract_key,
        native_name=contract_key,
        direction=direction,
        data_type="artifact_ref",
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        data_format=data_format,
        media_type=media_type,
        accepted_data_formats=[data_format],
        accepted_media_types=[media_type],
        allowed_source_scopes=(
            [BindingSourceScope.SUBMISSION_FILE, BindingSourceScope.UPSTREAM_ARTIFACT]
            if allow_upstream
            else []
        ),
        envelope_channel=(
            EnvelopeChannel.INPUT_FILES
            if direction == StepIODirection.INPUT
            else EnvelopeChannel.OUTPUT_ARTIFACTS
        ),
        role=contract_key,
        min_items=1,
        max_items=1,
        is_collection=False,
    )


def test_choices_only_include_compatible_earlier_outputs() -> None:
    """The author dropdown must hide later and format-incompatible outputs."""
    workflow = WorkflowFactory()
    pdf_validator = ValidatorFactory()
    xml_validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=pdf_validator,
        order=10,
        name="Inspect package",
    )
    consumer = WorkflowStepFactory(
        workflow=workflow,
        validator=xml_validator,
        order=20,
        name="Validate XML",
    )
    compatible = _artifact_port(
        validator=pdf_validator,
        contract_key="selected_xml",
        direction=StepIODirection.OUTPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
    )
    _artifact_port(
        validator=pdf_validator,
        contract_key="pdf_inventory",
        direction=StepIODirection.OUTPUT,
        data_format=SubmissionDataFormat.JSON,
        media_type="application/json",
    )
    consumer_port = _artifact_port(
        validator=xml_validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )

    choices = compatible_artifact_choices(
        consumer_step=consumer,
        consumer_port=consumer_port,
        workflow=workflow,
    )

    assert [choice.output_definition_id for choice in choices] == [compatible.pk]
    assert choices[0].reference == f"{producer.step_key}.selected_xml"


def test_relational_edge_blocks_producer_deletion_and_invalid_reordering() -> None:
    """A persisted dependency must protect its producer and topological order."""
    workflow = WorkflowFactory()
    pdf_validator = ValidatorFactory()
    xml_validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=pdf_validator,
        order=10,
    )
    consumer = WorkflowStepFactory(
        workflow=workflow,
        validator=xml_validator,
        order=20,
    )
    output = _artifact_port(
        validator=pdf_validator,
        contract_key="selected_xml",
        direction=StepIODirection.OUTPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
    )
    input_port = _artifact_port(
        validator=xml_validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )

    binding = set_artifact_input_binding(
        consumer_step=consumer,
        consumer_port=input_port,
        source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        artifact_reference=f"{producer.step_key}.{output.contract_key}",
    )

    assert binding.source_step == producer
    assert binding.source_output_io_definition == output
    with pytest.raises(ValidationError, match="must remain before"):
        validate_workflow_dependencies(
            workflow,
            proposed_order={producer.pk: 20, consumer.pk: 10},
        )
    with pytest.raises(RestrictedError):
        producer.delete()


def test_resolver_verifies_and_returns_exact_upstream_xml_bytes() -> None:
    """An in-process XML validator receives bytes from the exact run artifact."""
    workflow = WorkflowFactory()
    pdf_validator = ValidatorFactory()
    xml_validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=pdf_validator,
        order=10,
    )
    consumer = WorkflowStepFactory(
        workflow=workflow,
        validator=xml_validator,
        order=20,
    )
    output = _artifact_port(
        validator=pdf_validator,
        contract_key="selected_xml",
        direction=StepIODirection.OUTPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
    )
    input_port = _artifact_port(
        validator=xml_validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )
    set_artifact_input_binding(
        consumer_step=consumer,
        consumer_port=input_port,
        source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        artifact_reference=f"{producer.step_key}.{output.contract_key}",
    )
    run = ValidationRunFactory(workflow=workflow)
    producer_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=producer,
    )
    consumer_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=consumer,
    )
    xml = b'<handover xmlns="urn:example"><id>A-1</id></handover>'
    digest = hashlib.sha256(xml).hexdigest()
    artifact = Artifact.objects.create(
        org=run.org,
        validation_run=run,
        step_run=producer_run,
        workflow_step=producer,
        label="selected.xml",
        content_type="application/xml",
        contract_key="selected_xml",
        role="selected_xml",
        kind=ArtifactKind.FILE,
        data_format=SubmissionDataFormat.XML,
        size_bytes=len(xml),
        sha256=digest,
        storage_version=f"sha256:{digest}",
    )
    artifact.file.save("selected.xml", ContentFile(xml), save=True)

    resolved = resolve_file_inputs(
        run=run,
        step=consumer,
        step_run=consumer_run,
    )

    item = resolved["xml_document"]
    assert item.content == xml
    assert item.identity.sha256 == digest
    assert item.producer_step_key == producer.step_key
    trace = consumer_run.input_traces.get(input_contract_key="xml_document")
    assert trace.resolved is True
    assert trace.value_snapshot["artifact_id"] == str(artifact.pk)

    ruleset = RulesetFactory(
        org=workflow.org,
        ruleset_type=RulesetType.XML_SCHEMA,
        rules_text=(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:example" elementFormDefault="qualified">'
            '<xs:element name="handover"><xs:complexType><xs:sequence>'
            '<xs:element name="id" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element></xs:schema>"
        ),
        metadata={"schema_type": XMLSchemaType.XSD},
    )
    result = XmlSchemaValidator().validate(
        xml_validator,
        run.submission,
        ruleset,
        RunContext(
            validation_run=run,
            step=consumer,
            resolved_file_inputs=resolved,
        ),
    )

    assert result.passed is True
    assert result.issues == []


def _bound_xml_artifact_case():
    """Build one PDF-output-to-XML-input edge and its concrete run rows."""
    workflow = WorkflowFactory()
    pdf_validator = ValidatorFactory()
    xml_validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=pdf_validator,
        order=10,
        name="Inspect package",
    )
    consumer = WorkflowStepFactory(
        workflow=workflow,
        validator=xml_validator,
        order=20,
        name="Validate selected XML",
    )
    output = _artifact_port(
        validator=pdf_validator,
        contract_key="selected_xml",
        direction=StepIODirection.OUTPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
    )
    input_port = _artifact_port(
        validator=xml_validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )
    set_artifact_input_binding(
        consumer_step=consumer,
        consumer_port=input_port,
        source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        artifact_reference=f"{producer.step_key}.{output.contract_key}",
    )
    run = ValidationRunFactory(workflow=workflow)
    producer_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=producer,
    )
    consumer_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=consumer,
    )
    return run, producer, consumer, producer_run, consumer_run


def test_resolver_fails_when_required_upstream_output_is_missing() -> None:
    """A producer success without its promised singleton output must not fall back."""
    run, _producer, consumer, _producer_run, consumer_run = _bound_xml_artifact_case()

    with pytest.raises(ValueError, match="did not produce 'selected_xml'"):
        resolve_file_inputs(run=run, step=consumer, step_run=consumer_run)

    trace = consumer_run.input_traces.get(input_contract_key="xml_document")
    assert trace.resolved is False
    assert "did not produce 'selected_xml'" in trace.error_message


def test_resolver_fails_when_upstream_artifact_carrier_is_incompatible() -> None:
    """Envelope metadata cannot relabel JSON bytes as the consumer's XML input."""
    run, producer, consumer, producer_run, consumer_run = _bound_xml_artifact_case()
    payload = b'{"not":"xml"}'
    digest = hashlib.sha256(payload).hexdigest()
    artifact = Artifact.objects.create(
        org=run.org,
        validation_run=run,
        step_run=producer_run,
        workflow_step=producer,
        label="selected.json",
        content_type="application/json",
        contract_key="selected_xml",
        role="selected_xml",
        kind=ArtifactKind.FILE,
        data_format=SubmissionDataFormat.JSON,
        size_bytes=len(payload),
        sha256=digest,
        storage_version=f"sha256:{digest}",
    )
    artifact.file.save("selected.json", ContentFile(payload), save=True)

    with pytest.raises(ValueError, match="does not accept data format"):
        resolve_file_inputs(run=run, step=consumer, step_run=consumer_run)

    trace = consumer_run.input_traces.get(input_contract_key="xml_document")
    assert trace.resolved is False
    assert "does not accept data format" in trace.error_message


def test_resolver_fails_when_upstream_bytes_are_tampered() -> None:
    """A stored artifact whose bytes changed after indexing must fail closed."""
    run, producer, consumer, producer_run, consumer_run = _bound_xml_artifact_case()
    payload = b'<handover xmlns="urn:example"><id>A-1</id></handover>'
    digest = hashlib.sha256(payload).hexdigest()
    artifact = Artifact.objects.create(
        org=run.org,
        validation_run=run,
        step_run=producer_run,
        workflow_step=producer,
        label="selected.xml",
        content_type="application/xml",
        contract_key="selected_xml",
        role="selected_xml",
        kind=ArtifactKind.FILE,
        data_format=SubmissionDataFormat.XML,
        size_bytes=len(payload),
        sha256=digest,
        storage_version=f"sha256:{digest}",
    )
    artifact.file.save("selected.xml", ContentFile(payload), save=True)
    with artifact.file.open("wb") as stored_file:
        stored_file.write(payload.replace(b"A-1", b"A-2"))

    with pytest.raises(ValueError, match="trusted identity"):
        resolve_file_inputs(run=run, step=consumer, step_run=consumer_run)

    trace = consumer_run.input_traces.get(input_contract_key="xml_document")
    assert trace.resolved is False
    assert "trusted identity" in trace.error_message
