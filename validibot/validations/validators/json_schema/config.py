"""Validator config for the JSON Schema validator."""

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
    slug="json-schema-validator",
    name="JSON Schema Validator",
    short_description=(
        "Validate JSON payloads against a JSON schema provided by the workflow author."
    ),
    # NOTE: ValidatorConfig.description is strict-typed `str` (pydantic), so
    # do NOT wrap it in `gettext_lazy`. Other validator configs follow the
    # same convention — wrap-for-translation here would crash on app boot.
    description="Validate JSON data against a JSON Schema definition.",
    validation_type=ValidationType.JSON_SCHEMA,
    validator_class=(
        "validibot.validations.validators.json_schema.validator.JsonSchemaValidator"
    ),
    # v2 declares the JSON document as a generic singleton file input so the
    # validator can consume either the primary submission or an exact output
    # selected by PDFValidator or another earlier step.
    version=2,
    order=1,
    supported_file_types=[SubmissionFileType.JSON],
    supported_data_formats=[SubmissionDataFormat.JSON],
    allowed_extensions=["json"],
    supports_assertions=True,
    icon="bi-filetype-json",
    card_image="JSON_SCHEMA_card_img_small.png",
    catalog_entries=[
        CatalogEntrySpec(
            slug="json_document",
            label="JSON document",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description="JSON document validated against this step's schema.",
            metadata={"accepted_extensions": ["json"]},
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/json",
            data_format=SubmissionDataFormat.JSON,
            accepted_data_formats=[SubmissionDataFormat.JSON],
            accepted_media_types=["application/json"],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="json-document",
            min_items=1,
            max_items=1,
        )
    ],
)
