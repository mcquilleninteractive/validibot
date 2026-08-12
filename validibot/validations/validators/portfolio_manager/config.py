"""System configuration for the Portfolio Manager convenience validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import PORTFOLIO_MANAGER_EBL_RESOURCE
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


def _output(
    slug: str,
    label: str,
    data_type: str,
    description: str,
    order: int,
) -> CatalogEntrySpec:
    """Build one internal scalar output definition."""
    return CatalogEntrySpec(
        slug=slug,
        label=label,
        entry_type=CatalogEntryType.IO_DEFINITION,
        run_stage=CatalogRunStage.OUTPUT,
        data_type=data_type,
        description=description,
        order=order,
        source_kind=StepIOSourceKind.INTERNAL,
        is_path_editable=False,
    )


_OUTPUT_SPECS = [
    (
        "submission_structure",
        "Portfolio Manager submission structure",
        CatalogValueType.STRING,
    ),
    ("file_count", "File count", CatalogValueType.NUMBER),
    ("valid_file_count", "Valid file count", CatalogValueType.NUMBER),
    ("invalid_file_count", "Invalid file count", CatalogValueType.NUMBER),
    ("property_count", "Property count", CatalogValueType.NUMBER),
    ("reporting_cycle_count", "Reporting cycle count", CatalogValueType.NUMBER),
    ("reporting_cycles_match", "Reporting cycles match", CatalogValueType.BOOLEAN),
    (
        "complete_reporting_period_property_count",
        "Properties with a complete reporting period",
        CatalogValueType.NUMBER,
    ),
    (
        "fresh_reporting_period_property_count",
        "Properties with a current reporting period",
        CatalogValueType.NUMBER,
    ),
    ("expected_building_count", "Expected building count", CatalogValueType.NUMBER),
    (
        "matched_expected_building_count",
        "Matched expected building count",
        CatalogValueType.NUMBER,
    ),
    (
        "missing_expected_building_count",
        "Missing expected building count",
        CatalogValueType.NUMBER,
    ),
    (
        "unexpected_submitted_building_count",
        "Unexpected submitted building count",
        CatalogValueType.NUMBER,
    ),
    (
        "duplicate_submitted_property_count",
        "Duplicate submitted property count",
        CatalogValueType.NUMBER,
    ),
    (
        "parent_child_overlap_count",
        "Parent/child overlap count",
        CatalogValueType.NUMBER,
    ),
    (
        "target_covered_property_count",
        "Properties with a resolved EUIt",
        CatalogValueType.NUMBER,
    ),
    (
        "target_uncovered_property_count",
        "Properties without a resolved EUIt",
        CatalogValueType.NUMBER,
    ),
    (
        "target_comparable_property_count",
        "Properties with EUIt and measured WNEUI",
        CatalogValueType.NUMBER,
    ),
    (
        "target_met_property_count",
        "Properties meeting EUIt",
        CatalogValueType.NUMBER,
    ),
    (
        "target_above_property_count",
        "Properties above EUIt",
        CatalogValueType.NUMBER,
    ),
    (
        "target_near_property_count",
        "Properties near EUIt",
        CatalogValueType.NUMBER,
    ),
    (
        "benchmark_ready_property_count",
        "Benchmark-ready properties",
        CatalogValueType.NUMBER,
    ),
    (
        "form_c_ready_property_count",
        "Form C-ready properties",
        CatalogValueType.NUMBER,
    ),
    (
        "aggregate_metrics_available",
        "Aggregate metrics available",
        CatalogValueType.BOOLEAN,
    ),
    (
        "total_gross_floor_area_ft2",
        "Total gross floor area (ft²)",
        CatalogValueType.NUMBER,
    ),
    (
        "weighted_weather_normalized_site_eui_kbtu_ft2_yr",
        "GFA-weighted Weather Normalized Site EUI",
        CatalogValueType.NUMBER,
    ),
    (
        "energy_star_score_property_count",
        "Properties with ENERGY STAR score",
        CatalogValueType.NUMBER,
    ),
    (
        "weighted_energy_star_score",
        "GFA-weighted ENERGY STAR score",
        CatalogValueType.NUMBER,
    ),
    (
        "estimated_excess_energy_kbtu",
        "Estimated excess energy (kBtu)",
        CatalogValueType.NUMBER,
    ),
    (
        "target_coverage_percent",
        "EUIt target coverage (%)",
        CatalogValueType.NUMBER,
    ),
    (
        "target_compliance_percent",
        "EUIt target compliance (%)",
        CatalogValueType.NUMBER,
    ),
    (
        "floor_area_target_compliance_percent",
        "Floor area meeting EUIt (%)",
        CatalogValueType.NUMBER,
    ),
    ("property_id", "Portfolio Manager Property ID", CatalogValueType.STRING),
    (
        "parent_property_id",
        "Parent Portfolio Manager Property ID",
        CatalogValueType.STRING,
    ),
    (
        "washington_standard_id",
        "Washington Clean Buildings Standard ID",
        CatalogValueType.STRING,
    ),
    ("reporting_period_start", "Reporting period start", CatalogValueType.STRING),
    ("reporting_period_end", "Reporting period end", CatalogValueType.STRING),
    (
        "reporting_period_complete",
        "Reporting period is complete",
        CatalogValueType.BOOLEAN,
    ),
    (
        "reporting_period_fresh",
        "Reporting period is current",
        CatalogValueType.BOOLEAN,
    ),
    ("gross_floor_area_ft2", "Gross floor area (ft²)", CatalogValueType.NUMBER),
    (
        "site_eui_kbtu_ft2_yr",
        "Site EUI (kBtu/ft²/year)",
        CatalogValueType.NUMBER,
    ),
    (
        "weather_normalized_site_eui_kbtu_ft2_yr",
        "Weather Normalized Site EUI (kBtu/ft²/year)",
        CatalogValueType.NUMBER,
    ),
    (
        "source_eui_kbtu_ft2_yr",
        "Source EUI (kBtu/ft²/year)",
        CatalogValueType.NUMBER,
    ),
    (
        "national_median_site_eui_kbtu_ft2_yr",
        "National median Site EUI",
        CatalogValueType.NUMBER,
    ),
    ("energy_star_score", "ENERGY STAR score", CatalogValueType.NUMBER),
    ("heating_degree_days", "Heating degree days", CatalogValueType.NUMBER),
    ("cooling_degree_days", "Cooling degree days", CatalogValueType.NUMBER),
    ("weather_station_id", "Weather station ID", CatalogValueType.STRING),
    ("weather_station_name", "Weather station name", CatalogValueType.STRING),
    (
        "resolved_euit_kbtu_ft2_yr",
        "Resolved EUIt (kBtu/ft²/year)",
        CatalogValueType.NUMBER,
    ),
    ("resolved_euit_source", "Resolved EUIt source", CatalogValueType.STRING),
    (
        "euit_margin_kbtu_ft2_yr",
        "EUIt margin (kBtu/ft²/year)",
        CatalogValueType.NUMBER,
    ),
    ("euit_ratio", "WNEUI-to-EUIt ratio", CatalogValueType.NUMBER),
    (
        "euit_percent_difference",
        "WNEUI difference from EUIt (%)",
        CatalogValueType.NUMBER,
    ),
    ("meets_euit", "Meets EUIt", CatalogValueType.BOOLEAN),
    ("near_euit", "Near EUIt", CatalogValueType.BOOLEAN),
    ("benchmark_ready", "Benchmark ready", CatalogValueType.BOOLEAN),
    ("form_c_ready", "Form C ready", CatalogValueType.BOOLEAN),
]

config = ValidatorConfig(
    slug="portfolio-manager-validator",
    name="Building Benchmark Report Validator",
    short_description=(
        "Validate exports from ENERGY STAR® Portfolio Manager®, EUIt comparisons, "
        "and multi-building ZIP collections."
    ),
    description=(
        "Recognizes supported XLS, XLSX, and XML property report exports. "
        "Single-report mode exposes measured EUI facts. ZIP collection "
        "mode adds reporting-cycle, duplicate, roster, target-coverage, and "
        "portfolio aggregate facts. Authors explicitly configure reporting, "
        "benchmark/Form C readiness, EUIt, and data-quality requirements for "
        "their program."
    ),
    validation_type=ValidationType.PORTFOLIO_MANAGER,
    execution_backend_slug="portfolio_manager",
    execution_runtime_contract="validibot-execution-v1",
    validator_class=(
        "validibot.validations.validators.portfolio_manager.validator."
        "PortfolioManagerValidator"
    ),
    output_envelope_class=(
        "validibot_shared.portfolio_manager.envelopes.PortfolioManagerOutputEnvelope"
    ),
    image_name="validibot-validator-backend-portfolio-manager",
    version=3,
    order=6,
    has_processor=True,
    processor_name="Building Benchmark Report Validation",
    is_system=True,
    supports_assertions=True,
    compute_tier=ComputeTier.LOW,
    icon="bi-buildings",
    card_image="default_card_img_small.png",
    catalog_entries=[
        CatalogEntrySpec(
            slug="portfolio_manager_report",
            label="ENERGY STAR® Portfolio Manager® report",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description=("Submitted XLS, XLSX, XML, or ZIP building benchmark report."),
            accepted_file_types=[SubmissionFileType.BINARY, SubmissionFileType.XML],
            accepted_extensions=["xls", "xlsx", "xml", "zip"],
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/octet-stream",
            data_format=SubmissionDataFormat.PORTFOLIO_MANAGER_REPORT,
            accepted_data_formats=[
                SubmissionDataFormat.PORTFOLIO_MANAGER_REPORT,
            ],
            accepted_media_types=[
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/xml",
                "text/xml",
                "application/zip",
            ],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="portfolio-manager-report",
            min_items=1,
            max_items=1,
        ),
        CatalogEntrySpec(
            slug="default_euit_kbtu_ft2_yr",
            label="Default EUIt (kBtu/ft²/year)",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.NUMBER,
            description=(
                "Optional fixed EUIt from the typed step configuration. An EBL "
                "value for a matched building overrides it."
            ),
            is_required=False,
            on_missing="null",
            order=2,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            binding_config={"source": "constant"},
            allowed_source_scopes=[BindingSourceScope.CONSTANT],
        ),
        CatalogEntrySpec(
            slug="expected_buildings_list",
            label="Expected Buildings List",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Optional versioned JSON roster used in ZIP mode for identity "
                "reconciliation and per-building EUIt overrides."
            ),
            accepted_extensions=["json"],
            is_required=False,
            on_missing="null",
            order=3,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/json",
            data_format=PORTFOLIO_MANAGER_EBL_RESOURCE,
            accepted_data_formats=[PORTFOLIO_MANAGER_EBL_RESOURCE],
            accepted_media_types=["application/json"],
            allowed_source_scopes=[BindingSourceScope.WORKFLOW_RESOURCE],
            default_source_strategy=DefaultSourceStrategy.WORKFLOW_RESOURCE_DEFAULT,
            envelope_channel=EnvelopeChannel.RESOURCE_FILES,
            resource_type=PORTFOLIO_MANAGER_EBL_RESOURCE,
            role="expected-buildings-list",
            min_items=0,
            max_items=1,
        ),
        CatalogEntrySpec(
            slug="property_results",
            label="Building benchmark property results",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Carrier-neutral per-property JSON facts and roster reconciliation."
            ),
            binding_config={
                "source": "output_artifact",
                "role": "portfolio-manager-property-results",
            },
            is_required=False,
            on_missing="null",
            order=4,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/json",
            data_format="portfolio_manager_property_results_v1",
            accepted_data_formats=["portfolio_manager_property_results_v1"],
            accepted_media_types=["application/json"],
            allowed_source_scopes=[],
            default_source_strategy=DefaultSourceStrategy.NONE,
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="portfolio-manager-property-results",
            min_items=0,
            max_items=1,
        ),
        *[
            _output(
                slug,
                label,
                data_type,
                "Canonical building benchmark report validation output.",
                10 + index,
            )
            for index, (slug, label, data_type) in enumerate(_OUTPUT_SPECS)
        ],
    ],
)
