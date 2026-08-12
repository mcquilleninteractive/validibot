"""Validator config for the Tabular Validator.

Declaring this ``config`` is what makes the validator discoverable: at startup
``discover_configs()`` imports every ``<validator>/config.py`` and registers the
``config`` instance. Until this module existed, the ``tabular`` package was
skipped by discovery (no ``config.py``), so its modules were importable for
tests without the validator being surfaced as a choice.
"""

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
    slug="tabular-validator",
    name="Tabular Validator",
    short_description=(
        "Validate tabular data (CSV in V1) against a column schema and per-row rules."
    ),
    # NOTE: ValidatorConfig.description is strict-typed `str` (pydantic), so do
    # NOT wrap it in `gettext_lazy` — that would crash on app boot. Other
    # validator configs follow the same convention.
    description=(
        "Validate a table of typed rows: required columns, column types, "
        "numeric ranges, string length, regex, enum membership, and "
        "single/composite uniqueness, plus CEL row assertions for "
        "cross-field and conditional logic."
    ),
    validation_type=ValidationType.TABULAR,
    validator_class=(
        "validibot.validations.validators.tabular.validator.TabularValidator"
    ),
    version=2,
    order=10,
    supports_assertions=True,
    icon="bi-table",
    # Tabular owns its workflow import/export body so a re-imported ruleset's
    # row assertions are re-checked against the declared Table Schema columns.
    step_serializer_class=(
        "validibot.validations.validators.tabular.serializer.TabularStepSerializer"
    ),
    catalog_entries=[
        CatalogEntrySpec(
            slug="table_document",
            label="CSV or TSV table",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description="Resolved UTF-8 CSV or TSV table validated by this step.",
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="text/csv",
            data_format=SubmissionDataFormat.CSV,
            accepted_data_formats=[SubmissionDataFormat.CSV],
            accepted_media_types=["text/csv", "text/tab-separated-values"],
            accepted_file_types=[SubmissionFileType.TEXT],
            accepted_extensions=["csv", "tsv"],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="table-document",
            min_items=1,
            max_items=1,
        )
    ],
)
