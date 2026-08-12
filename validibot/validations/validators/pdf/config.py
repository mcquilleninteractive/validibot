"""System catalog contract for the generic PDF package validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ComputeTier
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig


def _output_port(
    *,
    slug: str,
    label: str,
    description: str,
    data_format: str,
    media_type: str,
    artifact_kind: str = ArtifactKind.FILE,
    accepted_extensions: list[str],
    required: bool = False,
    order: int,
) -> CatalogEntrySpec:
    """Build one fixed singleton output used for safe workflow composition."""
    return CatalogEntrySpec(
        slug=slug,
        label=label,
        entry_type=CatalogEntryType.IO_DEFINITION,
        run_stage=CatalogRunStage.OUTPUT,
        data_type=CatalogValueType.ARTIFACT_REF,
        description=description,
        binding_config={"source": "output_artifact", "role": slug},
        accepted_extensions=accepted_extensions,
        is_required=required,
        on_missing="error" if required else "null",
        order=order,
        source_kind=StepIOSourceKind.INTERNAL,
        is_path_editable=False,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=artifact_kind,
        media_type=media_type,
        data_format=data_format,
        accepted_data_formats=[data_format],
        accepted_media_types=[media_type],
        allowed_source_scopes=[],
        default_source_strategy=DefaultSourceStrategy.NONE,
        envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
        role=slug,
        min_items=1 if required else 0,
        max_items=1,
    )


config = ValidatorConfig(
    slug="pdf-validator",
    name="PDF Package Validator",
    short_description=(
        "Apply the fixed static_text_package_v1 policy to unencrypted PDFs "
        "containing document XMP and static XML, JSON, or STEP text files."
    ),
    description=(
        "This validator always enforces the static_text_package_v1 security "
        "policy; there is no less restrictive mode. It accepts an unencrypted "
        "PDF only when document-level XMP uses XML and every embedded or "
        "attached file is bounded static text detected as XML, JSON, or STEP "
        "Part 21. Files must be reached through the PDF EmbeddedFiles name "
        "tree, catalog/page/annotation Associated Files, or a FileAttachment "
        "annotation. The validator rejects scripts, forms, launch and remote "
        "actions, multimedia, 3D, collections, object-level metadata, unsafe "
        "or ambiguous filenames, unsupported stream filters, other attachment "
        "routes, and every other member format. Ordinary URI hyperlinks and "
        "digital signatures are outside scope: they are neither followed nor "
        "validated. On any policy error, only the inventory is published; no "
        "XMP, selected file, or extraction bundle is released. Carrier checks "
        "do not establish domain conformance, and passing is not a malware-free "
        "or safe-to-open guarantee."
    ),
    validation_type=ValidationType.PDF,
    execution_backend_slug="pdf",
    execution_runtime_contract="validibot-execution-v1",
    validator_class="validibot.validations.validators.pdf.validator.PdfValidator",
    output_envelope_class="validibot_shared.pdf.PdfOutputEnvelope",
    image_name="validibot-validator-backend-pdf",
    has_processor=True,
    processor_name="PDF Package Inspection",
    version=2,
    order=8,
    supports_assertions=True,
    compute_tier=ComputeTier.LOW,
    icon="bi-file-earmark-pdf",
    card_image="default_card_img_small.png",
    catalog_entries=[
        CatalogEntrySpec(
            slug="pdf_document",
            label="PDF document",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description="Immutable PDF document inspected by this step.",
            accepted_file_types=[SubmissionFileType.PDF],
            accepted_extensions=["pdf"],
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/pdf",
            data_format=SubmissionDataFormat.PDF,
            accepted_data_formats=[SubmissionDataFormat.PDF],
            accepted_media_types=["application/pdf"],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="pdf-document",
            min_items=1,
            max_items=1,
        ),
        _output_port(
            slug="pdf_inventory",
            label="PDF package inventory",
            description="Canonical validibot.pdf_inventory.v2 JSON report.",
            data_format=SubmissionDataFormat.JSON,
            media_type="application/json",
            artifact_kind=ArtifactKind.REPORT,
            accepted_extensions=["json"],
            required=True,
            order=10,
        ),
        _output_port(
            slug="extracted_files_bundle",
            label="Extracted files bundle",
            description=(
                "Deterministic ZIP evidence bundle containing only policy-eligible "
                "XML, JSON, and STEP Part 21 members; omitted on any policy error."
            ),
            data_format=SubmissionDataFormat.ZIP,
            media_type="application/zip",
            artifact_kind=ArtifactKind.ARCHIVE,
            accepted_extensions=["zip"],
            order=20,
        ),
        _output_port(
            slug="xmp_metadata",
            label="Document XMP metadata",
            description="Original safely readable document-level XMP packet.",
            data_format=SubmissionDataFormat.XML,
            media_type="application/xml",
            accepted_extensions=["xml"],
            order=30,
        ),
        _output_port(
            slug="selected_xml",
            label="Selected XML payload",
            description="Exactly one selected and carrier-preflighted XML member.",
            data_format=SubmissionDataFormat.XML,
            media_type="application/xml",
            accepted_extensions=["xml"],
            order=40,
        ),
        _output_port(
            slug="selected_json",
            label="Selected JSON payload",
            description="Exactly one selected and carrier-preflighted JSON member.",
            data_format=SubmissionDataFormat.JSON,
            media_type="application/json",
            accepted_extensions=["json"],
            order=50,
        ),
        _output_port(
            slug="selected_step_p21",
            label="Selected STEP Part 21 payload",
            description="Exactly one selected STEP physical file carrier.",
            data_format=SubmissionDataFormat.STEP_P21,
            media_type="model/step",
            accepted_extensions=["p21", "step", "stp"],
            order=60,
        ),
        CatalogEntrySpec(
            slug="passed",
            label="Passed",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether PDF inspection and configured selections passed.",
            order=100,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="member_count",
            label="Package member count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of deduplicated embedded member byte sequences.",
            order=110,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="finding_summary",
            label="Finding summary",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.OBJECT,
            description="Finding counts keyed by severity.",
            order=120,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
    ],
)
