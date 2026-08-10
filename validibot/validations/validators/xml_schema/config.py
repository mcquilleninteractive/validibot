"""Validator config for the XML Schema validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="xml-validator",
    name="XML Validator",
    short_description=(
        "Validate XML submissions against a XSD, DTD, or RelaxNG "
        "schema provided by the workflow author."
    ),
    description="Validate XML data against XSD, RelaxNG, or DTD schemas.",
    validation_type=ValidationType.XML_SCHEMA,
    validator_class=(
        "validibot.validations.validators.xml_schema.validator.XmlSchemaValidator"
    ),
    # v2 declares the XML document as a generic singleton file input. This
    # allows the same validator to consume the primary submission or exact XML
    # bytes produced by PDFValidator and other earlier steps.
    version=2,
    order=2,
    supported_file_types=[SubmissionFileType.XML],
    supported_data_formats=[SubmissionDataFormat.XML],
    allowed_extensions=["xml", "xsd", "rng", "dtd"],
    supports_assertions=True,
    icon="bi-filetype-xml",
    card_image="XML_SCHEMA_card_img_small.png",
    catalog_entries=[
        CatalogEntrySpec(
            slug="xml_document",
            label="XML document",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description="XML document validated against this step's schema.",
            metadata={"accepted_extensions": ["xml"]},
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/xml",
            data_format=SubmissionDataFormat.XML,
            accepted_data_formats=[SubmissionDataFormat.XML],
            accepted_media_types=["application/xml", "text/xml"],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="xml-document",
            min_items=1,
            max_items=1,
        )
    ],
)
