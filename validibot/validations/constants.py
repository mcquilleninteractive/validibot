from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from django.conf import settings as django_settings
from django.db import models
from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class StorageCapabilityMode(StrEnum):
    """Effective storage mechanism exposed to one validator execution.

    These values are operator-facing and appear in the stable doctor JSON
    document. They describe what the runtime can actually use, not merely the
    backing store named in Django settings.
    """

    LOCAL_ATTEMPT_MOUNT = "local_attempt_mount"
    GCS_DOWNSCOPED_TOKEN = "gcs_downscoped_token"  # noqa: S105
    S3_CONDITIONAL = "s3_conditional"
    SERVER_MEDIATED_BROKER = "server_mediated_broker"
    UNSUPPORTED = "unsupported"


class RuntimeStorageIsolation(StrEnum):
    """Confidentiality boundary provided to one validator execution."""

    ATTEMPT_SCOPED = "attempt_scoped"
    UNSUPPORTED = "unsupported"


class ExecutionProviderType(TextChoices):
    """Managed infrastructure provider for a validator deployment."""

    GCP = "GCP", _("Google Cloud Platform")


class ExecutionDeploymentKind(TextChoices):
    """Provider primitive that accepts one validator execution."""

    CLOUD_RUN_JOB = "CLOUD_RUN_JOB", _("Cloud Run Job")
    CLOUD_RUN_SERVICE = "CLOUD_RUN_SERVICE", _("Cloud Run Service")


class ExecutionDeploymentReadiness(TextChoices):
    """Operator-verified lifecycle state of an execution deployment."""

    DRAFT = "DRAFT", _("Draft")
    VERIFYING = "VERIFYING", _("Verifying")
    READY = "READY", _("Ready")
    FAILED = "FAILED", _("Verification failed")
    RETIRED = "RETIRED", _("Retired")


class ExecutionDeploymentRoutingRole(TextChoices):
    """Exclusive routing slot occupied by a launchable deployment."""

    INACTIVE = "INACTIVE", _("Inactive")
    PRIMARY = "PRIMARY", _("Primary")
    LONG_RUNNING = "LONG_RUNNING", _("Long-running compatibility")


class ExecutionDeploymentDeactivationCause(TextChoices):
    """Bounded reason for the current continuous period without a route."""

    SUPERSEDED_BY_ACCEPTED_RELEASE = (
        "SUPERSEDED_BY_ACCEPTED_RELEASE",
        _("Superseded by an accepted release"),
    )
    RELEASE_ROLLBACK_FROM = (
        "RELEASE_ROLLBACK_FROM",
        _("Release rolled back from"),
    )
    ACCEPTANCE_FAILURE = "ACCEPTANCE_FAILURE", _("Acceptance failure")
    SHAPE_ROLLBACK = "SHAPE_ROLLBACK", _("Execution-shape rollback")
    OPERATOR_DEACTIVATION = (
        "OPERATOR_DEACTIVATION",
        _("Operator deactivation"),
    )


class ExecutionRoutingMode(StrEnum):
    """Calculated execution shape for one Service/Job routing pair."""

    NORMAL = "normal"
    JOB_ONLY = "job-only"
    INACTIVE = "inactive"
    INCONSISTENT = "inconsistent"


class ValidatorExecutionProfile(TextChoices):
    """Workflow-author intent for one container-based validator step.

    Authors choose the workload shape, while operators retain control over the
    concrete provider deployment. A fast-response step normally uses the
    primary request-driven route; a long-running step uses the retained route
    with the larger verified execution budget.
    """

    FAST_RESPONSE = "FAST_RESPONSE", _("Fast response")
    LONG_RUNNING = "LONG_RUNNING", _("Long-running")


class ExecutionShape(TextChoices):
    """How a provider holds and represents an execution."""

    REQUEST = "REQUEST", _("Request-driven")
    JOB = "JOB", _("Provider job")


class ProviderStatusLookupCapability(TextChoices):
    """Whether provider state can reconcile a missing callback."""

    SUPPORTED = "SUPPORTED", _("Supported")
    UNSUPPORTED = "UNSUPPORTED", _("Unsupported")


class ProviderCancellationCapability(TextChoices):
    """Whether the provider exposes cancellation for one execution."""

    SUPPORTED = "SUPPORTED", _("Supported")
    BEST_EFFORT = "BEST_EFFORT", _("Best effort before execution starts")
    UNSUPPORTED = "UNSUPPORTED", _("Unsupported")


