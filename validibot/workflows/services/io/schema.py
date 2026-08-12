"""Shared constants for the workflow definition (``workflow.json``) schema.

One place names the format version and the exact field sets the exporter writes
and the importer reads, so the two halves can never drift on *which* fields make
up the definition. The values themselves live in the live models; this module
only enumerates the contract.
"""

from __future__ import annotations

# Bumped when the on-disk shape of ``workflow.json`` changes incompatibly. The
# importer refuses a definition whose ``format_version`` it doesn't understand.
FORMAT_VERSION = 2

# Workflow contract fields copied verbatim (the same set
# ``WorkflowVersioningService.clone`` treats as the workflow contract), minus
# identity/ownership/lifecycle which the importer mints or rebinds.
#
# Deliberately EXCLUDED — external-exposure toggles are org-specific decisions
# that must never travel in an import, or a public/agent-exposed export (or a
# crafted JSON) would auto-publish itself in the target org:
# ``workflow_visibility``, ``make_info_page_public``, ``mcp_enabled``,
# ``x402_enabled``. The importer forces these to the locked state regardless
# (see ``importer._create_workflow``), so they are also belt-and-suspenders
# against a hand-edited definition.
WORKFLOW_SCALAR_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "history_policy",
    "allow_submission_name",
    "allow_submission_meta_data",
    "allow_submission_short_description",
    "input_retention",
    "output_retention",
    "success_message",
    "input_schema_source_mode",
    "input_schema_source_text",
    "agent_billing_mode",
    "agent_price_cents",
    "agent_max_launches_per_hour",
)

# WorkflowStep scalar fields (validator/action/ruleset FKs handled separately).
# The two JSON config buckets — ``config`` (semantic) and ``display_settings``
# (cosmetic), per ADR-2026-06-18 — are ALSO handled separately (deep-copied) in
# the exporter/importer, not listed here, so both round-trip through VAF.
STEP_SCALAR_FIELDS: tuple[str, ...] = (
    "order",
    "step_key",
    "name",
    "description",
    "notes",
    "display_schema",
    "show_success_messages",
)

# StepIODefinition fields owned by a workflow step that round-trip verbatim.
STEP_IO_DEFINITION_FIELDS: tuple[str, ...] = (
    "contract_key",
    "native_name",
    "label",
    "description",
    "direction",
    "data_type",
    "io_medium",
    "artifact_kind",
    "media_type",
    "data_format",
    "default_source_strategy",
    "envelope_channel",
    "resource_type",
    "role",
    "is_collection",
    "min_items",
    "max_items",
    "origin_kind",
    "source_kind",
    "is_path_editable",
    "order",
    "is_hidden",
    "unit",
    "promoted_signal_name",
)
STEP_IO_DEFINITION_JSON_DICT_FIELDS: tuple[str, ...] = (
    "provider_binding",
    "metadata",
)
STEP_IO_DEFINITION_JSON_LIST_FIELDS: tuple[str, ...] = (
    "accepted_data_formats",
    "accepted_extensions",
    "accepted_file_types",
    "accepted_media_types",
    "allowed_source_scopes",
)
STEP_IO_DEFINITION_JSON_FIELDS: tuple[str, ...] = (
    *STEP_IO_DEFINITION_JSON_DICT_FIELDS,
    *STEP_IO_DEFINITION_JSON_LIST_FIELDS,
)

# StepInputBinding fields (the step I/O definition reference is handled separately).
INPUT_BINDING_FIELDS: tuple[str, ...] = (
    "source_scope",
    "source_data_path",
    "is_required",
)

# Derivation (step-owned) fields.
DERIVATION_FIELDS: tuple[str, ...] = (
    "contract_key",
    "expression",
    "description",
    "label",
    "data_type",
    "order",
    "is_hidden",
    "unit",
)

# WorkflowSignalMapping fields.
SIGNAL_MAPPING_FIELDS: tuple[str, ...] = (
    "name",
    "source_path",
    "on_missing",
    "data_type",
    "position",
)

# Workflow Constants (c.* namespace, ADR-2026-06-18). ``value`` is handled
# separately (deep-copied) like signal mappings' ``default_value`` because it
# may hold structured JSON. A portable workflow must keep the thresholds its
# assertions depend on, so constants round-trip through VAF.
CONSTANT_FIELDS: tuple[str, ...] = (
    "name",
    "data_type",
    "description",
    "position",
)
