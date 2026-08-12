"""Type-safe Pydantic models for a WorkflowStep's two config buckets.

ADR-2026-06-18 split ``WorkflowStep``'s single ``config`` JSONField into two
physically-separate buckets so that "hash the whole step config" is correct by
construction:

* **``config``** — the **semantic** bucket. Only settings that change what
  validation *does* or how its compute is planned (``schema_type``,
  ``delimiter``, ``encoding``, ``execution_profile``, ``case_sensitive``, FMU
  sim settings, …). Its models forbid
  extra keys (see :data:`_SEMANTIC_EXTRA`), so the workflow-definition digest can
  hash this field **wholesale** and stay provably free of cosmetic or
  run-injected data.
* **``display_settings``** — the **cosmetic + runtime-injected** bucket
  (``schema_type_label``, previews, column counts, ``display_step_outputs``, and
  keys the runner injects such as ``primary_file_uri``). Its models use
  ``extra="allow"`` and are **never hashed**.

This module is the single source of truth for *which key belongs in which
bucket*. Three consumers read that boundary and must never re-derive it:

* :func:`get_step_config` / :func:`get_step_display_settings` — typed access,
  backing ``WorkflowStep.typed_config`` / ``WorkflowStep.display_settings_typed``.
* :func:`partition_step_config` — routes a freshly-built config dict into the two
  buckets at save time, using the semantic model's declared field set as the
  discriminator.
* The contract-snapshot digest (``workflows/services/contract_snapshot.py``),
  which hashes ``config`` wholesale precisely because this module guarantees it
  holds only semantic keys.

Usage::

    from validibot.workflows.step_configs import get_step_config

    typed = get_step_config(step)          # semantic bucket
    if isinstance(typed, EnergyPlusStepConfig):
        checks = typed.idf_checks          # list[str], type-checked

    display = get_step_display_settings(step)   # cosmetic bucket
    label = display.schema_type_label if hasattr(display, "schema_type_label") else ""

See Also:
    - ADR-2026-06-18 (implementation note, "Step config: split the semantic and
      cosmetic buckets").
    - GitHub issue #96: Add type-safe Pydantic models for WorkflowStep.config.
    - WorkflowStep.typed_config / display_settings_typed (workflows/models.py).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from validibot.validations.constants import ValidatorExecutionProfile

if TYPE_CHECKING:
    from validibot.workflows.models import WorkflowStep


# ---------------------------------------------------------------------------
# The semantic/cosmetic boundary switch
# ---------------------------------------------------------------------------

# The semantic (``config``) models forbid undeclared keys so nothing cosmetic or
# runtime-injected can land in the hashed bucket. This is the whole point of the
# split: with ``extra="forbid"`` the workflow-definition digest can hash the
# ``config`` field wholesale and stay provably semantic-only.
#
# One switch controls every semantic model at once. The flip to ``"forbid"`` is
# only safe once (a) the data migration has moved legacy cosmetic keys out of
# ``config`` and (b) every writer targets the right bucket — until both hold, an
# existing/in-flight config could still carry a cosmetic key and
# ``get_step_config`` (called from ``WorkflowStep.clean``) would reject it. The
# committed end state is ``"forbid"``.
_SEMANTIC_EXTRA = "forbid"


# ---------------------------------------------------------------------------
# Base models
# ---------------------------------------------------------------------------


class BaseStepConfig(BaseModel):
    """Base model for the **semantic** ``config`` bucket.

    A step type with no semantic settings (Basic, Custom) uses this directly.
    Subclasses add only keys that change validation or execution behaviour.
    ``extra`` is forbidden so an undeclared/cosmetic/run-injected key cannot
    silently enter the hashed bucket; cosmetic data belongs in
    ``display_settings`` (see :class:`BaseDisplaySettings`).
    """

    model_config = ConfigDict(extra=_SEMANTIC_EXTRA)


class ContainerExecutionStepConfig(BaseStepConfig):
    """Provider-neutral compute intent shared only by container validators."""

    execution_profile: ValidatorExecutionProfile = (
        ValidatorExecutionProfile.FAST_RESPONSE
    )
    """Workload profile requested by the workflow author.

    This is provider-neutral: hosted GCP currently maps fast-response work to
    the primary Service route and long-running work to the retained Job route.
    Self-hosted and future providers may implement the same intent differently.
    """

    execution_timeout_seconds: int | None = Field(default=None, ge=1)
    """Optional API/import override bounded again by deployment policy at launch.

    The authoring UI intentionally exposes only the simple execution profile.
    This field preserves the existing machine-authored contract for clients
    that need a narrower explicit deadline.
    """


class BaseDisplaySettings(BaseModel):
    """Base model for the **cosmetic + runtime-injected** ``display_settings``
    bucket.

    Uses ``extra="allow"`` so derived caches (labels, previews) and keys the
    runner injects at launch time (``primary_file_uri`` …) never raise. Nothing
    here is hashed into the workflow-definition digest.
    """

    model_config = ConfigDict(extra="allow")

    display_step_outputs: list[str] = Field(default_factory=list)
    """Catalog entry slugs for output values to display to the submitter.

    Controls which output values are shown in the results view and returned by
    the API. **Empty means show NONE** — authors opt in to each output they want
    exposed. This is cross-validator — any step type can use it. It changes only
    *what is shown*, never pass/fail, so it lives in ``display_settings``.

    A workflow-step toggle to "show all output values" is on the roadmap
    (tracked in validibot-project); until then, authors who want every output
    exposed must list every slug here."""


# ---------------------------------------------------------------------------
# Semantic config models (one per validator/action type)
# ---------------------------------------------------------------------------


class JsonSchemaStepConfig(BaseStepConfig):
    """Semantic config for JSON Schema validator steps.

    The schema *content* lives on the Ruleset; the only step-level semantic knob
    is which draft to validate against. Display metadata (source, preview, label)
    lives in :class:`JsonSchemaDisplaySettings`.
    """

    schema_type: str = ""
    """JSON Schema draft version (e.g., "2020-12", "draft-07")."""


class XmlSchemaStepConfig(BaseStepConfig):
    """Semantic config for XML Schema validator steps.

    Mirror of :class:`JsonSchemaStepConfig`: only the schema type is semantic.
    """

    schema_type: str = ""
    """Schema type: "XSD", "DTD", or "RELAXNG"."""


class TabularStepConfig(BaseStepConfig):
    """Semantic config for Tabular Validator steps.

    The dialect knobs (delimiter / encoding / header) change how the file is
    parsed, so they are semantic. The Table Schema descriptor itself lives on the
    Ruleset. Display metadata (labels, counts, preview) lives in
    :class:`TabularDisplaySettings`.
    """

    delimiter: str = ""
    """Declared delimiter, or "" for auto-detect (sniffed at read time)."""

    encoding: str = ""
    """Declared file encoding (e.g. "utf-8")."""

    has_header: bool = True
    """Whether the file has a header row."""


class ShaclStepConfig(ContainerExecutionStepConfig):
    """Semantic config for SHACL (RDF graph) validator steps.

    The engine knobs below change how validation runs (inference, bundled
    standards, result handling). They are duplicated on ``ruleset.metadata``
    (the authoritative copy the engine reads); the copies here keep the step's
    semantics self-describing and hashable. File-upload metadata and the shapes
    preview are display-only and live in :class:`ShaclDisplaySettings`.
    """

    bundled_standards: list[str] = Field(default_factory=list)
    """Bundled vocabularies to load before validation (e.g. "brick-1.4")."""

    inference_mode: str = ""
    """RDFS/OWL inference mode applied before shape checking."""

    advanced_shacl: bool = False
    """Whether SHACL-AF (advanced features: SPARQL constraints) is enabled."""

    submission_format: str = ""
    """Declared RDF serialization ("auto", "turtle", "xml", …)."""

    shacl_result_handling: str = ""
    """How violation severities map to pass/fail."""


class EnergyPlusStepConfig(ContainerExecutionStepConfig):
    """Semantic config for EnergyPlus validator steps.

    Simulation and template-matching settings change what is validated, so they
    are semantic. Warning display and step-output selection are cosmetic and
    live in :class:`EnergyPlusDisplaySettings`. Resource files (weather EPWs,
    model templates) are stored relationally via ``WorkflowStepResource``.

    All simulation settings are forwarded through the strict shared envelope to
    the backend. Template mode forces a full simulation at form-save time.
    """

    validation_mode: str = ""
    """"direct" (native preflight/simulation) or "template" (parameterized IDF)."""

    idf_checks: list[str] = Field(default_factory=list)
    """Author-selected modelling-review checks to run before EnergyPlus.
    """

    run_simulation: bool = False
    """Whether to run a full simulation or native conversion-only preflight.
    """

    timestep_per_hour: int = Field(default=4, ge=1, le=60)
    """Number of simulation timesteps per hour applied to a private model copy."""

    review_profile: Literal["standard", "leed_review"] = "standard"
    """Evidence/severity profile layered over the same EnergyPlus engine."""

    case_sensitive: bool = True
    """Whether template-variable matching is case-sensitive.

    When True (default), only ``$UPPERCASE_NAMES`` are detected as template
    variables; when False, names are normalized to uppercase during scanning and
    matching. This changes which placeholders the pipeline substitutes, so it is
    semantic."""


class FMUSimulationConfig(BaseModel):
    """Simulation settings for step-level FMU execution.

    Pre-populated from the FMU's ``DefaultExperiment`` element when available.
    The workflow author can override any value. When a field is ``None``, the
    container runner uses its own default.
    """

    start_time: float | None = None
    stop_time: float | None = None
    step_size: float | None = None
    tolerance: float | None = None


class FmuStepConfig(ContainerExecutionStepConfig):
    """Semantic config for FMU validator steps.

    Both fields are set at *authoring* time (when the author uploads/edits the
    FMU), not injected per run, and both change what runs or how ``i.*`` values
    resolve — so they are semantic and hashed. FMU variable metadata is stored
    relationally in ``StepIODefinition`` rows.
    """

    fmu_simulation: FMUSimulationConfig | None = None
    """Simulation settings, pre-populated from DefaultExperiment. Only populated
    for step-level FMU uploads."""

    fmu_introspection: dict[str, Any] | None = None
    """Parser facts stamped from the uploaded FMU (fmi_version, variable counts,
    …), used at runtime to resolve ``i.*`` values for step-level uploads.
    Derived from the FMU file at authoring time and read from ``step.config`` by
    ``FMUValidator``, so it stays in the semantic bucket."""


class SchematronStepConfig(ContainerExecutionStepConfig):
    """Semantic config for isolated Schematron validator execution."""


class PortfolioManagerStepConfig(ContainerExecutionStepConfig):
    """Semantic configuration for Portfolio Manager report validation."""

    submission_structure: Literal["single_report", "zip_collection"] = "single_report"
    default_euit_kbtu_ft2_yr: Decimal | None = Field(default=None, gt=0)
    compare_to_euit: bool = False
    near_target_percent: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    require_complete_reporting_period: bool = False
    minimum_reporting_period_months: int = Field(default=12, ge=1, le=36)
    maximum_reporting_period_age_months: int | None = Field(
        default=None,
        ge=0,
        le=120,
    )
    require_benchmark_ready: bool = False
    require_form_c_ready: bool = False
    require_weather_normalized_site_eui: bool = False
    require_washington_standard_id: bool = False
    require_energy_star_score: bool = False
    meter_less_than_12_months_policy: Literal["allow", "warning", "error"] = "allow"
    meter_gap_policy: Literal["allow", "warning", "error"] = "allow"
    meter_overlap_policy: Literal["allow", "warning", "error"] = "allow"
    no_meters_selected_policy: Literal["allow", "warning", "error"] = "allow"
    long_meter_entry_policy: Literal["allow", "warning", "error"] = "allow"
    estimated_energy_policy: Literal["allow", "warning", "error"] = "allow"
    other_alert_policy: Literal["allow", "warning", "error"] = "allow"
    max_archive_members: int = Field(default=250, ge=1, le=1_000)
    max_member_bytes: int = Field(default=20_000_000, ge=1, le=100_000_000)
    max_uncompressed_bytes: int = Field(
        default=250_000_000,
        ge=1,
        le=1_000_000_000,
    )


class PdfPayloadSelectorStepConfig(BaseModel):
    """Exact author-controlled selector for one typed PDF output."""

    required: bool = False
    discovery_kinds: list[
        Literal[
            "embedded_files_name_tree",
            "associated_file",
            "file_attachment_annotation",
        ]
    ] = Field(default_factory=list)
    original_filename: str = ""
    declared_media_type: str = ""
    detected_media_type: str = ""
    af_relationship: str = ""
    xml_root_qname: str = ""
    step_file_schema: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class PdfStepConfig(ContainerExecutionStepConfig):
    """Semantic configuration for isolated PDF package inspection."""

    execution_timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    policy: Literal["static_text_package_v1"] = "static_text_package_v1"
    emit_extracted_files_bundle: bool = False
    selected_xml: PdfPayloadSelectorStepConfig | None = None
    selected_json: PdfPayloadSelectorStepConfig | None = None
    selected_step_p21: PdfPayloadSelectorStepConfig | None = None


class BasicStepConfig(BaseStepConfig):
    """Semantic config for Basic assertion validator steps.

    Basic validation has no per-step semantic configuration — assertions live on
    the Ruleset.
    """


class AiAssistStepConfig(BaseStepConfig):
    """Semantic config for AI Assist validator steps.

    Template, mode, cost cap, selectors, and policy rules all change what the AI
    step does, so all are semantic.
    """

    template: str = ""
    """AI template type: "ai_critic" or "policy_check"."""

    mode: str = ""
    """Enforcement mode: "ADVISORY" (non-blocking) or "BLOCKING"."""

    cost_cap_cents: int = 10
    """Maximum cost in cents for this AI step (1-500)."""

    selectors: list[str] = Field(default_factory=list)
    """JSONPath selectors to extract parts of the submission (max 20)."""

    policy_rules: list[dict[str, Any]] = Field(default_factory=list)
    """Policy rules as dicts with keys: path, operator, value, value_b, message."""


class CustomValidatorStepConfig(BaseStepConfig):
    """Semantic config for Custom Validator steps.

    Custom validators have no per-step semantic configuration — assertions live
    on the Ruleset.
    """


# ---------------------------------------------------------------------------
# Action step configs
# ---------------------------------------------------------------------------
#
# Action steps are EXCLUDED from the workflow-definition digest (see
# contract_snapshot._project_validation_steps), so they do not need the
# forbid-guarded semantic bucket. They keep ``extra="allow"`` so an action's
# display summary (persisted on ``step.config`` by ``build_step_summary``) does
# not need to be partitioned.


class BaseActionStepConfig(BaseModel):
    """Base for action-step configs — permissive because actions aren't hashed."""

    model_config = ConfigDict(extra="allow")


