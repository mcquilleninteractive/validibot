"""Verify PDF validator catalog, authoring, and XML composition wiring.

These tests focus on the application side of the isolated backend boundary:
fixed typed ports, safe exact-selector configuration, and compatibility-filtered
earlier-step choices in the XML validator form.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import ComputeTier
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.pdf.config import config
from validibot.workflows.forms import PdfStepConfigForm
from validibot.workflows.forms import XmlSchemaStepConfigForm
from validibot.workflows.step_configs import PdfStepConfig
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import build_pdf_config

pytestmark = pytest.mark.django_db
EXPECTED_ARTIFACT_OUTPUT_COUNT = 6


def test_pdf_step_config_rejects_a_deadline_above_the_backend_ceiling() -> None:
    """Machine-authored configs cannot promise work beyond the PDF hard limit."""
    with pytest.raises(ValidationError, match="less than or equal to 300"):
        PdfStepConfig(execution_timeout_seconds=301)


@pytest.mark.parametrize("legacy_profile", ["inventory_v1", "safe_static_package_v1"])
def test_pdf_step_config_rejects_every_legacy_profile(legacy_profile: str) -> None:
    """Machine-authored workflows cannot bypass the fixed static-text policy."""
    with pytest.raises(ValidationError, match="static_text_package_v1"):
        PdfStepConfig(profile=legacy_profile)


def _file_port(
    *,
    validator,
    contract_key,
    direction,
    data_format,
    media_type,
    allow_upstream=False,
):
    """Create a singleton file port for form-level composition tests."""
    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key=contract_key,
        native_name=contract_key,
        label=contract_key.replace("_", " ").title(),
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
        min_items=1 if direction == StepIODirection.INPUT else 0,
        max_items=1,
        is_collection=False,
    )


def test_pdf_catalog_declares_isolated_fixed_typed_ports() -> None:
    """The app catalog must align exactly with the shared/backend contract."""
    entries = {entry.slug: entry for entry in config.catalog_entries}

    assert config.validation_type == ValidationType.PDF
    assert config.compute_tier == ComputeTier.LOW
    assert config.execution_backend_slug == "pdf"
    assert "static_text_package_v1" in config.description
    assert "there is no less restrictive mode" in config.description
    assert config.output_envelope_class == "validibot_shared.pdf.PdfOutputEnvelope"
    assert entries["pdf_document"].accepted_file_types == [SubmissionFileType.PDF]
    assert ValidationType.PDF in ADVANCED_VALIDATION_TYPES
    assert set(entries) >= {
        "pdf_document",
        "pdf_inventory",
        "extracted_files_bundle",
        "xmp_metadata",
        "selected_xml",
        "selected_json",
        "selected_step_p21",
    }
    assert entries["pdf_document"].allowed_source_scopes == [
        BindingSourceScope.SUBMISSION_FILE,
        BindingSourceScope.UPSTREAM_ARTIFACT,
    ]
    assert entries["selected_xml"].data_format == SubmissionDataFormat.XML
    assert entries["selected_xml"].media_type == "application/xml"
    artifact_outputs = [
        entry
        for entry in entries.values()
        if entry.run_stage == "output" and entry.io_medium == StepIOMedium.ARTIFACT
    ]
    assert len(artifact_outputs) == EXPECTED_ARTIFACT_OUTPUT_COUNT
    assert (
        len({entry.slug for entry in artifact_outputs})
        == EXPECTED_ARTIFACT_OUTPUT_COUNT
    )
    assert (
        len({entry.role for entry in artifact_outputs})
        == EXPECTED_ARTIFACT_OUTPUT_COUNT
    )


def test_pdf_form_builds_exact_typed_selectors_and_file_source() -> None:
    """Authors can request fixed typed outputs without writing artifact paths."""
    workflow = WorkflowFactory()
    validator = ValidatorFactory(validation_type=ValidationType.PDF)
    _file_port(
        validator=validator,
        contract_key="pdf_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.PDF,
        media_type="application/pdf",
        allow_upstream=True,
    )
    form = PdfStepConfigForm(
        data={
            "name": "Inspect engineering PDF",
            "description": "Inventory and expose the handover XML.",
            "pdf_document_source": BindingSourceScope.SUBMISSION_FILE,
            "select_xml": "on",
            "selected_xml_required": "on",
            "selected_xml_filename": "asset-handover.xml",
            "selected_xml_root_qname": "{urn:example:asset}handover",
            "selected_xml_declared_media_type": "application/xml",
            "selected_xml_detected_media_type": "application/xml",
            "selected_xml_discovery_kinds": [
                "embedded_files_name_tree",
                "associated_file",
            ],
            "select_json": "on",
            "selected_json_required": "on",
            "selected_json_filename": "asset-index.json",
            "selected_json_declared_media_type": "application/json",
            "select_step_p21": "on",
            "selected_step_p21_required": "on",
            "selected_step_p21_filename": "assembly.p21",
            "selected_step_p21_declared_media_type": "model/step",
            "selected_step_p21_file_schema": ("AP242_FIXTURE\nCONFIG_CONTROL_DESIGN"),
        },
        workflow=workflow,
        validator=validator,
        proposed_order=10,
    )

    assert form.is_valid(), form.errors
    assert "profile" not in form.fields
    assert form.build_file_port_binding_updates()[0]["source_data_path"] == "primary"
    built_config = build_pdf_config(form)
    assert built_config["selected_xml"] == {
        "required": True,
        "original_filename": "asset-handover.xml",
        "declared_media_type": "application/xml",
        "af_relationship": "",
        "detected_media_type": "application/xml",
        "discovery_kinds": ["embedded_files_name_tree", "associated_file"],
        "xml_root_qname": "{urn:example:asset}handover",
    }
    assert built_config["selected_json"] == {
        "required": True,
        "original_filename": "asset-index.json",
        "declared_media_type": "application/json",
        "af_relationship": "",
        "detected_media_type": "",
        "discovery_kinds": [],
    }
    assert built_config["selected_step_p21"] == {
        "required": True,
        "original_filename": "assembly.p21",
        "declared_media_type": "model/step",
        "af_relationship": "",
        "detected_media_type": "",
        "discovery_kinds": [],
        "step_file_schema": ["AP242_FIXTURE", "CONFIG_CONTROL_DESIGN"],
    }


def test_pdf_form_round_trips_every_exact_selector_field() -> None:
    """Editing a step must preserve every selector the backend can evaluate."""
    workflow = WorkflowFactory()
    validator = ValidatorFactory(validation_type=ValidationType.PDF)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config={
            "selected_json": {
                "required": True,
                "discovery_kinds": ["file_attachment_annotation"],
                "original_filename": "index.json",
                "declared_media_type": "application/json",
                "detected_media_type": "application/json",
                "af_relationship": "Data",
            },
            "selected_step_p21": {
                "required": True,
                "original_filename": "assembly.p21",
                "step_file_schema": ["AP242_FIXTURE", "CONFIG_CONTROL_DESIGN"],
            },
        },
    )

    form = PdfStepConfigForm(
        step=step,
        workflow=workflow,
        validator=validator,
        proposed_order=step.order,
    )

    assert form.fields["selected_json_discovery_kinds"].initial == [
        "file_attachment_annotation"
    ]
    assert form.fields["selected_json_detected_media_type"].initial == (
        "application/json"
    )
    assert form.fields["selected_step_p21_file_schema"].initial == (
        "AP242_FIXTURE\nCONFIG_CONTROL_DESIGN"
    )


def test_xml_form_offers_selected_xml_but_not_pdf_inventory() -> None:
    """The downstream UI should list only earlier outputs compatible with XML."""
    workflow = WorkflowFactory()
    pdf_validator = ValidatorFactory(validation_type=ValidationType.PDF)
    xml_validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=pdf_validator,
        order=10,
        name="Inspect PDF",
    )
    selected_xml = _file_port(
        validator=pdf_validator,
        contract_key="selected_xml",
        direction=StepIODirection.OUTPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
    )
    _file_port(
        validator=pdf_validator,
        contract_key="pdf_inventory",
        direction=StepIODirection.OUTPUT,
        data_format=SubmissionDataFormat.JSON,
        media_type="application/json",
    )
    _file_port(
        validator=xml_validator,
        contract_key="xml_document",
        direction=StepIODirection.INPUT,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        allow_upstream=True,
    )
    reference = f"{producer.step_key}.{selected_xml.contract_key}"
    form = XmlSchemaStepConfigForm(
        data={
            "name": "Validate extracted XML",
            "schema_type": "XSD",
            "schema_text": ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'),
            "xml_document_source": BindingSourceScope.UPSTREAM_ARTIFACT,
            "xml_document_upstream_artifact": reference,
        },
        workflow=workflow,
        validator=xml_validator,
        proposed_order=20,
    )

    choices = dict(form.fields["xml_document_upstream_artifact"].choices)
    assert reference in choices
    assert all("pdf_inventory" not in value for value in choices)
    assert form.is_valid(), form.errors
