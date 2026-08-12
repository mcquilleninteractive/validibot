"""Validator config for the Custom validator."""

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
    slug="custom-validator",
    name="Custom Validator",
    short_description=("User-defined validator backed by your own container image."),
    description="User-defined validator with custom container logic.",
    validation_type=ValidationType.CUSTOM_VALIDATOR,
    validator_class=(
        "validibot.validations.validators.custom.validator.CustomValidator"
    ),
    output_envelope_class=(
        "validibot_shared.validations.envelopes.ValidationOutputEnvelope"
    ),
    version=2,
    order=99,
    is_system=False,
    supports_assertions=True,
    catalog_entries=[
        CatalogEntrySpec(
            slug="document",
            label="Input document",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description="Resolved document supplied to the custom validator.",
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/json",
            data_format=SubmissionDataFormat.JSON,
            accepted_data_formats=[
                SubmissionDataFormat.JSON,
                SubmissionDataFormat.YAML,
            ],
            accepted_media_types=[
                "application/json",
                "application/yaml",
                "text/yaml",
            ],
            accepted_file_types=[
                SubmissionFileType.JSON,
                SubmissionFileType.YAML,
            ],
            accepted_extensions=["json", "yaml", "yml"],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="document",
            min_items=1,
            max_items=1,
        )
    ],
)