class CallbackAuthenticationMethod(TextChoices):
    """Authentication contract expected for deployment callbacks."""

    ATTEMPT_NONCE_AND_OIDC = (
        "ATTEMPT_NONCE_AND_OIDC",
        _("Attempt nonce and provider OIDC identity"),
    )


CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS = 1500
CLOUD_RUN_SERVICE_REQUEST_TIMEOUT_LIMIT_SECONDS = 1650
CLOUD_RUN_SERVICE_DISPATCH_DEADLINE_SECONDS = 1800


class ValidationRunStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")
    CANCELED = "CANCELED", _("Canceled")
    TIMED_OUT = "TIMED_OUT", _("Timed Out")


class ExecutionAttemptState(TextChoices):
    """Monotonic lifecycle states for one concrete provider launch."""

    PENDING = "PENDING", _("Pending")
    DISPATCHING = "DISPATCHING", _("Dispatching")
    RUNNING = "RUNNING", _("Running")
    UNKNOWN = "UNKNOWN", _("Provider acceptance unknown")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    CANCELED = "CANCELED", _("Canceled")
    TIMED_OUT = "TIMED_OUT", _("Timed out")


EXECUTION_ATTEMPT_TERMINAL_STATES = frozenset(
    {
        ExecutionAttemptState.COMPLETED,
        ExecutionAttemptState.FAILED,
        ExecutionAttemptState.CANCELED,
        ExecutionAttemptState.TIMED_OUT,
    }
)

EXECUTION_ATTEMPT_ACTIVE_STATES = frozenset(
    set(ExecutionAttemptState.values) - EXECUTION_ATTEMPT_TERMINAL_STATES
)


class ValidationRunState(TextChoices):
    """
    Public-facing lifecycle state for a validation run.

    This is intentionally separate from `ValidationRunStatus`. The underlying
    model status captures both lifecycle and terminal outcomes (for example
    `SUCCEEDED`, `FAILED`). For API consumers and the CLI we expose a simpler
    state machine:

    - `PENDING`: Run created but not yet started.
    - `RUNNING`: Run is executing.
    - `COMPLETED`: Run reached a terminal status (success, failure, cancel, timeout).
    """

    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    COMPLETED = "COMPLETED", _("Completed")


class ValidationRunResult(TextChoices):
    """
    Public-facing outcome for a validation run.

    Unlike `ValidationRunStatus`, this focuses on the terminal conclusion and is
    designed to be stable for automation (CLI exit codes, CI pipelines).
    """

    PASS = "PASS", _("Pass")
    FAIL = "FAIL", _("Fail")
    ERROR = "ERROR", _("Error")
    CANCELED = "CANCELED", _("Canceled")
    TIMED_OUT = "TIMED_OUT", _("Timed Out")
    UNKNOWN = "UNKNOWN", _("Unknown")


VALIDATION_RUN_TERMINAL_STATUSES = [
    ValidationRunStatus.SUCCEEDED,
    ValidationRunStatus.FAILED,
    ValidationRunStatus.CANCELED,
    ValidationRunStatus.TIMED_OUT,
]


def project_run_state(status: str) -> str:
    """Project the model ``ValidationRunStatus`` to public ``ValidationRunState``.

    This is the single source of truth for "is the run still going?"
    semantics across every API surface (web, REST, MCP helper,
    anonymous x402). The model column captures both lifecycle and
    terminal outcomes (PENDING / RUNNING / SUCCEEDED / FAILED /
    CANCELED / TIMED_OUT) but consumers should see the simplified
    state machine (PENDING / RUNNING / COMPLETED) plus a separate
    ``result`` field that carries the terminal outcome.

    Previously the anonymous x402 status endpoint exposed
    ``vr.status`` verbatim under the same ``state`` key that the
    authenticated path used for the projected lifecycle value, so the
    MCP server needed a five-element ``_TERMINAL_STATES`` set that
    spanned both vocabularies. Centralising the projection here lets
    every Validibot endpoint emit one ``state`` vocabulary, so MCP,
    CLI, and any future shared re-export only have to know one enum.
    """

    if status == ValidationRunStatus.PENDING:
        return ValidationRunState.PENDING
    if status == ValidationRunStatus.RUNNING:
        return ValidationRunState.RUNNING
    return ValidationRunState.COMPLETED


