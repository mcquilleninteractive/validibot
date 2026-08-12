from pathlib import Path

from django.db import models
from django.utils.translation import gettext_lazy as _

from validibot.submissions.constants import SubmissionFileType

WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY = "workflow_launch_input_mode"
WORKFLOW_LIST_LAYOUT_SESSION_KEY = "workflow_list_layout"
WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY = "workflow_list_show_archived"


class WorkflowListLayout(models.TextChoices):
    GRID = "grid", _("Grid")
    TABLE = "table", _("Table")


class WorkflowHistoryPolicy(models.TextChoices):
    VERSIONED = "versioned", _("Versioned history")
    MUTABLE = "mutable", _("Mutable history")


class WorkflowConstantType(models.TextChoices):
    """Explicit value types for a workflow ``Constant`` (``c.*``).

    Per ADR-2026-06-18 the author always commits a type (no "Auto"): the
    chosen type coerces and validates the entered value at save time, so
    the constant's contract is guaranteed before any run. ``NUMBER`` is
    stored as a canonical decimal *string* (parsed with ``Decimal``) to
    preserve exact value and precision through the digest, manifest, and
    credential — CEL itself has no decimal type, so the value is coerced
    to ``double`` only at evaluation time.
    """

    STRING = "STRING", _("String")
    NUMBER = "NUMBER", _("Number")
    BOOLEAN = "BOOLEAN", _("Boolean")
    LIST = "LIST", _("List")
    OBJECT = "OBJECT", _("Object")


class AccessScope(models.TextChoices):
    ORG_ALL = "ORG_ALL", _("All members of the workflow's organization")
    RESTRICTED = "RESTRICTED", _("Restricted to allowed users and/or roles")


class FailPolicy(models.TextChoices):
    CONTINUE = "CONTINUE", _("Continue on failure")
    FAIL_FAST = "FAIL_FAST", _("Fail fast")


class TriggerType(models.TextChoices):
    MANUAL = "MANUAL", _("Manual")
    API = "API", _("API")
    SCHEDULE = "SCHEDULE", _("Schedule")
    GITHUB_APP = "GITHUB_APP", _("GitHub App")


class AgentBillingMode(models.TextChoices):
    """Billing mode for agent access to a workflow.

    AUTHOR_PAYS means the workflow author's plan quota covers agent
    usage.  This is the default and is appropriate for authenticated
    agents that connect with the author's own API key.

    AGENT_PAYS_X402 means anonymous agents pay per call via x402
    micropayments (USDC on Base).  Requires a non-zero
    ``agent_price_cents``.
    """

    AUTHOR_PAYS = "AUTHOR_PAYS", _("Author pays (plan quota)")
    AGENT_PAYS_X402 = "AGENT_PAYS_X402", _("Agent pays via x402 micropayment")


class WorkflowVisibility(models.TextChoices):
    """Who, by Validibot identity, may run a workflow for free.

    This is the workflow's *identity-scoped* audience, applied uniformly
    to the web UI and to authenticated MCP agents (an agent acts on
    behalf of its user, so it inherits the user's identity). It is
    INDEPENDENT of x402 paid access (``Workflow.x402_enabled``), which
    exposes the workflow to anonymous payers regardless of this setting.

    Distinct from ``AccessScope`` (ORG_ALL vs RESTRICTED), which narrows
    *within* the ORG tier to specific members/roles. Visibility is the
    outer dial; AccessScope refines the ORG rung.

    Ordered from most to least restrictive: PRIVATE < ORG < ALL_USERS.
    The order enforces the org-level cap — a workflow's visibility may
    not exceed its organization's ``workflow_visibility`` ceiling.
    """

    PRIVATE = "PRIVATE", _("Private — only you and people you invite")
    ORG = "ORG", _("Your organization")
    ALL_USERS = "ALL_USERS", _("All Validibot users")


# Visibility ordered most-restrictive first. A workflow's visibility is
# permitted only when its rank is <= the org ceiling's rank. Centralised
# here so the model, forms, and resolvers share one definition of "wider".
WORKFLOW_VISIBILITY_ORDER = (
    WorkflowVisibility.PRIVATE,
    WorkflowVisibility.ORG,
    WorkflowVisibility.ALL_USERS,
)