class SlackActionStepConfig(BaseActionStepConfig):
    """Config for Slack message action steps."""

    message: str = ""
    """Message text to send to the configured Slack channel."""


class CredentialActionStepConfig(BaseActionStepConfig):
    """Config for signed credential action steps."""


# ---------------------------------------------------------------------------
# Display settings models (one per validator type that has cosmetic keys)
# ---------------------------------------------------------------------------
#
# Types with no cosmetic keys beyond ``display_step_outputs`` (FMU, AI, Basic,
# Custom) fall back to BaseDisplaySettings via STEP_DISPLAY_MODELS.get(...).


class JsonSchemaDisplaySettings(BaseDisplaySettings):
    """Cosmetic display metadata for JSON Schema steps."""

    schema_source: str = ""
    """How the schema was provided: "text", "upload", or "keep"."""

    schema_text_preview: str = ""
    """First 1200 characters of the schema for display in the step editor."""

    schema_type_label: str = ""
    """Human-readable label for the schema type (computed in views)."""


class XmlSchemaDisplaySettings(BaseDisplaySettings):
    """Cosmetic display metadata for XML Schema steps."""

    schema_source: str = ""
    """How the schema was provided: "text", "upload", or "keep"."""

    schema_text_preview: str = ""
    """First 1200 characters of the schema for display in the step editor."""

    schema_type_label: str = ""
    """Human-readable label for the schema type (computed in views)."""