class ValidationRunErrorCategory(TextChoices):
    """
    Top-level classification of why a validation run failed.

    This categorizes the overall run outcome, not individual step findings.
    Used to provide human-friendly error messages and enable filtering
    in dashboards. The 'error' field on ValidationRun contains the detailed
    message; error_category classifies the type of failure.

    VALIDATION_FAILED: The validator ran successfully but found validation errors
    TIMEOUT: The validator exceeded the time limit
    OOM: The validator exceeded memory limits (container killed)
    RUNTIME_ERROR: The validator encountered an unexpected error
    SYSTEM_ERROR: Infrastructure/platform issues (storage, container runtime, etc.)
    """

    VALIDATION_FAILED = "VALIDATION_FAILED", _("Validation Failed")
    TIMEOUT = "TIMEOUT", _("Timed Out")
    OOM = "OOM", _("Out of Memory")
    RUNTIME_ERROR = "RUNTIME_ERROR", _("Runtime Error")
    SYSTEM_ERROR = "SYSTEM_ERROR", _("System Error")


# Human-friendly error messages by category
VALIDATION_RUN_ERROR_MESSAGES = {
    ValidationRunErrorCategory.VALIDATION_FAILED: (
        "Validibot found issues with your data. Validation failed."
    ),
    ValidationRunErrorCategory.TIMEOUT: (
        "The validation took too long and was stopped. "
        "Try a smaller file or contact support for larger models."
    ),
    ValidationRunErrorCategory.OOM: (
        "The validation ran out of memory. "
        "Try a smaller file or contact support for larger models."
    ),
    ValidationRunErrorCategory.RUNTIME_ERROR: (
        "An unexpected error occurred during validation. "
        "Please try again or contact support if the problem persists."
    ),
    ValidationRunErrorCategory.SYSTEM_ERROR: (
        "A system error prevented the validation from completing. "
        "Please try again in a few minutes."
    ),
}


class LibraryLayout(TextChoices):
    GRID = "grid", _("Grid")
    LIST = "list", _("List")


VALIDATION_LIBRARY_LAYOUT_SESSION_KEY = "validation_library_layout"
VALIDATION_LIBRARY_TAB_SESSION_KEY = "validation_library_tab"


class StepStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    PASSED = "PASSED", _("Passed")
    FAILED = "FAILED", _("Failed")
    SKIPPED = "SKIPPED", _("Skipped")


class CloudRunJobStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")


class RulesetType(TextChoices):
    BASIC = "BASIC", _("Basic Assertions")
    JSON_SCHEMA = "JSON_SCHEMA", _("JSON Schema")
    XML_SCHEMA = "XML_SCHEMA", _("XML Schema")
    SCHEMATRON = "SCHEMATRON", _("Schematron")
    SHACL = "SHACL", _("SHACL Shapes")
    ENERGYPLUS = "ENERGYPLUS", _("EnergyPlus")
    FMU = "FMU", _("FMU Validator")
    CUSTOM_VALIDATOR = "CUSTOM_VALIDATOR", _("Custom Basic Validator")
    THERM = "THERM", _("THERM")
    TABULAR = "TABULAR", _("Tabular")
    PORTFOLIO_MANAGER = "PORTFOLIO_MANAGER", _("Building Benchmark Reports")


class ValidationType(TextChoices):
    """
    Validator types.

    Assertion support by type:
    - BASIC: Supports BASIC and CEL assertions against JSON payload
    - AI_ASSIST: Supports CEL assertions against JSON payload
    - ENERGYPLUS: Supports CEL assertions against step outputs
    - FMU: Supports CEL assertions against step outputs
    - JSON_SCHEMA: Supports BASIC and CEL assertions after schema validation
    - XML_SCHEMA: Supports BASIC and CEL assertions after schema validation
    - SCHEMATRON: Supports CEL assertions against step outputs (the SVRL
      summary is exposed as ``o.*`` — e.g. ``o.error_count == 0`` for a
      warnings-tolerant gate)
    - SHACL: Supports CEL assertions against step outputs
    - CUSTOM_VALIDATOR: Supports BASIC and CEL assertions
    - THERM: Supports CEL assertions against step outputs
    - TABULAR: Supports CEL assertions (dataset-level i.*/o.* plus per-row
      ``row.*`` rules; the ``row.*`` lane is CEL-only)

    Namespace availability is otherwise per the validator's place on the
    process spectrum: ``i.*`` (step inputs) and ``o.*`` (step outputs) exist
    only for validators that parse/produce them (EnergyPlus, FMU, SHACL,
    SCHEMATRON, THERM, Custom, Tabular); schema validators (BASIC,
    JSON_SCHEMA, XML_SCHEMA) have none.
    The ``submission.*`` namespace (envelope metadata + server facts) and
    ``s.*`` (workflow signals) are available to BASIC and CEL assertions for
    EVERY validator regardless of file format — that universality is the whole
    point of ``submission.*`` (ADR-2026-06-03b). The one exception is the
    Tabular ``row.*`` lane, which binds only ``row``/``s``/``i`` per row.
    """

    BASIC = "BASIC", _("Basic Assertions")
    JSON_SCHEMA = "JSON_SCHEMA", _("JSON Schema")
    XML_SCHEMA = "XML_SCHEMA", _("XML Schema")
    SCHEMATRON = "SCHEMATRON", _("Schematron")
    SHACL = "SHACL", _("SHACL (RDF Graph)")
    ENERGYPLUS = "ENERGYPLUS", _("EnergyPlus")
    FMU = "FMU", _("FMU Validator")
    CUSTOM_VALIDATOR = "CUSTOM_VALIDATOR", _("Custom Basic Validator")
    AI_ASSIST = "AI_ASSIST", _("AI Assist")
    THERM = "THERM", _("THERM Thermal Analysis")
    TABULAR = "TABULAR", _("Tabular Validator")
    PORTFOLIO_MANAGER = (
        "PORTFOLIO_MANAGER",
        _("Building Benchmark Report Validator"),
    )
    PDF = "PDF", _("PDF Package Validator")
    # SYSMLV2 = "SYSMLV2", _("SysMLv2 Model Validator")


