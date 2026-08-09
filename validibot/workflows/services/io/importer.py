"""Import a workflow definition (+ bundled files) into a new live Workflow.

This is the deserialize half of the ``.vaf`` round-trip. It mirrors the
create-order and FK-rebinding rules of ``WorkflowVersioningService.clone`` —
rulesets before steps, I/O definitions before assertions — but its source is a portable
definition dict rather than live rows, and it rebinds ownership to the importing
user instead of copying it.

Three rules from the design shape the behaviour:

- **Always a new workflow.** A fresh ``uuid`` (model default), a unique ``slug``
  in the target org, ``version = 1``, owned by the importing user. Nothing is
  overwritten.
- **Validators are resolved, not created.** Built-ins resolve by
  ``validation_type``; custom validators by ``(validation_type, slug)`` in the
  importing org. A version that doesn't match is a *warning*; a validator that
  can't be resolved at all is a hard error (a step with no validator can't run).
- **Warnings, not silent gaps.** Version mismatches and un-restorable catalog
  resources are collected and surfaced on the import results page.

Everything runs in one transaction: a failure part-way leaves no half-imported
workflow.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify

from validibot.validations.validators.base.step_serializer import WorkflowImportError
from validibot.validations.validators.base.step_serializer import get_step_serializer
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.constants import WorkflowVisibility
from validibot.workflows.services.io import schema
from validibot.workflows.services.io import vaf

if TYPE_CHECKING:
    from validibot.users.models import Organization
    from validibot.users.models import User
    from validibot.validations.models import StepIODefinition
    from validibot.validations.models import Validator
    from validibot.workflows.models import Workflow
    from validibot.workflows.models import WorkflowStep

_SLUG_MAX = 50


@dataclass
class ImportResult:
    """Outcome of an import: the new workflow plus warnings and per-type counts."""

    workflow: Workflow
    warnings: list[str] = field(default_factory=list)
    components: dict[str, int] = field(default_factory=dict)


def import_from_upload(
    data: bytes,
    *,
    filename: str | None,
    org: Organization,
    user: User,
) -> ImportResult:
    """Read uploaded ``.vaf``/``.json`` bytes and import the workflow within.

    Raises :class:`~validibot.workflows.services.io.vaf.VafError` or
    :class:`WorkflowImportError` (both carry a ``code``) on any failure, which
    the view renders on the error page.
    """
    bundle = vaf.read_input(data, filename=filename)
    return import_definition(
        bundle.workflow,
        files=bundle.files,
        org=org,
        user=user,
        had_archive=bundle.had_archive,
    )


@transaction.atomic
def import_definition(
    definition: dict[str, Any],
    *,
    files: dict[str, bytes],
    org: Organization,
    user: User,
    had_archive: bool = True,
) -> ImportResult:
    """Build a new Workflow from a definition dict + bundled files."""
    _check_format_version(definition)
    workflow_data = definition.get("workflow")
    if not isinstance(workflow_data, dict):
        raise WorkflowImportError(
            "Definition is missing its 'workflow' section.",
            code="vaf.malformed",
        )
    steps_data = definition.get("steps")
    if not isinstance(steps_data, list):
        raise WorkflowImportError(
            "Definition is missing its 'steps' list.",
            code="vaf.malformed",
        )

    warnings: list[str] = []
    components: dict[str, int] = {}

    workflow = _create_workflow(workflow_data, org=org, user=user)
    components["steps"] = 0
    components["assertions"] = 0
    for index, step_data in enumerate(steps_data):
        _import_step(
            step_data,
            index=index,
            workflow=workflow,
            org=org,
            user=user,
            files=files,
            had_archive=had_archive,
            warnings=warnings,
            components=components,
        )

    _import_public_info(workflow, workflow_data.get("public_info"))
    components["signal_mappings"] = _import_signal_mappings(
        workflow,
        workflow_data.get("signal_mappings") or [],
    )
    components["constants"] = _import_constants(
        workflow,
        workflow_data.get("constants") or [],
    )
    _note_unsupported_role_access(workflow_data, warnings)

    from validibot.validations.services.artifact_bindings import (
        validate_workflow_dependencies,
    )

    try:
        validate_workflow_dependencies(workflow)
    except ValidationError as exc:
        raise WorkflowImportError(
            "The imported workflow has an invalid earlier-step file dependency: "
            + "; ".join(exc.messages),
            code="vaf.invalid_artifact_dependency",
        ) from exc

    return ImportResult(workflow=workflow, warnings=warnings, components=components)


# ─────────────────────────────────────────────────────── workflow ──


def _check_format_version(definition: dict[str, Any]) -> None:
    version = definition.get("format_version")
    if version is not None and version != schema.FORMAT_VERSION:
        raise WorkflowImportError(
            f"Unsupported workflow definition version {version!r}; this server "
            f"supports version {schema.FORMAT_VERSION}.",
            code="vaf.unsupported_version",
        )


def _create_workflow(
    data: dict[str, Any],
    *,
    org: Organization,
    user: User,
) -> Workflow:
    """Create the new Workflow row, rebinding ownership and minting a slug."""
    from validibot.users.models import ensure_default_project
    from validibot.workflows.models import Workflow

    history_policy = data.get(
        "history_policy",
        WorkflowHistoryPolicy.VERSIONED,
    )
    if history_policy not in WorkflowHistoryPolicy.values:
        raise WorkflowImportError(
            "The workflow has an invalid editing policy.",
            code="vaf.invalid_history_policy",
        )

    fields = {
        field_name: data.get(field_name) for field_name in schema.WORKFLOW_SCALAR_FIELDS
    }
    fields["history_policy"] = history_policy
    name = fields.get("name") or "Imported workflow"
    # Imports don't carry a project reference, but every workflow must belong to
    # one. Bind the imported workflow to the importing org's default project
    # (created on demand if the org somehow lacks one). The owner can reassign it
    # afterwards. Without this, save() -> full_clean() would reject the import.
    workflow = Workflow(
        org=org,
        user=user,
        project=ensure_default_project(org),
        version=1,
        is_locked=False,
        # Imported workflows are ACTIVE (runnable) immediately. The original
        # ADR-2026-03-31 plan had them start inactive "for review", but in the
        # list UI an inactive workflow is presented as archived/unlaunchable,
        # which blocked the common import-then-run flow. A workflow you
        # deliberately imported is one you want to use; deactivate it if not.
        is_active=True,
        # External exposure is never inherited on import. An imported workflow
        # lands maximally locked — PRIVATE visibility, no public info page, no
        # MCP agent access, no x402 paid access — until the owner explicitly
        # widens it. These are forced here (NOT left to model defaults — the
        # default visibility is ORG, which would expose the import to the whole
        # organization) so a hand-crafted definition can't auto-expose a
        # workflow. The serialized field set (``io/schema.py``) also excludes
        # the access fields, so they cannot be injected via ``fields`` (a
        # duplicate-kwarg TypeError would fire if it tried).
        workflow_visibility=WorkflowVisibility.PRIVATE,
        make_info_page_public=False,
        mcp_enabled=False,
        x402_enabled=False,
        slug=_unique_slug(org, data.get("slug") or slugify(name)),
        allowed_file_types=list(data.get("allowed_file_types") or []),
        input_schema=deepcopy(data.get("input_schema")),
        **{k: v for k, v in fields.items() if v is not None},
    )
    workflow.save()  # save() runs full_clean() and enforces the public-info rule
    return workflow


def _unique_slug(org: Organization, base: str) -> str:
    """Return a slug unique within *org*, suffixing -2/-3/... on collision."""
    from validibot.workflows.models import Workflow

    base = (slugify(base) or "imported-workflow")[:_SLUG_MAX]
    if not Workflow.objects.filter(org=org, slug=base).exists():
        return base
    suffix = 2
    while True:
        candidate = f"{base[: _SLUG_MAX - len(str(suffix)) - 1]}-{suffix}"
        if not Workflow.objects.filter(org=org, slug=candidate).exists():
            return candidate
        suffix += 1


# ─────────────────────────────────────────────────────────── step ──


def _import_step(
    data: dict[str, Any],
    *,
    index: int,
    workflow: Workflow,
    org: Organization,
    user: User,
    files: dict[str, bytes],
    had_archive: bool,
    warnings: list[str],
    components: dict[str, int],
) -> None:
    """Create one step and its full validator body (ruleset, step I/O, etc.)."""
    from validibot.workflows.models import WorkflowStep

    if data.get("kind") == "action":
        raise WorkflowImportError(
            "This workflow contains an action step, which import does not yet support.",
            code="vaf.action_unsupported",
        )

    validator = _resolve_validator(
        data.get("validator_ref") or {},
        org=org,
        step_label=data.get("name") or f"step {index + 1}",
        warnings=warnings,
    )
    serializer = get_step_serializer(validator.validation_type)

    ruleset_body = data.get("ruleset")
    ruleset = (
        serializer.create_ruleset_row(ruleset_body, org=org, user=user, files=files)
        if ruleset_body
        else None
    )

    step_fields = {f: data.get(f) for f in schema.STEP_SCALAR_FIELDS}
    step = WorkflowStep(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        config=deepcopy(data.get("config") or {}),
        # ``display_settings`` defaults to {} for pre-split exports (additive,
        # so FORMAT_VERSION stays 1) — ADR-2026-06-18.
        display_settings=deepcopy(data.get("display_settings") or {}),
        **{k: v for k, v in step_fields.items() if v is not None},
    )
    step.save()
    components["steps"] = components.get("steps", 0) + 1

    # Step-owned I/O definitions must exist before assertions/bindings target them.
    io_definition_index = _import_step_io_definitions(
        step, data.get("step_io_definitions") or []
    )
    resolver = _make_io_definition_resolver(step, validator, io_definition_index)

    if ruleset is not None:
        made = serializer.create_assertions(
            ruleset, ruleset_body, io_definition_resolver=resolver
        )
        serializer.validate_imported_ruleset(ruleset, ruleset_body)
        components["assertions"] = components.get("assertions", 0) + made

    _import_input_bindings(step, data.get("input_bindings") or [], resolver)
    _import_derivations(step, data.get("derivations") or [])
    _import_io_promotions(step, data.get("io_promotions") or [], resolver)
    _import_resources(
        step,
        data.get("resources") or [],
        files=files,
        had_archive=had_archive,
        warnings=warnings,
    )


def _resolve_validator(
    ref: dict[str, Any],
    *,
    org: Organization,
    step_label: str,
    warnings: list[str],
) -> Validator:
    """Resolve the step's validator on this system, or fail with a clear error.

    Built-ins are matched by ``validation_type`` (portable); custom validators by
    ``(validation_type, slug)`` within the importing org. A mismatched version is
    a warning, not a failure.
    """
    from validibot.validations.models import Validator

    validation_type = ref.get("validation_type")
    if not validation_type:
        raise WorkflowImportError(
            f"Step {step_label!r} does not name a validator.",
            code="vaf.validator_missing",
        )

    is_system = ref.get("is_system", True)
    if is_system:
        qs = Validator.objects.filter(validation_type=validation_type, is_system=True)
        descriptor = f"built-in validator '{validation_type}'"
    else:
        qs = Validator.objects.filter(
            validation_type=validation_type,
            slug=ref.get("slug") or "",
            org=org,
        )
        descriptor = f"custom validator '{ref.get('slug') or validation_type}'"

    wanted_version = ref.get("version")
    exact = (
        qs.filter(version=wanted_version).first()
        if wanted_version is not None
        else None
    )
    if exact is not None:
        return exact

    fallback = qs.order_by("-version").first()
    if fallback is None:
        raise WorkflowImportError(
            f"Step {step_label!r} needs {descriptor}, which isn't available on "
            f"this system.",
            code="vaf.validator_unresolved",
        )
    if wanted_version is not None and fallback.version != wanted_version:
        warnings.append(
            f"Step {step_label!r}: {descriptor} version {wanted_version} wasn't "
            f"found; using version {fallback.version} instead.",
        )
    return fallback


# ───────────────────────────────────────────── step I/O definitions ──


def _import_step_io_definitions(
    step: WorkflowStep,
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], StepIODefinition]:
    """Create step-owned I/O definitions indexed by contract key and direction."""
    from validibot.validations.models import StepIODefinition

    index: dict[tuple[str, str], StepIODefinition] = {}
    for row in rows:
        io_definition = StepIODefinition(workflow_step=step, validator=None)
        for field_name in schema.STEP_IO_DEFINITION_FIELDS:
            if field_name in row:
                setattr(io_definition, field_name, row[field_name])
        for json_field in schema.STEP_IO_DEFINITION_JSON_DICT_FIELDS:
            setattr(io_definition, json_field, deepcopy(row.get(json_field) or {}))
        for json_field in schema.STEP_IO_DEFINITION_JSON_LIST_FIELDS:
            setattr(io_definition, json_field, deepcopy(row.get(json_field) or []))
        io_definition.full_clean()
        io_definition.save()
        index[(io_definition.contract_key, io_definition.direction)] = io_definition
    return index


def _make_io_definition_resolver(
    step: WorkflowStep,
    validator: Validator,
    step_outputs: dict[tuple[str, str], StepIODefinition],
):
    """Return a resolver mapping a serialized io_definition ref to a live row (or None).

    Step-owned refs resolve within this step's freshly created I/O definitions;
    validator-owned refs resolve via the resolved validator's I/O catalog.
    """

    def resolve(ref: dict[str, Any]) -> StepIODefinition | None:
        if not ref:
            return None
        key = (ref.get("contract_key"), ref.get("direction"))
        if ref.get("owner") == "step":
            return step_outputs.get(key)
        return validator.step_io_definitions.filter(
            contract_key=ref.get("contract_key"),
            direction=ref.get("direction"),
        ).first()

    return resolve


def _import_input_bindings(step, rows, resolver) -> None:
    """Create step input bindings, re-binding each to its io_definition definition."""
    from validibot.validations.constants import BindingSourceScope
    from validibot.validations.models import StepInputBinding
    from validibot.validations.services.artifact_bindings import (
        resolve_artifact_reference,
    )

    for row in rows:
        io_definition = resolver(row.get("io_definition_ref"))
        if io_definition is None:
            continue  # nothing to bind to (warned elsewhere if validator-owned)
        binding = StepInputBinding(
            workflow_step=step,
            io_definition=io_definition,
            default_value=deepcopy(row.get("default_value")),
            **{
                f: row.get(f)
                for f in schema.INPUT_BINDING_FIELDS
                if row.get(f) is not None
            },
        )
        if binding.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT:
            source_step, source_output = resolve_artifact_reference(
                workflow=step.workflow,
                reference=binding.source_data_path,
            )
            binding.source_step = source_step
            binding.source_output_io_definition = source_output
        binding.full_clean()
        binding.save()


def _import_derivations(step, rows) -> None:
    """Create step-owned derivations."""
    from validibot.validations.models import Derivation

    for row in rows:
        derivation = Derivation(
            workflow_step=step,
            validator=None,
            metadata=deepcopy(row.get("metadata") or {}),
        )
        for field_name in schema.DERIVATION_FIELDS:
            if field_name in row:
                setattr(derivation, field_name, row[field_name])
        derivation.full_clean()
        derivation.save()


def _import_io_promotions(step, rows, resolver) -> None:
    """Create I/O-definition-to-s.* overlays for validator-owned value ports."""
    from validibot.validations.models import WorkflowStepIOPromotion

    for row in rows:
        io_definition = resolver(row.get("io_definition_ref"))
        if io_definition is None:
            continue
        WorkflowStepIOPromotion.objects.create(
            workflow_step=step,
            io_definition=io_definition,
            promoted_signal_name=row.get("promoted_signal_name") or "",
        )


def _import_resources(step, rows, *, files, had_archive, warnings) -> None:
    """Restore step resources: bundled step-owned files; warn on catalog refs."""
    from django.core.files.base import ContentFile

    from validibot.workflows.models import WorkflowStepResource

    for row in rows:
        if row.get("mode") == "catalog":
            ref = row.get("catalog_ref") or {}
            warnings.append(
                f"Step {step.name!r}: shared resource "
                f"{ref.get('filename') or row.get('role')!r} couldn't be matched "
                f"on this system and was skipped; attach it after import.",
            )
            continue

        content_ref = row.get("content_ref")
        payload = files.get(content_ref) if content_ref else None
        if payload is None:
            hint = (
                "the bundled file is missing"
                if had_archive
                else "this workflow includes files — please import the .vaf, not "
                "the .json"
            )
            raise WorkflowImportError(
                f"Step {step.name!r} resource could not be restored ({hint}).",
                code="vaf.missing_bundled_file",
            )
        WorkflowStepResource.objects.create(
            step=step,
            role=row.get("role") or "",
            step_resource_file=ContentFile(payload, name=row.get("filename") or "file"),
            filename=row.get("filename") or "",
            resource_type=row.get("resource_type") or "",
        )


# ───────────────────────────────────────────────── workflow-level ──


def _import_public_info(workflow, data) -> None:
    """Recreate the public info page (HTML is recompiled on save)."""
    if not data:
        return
    from validibot.workflows.models import WorkflowPublicInfo

    WorkflowPublicInfo.objects.create(
        workflow=workflow,
        title=data.get("title") or "",
        content_md=data.get("content_md") or "",
        show_steps=bool(data.get("show_steps", True)),
    )


def _import_signal_mappings(workflow, rows) -> int:
    """Recreate workflow-level signal mappings; return the count."""
    from validibot.workflows.models import WorkflowSignalMapping

    count = 0
    for row in rows:
        mapping = WorkflowSignalMapping(
            workflow=workflow,
            default_value=deepcopy(row.get("default_value")),
            **{
                f: row.get(f)
                for f in schema.SIGNAL_MAPPING_FIELDS
                if row.get(f) is not None
            },
        )
        mapping.full_clean()
        mapping.save()
        count += 1
    return count


def _import_constants(workflow, rows) -> int:
    """Recreate workflow Constants (c.* namespace); return the count.

    ``value`` is restored verbatim (deep-copied); ``full_clean()`` re-runs
    ``coerce_constant_value`` so an imported constant is re-validated against
    its declared type, and an export→import reproduces identical constants.
    """
    from validibot.workflows.models import WorkflowConstant

    count = 0
    for row in rows:
        constant = WorkflowConstant(
            workflow=workflow,
            value=deepcopy(row.get("value")),
            **{f: row.get(f) for f in schema.CONSTANT_FIELDS if row.get(f) is not None},
        )
        constant.full_clean()
        constant.save()
        count += 1
    return count


def _note_unsupported_role_access(workflow_data, warnings) -> None:
    """Warn that role-based access grants are not recreated on import."""
    if workflow_data.get("role_access"):
        warnings.append(
            "Role-based access grants were not imported; configure workflow "
            "access after import.",
        )
