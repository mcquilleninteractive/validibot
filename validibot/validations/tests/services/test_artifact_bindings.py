"""Test generic singleton cross-step artifact dependency edges.

The suite protects the authoring compatibility filter, relational producer
identity, workflow ordering invariant, and execution-time byte verification
used by PDF-to-XML composition.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from django import forms
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
from validibot.validations.models import StepInputBinding
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
from validibot.workflows.forms import ArtifactInputBindingsFormMixin
from validibot.workflows.forms import BaseStepConfigForm
from validibot.workflows.forms import FMUValidatorStepConfigForm
from validibot.workflows.forms import PortfolioManagerStepConfigForm
from validibot.workflows.forms import ShaclStepConfigForm
from validibot.workflows.forms import StepInputBindingEditForm
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import save_workflow_step

pytestmark = pytest.mark.django_db


class _ArtifactOnlyForm(ArtifactInputBindingsFormMixin, forms.Form):
    """Small harness that exercises the reusable file-source form behavior."""

    def __init__(self, *args, workflow, step, validator, **kwargs):
        self.workflow = workflow
        self.step = step
        self.validator = validator
        super().__init__(*args, **kwargs)


class _TwoPortStepForm(forms.Form):
    """Minimal valid step form whose second binding update deliberately fails."""

    name = forms.CharField()

    def __init__(self, *args, binding_updates, **kwargs):
        self._binding_updates = binding_updates
        super().__init__(*args, **kwargs)

    def build_file_port_binding_updates(self):
        """Return the ordered updates used to prove transaction rollback."""
        return self._binding_updates


def _artifact_port(
    *,
    validator=None,
    workflow_step=None,
    contract_key: str,
    direction: str,
    data_format: str,
    media_type: str,
    allow_upstream: bool = False,
    allowed_source_scopes: list[str] | None = None,
    resource_type: str = "",
):
    """Create one singleton file port with the fields compatibility needs."""
    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=workflow_step,
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
            allowed_source_scopes
            if allowed_source_scopes is not None
            else (
                [
                    BindingSourceScope.SUBMISSION_FILE,
                    BindingSourceScope.UPSTREAM_ARTIFACT,
                ]
                if allow_upstream
                else []
            )
        ),
        envelope_channel=(
            EnvelopeChannel.INPUT_FILES
            if direction == StepIODirection.INPUT
            else EnvelopeChannel.OUTPUT_ARTIFACTS
        ),
        role=contract_key,
        resource_type=resource_type,
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


def test_compatible_choices_query_count_does_not_scale_with_producer_steps(
    django_assert_num_queries,
) -> None:
    """A long workflow must not issue one artifact-port query per producer."""

    workflow = WorkflowFactory()
    xml_validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    consumer = WorkflowStepFactory(
        workflow=workflow,
        validator=xml_validator,
        order=100,
        name="Validate XML",
    )
    consumer_port = _artifact_port(
        validator=xml_validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )

    def add_producer(*, order: int, step_owned: bool = False) -> None:
        """Add one earlier step with one compatible declared output."""

        validator = ValidatorFactory()
        producer = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            order=order,
            name=f"Extract XML {order}",
        )
        _artifact_port(
            validator=None if step_owned else validator,
            workflow_step=producer if step_owned else None,
            contract_key="selected_xml",
            direction=StepIODirection.OUTPUT,
            data_format=SubmissionDataFormat.XML,
            media_type="application/xml",
        )

    add_producer(order=10)
    with django_assert_num_queries(2):
        one_choice = compatible_artifact_choices(
            consumer_step=consumer,
            consumer_port=consumer_port,
            workflow=workflow,
        )

    expected_producer_count = 8
    for order in range(20, (expected_producer_count + 1) * 10, 10):
        add_producer(order=order, step_owned=order == expected_producer_count * 10)
    with django_assert_num_queries(2):
        many_choices = compatible_artifact_choices(
            consumer_step=consumer,
            consumer_port=consumer_port,
            workflow=workflow,
        )

    assert len(one_choice) == 1
    assert len(many_choices) == expected_producer_count


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


def test_binding_service_rejects_a_revision_that_changed_before_locked_save() -> None:
    """A late competing edit must not slip through after form validation.

    Forms provide an early, friendly stale-edit warning, but correctness rests
    on repeating the comparison after the workflow write lock is held. This
    test protects that final check independently of any particular view.
    """
    workflow = WorkflowFactory()
    validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    port = _artifact_port(
        validator=validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )
    binding = set_artifact_input_binding(
        consumer_step=step,
        consumer_port=port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
    )
    stale_revision = binding.modified.isoformat()
    newer_modified = binding.modified + timedelta(seconds=1)
    StepInputBinding.objects.filter(pk=binding.pk).update(modified=newer_modified)

    with pytest.raises(ValidationError, match="changed in another editor"):
        set_artifact_input_binding(
            consumer_step=step,
            consumer_port=port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="replacement",
            expected_revision=stale_revision,
        )

    binding.refresh_from_db()
    assert binding.source_data_path == "primary"
    assert binding.modified == newer_modified


def test_full_step_form_rejects_a_stale_file_source_revision() -> None:
    """The full editor must provide the same stale-write warning as the modal.

    Without a hidden revision per declared file port, a long-open step form can
    overwrite a newer change made through the Inputs modal. The reusable mixin
    now carries and validates that revision for every rendered port.
    """
    workflow = WorkflowFactory()
    validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    port = _artifact_port(
        validator=validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )
    binding = set_artifact_input_binding(
        consumer_step=step,
        consumer_port=port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
    )
    opened_form = _ArtifactOnlyForm(
        workflow=workflow,
        step=step,
        validator=validator,
    )
    revision_field = "xml_document_binding_revision"
    stale_revision = opened_form.fields[revision_field].initial
    StepInputBinding.objects.filter(pk=binding.pk).update(
        modified=binding.modified + timedelta(seconds=1),
    )

    submitted_form = _ArtifactOnlyForm(
        data={
            "xml_document_source": BindingSourceScope.SUBMISSION_FILE,
            "xml_document_upstream_artifact": "",
            revision_field: stale_revision,
        },
        workflow=workflow,
        step=step,
        validator=validator,
    )

    assert not submitted_form.is_valid()
    assert "changed in another editor" in str(
        submitted_form.errors["xml_document_source"],
    )


def test_base_step_form_enables_declared_file_inputs_without_opt_in() -> None:
    """A new validator form should gain generic file controls from its base class.

    This protects the default-on design: future validators must not need to
    remember a second mixin before their declared artifact contract becomes
    authorable.
    """
    workflow = WorkflowFactory()
    validator = ValidatorFactory(validation_type="TEST_GENERIC_FILE_FORM")
    _artifact_port(
        validator=validator,
        contract_key="source_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.PDF,
        media_type="application/pdf",
        allow_upstream=True,
    )

    form = BaseStepConfigForm(workflow=workflow, validator=validator)

    assert set(form.artifact_input_ports) == {"source_document"}
    assert "source_document_source" in form.fields
    assert "source_document_upstream_artifact" in form.fields
    assert "source_document_binding_revision" in form.fields
    assert form.helper.render_hidden_fields is True


def test_fmu_form_and_inputs_modal_share_the_storage_mode_choice() -> None:
    """FMU authors must see one consistent source in both editing surfaces.

    Step-upload validators own a workflow resource, while library validators
    own a system resource. The modal delegates to the same form-class hook so
    it cannot accidentally offer a source that the full editor forbids.
    """
    workflow = WorkflowFactory()
    system_validator = ValidatorFactory(
        validation_type=ValidationType.FMU,
        is_system=True,
    )
    system_port = _artifact_port(
        validator=system_validator,
        contract_key="fmu_model",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.FMU,
        media_type="application/vnd.fmi.fmu",
        allowed_source_scopes=[
            BindingSourceScope.WORKFLOW_RESOURCE,
            BindingSourceScope.SYSTEM,
        ],
        resource_type="fmu_model",
    )
    system_step = WorkflowStepFactory(
        workflow=workflow,
        validator=system_validator,
    )
    system_binding = set_artifact_input_binding(
        consumer_step=system_step,
        consumer_port=system_port,
        source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
        source_data_path="fmu_model",
    )

    system_form = FMUValidatorStepConfigForm(
        workflow=workflow,
        step=system_step,
        validator=system_validator,
    )
    system_modal = StepInputBindingEditForm(
        io_definition=system_port,
        binding=system_binding,
    )

    expected_system_choices = [
        (BindingSourceScope.WORKFLOW_RESOURCE, "Workflow resource"),
    ]
    assert list(system_form.fields["fmu_model_source"].choices) == (
        expected_system_choices
    )
    assert list(system_modal.fields["file_source"].choices) == expected_system_choices
    assert "earlier_step_output" not in system_modal.fields
    assert system_form.fields["fmu_model_source"].widget.is_hidden

    # The form hook only needs the validator mode. Reuse the same catalog row
    # in memory so this focused form test does not have to manufacture a valid
    # approved FMU asset merely to satisfy Validator.clean().
    system_validator.is_system = False
    library_form = FMUValidatorStepConfigForm(
        workflow=workflow,
        validator=system_validator,
    )

    assert list(library_form.fields["fmu_model_source"].choices) == [
        (BindingSourceScope.SYSTEM, "System resource"),
    ]
    assert library_form.fields["fmu_model_source"].initial == BindingSourceScope.SYSTEM


def test_shacl_form_and_inputs_modal_use_the_same_composable_data_graph() -> None:
    """SHACL's RDF input should use the generic submitted-or-upstream picker."""
    workflow = WorkflowFactory()
    validator = ValidatorFactory(validation_type=ValidationType.SHACL)
    port = _artifact_port(
        validator=validator,
        contract_key="data_graph",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.TEXT,
        media_type="text/turtle",
        allow_upstream=True,
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    binding = set_artifact_input_binding(
        consumer_step=step,
        consumer_port=port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
    )

    full_form = ShaclStepConfigForm(
        workflow=workflow,
        step=step,
        validator=validator,
    )
    modal_form = StepInputBindingEditForm(
        io_definition=port,
        binding=binding,
    )

    assert list(full_form.fields["data_graph_source"].choices) == list(
        modal_form.fields["file_source"].choices
    )
    assert "data_graph_upstream_artifact" in full_form.fields
    assert "data_graph_source" in full_form.artifact_input_layout_fields()


def test_portfolio_form_handles_report_and_resource_ports_generically() -> None:
    """Portfolio Manager should not need private controls for either file input.

    Its report can compose from an earlier step, while the optional Expected
    Buildings List is always a workflow resource and therefore stays hidden.
    """
    workflow = WorkflowFactory()
    validator = ValidatorFactory(validation_type=ValidationType.PORTFOLIO_MANAGER)
    _artifact_port(
        validator=validator,
        contract_key="portfolio_manager_report",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.PORTFOLIO_MANAGER_REPORT,
        media_type="application/octet-stream",
        allow_upstream=True,
    )
    _artifact_port(
        validator=validator,
        contract_key="expected_buildings_list",
        direction=StepIODirection.INPUT,
        data_format="portfolio_manager_ebl",
        media_type="application/json",
        allowed_source_scopes=[BindingSourceScope.WORKFLOW_RESOURCE],
        resource_type="portfolio_manager_ebl",
    )

    form = PortfolioManagerStepConfigForm(
        workflow=workflow,
        validator=validator,
    )

    assert "portfolio_manager_report_upstream_artifact" in form.fields
    assert "portfolio_manager_report_source" in form.artifact_input_layout_fields()
    assert form.fields["expected_buildings_list_source"].widget.is_hidden
    assert "expected_buildings_list_binding_revision" in form.fields


def test_step_and_all_file_bindings_roll_back_when_one_update_is_invalid() -> None:
    """A failed later binding must not leave a new step or earlier binding.

    ``save_workflow_step`` is used outside the browser view by tests and other
    service-level callers. Its own transaction therefore has to protect the
    complete step, rather than relying on a particular view to wrap it.
    """
    workflow = WorkflowFactory()
    validator = ValidatorFactory(
        validation_type="TEST_ARTIFACT_TRANSACTION",
        supports_assertions=False,
    )
    first_port = _artifact_port(
        validator=validator,
        contract_key="first_file",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )
    second_port = _artifact_port(
        validator=validator,
        contract_key="second_file",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )
    form = _TwoPortStepForm(
        data={"name": "Transactional file inputs"},
        binding_updates=[
            {
                "io_definition": first_port,
                "source_scope": BindingSourceScope.SUBMISSION_FILE,
                "source_data_path": "primary",
            },
            {
                "io_definition": second_port,
                "source_scope": BindingSourceScope.SYSTEM,
                "source_data_path": "forbidden-system-file",
            },
        ],
    )
    assert form.is_valid(), form.errors

    with pytest.raises(ValueError, match="does not allow source scope"):
        save_workflow_step(workflow, validator, form)

    assert not workflow.steps.exists()
    assert not StepInputBinding.objects.filter(
        workflow_step__workflow=workflow,
    ).exists()


def test_resolver_validates_inline_submission_filename_not_opaque_identity() -> None:
    """An inline file keeps its extension contract despite having no storage URL.

    Raw JSON and XML API bodies are stored inline below the configured size
    limit. Their immutable identity is ``submission:<uuid>``, while their
    sanitized ``original_filename`` carries the file extension declared by the
    port. The resolver must not mistake that opaque evidence identity for a
    filename and reject every normal inline workflow launch.
    """
    workflow = WorkflowFactory()
    validator = ValidatorFactory(validation_type=ValidationType.JSON_SCHEMA)
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    port = _artifact_port(
        validator=validator,
        contract_key="json_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.JSON,
        media_type="application/json",
        allow_upstream=True,
    )
    port.metadata = {"accepted_extensions": ["json"]}
    port.save(update_fields=["metadata"])
    set_artifact_input_binding(
        consumer_step=step,
        consumer_port=port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        artifact_reference="primary",
    )
    run = ValidationRunFactory(workflow=workflow)
    run.submission.set_content(
        inline_text='{"valid": true}',
        filename="document.json",
    )
    run.submission.save()
    step_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=step,
    )

    resolved = resolve_file_inputs(run=run, step=step, step_run=step_run)

    item = resolved["json_document"]
    assert item.name == "document.json"
    assert item.identity.uri == f"submission:{run.submission_id}"
    assert item.content == b'{"valid": true}'


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