class ValidatorReleaseState(TextChoices):
    """
    Release state for system validators.

    DRAFT: Validator is not shown anywhere in the UI. Used for validators
           still under development.
    COMING_SOON: Validator card is shown in the system library but cannot be
                 viewed or used. The "View" button shows "Coming soon" and
                 is disabled.
    PUBLISHED: Validator is fully available - viewable and usable in workflows.
    """

    DRAFT = "DRAFT", _("Draft")
    COMING_SOON = "COMING_SOON", _("Coming Soon")
    PUBLISHED = "PUBLISHED", _("Published")


class ValidatorAvailabilityState(TextChoices):
    """
    Runtime availability for config-managed validators.

    This is intentionally separate from release state. A validator can be
    published as a product/catalog item but unavailable in a particular process
    because its plugin package was removed or failed to register.
    """

    AVAILABLE = "AVAILABLE", _("Available")
    MISSING_CONFIG = "MISSING_CONFIG", _("Missing config")
    RETIRED = "RETIRED", _("Retired")


# 'advanced' validation types that require dedicated compute resources —
# either container-based (EnergyPlus, FMU, custom Docker containers) or
# compute-intensive services (AI via external API calls). These are
# metered separately from simple validators that run inline in the
# Django worker process.
ADVANCED_VALIDATION_TYPES = {
    ValidationType.SHACL,
    ValidationType.SCHEMATRON,
    ValidationType.ENERGYPLUS,
    ValidationType.FMU,
    ValidationType.CUSTOM_VALIDATOR,
    ValidationType.AI_ASSIST,
    ValidationType.PORTFOLIO_MANAGER,
    ValidationType.PDF,
}
# NOTE on SHACL: it is "advanced" purely for ROUTING — SHACL parses untrusted RDF
# and runs author-supplied SPARQL, which must execute inside the isolated
# container backend, not in the worker. It stays ``ComputeTier.LOW`` (see
# DEFAULT_COMPUTE_TIERS below / config.py), so it is NOT metered as heavy compute:
# cloud metering only deducts credits for ComputeTier.HIGH validators. Isolation
# is a safety upgrade, not a price change.
#
# NOTE on SCHEMATRON: same posture as SHACL — routed to the isolated
# ``validibot-validator-backend-schematron`` container because the official
# rule packs require an XSLT 2.0 engine (Saxon) executing rule-pack XSLT over
# untrusted submitted XML, which must never run in the worker process. It
# stays ``ComputeTier.LOW``: metered by launch count, not credits (see
# ADR-2026-07-01, decision D4).


class ComputeTier(models.TextChoices):
    """
    Compute intensity classification for validators.

    LOW: Lightweight operations (negligible per-run cost, metered by launch count).
    HIGH: Resource-intensive operations (metered by credit consumption).
    """

    LOW = "LOW", _("Low compute")
    HIGH = "HIGH", _("High compute")


class ValidatorWeight(models.IntegerChoices):
    """
    Credit multiplier for high-compute validators.

    Higher weight = more credits consumed per minute of runtime.
    """

    NORMAL = 1, _("Normal (1x)")
    MEDIUM = 2, _("Medium (2x)")
    HEAVY = 3, _("Heavy (3x)")
    EXTREME = 5, _("Extreme (5x)")