class TabularDisplaySettings(BaseDisplaySettings):
    """Cosmetic display metadata for the Tabular step summary card."""

    schema_source: str = ""
    """How the schema was provided: "editor", "text", "upload", "infer", "keep"."""

    schema_text_preview: str = ""
    """First 1200 characters of the Table Schema descriptor, for display."""

    delimiter_label: str = ""
    """Human-readable delimiter label for the summary card (e.g. "Tab")."""

    column_count: int = 0
    """Number of declared columns, for the summary card."""

    required_column_count: int = 0
    """Number of columns whose values are required."""


class ShaclDisplaySettings(BaseDisplaySettings):
    """Cosmetic display metadata for SHACL steps.

    The authoritative copies of the file lists and snapshot live on
    ``ruleset.metadata``; these are display duplicates for the step editor.
    """

    shape_files: list[dict[str, Any]] = Field(default_factory=list)
    """Metadata (name/size) for the uploaded shapes files, for the editor."""

    ontology_files: list[dict[str, Any]] = Field(default_factory=list)
    """Metadata for the uploaded ontology files, for the editor."""

    shapes_text_preview: str = ""
    """First 1200 characters of the merged shapes, for a read-only preview."""

    library_default_snapshot: dict[str, Any] | None = None
    """Provenance of the inlined library default ruleset (ids, hashes)."""