def visibility_rank(value: str) -> int:
    """Return restrictiveness rank of a visibility value (0 = most restrictive)."""

    return WORKFLOW_VISIBILITY_ORDER.index(WorkflowVisibility(value))


def visibility_within_cap(value: str, cap: str) -> bool:
    """Return True if ``value`` is no wider than ``cap`` (org guardrail)."""

    return visibility_rank(value) <= visibility_rank(cap)


class WorkflowStartErrorCode(models.TextChoices):
    NO_WORKFLOW_STEPS = "NO_WORKFLOW_STEPS", _("Workflow has no steps to execute")
    WORKFLOW_INACTIVE = "WORKFLOW_INACTIVE", _("Workflow is inactive")
    INVALID_PAYLOAD = "INVALID_PAYLOAD", _("Invalid request payload")
    FILE_TYPE_UNSUPPORTED = (
        "FILE_TYPE_UNSUPPORTED",
        _("Workflow cannot accept the submitted file type"),
    )
    VALIDATOR_UNAVAILABLE = (
        "VALIDATOR_UNAVAILABLE",
        _("A workflow validator is unavailable"),
    )
    PERMISSION_DENIED = (
        "PERMISSION_DENIED",
        _("You do not have permission to run this workflow"),
    )
    ORG_POLICY_DENIED = (
        "ORG_POLICY_DENIED",
        _("Your organization can't run this workflow right now"),
    )


SUPPORTED_CONTENT_TYPES = {
    "application/json": SubmissionFileType.JSON,
    "application/xml": SubmissionFileType.XML,
    "text/plain": SubmissionFileType.TEXT,
    "text/x-idf": SubmissionFileType.TEXT,
    "text/yaml": SubmissionFileType.YAML,
    "application/yaml": SubmissionFileType.YAML,
    "application/pdf": SubmissionFileType.PDF,
    "application/octet-stream": SubmissionFileType.BINARY,
    # RDF serializations used by the SHACL validator. Turtle is the
    # most common; the others cover JSON-LD, RDF/XML, N-Triples, and
    # N-Quads so the API accepts whichever serialization a submitter's
    # HTTP client sets as the Content-Type. Mapped to the closest
    # underlying SubmissionFileType so existing storage paths work
    # without bespoke RDF file-type machinery.
    "text/turtle": SubmissionFileType.TEXT,
    "application/n-triples": SubmissionFileType.TEXT,
    "application/n-quads": SubmissionFileType.TEXT,
    "application/ld+json": SubmissionFileType.JSON,
    "application/rdf+xml": SubmissionFileType.XML,
}

DEFAULT_CONTENT_TYPE_BY_FILE_TYPE = {
    SubmissionFileType.JSON: "application/json",
    SubmissionFileType.XML: "application/xml",
    SubmissionFileType.TEXT: "text/plain",
    SubmissionFileType.YAML: "text/yaml",
    SubmissionFileType.PDF: "application/pdf",
    SubmissionFileType.BINARY: "application/octet-stream",
}

_SPECIAL_EXT_CONTENT_TYPES: dict[str, dict[str, str]] = {
    SubmissionFileType.TEXT: {
        ".idf": "text/x-idf",
        ".ttl": "text/turtle",
        ".nt": "application/n-triples",
        ".nq": "application/n-quads",
    },
    SubmissionFileType.YAML: {
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    },
    SubmissionFileType.JSON: {
        ".jsonld": "application/ld+json",
    },
    SubmissionFileType.XML: {
        ".rdf": "application/rdf+xml",
    },
}


def preferred_content_type_for_file(
    file_type: str,
    *,
    filename: str | None = None,
) -> str:
    """
    Return the most appropriate MIME type for a logical submission file type.

    Some logical types (like TEXT) map to more specific MIME values based on
    filename extension—for example, IDF uploads should stay ``text/x-idf`` so we
    preserve ``.idf`` extensions on sanitized filenames.
    """

    if filename:
        ext = Path(filename).suffix.lower()
        mapping = _SPECIAL_EXT_CONTENT_TYPES.get(file_type)
        if mapping:
            hint = mapping.get(ext)
            if hint:
                return hint

    return DEFAULT_CONTENT_TYPE_BY_FILE_TYPE.get(
        file_type,
        DEFAULT_CONTENT_TYPE_BY_FILE_TYPE[SubmissionFileType.TEXT],
    )