class ValidatorTrustTier(models.TextChoices):
    """Trust tier of the validator backend a validator dispatches to.

    Trust ADR Phase 5 Session C — first-party validator backends
    (everything that ships in the open-source repo today) ride the
    Phase 1 hardening profile because we built and audit the images.
    Once user-added or partner-authored backends become a registration
    flow, those need a stricter sandbox by default.

    The two tiers map to different `ValidatorRunner` configurations:

    - `TIER_1` keeps the existing Phase 1 defaults: UID 1000,
      cap_drop=ALL, no-new-privileges, read-only rootfs, network
      disabled, ro input mount, rw output mount, tmpfs at /tmp,
      memory/CPU/timeout limits.
    - `TIER_2` adds: explicit egress allowlist or network=none,
      tighter CPU/memory caps, gVisor or Kata runtime when
      available, cosign-signed image required, pre-flight scan on
      registration. The tier-2 profile is what the runner applies
      when it sees a `Validator.trust_tier == TIER_2` row.

    Field name vs. semantics: the column lives on `Validator`
    because that's the registered, addressable row a workflow step
    references — but its *meaning* is the trust rating of the
    **validator backend** that validator dispatches to. Simple
    validators (no backend, run inline in Django) take TIER_1 by
    construction; the field is irrelevant for them.
    """

    TIER_1 = "TIER_1", _("Tier 1 (first-party)")
    TIER_2 = "TIER_2", _("Tier 2 (user-added or partner-authored)")


# Default compute tier by validation type. LOW-compute validators are metered
# by launch count; HIGH-compute validators are metered by credit consumption.
DEFAULT_COMPUTE_TIERS: dict[str, str] = {
    ValidationType.BASIC: ComputeTier.LOW,
    ValidationType.JSON_SCHEMA: ComputeTier.LOW,
    ValidationType.XML_SCHEMA: ComputeTier.LOW,
    # Schematron is container-routed for isolation (Saxon/XSLT), not for
    # heavy compute — metered by launch count like SHACL (ADR-2026-07-01 D4).
    ValidationType.SCHEMATRON: ComputeTier.LOW,
    ValidationType.SHACL: ComputeTier.LOW,
    ValidationType.CUSTOM_VALIDATOR: ComputeTier.LOW,
    ValidationType.ENERGYPLUS: ComputeTier.HIGH,
    ValidationType.FMU: ComputeTier.HIGH,
    ValidationType.THERM: ComputeTier.HIGH,
    ValidationType.AI_ASSIST: ComputeTier.LOW,
    # Tabular is in-process and human-scale (bounded by the reader's caps),
    # so it is a low-compute validator metered by launch count.
    ValidationType.TABULAR: ComputeTier.LOW,
    # Spreadsheet/XML/ZIP parsing is isolated for safety, not heavy compute.
    ValidationType.PORTFOLIO_MANAGER: ComputeTier.LOW,
    # PDF parsing is isolated because it crosses an untrusted binary boundary.
    ValidationType.PDF: ComputeTier.LOW,
    # ValidationType.SYSMLV2: ComputeTier.LOW,
}


class FMUProbeStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")


class CustomValidatorType(TextChoices):
    SIMPLE = "SIMPLE", _("Simple")
    MODELICA = "MODELICA", _("Modelica")
    KERML = "KERML", _("KerML")


class Severity(TextChoices):
    SUCCESS = "SUCCESS", _("Success")
    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    ERROR = "ERROR", _("Error")


class ValidationRunSource(TextChoices):
    """How a validation run was launched.

    This value MUST be derived from the authenticated route / auth
    channel — never from a caller-controlled header.  Each value
    corresponds to a distinct launch path with a distinct trust
    profile, and the evidence manifest needs to distinguish them.
    """

    LAUNCH_PAGE = "LAUNCH_PAGE", _("Launch Page")
    API = "API", _("API")
    MCP = "MCP", _("MCP (AI Agent)")
    # Anonymous x402-paid agent — distinct from MCP because x402 has
    # no Validibot-side authenticated identity. Run is anonymous,
    # bound only to a payment receipt.
    X402_AGENT = "X402_AGENT", _("x402 Anonymous Agent")
    # CLI (validibot-cli) — distinct from raw API because the CLI
    # has its own user-agent and may have different conventions
    # (e.g. interactive auth).
    CLI = "CLI", _("Command-line Interface")
    # Scheduled / cron-driven launch from internal automation.
    SCHEDULE = "SCHEDULE", _("Scheduled / Automation")


class XMLSchemaType(TextChoices):
    DTD = "DTD", _("Document Type Definition (DTD)")
    XSD = "XSD", _("XML Schema Definition (XSD)")
    RELAXNG = "RELAXNG", _("Relax NG (RNG)")


