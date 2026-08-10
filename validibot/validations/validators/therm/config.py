"""
Configuration for the THERM system validator.

The THERM validator is a simple/inline validator that parses THMX and THMZ
files and extracts structured step output values for assertion evaluation.
It does not run simulations -- it reads values directly from the XML.

Catalog entries define the step outputs that workflow authors can
reference when building assertion rulesets (e.g. NFRC 100 compliance).
"""

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

config = ValidatorConfig(
    slug="therm-validator",
    name="THERM Validator",
    short_description=(
        "Validate THERM thermal analysis files (THMX/THMZ) for "
        "geometry, materials, and boundary conditions."
    ),
    description=(
        "Validate LBNL THERM thermal analysis files (THMX/THMZ). "
        "Checks geometry closure, material property ranges, boundary "
        "condition completeness, and reference integrity. Extracts "
        "step output values for downstream compliance assertions."
    ),
    validation_type=ValidationType.THERM,
    validator_class=("validibot.validations.validators.therm.validator.ThermValidator"),
    version=2,
    order=30,
    has_processor=False,
    is_system=True,
    supports_assertions=True,
    compute_tier=ComputeTier.HIGH,
    supported_file_types=[SubmissionFileType.XML, SubmissionFileType.BINARY],
    supported_data_formats=[
        SubmissionDataFormat.THERM_THMX,
        SubmissionDataFormat.THERM_THMZ,
    ],
    allowed_extensions=["thmx", "thmz"],
    catalog_entries=[
        CatalogEntrySpec(
            slug="therm_model",
            label="THERM model",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Resolved THMX or THMZ model parsed and checked by this step."
            ),
            metadata={"accepted_extensions": ["thmx", "thmz"]},
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/xml",
            data_format=SubmissionDataFormat.THERM_THMX,
            accepted_data_formats=[
                SubmissionDataFormat.THERM_THMX,
                SubmissionDataFormat.THERM_THMZ,
            ],
            accepted_media_types=[
                "application/xml",
                "text/xml",
                "application/zip",
                "application/octet-stream",
            ],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="therm-model",
            min_items=1,
            max_items=1,
        ),
        # -- Counts --
        CatalogEntrySpec(
            slug="polygon_count",
            label="Polygon Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=10,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="material_count",
            label="Material Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=20,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="bc_count",
            label="BC Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=30,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # -- Geometry --
        CatalogEntrySpec(
            slug="geometry_width_mm",
            label="Geometry Width",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=40,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="geometry_height_mm",
            label="Geometry Height",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=50,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="all_polygons_closed",
            label="All Polygons Closed",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            order=60,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # -- Boundary conditions --
        CatalogEntrySpec(
            slug="interior_bc_temp",
            label="Interior BC Temperature",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=70,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="exterior_bc_temp",
            label="Exterior BC Temperature",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=80,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="interior_film_coeff",
            label="Interior Film Coefficient",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=90,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="exterior_film_coeff",
            label="Exterior Film Coefficient",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=100,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # -- U-factor tags --
        CatalogEntrySpec(
            slug="ufactor_tags_found",
            label="U-Factor Tags",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.OBJECT,
            order=110,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # -- Mesh --
        CatalogEntrySpec(
            slug="mesh_level",
            label="Mesh Level",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            order=120,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # -- Flags --
        CatalogEntrySpec(
            slug="has_cma_data",
            label="Has CMA Data",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            order=130,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="has_glazing_system",
            label="Has Glazing System",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            order=140,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # -- Version --
        CatalogEntrySpec(
            slug="therm_version",
            label="THERM Version",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            order=150,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
    ],
)
