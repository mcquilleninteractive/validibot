"""WorkflowVersioningService — the single decision point for workflow version cloning.

ADR-2026-04-27 Phase 3: workflow versioning was historically a single
``Workflow.clone_to_new_version()`` method that copied the workflow,
its steps, and its step resources — but missed
``WorkflowPublicInfo``, ``WorkflowRoleAccess``, and
``WorkflowSignalMapping``. The result: a "new version" silently
inherited some pre-clone state from the old version, which violates
the trust-boundary principle that a workflow with a run cannot
silently change its launch contract.

This service replaces the inline clone logic with a structured
service that:

1. Copies the **complete** workflow contract — every related object
   that defines what a launched run was operating under.
2. Returns a structured :class:`CloneReport` so callers (and tests)
   can verify *what* got copied and *how many* of each component.
3. Centralises the policy decision of "what counts as the contract"
   in one place. Adding a new related object becomes a single edit
   here, not a four-place edit across model, form, serializer, and
   admin.

What's in scope vs. out of scope
================================

In scope (this module):

- Workflow row fields (every contract field listed in
  :data:`CONTRACT_FIELDS`)
- :class:`WorkflowStep` rows including the ``config`` JSONField
- :class:`WorkflowStepResource` rows (both catalog references and
  step-owned files)
- Step-level rulesets and their assertions
- Step-owned I/O definitions, input bindings, and derivations
- :class:`WorkflowPublicInfo` (the public-facing info page)
- :class:`WorkflowRoleAccess` (per-role permission grants)
- :class:`WorkflowSignalMapping` (workflow-level named values)

Out of scope (deferred to later Phase 3 sessions):

- **Validator semantic digest** (Phase 3 Session B, tasks 7–9). The
  cloned step references the same ``Validator`` row; we don't yet
  capture a snapshot of the validator's behavior at clone time.

Resource-file boundaries are in scope: step-owned files are copied to
new ``WorkflowStepResource`` rows, and catalog resources stay shared by
reference only because ``ValidatorResourceFile`` enforces content-hash
drift checks for versioned workflows that have runs or are locked.

The structured report's ``warnings`` field surfaces these gaps to the
caller so operators using the API today know what their "new version"
does and does not yet protect against.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.contrib.auth.models import AbstractBaseUser

    from validibot.workflows.models import Workflow

logger = logging.getLogger(__name__)


# Fields that constitute the "validation contract" — the rules that
# define what a previously-launched run actually validated. Editing any
# of these on a workflow that has runs (or is locked) MAY require a new
# version depending on the *direction* of the edit. Accepting more file
# types and shortening retention are safe in place. Removing a file type
# changes the execution contract, while extending retention expands the
# privacy scope authors previously published. The direction logic lives in
# ``CONTRACT_FIELD_SAFETY`` below.
#
# What is NOT a contract field:
#   - name, description (cosmetic)
#   - is_active, is_archived, is_tombstoned (lifecycle flags;
#     editing them changes which workflows are *visible*, not the
#     rules of any past run)
#   - is_locked (a marker, not a contract field)
#   - slug, version (identifiers; changing requires care but is a
#     separate concern)
#   - make_info_page_public, featured_image (presentation)
#   - agent_* fields (commercial/access settings, not validation
#     behaviour — a past run's outcome doesn't depend on the current
#     agent price, billing mode, rate limit, or discovery flag. These
#     can change in place without touching reproducibility. The form
#     layer separately gates agent_* fields to superusers because
#     they're commercial settings, but that's an access-control
#     concern, not a contract-immutability concern.)
CONTRACT_FIELDS = frozenset(
    {
        "allowed_file_types",
        "input_retention",
        "input_schema",
        "output_retention",
    },
)


# Per-field safety classifiers for in-place edits on workflows that
# already have runs (or are locked). Each classifier answers a single
# question: "is this proposed change safe to apply in place, or does
# it invalidate past runs or expand the privacy scope?". Safe changes
# (file-type widening, retention shortening) pass through the form gate;
# unsafe changes (file-type narrowing, retention extension) are
# blocked unless the user is a superuser.
#
# The classifier functions take ``(current, proposed)`` and return
# True for "safe in place", False for "blocked unless superuser".
# Returning True for a value that's actually identical to current is
# harmless — the upstream ``changed_contract_fields`` already filters
# unchanged values before the safety check runs.


def _set_widening_safe(current: Any, proposed: Any) -> bool:
    """Set-valued change is safe iff every current item is still allowed.

    For ``allowed_file_types``: adding new types is fine (past runs
    accepted what they accepted; future runs accept more). Removing
    a type breaks reproducibility for past runs that used it.
    """
    current_set = set(current or [])
    proposed_set = set(proposed or [])
    return current_set.issubset(proposed_set)


def _retention_shortening_safe(current: Any, proposed: Any) -> bool:
    """Retention change is safe iff it keeps data no longer than before.

    Delegates to the existing day-count maps in ``submissions.constants``
    so this safety check stays in sync with the enums automatically. The
    maps express retention in days; ``None`` means "store permanently"
    (the strongest extension). The comparison uses a sortable rank so
    permanently > any-finite-days > 0-days (DO_NOT_STORE).

    Run rows snapshot their retention policy, so shortening the workflow
    setting only reduces storage for future runs and is privacy-safe.
    Extending retention increases the privacy scope authors previously
    published and therefore requires an explicit new workflow version.
    """
    return _retention_rank(proposed) <= _retention_rank(current)


def _retention_rank(value: Any) -> int:
    """Numeric rank where bigger = longer retention.

    Walks the submission-retention and output-retention day-count maps
    so this helper handles both ``input_retention`` and
    ``output_retention`` fields uniformly. Permanent retention sorts
    above any finite-day value; finite days sort by day count;
    unknown values sort below zero so any real retention beats them.
    """
    from validibot.submissions.constants import OUTPUT_RETENTION_DAYS
    from validibot.submissions.constants import SUBMISSION_RETENTION_DAYS

    if value is None:
        return -1
    for table in (SUBMISSION_RETENTION_DAYS, OUTPUT_RETENTION_DAYS):
        if value in table:
            days = table[value]
            if days is None:
                # Permanent retention — beats every finite-day value.
                return 1_000_000
            return int(days)
    return -1


CONTRACT_FIELD_SAFETY: dict[str, Callable[[Any, Any], bool]] = {
    "allowed_file_types": _set_widening_safe,
    "input_retention": _retention_shortening_safe,
    "output_retention": _retention_shortening_safe,
}


@dataclass(frozen=True)
class CloneReport:
    """Structured result of a workflow clone operation.

    Returned by :meth:`WorkflowVersioningService.clone` so callers
    (and tests) can verify exactly what got copied and surface gaps
    to operators. Fields:

    Attributes:
        source_workflow_id: PK of the source workflow.
        new_workflow_id: PK of the newly-created workflow row.
        new_version_label: The positive integer version assigned to the clone
            (e.g. ``2``).
        components_copied: Per-component counts. Keys are short
            names (``steps``, ``step_resources``, ``public_info``,
            ``role_access``, ``signal_mappings``); values are counts.
            Useful for asserting in tests that the clone didn't
            silently skip any component.
        warnings: Free-form messages about partial copies — e.g.
            "ruleset references not yet immutable (Phase 3 Session
            C)". Visible to operators via API responses and to
            developers via logs.
    """

    source_workflow_id: int
    new_workflow_id: int
    new_version_label: int
    components_copied: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class WorkflowVersioningService:
    """The single decision point for workflow version cloning.

    Stateless — methods are static. See sibling
    :class:`validibot.workflows.services.access.WorkflowAccessResolver`
    for the rationale on class-vs-free-functions.

    The service locks the source workflow after the clone succeeds.
    That preserves the historical ``Workflow.clone_to_new_version()``
    contract while making the clone boundary explicit and testable.
    """

    @staticmethod
    @transaction.atomic
    def clone(workflow: Workflow, *, user: AbstractBaseUser) -> CloneReport:
        """Clone ``workflow`` to a new version, copying the complete contract.

        Performs the following copy operations in order:

        1. Determine the next version label.
        2. Create the new ``Workflow`` row with all contract fields.
        3. Copy ``WorkflowStep`` rows (including ``config`` JSONField).
        4. Copy ``WorkflowStepResource`` rows for each new step.
        5. Copy ``WorkflowPublicInfo`` (one-to-one).
        6. Copy ``WorkflowRoleAccess`` rows.
        7. Copy ``WorkflowSignalMapping`` rows.
        7b. Copy ``WorkflowConstant`` rows (c.* namespace).
        8. Lock the source workflow.

        All operations run inside a single transaction so a failure
        midway leaves the database unchanged — no half-cloned
        workflows.

        Args:
            workflow: The source workflow to clone.
            user: The user performing the clone (recorded as the
                new workflow's ``user`` field for audit purposes).

        Returns:
            A :class:`CloneReport` with the new workflow's ID,
            version label, per-component copy counts, and any
            warnings about partial-copy gaps.
        """
        # Local imports to avoid a circular import (services
        # import models, and models in turn pull in some service-
        # level helpers via signals).
        from validibot.validations.models import Ruleset
        from validibot.validations.models import RulesetAssertion
        from validibot.validations.models import StepIODefinition
        from validibot.validations.models import WorkflowStepIOPromotion
        from validibot.validations.services.artifact_bindings import (
            validate_workflow_dependencies,
        )
        from validibot.workflows.models import Workflow
        from validibot.workflows.models import WorkflowConstant
        from validibot.workflows.models import WorkflowPublicInfo
        from validibot.workflows.models import WorkflowRoleAccess
        from validibot.workflows.models import WorkflowSignalMapping
        from validibot.workflows.models import WorkflowStep
        from validibot.workflows.models import WorkflowStepResource

        components_copied: dict[str, int] = {}
        warnings: list[str] = []

        # 1. Determine next version label
        sibling_versions = list(
            Workflow.objects.filter(org=workflow.org, slug=workflow.slug)
            .exclude(pk=workflow.pk)
            .values_list("version", flat=True),
        )
        sibling_versions.append(workflow.version)
        # Reuse the existing private helper on Workflow for
        # version-label arithmetic — that's pure-data logic that
        # doesn't need to move into the service yet.
        next_version = workflow._determine_next_version_label(sibling_versions)

        # 2. Create the new workflow row with all contract fields
        new_workflow = Workflow.objects.create(
            org=workflow.org,
            project=workflow.project,
            user=user,
            name=workflow.name,
            description=workflow.description,
            slug=workflow.slug,
            version=next_version,
            history_policy=workflow.history_policy,
            is_locked=False,
            is_active=workflow.is_active,
            allow_submission_name=workflow.allow_submission_name,
            allow_submission_meta_data=workflow.allow_submission_meta_data,
            allow_submission_short_description=workflow.allow_submission_short_description,
            make_info_page_public=workflow.make_info_page_public,
            workflow_visibility=workflow.workflow_visibility,
            allowed_file_types=list(workflow.allowed_file_types or []),
            input_retention=workflow.input_retention,
            output_retention=workflow.output_retention,
            success_message=workflow.success_message,
            input_schema=deepcopy(workflow.input_schema),
            input_schema_source_mode=workflow.input_schema_source_mode,
            input_schema_source_text=workflow.input_schema_source_text,
            # Phase 3: copy the agent-launch contract too. The
            # historical clone_to_new_version omitted these,
            # meaning a new version of an x402-published workflow
            # silently lost its publication state.
            agent_billing_mode=workflow.agent_billing_mode,
            agent_price_cents=workflow.agent_price_cents,
            agent_max_launches_per_hour=workflow.agent_max_launches_per_hour,
            x402_enabled=workflow.x402_enabled,
            mcp_enabled=workflow.mcp_enabled,
        )
        if workflow.featured_image:
            with workflow.featured_image.open("rb") as source_image:
                new_workflow.featured_image.save(
                    workflow.featured_image.name.rsplit("/", 1)[-1],
                    ContentFile(source_image.read()),
                    save=True,
                )

        # 3. + 4. Steps, step-owned contract rows, and step resources.
        # Snapshot original related rows before mutating .pk = None,
        # because prefetch_related results are cached on the Python objects.
        original_steps = list(
            workflow.steps.select_related("ruleset")
            .prefetch_related(
                "step_resources",
                "step_io_definitions",
                "input_bindings",
                "derivations",
                # io_promotions is the related_name for
                # WorkflowStepIOPromotion. We prefetch it here so the
                # overlay clone below doesn't issue N queries when
                # iterating steps. Per the May 2026 P1 review: the
                # overlay carries workflow-scoped promotion names for
                # validator-owned StepIODefinitions and is workflow
                # contract state — it must clone with the workflow.
                "io_promotions",
            )
            .order_by("order"),
        )
        old_pks = [step.pk for step in original_steps]
        step_resource_map = {
            step.pk: list(step.step_resources.all()) for step in original_steps
        }
        step_io_definition_map = {
            step.pk: list(step.step_io_definitions.all()) for step in original_steps
        }
        step_binding_map = {
            step.pk: list(step.input_bindings.all()) for step in original_steps
        }
        step_derivation_map = {
            step.pk: list(step.derivations.all()) for step in original_steps
        }
        step_promotion_map = {
            step.pk: list(step.io_promotions.all()) for step in original_steps
        }

        source_rulesets = {
            step.ruleset_id: step.ruleset
            for step in original_steps
            if step.ruleset_id and step.ruleset
        }
        ruleset_clone_map: dict[int, Ruleset] = {}
        for source_ruleset_id, source_ruleset in source_rulesets.items():
            ruleset_clone_map[source_ruleset_id] = (
                WorkflowVersioningService._clone_ruleset(
                    source_ruleset,
                    user=user,
                    version_label=next_version,
                )
            )

        for step in original_steps:
            source_ruleset_id = step.ruleset_id
            step.pk = None
            step.workflow = new_workflow
            if source_ruleset_id:
                step.ruleset = ruleset_clone_map[source_ruleset_id]
        WorkflowStep.objects.bulk_create(original_steps)
        components_copied["steps"] = len(original_steps)

        step_clone_map = {
            old_pk: new_step
            for old_pk, new_step in zip(old_pks, original_steps, strict=True)
        }

        io_definition_clone_map: dict[int, StepIODefinition] = {}
        io_definition_count = 0
        binding_count = 0
        derivation_count = 0
        for old_pk, new_step in step_clone_map.items():
            for old_io_definition in step_io_definition_map.get(old_pk, []):
                old_io_definition_pk = old_io_definition.pk
                old_io_definition.pk = None
                old_io_definition.workflow_step = new_step
                old_io_definition.validator = None
                old_io_definition.provider_binding = deepcopy(
                    old_io_definition.provider_binding
                )
                old_io_definition.metadata = deepcopy(old_io_definition.metadata)
                old_io_definition.accepted_data_formats = deepcopy(
                    old_io_definition.accepted_data_formats
                )
                old_io_definition.accepted_media_types = deepcopy(
                    old_io_definition.accepted_media_types
                )
                old_io_definition.allowed_source_scopes = deepcopy(
                    old_io_definition.allowed_source_scopes
                )
                old_io_definition.save()
                io_definition_clone_map[old_io_definition_pk] = old_io_definition
                io_definition_count += 1

            for old_binding in step_binding_map.get(old_pk, []):
                source_step_id = old_binding.source_step_id
                source_output_id = old_binding.source_output_io_definition_id
                io_definition = io_definition_clone_map.get(
                    old_binding.io_definition_id,
                    old_binding.io_definition,
                )
                old_binding.pk = None
                old_binding.workflow_step = new_step
                old_binding.io_definition = io_definition
                old_binding.source_step = (
                    step_clone_map.get(source_step_id) if source_step_id else None
                )
                old_binding.source_output_io_definition = (
                    io_definition_clone_map.get(
                        source_output_id,
                        old_binding.source_output_io_definition,
                    )
                    if source_output_id
                    else None
                )
                old_binding.default_value = deepcopy(old_binding.default_value)
                old_binding.full_clean()
                old_binding.save()
                binding_count += 1

            for old_derivation in step_derivation_map.get(old_pk, []):
                old_derivation.pk = None
                old_derivation.validator = None
                old_derivation.workflow_step = new_step
                old_derivation.metadata = deepcopy(old_derivation.metadata)
                old_derivation.save()
                derivation_count += 1

        # Clone WorkflowStepIOPromotion overlays (per May 2026 P1 review).
        # The overlay carries workflow-scoped promoted names for validator-
        # owned StepIODefinition rows (catalog entries shared across
        # workflows). Two cases for resolving the cloned io_definition:
        #
        # 1. The overlay points at a STEP-OWNED row that we just cloned —
        #    use io_definition_clone_map to redirect to the new row.
        # 2. The overlay points at a VALIDATOR-OWNED row (catalog entry) —
        #    the original row is shared across workflows, so the clone
        #    keeps the same io_definition FK. io_definition_clone_map.get()
        #    falls back to the original row in this case.
        #
        # Without this clone, the new workflow version would silently
        # drop all validator-owned promotions, and downstream s.<name>
        # assertions that referenced them would break.
        promotion_count = 0
        for old_pk, new_step in step_clone_map.items():
            for old_promotion in step_promotion_map.get(old_pk, []):
                cloned_io_definition = io_definition_clone_map.get(
                    old_promotion.io_definition_id,
                    old_promotion.io_definition,
                )
                WorkflowStepIOPromotion.objects.create(
                    workflow_step=new_step,
                    io_definition=cloned_io_definition,
                    promoted_signal_name=old_promotion.promoted_signal_name,
                )
                promotion_count += 1
        components_copied["io_promotions"] = promotion_count

        assertion_count = 0
        for source_ruleset_id, cloned_ruleset in ruleset_clone_map.items():
            source_assertions = RulesetAssertion.objects.filter(
                ruleset_id=source_ruleset_id,
            ).order_by("order", "pk")
            for assertion in source_assertions:
                target_io_definition = io_definition_clone_map.get(
                    assertion.target_io_definition_id,
                    assertion.target_io_definition,
                )
                RulesetAssertion.objects.create(
                    ruleset=cloned_ruleset,
                    order=assertion.order,
                    assertion_type=assertion.assertion_type,
                    operator=assertion.operator,
                    target_io_definition=target_io_definition,
                    target_data_path=assertion.target_data_path,
                    severity=assertion.severity,
                    when_expression=assertion.when_expression,
                    rhs=deepcopy(assertion.rhs),
                    options=deepcopy(assertion.options),
                    message_template=assertion.message_template,
                    success_message=assertion.success_message,
                    notes=assertion.notes,
                    cel_cache=assertion.cel_cache,
                    spec_version=assertion.spec_version,
                )
                assertion_count += 1
        components_copied["rulesets"] = len(ruleset_clone_map)
        components_copied["assertions"] = assertion_count
        components_copied["step_io_definitions"] = io_definition_count
        components_copied["input_bindings"] = binding_count
        components_copied["derivations"] = derivation_count

        step_resource_count = 0
        for old_pk, new_step in step_clone_map.items():
            for old_res in step_resource_map.get(old_pk, []):
                if old_res.is_catalog_reference:
                    WorkflowStepResource.objects.create(
                        step=new_step,
                        role=old_res.role,
                        validator_resource_file=old_res.validator_resource_file,
                    )
                    step_resource_count += 1
                else:
                    # Use a context manager to avoid file-handle leaks
                    # if read() or the subsequent create() raises.
                    with old_res.step_resource_file.open("rb") as f:
                        file_content = f.read()
                    WorkflowStepResource.objects.create(
                        step=new_step,
                        role=old_res.role,
                        step_resource_file=ContentFile(
                            file_content,
                            name=old_res.filename or "file",
                        ),
                        filename=old_res.filename,
                        resource_type=old_res.resource_type,
                    )
                    step_resource_count += 1
        components_copied["step_resources"] = step_resource_count

        # 5. WorkflowPublicInfo (one-to-one). Skip if source has none.
        try:
            old_public_info = workflow.public_info
        except WorkflowPublicInfo.DoesNotExist:
            old_public_info = None

        if old_public_info is not None:
            old_public_info.pk = None
            old_public_info.workflow = new_workflow
            old_public_info.save()
            components_copied["public_info"] = 1
        else:
            components_copied["public_info"] = 0

        # 6. WorkflowRoleAccess
        role_access_rows = list(WorkflowRoleAccess.objects.filter(workflow=workflow))
        for ra in role_access_rows:
            ra.pk = None
            ra.workflow = new_workflow
        WorkflowRoleAccess.objects.bulk_create(role_access_rows)
        components_copied["role_access"] = len(role_access_rows)

        # 7. WorkflowSignalMapping
        signal_mappings = list(WorkflowSignalMapping.objects.filter(workflow=workflow))
        for sm in signal_mappings:
            sm.pk = None
            sm.workflow = new_workflow
        WorkflowSignalMapping.objects.bulk_create(signal_mappings)
        components_copied["signal_mappings"] = len(signal_mappings)

        # 7b. WorkflowConstant (c.* namespace, ADR-2026-06-18). Constants are
        # part of the versioned trust contract, so a new version must carry them
        # forward verbatim — bulk_create on the fresh (unlocked) workflow
        # intentionally bypasses the per-instance edit-after-runs guard.
        constants = list(WorkflowConstant.objects.filter(workflow=workflow))
        for const in constants:
            const.pk = None
            const.workflow = new_workflow
        WorkflowConstant.objects.bulk_create(constants)
        components_copied["constants"] = len(constants)

        # Validate the cloned dependency graph as one contract before the
        # transaction locks the source or returns a runnable new version.
        validate_workflow_dependencies(new_workflow)

        # 8. Lock the source workflow.
        workflow.is_locked = True
        workflow.save(update_fields=["is_locked"])

        # Surface deferred-Phase warnings.
        if any(step.validator_id for step in original_steps):
            warnings.append(
                "Cloned steps reference the same Validator rows as the "
                "source workflow; validator semantic digest lands in "
                "Phase 3 Session B (tasks 7-9).",
            )

        report = CloneReport(
            source_workflow_id=workflow.pk,
            new_workflow_id=new_workflow.pk,
            new_version_label=next_version,
            components_copied=components_copied,
            warnings=warnings,
        )
        logger.info(
            "Cloned workflow %s (v%s) to %s (v%s): %s",
            workflow.pk,
            workflow.version,
            new_workflow.pk,
            next_version,
            components_copied,
        )
        return report

    @staticmethod
    def _clone_ruleset(source_ruleset, *, user: AbstractBaseUser, version_label: int):
        """Create an editable copy of a step-level ruleset for a workflow clone."""

        from validibot.validations.models import Ruleset

        clone = Ruleset(
            org=source_ruleset.org,
            user=user if getattr(user, "pk", None) else source_ruleset.user,
            name=WorkflowVersioningService._unique_ruleset_clone_name(
                org=source_ruleset.org,
                ruleset_type=source_ruleset.ruleset_type,
                base_name=f"{source_ruleset.name}-workflow-v{version_label}",
                version=source_ruleset.version or "1",
            ),
            ruleset_type=source_ruleset.ruleset_type,
            version=source_ruleset.version or "1",
            rules_text=source_ruleset.rules_text,
            metadata=deepcopy(source_ruleset.metadata),
        )
        if source_ruleset.rules_file:
            with source_ruleset.rules_file.open("rb") as source_file:
                clone.rules_file = ContentFile(
                    source_file.read(),
                    name=source_ruleset.rules_file.name.rsplit("/", 1)[-1],
                )
        clone.full_clean()
        clone.save()
        return clone

    @staticmethod
    def _unique_ruleset_clone_name(
        *,
        org,
        ruleset_type: str,
        base_name: str,
        version: str,
    ) -> str:
        """Return a ruleset name unique within the model's natural key."""

        from validibot.validations.models import Ruleset

        name = base_name[:200]
        suffix = 2
        while Ruleset.objects.filter(
            org=org,
            ruleset_type=ruleset_type,
            name=name,
            version=version,
        ).exists():
            suffix_text = f"-{suffix}"
            name = f"{base_name[: 200 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        return name


__all__ = ["CONTRACT_FIELDS", "CloneReport", "WorkflowVersioningService"]