class EnergyPlusDisplaySettings(BaseDisplaySettings):
    """Cosmetic display metadata for EnergyPlus steps."""

    show_energyplus_warnings: bool = True
    """Whether to include EnergyPlus simulation warnings in the findings shown to
    submitters. Filters *display* of non-blocking warnings; it never changes
    pass/fail, so it is cosmetic."""


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

# Maps ValidationType / action type string → SEMANTIC config model class.
STEP_CONFIG_MODELS: dict[str, type[BaseModel]] = {
    # Validator types (from ValidationType)
    "JSON_SCHEMA": JsonSchemaStepConfig,
    "XML_SCHEMA": XmlSchemaStepConfig,
    "TABULAR": TabularStepConfig,
    "SCHEMATRON": SchematronStepConfig,
    "PORTFOLIO_MANAGER": PortfolioManagerStepConfig,
    "PDF": PdfStepConfig,
    "SHACL": ShaclStepConfig,
    "ENERGYPLUS": EnergyPlusStepConfig,
    "FMU": FmuStepConfig,
    "BASIC": BasicStepConfig,
    "AI_ASSIST": AiAssistStepConfig,
    "CUSTOM_VALIDATOR": CustomValidatorStepConfig,
    # Action types
    "SLACK_MESSAGE": SlackActionStepConfig,
    "SIGNED_CREDENTIAL": CredentialActionStepConfig,
}

