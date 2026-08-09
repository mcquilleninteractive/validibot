"""
Ensure StepInputBinding rows exist for all validator-owned step
inputs on a workflow step.

Called after step creation/update so that the step input resolution
engine and envelope builder have bindings to work with. Without
bindings, launch fails closed for validators that declare external
inputs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from validibot.validations.constants import FMU_MODEL_RESOURCE
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.services.catalog_entry_normalization import (
    build_step_binding_defaults_from_mapping,
)
from validibot.validations.validators.base.config import get_config

if TYPE_CHECKING:
    from validibot.validations.validators.base.config import CatalogEntrySpec
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def ensure_step_input_bindings(step: WorkflowStep) -> int:
    """Create missing bindings under the owning workflow's definition lock."""

    from validibot.workflows.services.editing_policy import (
        guard_workflow_definition_mutation,
    )

    with guard_workflow_definition_mutation(step.workflow_id):
        return _ensure_step_input_bindings(step)


def _ensure_step_input_bindings(step: WorkflowStep) -> int:
    """Create default StepInputBinding rows for validator-owned input definitions.

    For each input StepIODefinition owned by the step's validator that
    doesn't already have a binding on this step, creates a binding with:

    - source_scope/source_data_path: derived from the owning validator's
      catalog entry config when available
    - is_required/default_value: copied from the validator config defaults
      when declared
    - fallback defaults: submission payload + input native name

    Returns the number of bindings created.
    """
    if not step.validator_id:
        return 0

    # Find all CATALOG-origin input definitions owned by this validator.
    # We only handle CATALOG definitions here — FMU and TEMPLATE definitions
    # are managed by their own dedicated sync functions.
    validator_input_values = StepIODefinition.objects.filter(
        validator_id=step.validator_id,
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
    )

    if not validator_input_values.exists():
        return 0

    # Find which definitions already have a binding on this step so we
    # don't overwrite any author-customised bindings.
    existing_io_definition_ids = set(
        StepInputBinding.objects.filter(
            workflow_step=step,
            io_definition__in=validator_input_values,
        ).values_list("io_definition_id", flat=True)
    )

    created = 0
    catalog_entries = _build_validator_catalog_entry_map(step)
    for io_definition in validator_input_values:
        if io_definition.pk in existing_io_definition_ids:
            continue

        entry = catalog_entries.get(
            (io_definition.contract_key, io_definition.direction)
        )

        # ── P1-1 fix: skip parser-sourced step inputs ──────────────
        # Per ADR-2026-05-22, parser-extracted step inputs (catalog
        # entries with binding_config={"source": "parser", ...}) are
        # populated by the validator's extract_input_values() at
        # runtime — no author-supplied payload path is involved. Creating
        # a StepInputBinding row for these would either dispatch-fail
        # (binding can't resolve a path that doesn't exist) or surface
        # them in the UI as user-mappable required inputs (wrong — the
        # validator owns these values). Skip them entirely.
        if entry and (entry.binding_config or {}).get("source") == "parser":
            continue

        if io_definition.io_medium == StepIOMedium.ARTIFACT:
            defaults = _build_artifact_binding_defaults(io_definition, step=step)
        elif entry:
            fallback_path = ""
            defaults = build_step_binding_defaults_from_mapping(
                entry.binding_config,
                fallback_path=fallback_path,
                default_required=entry.is_required,
            )
        else:
            fallback_path = ""
            defaults = build_step_binding_defaults_from_mapping(
                io_definition.provider_binding,
                fallback_path=fallback_path,
                default_required=True,
            )

        StepInputBinding.objects.create(
            workflow_step=step,
            io_definition=io_definition,
            source_scope=defaults["source_scope"],
            source_data_path=defaults["source_data_path"],
            is_required=defaults["is_required"],
            default_value=defaults["default_value"],
        )
        created += 1

    if created:
        logger.info(
            "Created %d default input binding(s) for step %s (validator %s)",
            created,
            step.pk,
            step.validator_id,
        )

    return created


def _build_validator_catalog_entry_map(
    step: WorkflowStep,
) -> dict[tuple[str, str], CatalogEntrySpec]:
    """Index the system validator's catalog entries by ``(slug, run_stage)``.

    Validator-owned input definitions should derive their default step binding
    values from the current validator config, not from
    ``StepIODefinition.provider_binding``. The config remains the source
    of truth for library step-input defaults such as submission metadata
    paths and required/optional semantics.
    """
    validator = step.validator
    if not validator or not validator.is_system:
        return {}

    cfg = get_config(validator.validation_type)
    if not cfg or cfg.slug != validator.slug:
        return {}

    return {
        (entry.slug, entry.run_stage): entry
        for entry in cfg.catalog_entries
        if entry.entry_type == "io_definition"
    }


def _build_artifact_binding_defaults(
    io_definition: StepIODefinition,
    *,
    step: WorkflowStep,
) -> dict[str, object]:
    """Return default binding values for a declared artifact input port."""

    if io_definition.contract_key == "fmu_model":
        from validibot.workflows.models import WorkflowStepResource

        has_step_fmu = step.step_resources.filter(
            role=WorkflowStepResource.FMU_MODEL,
        ).exists()
        if has_step_fmu or not getattr(io_definition.validator, "fmu_model_id", None):
            source_scope = BindingSourceScope.WORKFLOW_RESOURCE
            source_data_path = io_definition.resource_type or FMU_MODEL_RESOURCE
        else:
            source_scope = BindingSourceScope.SYSTEM
            source_data_path = io_definition.contract_key
    elif io_definition.resource_type:
        source_scope = BindingSourceScope.WORKFLOW_RESOURCE
        source_data_path = io_definition.resource_type
    else:
        source_scope = BindingSourceScope.SUBMISSION_FILE
        source_data_path = "primary"

    return {
        "source_scope": source_scope,
        "source_data_path": source_data_path,
        "default_value": None,
        "is_required": io_definition.min_items > 0,
    }