class JSONSchemaVersion(TextChoices):
    DRAFT_2020_12 = "2020-12", _("Draft 2020-12")
    DRAFT_2019_09 = "2019-09", _("Draft 2019-09")
    DRAFT_07 = "draft-07", _("Draft 7")
    DRAFT_06 = "draft-06", _("Draft 6")
    DRAFT_04 = "draft-04", _("Draft 4")


class CatalogEntryType(TextChoices):
    IO_DEFINITION = "io_definition", _("Step I/O Definition")
    DERIVATION = "derivation", _("Derivation")


class CatalogRunStage(TextChoices):
    INPUT = "input", _("Input")
    OUTPUT = "output", _("Output")


class CatalogValueType(TextChoices):
    NUMBER = "number", _("Number")
    TIMESERIES = "timeseries", _("Timeseries")
    STRING = "string", _("String")
    BOOLEAN = "boolean", _("Boolean")
    OBJECT = "object", _("Object")
    ARTIFACT_REF = "artifact_ref", _("Artifact reference")


class StepIOMedium(TextChoices):
    VALUE = "value", _("Value")
    ARTIFACT = "artifact", _("Artifact")


class EnvelopeChannel(TextChoices):
    INPUT_FILES = "input_files", _("Input files")
    RESOURCE_FILES = "resource_files", _("Resource files")
    OUTPUT_ARTIFACTS = "output_artifacts", _("Output artifacts")


class DefaultSourceStrategy(TextChoices):
    SUBMITTED_FILE_FIRST = "submitted_file_first", _("Submitted file first")
    SUBMITTED_FILE_THEN_DEFAULT_RESOURCE = (
        "submitted_file_then_default_resource",
        _("Submitted file then default resource"),
    )
    WORKFLOW_RESOURCE_DEFAULT = (
        "workflow_resource_default",
        _("Workflow resource default"),
    )
    UPSTREAM_ARTIFACT_SUGGESTION = (
        "upstream_artifact_suggestion",
        _("Upstream artifact suggestion"),
    )
    MANUAL = "manual", _("Manual")
    NONE = "none", _("None")


class ArtifactKind(TextChoices):
    FILE = "file", _("File")
    DIRECTORY = "directory", _("Directory")
    ARCHIVE = "archive", _("Archive")
    DATASET = "dataset", _("Dataset")
    REPORT = "report", _("Report")
    LOG = "log", _("Log")
    OTHER = "other", _("Other")


class StepIODirection(TextChoices):
    INPUT = "input", _("Input")
    OUTPUT = "output", _("Output")


class StepIOOriginKind(TextChoices):
    CATALOG = "catalog", _("Catalog")
    FMU = "fmu", _("FMU")
    TEMPLATE = "template", _("Template")


class StepIOSourceKind(TextChoices):
    PAYLOAD_PATH = "payload_path", _("Payload Path")
    INTERNAL = "internal", _("Internal")


class BindingSourceScope(TextChoices):
    SUBMISSION_PAYLOAD = "submission_payload", _("Submission Payload")
    SUBMISSION_METADATA = "submission_metadata", _("Submission Metadata")
    SUBMISSION_FILE = "submission_file", _("Submission File")
    UPSTREAM_STEP = "upstream_step", _("Upstream Step")
    UPSTREAM_ARTIFACT = "upstream_artifact", _("Upstream Step Artifact")
    SIGNAL = "signal", _("Workflow Signal")
    CONSTANT = "constant", _("Workflow Constant")
    WORKFLOW_RESOURCE = "workflow_resource", _("Workflow Resource")
    SYSTEM = "system", _("System")


class AssertionType(TextChoices):
    SHACL = "shacl", _("SHACL")
    BASIC = "basic", _("Basic Assertion")
    CEL_EXPRESSION = "cel_expr", _("CEL expression")


class ValidatorRuleType(TextChoices):
    CEL_EXPRESSION = "cel_expr", _("CEL expression")


