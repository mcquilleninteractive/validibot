"""Validator config for the Basic validator."""

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
    slug="basic-validator",
    name="Basic Validator",
    short_description=(
        "The simplest validator. Lets workflow authors map workflow signals "
        "and add assertions without a validator-specific step I/O catalog."
    ),
    description=(
        "The simplest validator. Lets workflow authors map workflow signals"
        " and add assertions without a validator-specific step I/O catalog."
    ),
    validation_type=ValidationType.BASIC,
    validator_class=("validibot.validations.validators.basic.validator.BasicValidator"),
    version=2,
    order=0,
    supports_assertions=True,
    icon="bi-journal-bookmark",
    card_image="BASIC_card_img_small.png",
    catalog_entries=[
        CatalogEntrySpec(
            slug="document",
            label="JSON or XML document",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description="Resolved JSON or XML document evaluated by this step.",
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
                SubmissionDataFormat.XML,
            ],
            accepted_media_types=[
                "application/json",
                "application/xml",
                "text/xml",
            ],
            accepted_file_types=[
                SubmissionFileType.JSON,
                SubmissionFileType.XML,
            ],
            accepted_extensions=["json", "xml"],
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