# Maps the same type strings → DISPLAY settings model class. Types absent here
# fall back to BaseDisplaySettings (only ``display_step_outputs``).
STEP_DISPLAY_MODELS: dict[str, type[BaseDisplaySettings]] = {
    "JSON_SCHEMA": JsonSchemaDisplaySettings,
    "XML_SCHEMA": XmlSchemaDisplaySettings,
    "TABULAR": TabularDisplaySettings,
    "SHACL": ShaclDisplaySettings,
    "ENERGYPLUS": EnergyPlusDisplaySettings,
}


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _resolve_step_type(step: WorkflowStep) -> str | None:
    """Return the validator/action type string for a step, or None if unknown."""
    if step.validator_id:
        return getattr(step.validator, "validation_type", None)
    if step.action_id:
        action = getattr(step, "action", None)
        definition = getattr(action, "definition", None) if action else None
        if definition:
            return getattr(definition, "type", None)
    return None


def get_step_config(step: WorkflowStep) -> BaseModel:
    """Parse a step's ``config`` (semantic bucket) into a typed Pydantic model.

    Resolves the step's validator or action type, looks up the matching semantic
    model, and returns a validated instance. Falls back to :class:`BaseStepConfig`
    for unknown types.

    Args:
        step: The WorkflowStep whose ``config`` to parse.

    Returns:
        A typed semantic config model instance (e.g., EnergyPlusStepConfig).
    """
    step_type = _resolve_step_type(step)
    model_class = STEP_CONFIG_MODELS.get(step_type or "", BaseStepConfig)
    return model_class.model_validate(step.config or {})


def get_step_display_settings(step: WorkflowStep) -> BaseDisplaySettings:
    """Parse a step's ``display_settings`` (cosmetic bucket) into a typed model.

    Mirror of :func:`get_step_config` for the cosmetic bucket. Falls back to
    :class:`BaseDisplaySettings` (which still exposes ``display_step_outputs``)
    for types without extra display fields.

    Args:
        step: The WorkflowStep whose ``display_settings`` to parse.

    Returns:
        A typed display settings model instance.
    """
    step_type = _resolve_step_type(step)
    model_class = STEP_DISPLAY_MODELS.get(step_type or "", BaseDisplaySettings)
    return model_class.model_validate(step.display_settings or {})


def partition_step_config(
    step_type: str | None,
    merged: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a freshly-built config dict into (``config``, ``display_settings``).

    The semantic model's declared field set is the single discriminator: a key
    declared on the semantic model goes to ``config``; everything else (cosmetic
    labels, previews, ``display_step_outputs``, and any undeclared key) goes to
    ``display_settings``. Routing undeclared keys to the permissive display
    bucket is deliberate and fail-safe — it keeps run/cosmetic data out of the
    hashed bucket even if a new key is added without updating a model.

    Args:
        step_type: The validator/action type string (as returned by
            ``validator.validation_type``).
        merged: The combined config dict a builder produced.

    Returns:
        ``(config, display_settings)`` — two disjoint dicts.
    """
    model_class = STEP_CONFIG_MODELS.get(step_type or "", BaseStepConfig)
    semantic_fields = set(model_class.model_fields)

    config: dict[str, Any] = {}
    display: dict[str, Any] = {}
    for key, value in merged.items():
        if key in semantic_fields:
            config[key] = value
        else:
            display[key] = value
    return config, display