class AssertionOperator(TextChoices):
    # SHACL-specific assertion execution. Stored as a RulesetAssertion row
    # but evaluated by SHACLValidator after pySHACL has produced the report graph.
    SPARQL_ASK = "sparql_ask", _("SPARQL ASK")

    # Comparisons (numeric/text/temporal where applicable)
    EQ = "eq", _("Equals")
    NE = "ne", _("Not equals")
    LT = "lt", _("Less than")
    LE = "le", _("Less than or equal")  # alias for THRESHOLD_MAX UI copy
    GT = "gt", _("Greater than")
    GE = "ge", _("Greater than or equal")  # alias for THRESHOLD_MIN UI copy
    BETWEEN = "between", _("Between (range)")

    # Membership / set relations
    IN = "in", _("Is one of")
    NOT_IN = "not_in", _("Is not one of")
    SUBSET = "subset", _("Set is subset of")
    SUPERSET = "superset", _("Set is superset of")
    UNIQUE = "unique", _("All values unique")

    # String / pattern
    CONTAINS = "contains", _("Contains")
    NOT_CONTAINS = "not_contains", _("Does not contain")
    STARTS_WITH = "starts_with", _("Starts with")
    ENDS_WITH = "ends_with", _("Ends with")
    MATCHES = "matches", _("Matches regex")

    # Null/emptiness/type
    IS_NULL = "is_null", _("Is null")
    NOT_NULL = "not_null", _("Is not null")
    IS_EMPTY = "is_empty", _("Is empty")
    NOT_EMPTY = "not_empty", _("Is not empty")
    TYPE_IS = "type_is", _("Type is")

    # Length / cardinality
    LEN_EQ = "len_eq", _("Length equals")
    LEN_LE = "len_le", _("Length ≤")
    LEN_GE = "len_ge", _("Length ≥")
    COUNT_BETWEEN = "count_between", _("Count between")

    # Temporal
    BEFORE = "before", _("Before")
    AFTER = "after", _("After")
    WITHIN = "within", _("Within duration")

    # Numeric tolerance / approx
    APPROX_EQ = "approx_eq", _("≈ Equals (tolerance)")

    # Collection quantifiers
    ANY = "any", _("Any element satisfies")
    ALL = "all", _("All elements satisfy")
    NONE = "none", _("No element satisfies")
    CEL_EXPR = "cel_expr", _("CEL expression")


class ResourceFileType(TextChoices):
    """
    Types of resource files that can be attached to validators.

    Resource files are auxiliary files needed by advanced validators to run.
    Each type is specific to a validator and its requirements.

    Currently supported:
    - ENERGYPLUS_WEATHER: EPW weather files for EnergyPlus simulations

    Future types might include:
    - FMU_LIBRARY: Shared libraries for FMU validators
    - CONFIG: Configuration files
    """

    ENERGYPLUS_WEATHER = "energyplus_weather", _("EnergyPlus Weather File (EPW)")


# Step-owned resource type constants.
# These are NOT members of ResourceFileType (which is for catalog
# ValidatorResourceFile types).  They are plain string constants used as the
# ``resource_type`` value on ``WorkflowStepResource`` rows for step-owned
# files that don't belong in the shared catalog.

ENERGYPLUS_MODEL_TEMPLATE = "energyplus_model_template"
# Resource type for a parameterized IDF template uploaded by a workflow
# author.  Used on ``WorkflowStepResource`` rows with
# ``role=MODEL_TEMPLATE``.

FMU_MODEL_RESOURCE = "fmu"
# Resource type for a step-owned FMU uploaded by a workflow author. Used on
# ``WorkflowStepResource`` rows with ``role=FMU_MODEL``.

PORTFOLIO_MANAGER_EBL_RESOURCE = "portfolio_manager_ebl_v1"
PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES = 500_000_000
# Versioned JSON Expected Buildings List uploaded for one Portfolio Manager
# workflow step.


# ---------------------------------------------------------------------------
# Resource file type configuration registry
# ---------------------------------------------------------------------------


def _validate_epw_header(raw: bytes) -> bool:
    """EPW weather files must start with 'LOCATION,'."""
    return raw[:9] == b"LOCATION,"


@dataclass(frozen=True)
class ResourceTypeConfig:
    """
    Declarative configuration for a resource file type.

    Each ResourceFileType maps to one of these configs. Adding a new resource
    type (e.g., FMU libraries) requires only adding a new entry here -- no
    form or view changes needed.
    """

    allowed_extensions: frozenset[str]
    max_size_bytes: int
    header_validator: Callable[[bytes], bool] | None = None
    description: str = ""


_RESOURCE_TYPE_CONFIGS: dict[str, ResourceTypeConfig] = {
    ResourceFileType.ENERGYPLUS_WEATHER: ResourceTypeConfig(
        allowed_extensions=frozenset({"epw"}),
        max_size_bytes=15 * 1024 * 1024,  # 15 MB
        header_validator=_validate_epw_header,
        description="EnergyPlus Weather File (EPW)",
    ),
}


def get_resource_type_config(resource_type: str) -> ResourceTypeConfig | None:
    """Look up the validation config for a resource file type."""
    return _RESOURCE_TYPE_CONFIGS.get(resource_type)


