"""
Configuration for the EnergyPlus system validator.

The catalog entry binding_config["key"] values must match field names in
validibot_shared.energyplus.models.EnergyPlusSimulationMetrics, which is what
the container validator populates after running the simulation.
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
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig


def _energyplus_output_artifact(
    *,
    slug: str,
    label: str,
    role: str,
    artifact_kind: str,
    media_type: str,
    data_format: str,
    accepted_extensions: list[str],
    accepted_media_types: list[str] | None = None,
    order: int,
) -> CatalogEntrySpec:
    """Declare an EnergyPlus output file emitted by the backend."""

    return CatalogEntrySpec(
        entry_type=CatalogEntryType.IO_DEFINITION,
        run_stage=CatalogRunStage.OUTPUT,
        slug=slug,
        label=label,
        data_type=CatalogValueType.ARTIFACT_REF,
        description=f"EnergyPlus output artifact '{label}' uploaded by the backend.",
        binding_config={"source": "output_artifact", "role": role},
        accepted_extensions=accepted_extensions,
        is_required=False,
        on_missing="null",
        order=order,
        source_kind=StepIOSourceKind.INTERNAL,
        is_path_editable=False,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=artifact_kind,
        media_type=media_type,
        data_format=data_format,
        accepted_data_formats=[data_format],
        accepted_media_types=accepted_media_types or [media_type],
        allowed_source_scopes=[],
        default_source_strategy=DefaultSourceStrategy.NONE,
        envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
        role=role,
        min_items=0,
        max_items=1,
    )


def _energyplus_output_value(
    *,
    slug: str,
    label: str,
    data_type: str,
    description: str,
    order: int,
    category: str,
    units: str | None = None,
    on_missing: str = "null",
) -> CatalogEntrySpec:
    """Declare a backend-produced review/evidence value."""

    metadata = {"category": category}
    if units:
        metadata["units"] = units
    return CatalogEntrySpec(
        entry_type=CatalogEntryType.IO_DEFINITION,
        run_stage=CatalogRunStage.OUTPUT,
        slug=slug,
        label=label,
        data_type=data_type,
        description=description,
        binding_config={"source": "output", "key": slug},
        metadata=metadata,
        is_required=on_missing == "error",
        on_missing=on_missing,
        order=order,
        source_kind=StepIOSourceKind.INTERNAL,
        is_path_editable=False,
    )


config = ValidatorConfig(
    slug="energyplus-idf-validator",
    name="EnergyPlus\u2122 Validator",
    short_description="Validate EnergyPlus IDF files and outputs.",
    description="Validate EnergyPlus\u2122 IDF models and run simulations.",
    validation_type=ValidationType.ENERGYPLUS,
    execution_backend_slug="energyplus",
    execution_runtime_contract="validibot-execution-v1",
    validator_class=(
        "validibot.validations.validators.energyplus.validator.EnergyPlusValidator"
    ),
    output_envelope_class=(
        "validibot_shared.energyplus.envelopes.EnergyPlusOutputEnvelope"
    ),
    image_name="validibot-validator-backend-energyplus",
    version=4,
    order=10,
    has_processor=True,
    processor_name="EnergyPlus\u2122 Simulation",
    is_system=True,
    supports_assertions=True,
    compute_tier=ComputeTier.HIGH,
    icon="bi-lightning-charge-fill",
    card_image="ENERGYPLUS_card_img_small.png",
    catalog_entries=[
        # ==================================================================
        # ARTIFACT PORTS - files required by the EnergyPlus runtime.
        #
        # These entries declare the file contract that already exists at the
        # envelope boundary: the submitted model rides in input_files with role
        # "primary-model"; the selected EPW rides in resource_files with type
        # "energyplus_weather". Keeping these as StepIODefinition rows lets the
        # workflow engine reason about file dependencies without hard-coding
        # EnergyPlus-specific config keys.
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="primary_model",
            label="Primary Model",
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Resolved EnergyPlus model file passed to the backend as "
                "the primary input file. Accepts IDF and epJSON models."
            ),
            binding_config={
                "envelope_channel": EnvelopeChannel.INPUT_FILES,
                "role": "primary-model",
            },
            accepted_file_types=[SubmissionFileType.TEXT, SubmissionFileType.JSON],
            accepted_extensions=["idf", "epjson", "json"],
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/vnd.energyplus.idf",
            data_format=SubmissionDataFormat.ENERGYPLUS_IDF,
            accepted_data_formats=[
                SubmissionDataFormat.ENERGYPLUS_IDF,
                SubmissionDataFormat.ENERGYPLUS_EPJSON,
            ],
            accepted_media_types=[
                "application/vnd.energyplus.idf",
                "application/vnd.energyplus.epjson",
            ],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
                BindingSourceScope.SIGNAL,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="primary-model",
            min_items=1,
            max_items=1,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="weather_file",
            label="Weather File",
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "EnergyPlus EPW weather file selected from validator "
                "resource files and passed to the backend as a resource file."
            ),
            binding_config={
                "envelope_channel": EnvelopeChannel.RESOURCE_FILES,
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
            },
            accepted_extensions=["epw"],
            is_required=False,
            on_missing="null",
            order=2,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/vnd.energyplus.epw",
            data_format=ResourceFileType.ENERGYPLUS_WEATHER,
            accepted_data_formats=[ResourceFileType.ENERGYPLUS_WEATHER],
            accepted_media_types=["application/vnd.energyplus.epw"],
            allowed_source_scopes=[
                BindingSourceScope.WORKFLOW_RESOURCE,
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=(
                DefaultSourceStrategy.SUBMITTED_FILE_THEN_DEFAULT_RESOURCE
            ),
            envelope_channel=EnvelopeChannel.RESOURCE_FILES,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            role="weather",
            min_items=0,
            max_items=1,
        ),
        # ==================================================================
        # OUTPUT ARTIFACT PORTS - files uploaded by the EnergyPlus backend.
        #
        # The backend labels artifacts by role ("simulation-db",
        # "timeseries-csv", etc.). These ports give workflow authors stable,
        # file-specific keys under steps.<step>.artifact.* while preserving the
        # backend-facing role mapping in the port contract.
        # ==================================================================
        _energyplus_output_artifact(
            slug="eplusout_sql",
            label="EnergyPlus SQL Output",
            role="simulation-db",
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/x-sqlite3",
            data_format="sqlite",
            accepted_extensions=["sql"],
            accepted_media_types=["application/x-sqlite3", "application/vnd.sqlite3"],
            order=90,
        ),
        _energyplus_output_artifact(
            slug="eplusout_csv",
            label="EnergyPlus CSV Output",
            role="timeseries-csv",
            artifact_kind=ArtifactKind.DATASET,
            media_type="text/csv",
            data_format="csv",
            accepted_extensions=["csv"],
            order=91,
        ),
        _energyplus_output_artifact(
            slug="eplusout_err",
            label="EnergyPlus Error Log",
            role="err-log",
            artifact_kind=ArtifactKind.LOG,
            media_type="text/plain",
            data_format="text",
            accepted_extensions=["err"],
            order=92,
        ),
        _energyplus_output_artifact(
            slug="eplusout_eso",
            label="EnergyPlus ESO Output",
            role="eso",
            artifact_kind=ArtifactKind.DATASET,
            media_type="text/plain",
            data_format="energyplus_eso",
            accepted_extensions=["eso"],
            order=93,
        ),
        # ==================================================================
        # STEP INPUTS \u2014 parser-extracted facts from the (resolved) IDF.
        #
        # Populated by EnergyPlusValidator.extract_input_values() running
        # after preprocess_submission() \u2014 works for both direct-IDF and
        # template-mode submissions because preprocessing has resolved any
        # template variables into a concrete IDF by then.
        #
        # Authors reference these values through ``i.<contract_key>`` in
        # input-stage CEL assertions.
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="idf_version",
            label="IDF Version",
            data_type=CatalogValueType.STRING,
            description=(
                "EnergyPlus version declared by the IDF Version object "
                "(e.g. '25.1'). Every valid IDF has a Version object; "
                "absence indicates the file is malformed."
            ),
            binding_config={"source": "parser", "key": "idf_version"},
            metadata={},
            is_required=True,
            on_missing="error",  # every valid IDF has a Version object
            order=10,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="zone_count",
            label="Zone Count",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Count of Zone objects in the IDF. Must be at least 1 "
                "for a meaningful model. Useful for 'must have \u2265N zones' "
                "assertions before paying for simulation."
            ),
            binding_config={"source": "parser", "key": "zone_count"},
            metadata={"units": "count"},
            is_required=True,
            on_missing="error",  # absence means parsing failed
            order=11,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="north_axis_deg",
            label="North Axis (deg)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Building rotation in degrees, from the Building object "
                "North Axis field. Defaults to 0.0 per EnergyPlus IDD. "
                "Useful for orientation-sensitivity assertions."
            ),
            binding_config={"source": "parser", "key": "north_axis_deg"},
            metadata={"units": "deg"},
            is_required=False,
            on_missing="null",  # fall back to EnergyPlus default 0.0
            order=12,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ── Building characteristics ──────────────────────────────────────
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="building_name",
            label="Building Name",
            data_type=CatalogValueType.STRING,
            description=(
                "Name field on the IDF Building object. Useful for "
                "assertions like 'must include the project code in the "
                "model name' or for sanity-checking that the right model "
                "is being run."
            ),
            binding_config={"source": "parser", "key": "building_name"},
            metadata={},
            is_required=False,
            on_missing="null",
            order=13,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="terrain",
            label="Terrain",
            data_type=CatalogValueType.STRING,
            description=(
                "Building object Terrain field. One of Country, "
                "Suburbs (default), City, Ocean, Urban. Drives the "
                "wind-speed profile EnergyPlus applies, so a sanity "
                "check that ``i.terrain == 'Urban'`` for an urban "
                "site is a useful preflight assertion."
            ),
            binding_config={"source": "parser", "key": "terrain"},
            metadata={},
            is_required=False,
            on_missing="null",  # IDD default "Suburbs" injected by parser
            order=14,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="solar_distribution",
            label="Solar Distribution",
            data_type=CatalogValueType.STRING,
            description=(
                "Building object Solar Distribution field. Common "
                "values: MinimalShadowing, FullExterior (default), "
                "FullInteriorAndExterior, FullExteriorWithReflections, "
                "FullInteriorAndExteriorWithReflections. Important "
                "for energy-balance accuracy in shoebox vs. detailed "
                "models."
            ),
            binding_config={"source": "parser", "key": "solar_distribution"},
            metadata={},
            is_required=False,
            on_missing="null",  # IDD default "FullExterior" injected by parser
            order=15,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ── Simulation configuration ──────────────────────────────────────
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="timestep_per_hour",
            label="Timesteps per Hour",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Number of simulation timesteps per hour, from the "
                "IDF Timestep object. Higher values produce more "
                "accurate HVAC dynamics at simulation-time cost. "
                "Defaults to 4 per the IDD when the Timestep object "
                "is absent."
            ),
            binding_config={"source": "parser", "key": "timestep_per_hour"},
            metadata={"units": "count/hour"},
            is_required=False,
            on_missing="null",
            order=16,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="run_period_count",
            label="Run Period Count",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Number of RunPeriod objects in the IDF. A model "
                "without any RunPeriod won't actually simulate a "
                "time range — assertions like "
                "``i.run_period_count >= 1`` catch this before "
                "dispatch."
            ),
            binding_config={"source": "parser", "key": "run_period_count"},
            metadata={"units": "count"},
            is_required=False,
            on_missing="null",
            order=17,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ── Geometry counts ───────────────────────────────────────────────
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="surface_count",
            label="Surface Count",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Count of BuildingSurface:Detailed objects in the "
                "IDF. Useful for catching empty/minimal geometry "
                "before paying for simulation."
            ),
            binding_config={"source": "parser", "key": "surface_count"},
            metadata={"units": "count"},
            is_required=False,
            on_missing="null",
            order=18,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="window_count",
            label="Window Count",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Count of Window + FenestrationSurface:Detailed "
                "objects in the IDF. Both ``Window,`` and "
                "``FenestrationSurface:Detailed`` declarations contribute. "
                "Useful for catching daylight/solar-gain models "
                "with no glazing."
            ),
            binding_config={"source": "parser", "key": "window_count"},
            metadata={"units": "count"},
            is_required=False,
            on_missing="null",
            order=19,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="construction_count",
            label="Construction Count",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Count of Construction objects in the IDF. The "
                "bare object only — sub-types like "
                "Construction:CfactorUndergroundWall and "
                "Construction:FfactorGroundFloor are tracked "
                "separately and don't contribute here."
            ),
            binding_config={"source": "parser", "key": "construction_count"},
            metadata={"units": "count"},
            is_required=False,
            on_missing="null",
            order=20,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ── Capability flag ───────────────────────────────────────────────
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="has_hvac",
            label="Has HVAC",
            data_type=CatalogValueType.BOOLEAN,
            description=(
                "True when the IDF declares any HVAC system "
                "(HVACTemplate:*, AirLoopHVAC, or ZoneHVAC:*). "
                "A pure-envelope model returns False — useful for "
                "branching assertions like 'EUI must be < N when "
                "an HVAC system is present'."
            ),
            binding_config={"source": "parser", "key": "has_hvac"},
            metadata={},
            is_required=False,
            on_missing="null",
            order=21,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # STEP OUTPUTS - Execution, IDD, issue, and artifact evidence
        # ==================================================================
        _energyplus_output_value(
            slug="energyplus_binary_version",
            label="EnergyPlus Binary Version",
            data_type=CatalogValueType.STRING,
            description="Numeric version reported by the exact EnergyPlus binary.",
            order=60,
            category="Execution Evidence",
        ),
        _energyplus_output_value(
            slug="energyplus_binary_build",
            label="EnergyPlus Binary Build",
            data_type=CatalogValueType.STRING,
            description="Build identifier reported by the EnergyPlus binary.",
            order=61,
            category="Execution Evidence",
        ),
        _energyplus_output_value(
            slug="idd_version",
            label="IDD Version",
            data_type=CatalogValueType.STRING,
            description=(
                "Version parsed from the explicit Energy+.idd used for the run."
            ),
            order=62,
            category="Execution Evidence",
        ),
        _energyplus_output_value(
            slug="idd_build",
            label="IDD Build",
            data_type=CatalogValueType.STRING,
            description="Build identifier parsed from the explicit Energy+.idd.",
            order=63,
            category="Execution Evidence",
        ),
        _energyplus_output_value(
            slug="idd_path",
            label="IDD Path",
            data_type=CatalogValueType.STRING,
            description="Runtime path of the exact IDD passed to EnergyPlus.",
            order=64,
            category="Execution Evidence",
        ),
        _energyplus_output_value(
            slug="version_match",
            label="Model/Binary/IDD Version Match",
            data_type=CatalogValueType.BOOLEAN,
            description=(
                "True when model, binary, and IDD major/minor versions match; "
                "null when complete evidence is unavailable."
            ),
            order=65,
            category="Execution Evidence",
        ),
        _energyplus_output_value(
            slug="completed_successfully",
            label="Completed Successfully",
            data_type=CatalogValueType.BOOLEAN,
            description=(
                "True only for return code zero with no EnergyPlus fatal marker."
            ),
            order=66,
            category="Execution Evidence",
            on_missing="error",
        ),
        _energyplus_output_value(
            slug="energyplus_returncode",
            label="EnergyPlus Return Code",
            data_type=CatalogValueType.NUMBER,
            description="Raw EnergyPlus process return code.",
            order=67,
            category="Execution Evidence",
            on_missing="error",
        ),
        _energyplus_output_value(
            slug="execution_seconds",
            label="Execution Time (seconds)",
            data_type=CatalogValueType.NUMBER,
            description="Elapsed backend execution time in seconds.",
            order=68,
            category="Execution Evidence",
            units="seconds",
            on_missing="error",
        ),
        *[
            _energyplus_output_value(
                slug=slug,
                label=label,
                data_type=CatalogValueType.NUMBER,
                description=description,
                order=order,
                category="Issue Summary",
                units="count",
                on_missing="error",
            )
            for slug, label, description, order in (
                ("warning_count", "Warning Count", "EnergyPlus warning markers.", 70),
                (
                    "severe_count",
                    "Severe Error Count",
                    "EnergyPlus severe markers.",
                    71,
                ),
                ("fatal_count", "Fatal Error Count", "EnergyPlus fatal markers.", 72),
                (
                    "review_issue_count",
                    "Classified Review Issue Count",
                    "Reviewer-taxonomy and selected profile findings.",
                    73,
                ),
            )
        ],
        *[
            _energyplus_output_value(
                slug=slug,
                label=label,
                data_type=CatalogValueType.BOOLEAN,
                description=description,
                order=order,
                category="Artifact Availability",
                on_missing="error",
            )
            for slug, label, description, order in (
                (
                    "has_sql_output",
                    "Has SQL Output",
                    "Whether eplusout.sql exists.",
                    80,
                ),
                ("has_err_output", "Has Error Log", "Whether eplusout.err exists.", 81),
                (
                    "has_csv_output",
                    "Has CSV Output",
                    "Whether eplusout.csv exists.",
                    82,
                ),
                (
                    "has_eso_output",
                    "Has ESO Output",
                    "Whether eplusout.eso exists.",
                    83,
                ),
            )
        ],
        # ==================================================================
        # STEP OUTPUTS - Energy Consumption
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_electricity_kwh",
            label="Site Electricity (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total site electricity consumption from simulation.",
            binding_config={"source": "metric", "key": "site_electricity_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=100,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_natural_gas_kwh",
            label="Site Natural Gas (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total site natural gas consumption from simulation.",
            binding_config={"source": "metric", "key": "site_natural_gas_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=101,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_district_cooling_kwh",
            label="Site District Cooling (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total district cooling energy (if present in model).",
            binding_config={"source": "metric", "key": "site_district_cooling_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=102,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_district_heating_kwh",
            label="Site District Heating (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total district heating energy (if present in model).",
            binding_config={"source": "metric", "key": "site_district_heating_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=103,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_other_fuels_kwh",
            label="Other Site Fuels (kWh)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "All other GJ-valued site fuels combined, including fuel oil, "
                "propane, coal, gasoline/diesel, and EnergyPlus other fuels."
            ),
            binding_config={"source": "metric", "key": "site_other_fuels_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=104,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # STEP OUTPUTS - Energy Use Intensity
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_eui_kwh_m2",
            label="Modeled Site EUI (kWh/m\u00b2)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Modeled site energy use intensity: electricity, natural gas, "
                "district energy, and other applicable site fuels divided by "
                "simulated conditioned area. This is not measured or "
                "weather-normalized "
                "CBPS WNEUI/EUIt."
            ),
            binding_config={"source": "metric", "key": "site_eui_kwh_m2"},
            metadata={"units": "kWh/m\u00b2"},
            is_required=False,
            order=110,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # STEP OUTPUTS - End-Use Breakdown
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="heating_energy_kwh",
            label="Heating Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total space heating energy across all fuel types.",
            binding_config={"source": "metric", "key": "heating_energy_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=120,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="cooling_energy_kwh",
            label="Cooling Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total space cooling energy.",
            binding_config={"source": "metric", "key": "cooling_energy_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=121,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="interior_lighting_kwh",
            label="Interior Lighting (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total interior lighting energy.",
            binding_config={"source": "metric", "key": "interior_lighting_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=122,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="fans_energy_kwh",
            label="Fans Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total fan energy (supply, return, exhaust fans).",
            binding_config={"source": "metric", "key": "fans_energy_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=123,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="pumps_energy_kwh",
            label="Pumps Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total pump energy (chilled water, hot water, condenser).",
            binding_config={"source": "metric", "key": "pumps_energy_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=124,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="water_systems_kwh",
            label="Water Systems (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total domestic hot water energy.",
            binding_config={"source": "metric", "key": "water_systems_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=125,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # STEP OUTPUTS - Comfort / Performance
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="unmet_heating_hours",
            label="Unmet Heating Hours",
            data_type=CatalogValueType.NUMBER,
            description="Hours when heating setpoint was not met.",
            binding_config={"source": "metric", "key": "unmet_heating_hours"},
            metadata={"units": "hours"},
            is_required=False,
            order=130,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="unmet_cooling_hours",
            label="Unmet Cooling Hours",
            data_type=CatalogValueType.NUMBER,
            description="Hours when cooling setpoint was not met.",
            binding_config={"source": "metric", "key": "unmet_cooling_hours"},
            metadata={"units": "hours"},
            is_required=False,
            order=131,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="peak_electric_demand_w",
            label="Peak Electric Demand (W)",
            data_type=CatalogValueType.NUMBER,
            description="Peak electric demand during simulation.",
            binding_config={"source": "metric", "key": "peak_electric_demand_w"},
            metadata={"units": "W"},
            is_required=False,
            order=132,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # STEP OUTPUTS - Building Characteristics (simulation-derived)
        #
        # Values parsed from the IDF are step inputs (i.*); values derived
        # from EnergyPlus simulation results are step outputs (o.*).
        #
        # The simulation-derived conditioned area is named
        # ``simulated_conditioned_area_m2`` (not ``floor_area_m2``) to
        # disambiguate the value from any design floor area an author
        # might supply as input.
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="simulated_conditioned_area_m2",
            label="Simulated Conditioned Area (m\u00b2)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Total conditioned floor area as computed by EnergyPlus "
                "from the simulated geometry. Distinct from any design "
                "floor area declared in the IDF (which is a step input)."
            ),
            binding_config={
                "source": "metric",
                "key": "simulated_conditioned_area_m2",
            },
            metadata={"units": "m\u00b2"},
            is_required=False,
            order=140,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # STEP OUTPUTS - Window Envelope
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="window_heat_gain_kwh",
            label="Window Heat Gain (kWh)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Total annual heat gain through windows. Extracted from "
                "Surface Window Heat Gain Energy output variable."
            ),
            binding_config={"source": "metric", "key": "window_heat_gain_kwh"},
            metadata={"units": "kWh", "precision": 1},
            is_required=False,
            order=150,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="window_heat_loss_kwh",
            label="Window Heat Loss (kWh)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Total annual heat loss through windows. Extracted from "
                "Surface Window Heat Loss Energy output variable."
            ),
            binding_config={"source": "metric", "key": "window_heat_loss_kwh"},
            metadata={"units": "kWh", "precision": 1},
            is_required=False,
            order=151,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="window_transmitted_solar_kwh",
            label="Transmitted Solar (kWh)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Total annual solar radiation transmitted through windows. "
                "Direct expression of SHGC effect. Extracted from Surface "
                "Window Transmitted Solar Radiation Energy output variable."
            ),
            binding_config={
                "source": "metric",
                "key": "window_transmitted_solar_kwh",
            },
            metadata={"units": "kWh", "precision": 1},
            is_required=False,
            order=152,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # DERIVATIONS (computed from other step values)
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.DERIVATION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="total_unmet_hours",
            label="Total Unmet Hours",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Combined unmet heating and cooling hours. "
                "Derived from unmet_heating_hours + unmet_cooling_hours."
            ),
            binding_config={
                "expr": "unmet_heating_hours + unmet_cooling_hours",
            },
            metadata={"units": "hours"},
            is_required=False,
            order=200,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.DERIVATION,
            run_stage=CatalogRunStage.OUTPUT,
            slug="total_site_energy_kwh",
            label="Total Site Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Total site energy consumption across electricity, natural "
                "gas, district energy, and other applicable site fuels."
            ),
            binding_config={
                "expr": (
                    "(site_electricity_kwh ?? 0) + "
                    "(site_natural_gas_kwh ?? 0) + "
                    "(site_district_cooling_kwh ?? 0) + "
                    "(site_district_heating_kwh ?? 0) + "
                    "(site_other_fuels_kwh ?? 0)"
                ),
            },
            metadata={"units": "kWh"},
            is_required=False,
            order=201,
        ),
    ],
    # Template variable editing is handled by the workflow data card.
    # no custom step_editor_cards needed.
)
