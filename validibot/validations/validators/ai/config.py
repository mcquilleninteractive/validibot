"""Validator config for the AI Assisted validator."""

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
    slug="ai-assisted-validator",
    name="AI Assisted Validator",
    short_description=("Use AI to validate submission content against your criteria."),
    description="AI-powered validation using language models.",
    validation_type=ValidationType.AI_ASSIST,
    validator_class=("validibot.validations.validators.ai.validator.AIValidator"),
    version=2,
    order=5,
    supports_assertions=True,
    icon="bi-robot",
    card_image="AI_ASSIST_card_img_small.png",
    catalog_entries=[
        CatalogEntrySpec(
            slug="document",
            label="Source document",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description="Resolved JSON or text document supplied for analysis.",
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="text/plain",
            data_format=SubmissionDataFormat.TEXT,
            accepted_data_formats=[
                SubmissionDataFormat.JSON,
                SubmissionDataFormat.TEXT,
            ],
            accepted_media_types=["application/json", "text/plain"],
            accepted_file_types=[
                SubmissionFileType.JSON,
                SubmissionFileType.TEXT,
            ],
            accepted_extensions=["json", "txt"],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="source-document",
            min_items=1,
            max_items=1,
        )
    ],
)