def get_resource_types_for_validator(validation_type: str) -> list[str]:
    """Return the resource file types supported by a validation type.

    Reads from the config registry. Returns an empty list if no config
    is registered or the validator doesn't use resource files.
    """
    from validibot.validations.validators.base.config import get_config

    cfg = get_config(validation_type)
    if cfg:
        return list(cfg.resource_types)
    return []


# CEL evaluation limits (adjust as needed)
# Timeout can be overridden via settings.CEL_MAX_EVAL_TIMEOUT_MS for tests.
# Default 500ms accounts for first-evaluation compilation overhead and
# large output contexts (e.g., FMU simulation results with many variables).
CEL_MAX_EVAL_TIMEOUT_MS = getattr(django_settings, "CEL_MAX_EVAL_TIMEOUT_MS", 2000)
CEL_MAX_EXPRESSION_CHARS = 2000

# Top-level variable-namespace bound. An expression can reference at
# most this many distinct top-level names (``p``, ``s``, ``output`` ...).
# Independent of — and complementary to — the deep bounds below.
CEL_MAX_CONTEXT_SYMBOLS = 200

# Maximum nesting depth of the CEL evaluation context. Mirrors the
# ``DEFAULT_MAX_DEPTH`` discipline in ``xml_utils.py``: a maliciously
# nested payload (5 MB of recursive JSON, say) balloons CPU and memory
# inside ``celpy.json_to_cel()`` during normalization, even though the
# top-level symbol count is tiny. Real CEL contexts are namespace-style
# (``s.price``, ``p.foo.bar``) and rarely exceed 5 levels; 32 is ~10x
# the realistic maximum and well below Python's recursion limit.
CEL_MAX_CONTEXT_DEPTH = 32

# Maximum total symbol count (dict keys + list items) across the entire
# context tree. Complements ``CEL_MAX_CONTEXT_SYMBOLS`` (top-level only)
# with a bounded-work guarantee for the normalization step — a context
# with one top-level key holding a 100k-entry nested structure is
# rejected before ``json_to_cel`` is called.
CEL_MAX_CONTEXT_TOTAL_SYMBOLS = 10_000

# Maximum nesting depth of CEL macros within a single expression.
# Macros (``all``, ``exists``, ``map``, ``filter``, ...) are the only
# avenue for exponential evaluation time in CEL — the cel-spec itself
# calls this out. An expression like
# ``items.all(a, items.all(b, items.all(c, ...)))`` is O(|items|^N)
# where N is the nesting depth, so even a 230-char expression with
# five levels and lists of ten is 10^5 evaluations.
#
# Two levels accommodates the common real-world intent
# (``items.all(i, i.tags.all(t, ...))``) and rejects the exponential
# pathology. Mirrors cel-go's ``ValidateComprehensionNestingLimit``.
# Chained macros (``items.all(...).filter(...)``) are additive, not
# nested, and are not counted by this limit.
CEL_MAX_MACRO_NESTING = 2

# Maximum total number of CEL macro calls anywhere in one expression.
# Chained ``.map(...).map(...).map(...)`` past five stages is never
# legitimate business logic — it is either a mistake or an attacker
# probing for cost amplification. Sits alongside CEL_MAX_MACRO_NESTING
# so that neither dimension can be exploited in isolation.
CEL_MAX_MACRO_COUNT = 5

# Regex evaluation timeout (milliseconds). Prevents ReDoS from pathological patterns.
REGEX_EVAL_TIMEOUT_MS = getattr(django_settings, "REGEX_EVAL_TIMEOUT_MS", 1000)

# Maximum number of JSONPath filter segments ([?...]) allowed in a single
# path expression. Each filter iterates an array, so chaining N filters on
# nested arrays has O(n^N) worst-case complexity. The motivating use case
# (SysML v2 named-element resolution) typically uses 1-2 filters.
MAX_JSONPATH_FILTER_SEGMENTS = 4

# Maximum length of an assertion's author-facing ``notes`` (rationale) field.
# A generous cap for prose that still bounds the storage/DoS surface of a
# free-text input. On a Postgres ``text`` column this is enforced at the
# validation layer (model MaxLengthValidator + form field), not by the DB.
RULESET_ASSERTION_NOTES_MAX_LENGTH = 5000

# Maximum length of ``ValidationRun.short_description`` — the submitter-set run
# description surfaced to assertions as ``submission.short_description``. The
# DB column is ``varchar`` at this width, and the launch path creates the run
# via ``objects.create(**extra)`` (no ``full_clean``), so this cap MUST be
# enforced at every input layer (the model field, the web launch form, and the
# API serializer) — otherwise an over-long value reaches the DB as a 500
# instead of a clean validation error. Single source so the three can't drift.
VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH = 255
