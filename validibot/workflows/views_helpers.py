import json
import logging
from dataclasses import asdict
from hashlib import sha256
from typing import Any
from typing import cast
from uuid import uuid4

from django import forms
from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.http import Http404
from django.http import HttpRequest
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from rest_framework.request import Request

from validibot.actions.models import ActionDefinition
from validibot.projects.models import Project
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.models import detect_file_type
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorExecutionProfile
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Ruleset
from validibot.validations.models import Validator
from validibot.validations.validators.shacl.constants import (
    SHACL_RESULT_HANDLING_DEFAULT,
)
from validibot.validations.validators.shacl.engine import FILE_SEPARATOR
from validibot.validations.validators.shacl.persistence import (
    concatenate_uploaded_files,
)
from validibot.workflows.forms import AiAssistStepConfigForm
from validibot.workflows.forms import EnergyPlusStepConfigForm
from validibot.workflows.forms import FMUValidatorStepConfigForm
from validibot.workflows.forms import JsonSchemaStepConfigForm
from validibot.workflows.forms import PdfStepConfigForm
from validibot.workflows.forms import PortfolioManagerStepConfigForm
from validibot.workflows.forms import ShaclStepConfigForm
from validibot.workflows.forms import TabularStepConfigForm
from validibot.workflows.forms import XmlSchemaStepConfigForm
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.step_configs import partition_step_config

logger = logging.getLogger(__name__)


def user_has_executor_role(user: User, workflow: Workflow) -> bool:
    """
    Return True when the user has EXECUTOR access to the workflow.
    """
    return user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow)


def user_has_workflow_manager_role(user: User, workflow: Workflow) -> bool:
    """
    Return True when the user can manage the workflow (author/admin/owner).
    """

    if not getattr(user, "is_authenticated", False):
        return False
    return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, workflow)


def resolve_project(
    workflow: Workflow,
    request: Request | HttpRequest,
) -> Project | None:
    """
    Derive the project for a workflow launch request, honoring ?project= overrides.
    """
    project_id = None
    if hasattr(request, "query_params"):
        project_id = request.query_params.get("project")
    if not project_id:
        project_id = request.GET.get("project")
    if project_id:
        try:
            return Project.objects.get(pk=project_id, org=workflow.org)
        except Project.DoesNotExist as exc:  # pragma: no cover
            raise Http404 from exc
    return workflow.project


def file_type_label(value: str) -> str:
    try:
        return SubmissionFileType(value).label
    except Exception:
        return value


def get_validator_operation_display(
    validator: Validator | None,
) -> dict[str, Any] | None:
    """Return display copy for inline validators that have a real validation step.

    Processor validators already render their processor between input and output
    assertions. Schema/RDF validators do not have that staged processor view, but
    authors still need to see that the schema validation runs before any
    step-level assertions.
    """
    if not validator:
        return None

    operation_copy = {
        ValidationType.JSON_SCHEMA: {
            "label": _("JSON Schema Validation"),
            "description": _(
                "Validates the submitted JSON document against the configured "
                "JSON Schema before any step assertions run.",
            ),
        },
        ValidationType.XML_SCHEMA: {
            "label": _("XML Validation"),
            "description": _(
                "Validates the submitted XML document against the configured "
                "XSD, RelaxNG, or DTD schema before any step assertions run.",
            ),
        },
        ValidationType.SCHEMATRON: {
            "label": _("Schematron Validation"),
            "description": _(
                "Runs the step's uploaded Schematron rules against the "
                "submitted XML document before any step assertions run, "
                "reporting failed rules by their native IDs.",
            ),
        },
        ValidationType.SHACL: {
            "label": _("SHACL Validation"),
            "description": _(
                "Validates the submitted RDF graph against the configured "
                "SHACL shapes and optional ontology files before any step "
                "assertions run.",
            ),
        },
        ValidationType.TABULAR: {
            "label": _("Tabular Validation"),
            "description": _(
                "Validates the submitted CSV against the configured column "
                "schema and row rules before any step assertions run.",
            ),
        },
    }
    display = operation_copy.get(validator.validation_type)
    if display is None:
        return None
    return {
        "label": display["label"],
        "description": display["description"],
    }


def describe_workflow_file_type_violation(
    workflow: Workflow,
    file_type: str,
) -> str | None:
    """
    Describe why the given workflow cannot accept submissions of the given file type.

    Phase 2 of ADR-2026-04-27: this helper now delegates to
    :class:`validibot.workflows.services.launch_contract.LaunchContract`
    so the web view, REST API, and MCP helper API (all callers of
    this helper) share their file-type/step-compatibility decisions
    with the x402 cloud agent (which calls ``LaunchContract.validate``
    directly).

    Why this signature stays string-returning: the existing callers
    (form ``clean_*`` methods, API serializers, MCP helper) all expect
    a translatable error string for direct display. Changing them
    all to consume a structured ``LaunchContractViolation`` is the
    next refactor — for now, this delegation gets the unification at
    the decision-logic layer and leaves the rendering shape unchanged.
    """
    # Local import to avoid a circular import (services import models;
    # models indirectly import this module via signals).
    from validibot.workflows.services.launch_contract import LaunchContract

    if not file_type:
        # The contract treats missing file_type as "skip the check"
        # rather than a violation — so we keep the existing helper's
        # explicit "select a file type" message here for callers
        # that want it. Web forms in particular need this prompt;
        # API callers usually have a known file_type by the time
        # they reach this helper.
        return str(_("Select a file type before launching the workflow."))

    violation = LaunchContract.validate(
        workflow=workflow,
        file_type=file_type,
    )
    if violation is None:
        return None
    # Workflow-state violations (inactive / no_steps) shouldn't surface
    # through this helper because callers pre-check workflow state via
    # ``ensure_workflow_ready_for_launch``. If they slip through, fall
    # through to ``None`` rather than emitting a confusing file-type
    # message — the caller's other checks will catch the underlying
    # problem.
    if violation.code.startswith("workflow_") or violation.code == "no_steps":
        return None
    return violation.message


def resolve_submission_file_type(
    *,
    requested: str,
    filename: str,
    inline_text: str | None = None,
) -> str:
    """
    Determine the submission file type based on user request and file detection.
    """
    detected = detect_file_type(
        filename=filename or None,
        text=inline_text if inline_text else None,
    )
    if detected and detected != SubmissionFileType.UNKNOWN:
        return detected
    return requested


def build_public_info_url(request, workflow: Workflow) -> str | None:
    """
    Build the public info URL for the given workflow, if public info is enabled.
    """
    if not workflow.make_info_page_public:
        return None
    return request.build_absolute_uri(
        reverse(
            "workflow_public_info",
            kwargs={"workflow_uuid": workflow.uuid},
        ),
    )


def public_info_card_context(
    request,
    workflow: Workflow,
    *,
    can_manage: bool,
) -> dict[str, object]:
    """
    Build context for the public info card for the given workflow.
    """
    return {
        "workflow": workflow,
        "public_info_url": build_public_info_url(request, workflow),
        "can_manage_public_info": can_manage,
    }


def resequence_workflow_steps(workflow: Workflow) -> None:
    ordered = list(workflow.steps.all().order_by("order", "pk"))
    changed = False
    for index, step in enumerate(ordered, start=1):
        desired = index * 10
        if step.order != desired:
            changed = True
            break
    if not changed:
        return
    # Two-pass update to avoid unique-constraint violations on
    # (workflow_id, order).  PostgreSQL checks constraints per row
    # during a CASE-based bulk_update, so moving order 11→20 can
    # collide with a row that still holds order 20.  Pass 1 pushes
    # everything to a high temporary range; pass 2 sets final values.
    for index, step in enumerate(ordered, start=1):
        step.order = 10_000 + index
    WorkflowStep.objects.bulk_update(ordered, ["order"])
    for index, step in enumerate(ordered, start=1):
        step.order = index * 10
    WorkflowStep.objects.bulk_update(ordered, ["order"])


def ensure_ruleset(
    *,
    workflow: Workflow,
    step: WorkflowStep | None,
    ruleset_type: str,
) -> Ruleset:
    """
    Ensure a ruleset exists for the given workflow step and type.
    """

    if step and step.ruleset and step.ruleset.ruleset_type == ruleset_type:
        ruleset = step.ruleset
    else:
        ruleset = Ruleset(org=workflow.org, user=workflow.user)
    ruleset.org = workflow.org
    ruleset.user = workflow.user
    ruleset.ruleset_type = ruleset_type
    ruleset.name = ruleset.name or f"ruleset-{uuid4().hex[:8]}"
    ruleset.version = ruleset.version or "1"
    return ruleset


def ensure_advanced_ruleset(
    workflow: Workflow,
    step: WorkflowStep | None,
    validator: Validator,
) -> Ruleset:
    """Guarantee a ruleset exists for validators requiring assertions."""
    ruleset = getattr(step, "ruleset", None)
    if ruleset is None:
        base_name = f"{validator.slug}-ruleset"
        ruleset_name = unique_ruleset_name(
            org=workflow.org,
            ruleset_type=validator.validation_type,
            base_name=base_name,
            version="1",
        )
        ruleset = Ruleset(
            org=workflow.org,
            user=workflow.user,
            name=ruleset_name,
            ruleset_type=validator.validation_type,
            version="1",
        )
        ruleset.save()
        if step:
            step.ruleset = ruleset
            if step.pk:
                step.save(update_fields=["ruleset", "modified"])
    return ruleset


def unique_ruleset_name(
    *,
    org: Organization,
    ruleset_type: str,
    base_name: str,
    version: str,
) -> str:
    name = base_name
    suffix = 2
    # Cap the numeric-suffix probing so a pathological set of colliding names
    # (e.g. malicious input seeding many collisions) can't spin this
    # per-iteration DB query indefinitely. Past the cap, fall back to a UUID
    # suffix, which is collision-proof in a single shot. (ADR 04-23
    # §hyg.unbounded_while)
    max_numeric_suffix = 100
    while Ruleset.objects.filter(
        org=org,
        ruleset_type=ruleset_type,
        name=name,
        version=version,
    ).exists():
        truncated_base = base_name[:240]
        if suffix > max_numeric_suffix:
            return f"{truncated_base}-{uuid4().hex[:8]}"
        name = f"{truncated_base}-{suffix}"
        suffix += 1
    return name


def build_json_schema_config(
    workflow: Workflow,
    form: JsonSchemaStepConfigForm,
    step: WorkflowStep | None,
) -> tuple[dict[str, Any], Ruleset | None]:
    source = form.cleaned_data.get("schema_source")
    text = (form.cleaned_data.get("schema_text") or "").strip()
    uploaded = form.cleaned_data.get("schema_file")
    schema_type = form.cleaned_data.get("schema_type")

    if schema_type not in JSONSchemaVersion.values:
        raise ValidationError(_("Select a valid JSON Schema draft."))

    if source == "keep" and step and step.ruleset_id:
        preview = (step.display_settings or {}).get("schema_text_preview", "")
        ruleset = step.ruleset
        metadata = dict(ruleset.metadata or {})
        metadata["schema_type"] = schema_type
        metadata.pop("schema", None)
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save(update_fields=["metadata"])
        config = {
            "schema_source": "keep",
            "schema_text_preview": preview,
            "schema_type": schema_type,
            "schema_type_label": str(JSONSchemaVersion(schema_type).label),
        }
        return config, ruleset

    ruleset = ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.JSON_SCHEMA,
    )

    schema_payload: str | None = None

    if source == "text":
        ruleset.rules_text = text
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file = None
        schema_payload = text
        preview = text[:1200]
    else:
        if uploaded is None:
            raise ValidationError(_("Upload a JSON schema file."))
        uploaded.seek(0)
        raw_bytes = uploaded.read()
        uploaded.seek(0)
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file.save(uploaded.name, uploaded, save=False)
        ruleset.rules_text = ""
        schema_payload = (
            raw_bytes.decode("utf-8", errors="replace")
            if isinstance(raw_bytes, bytes)
            else str(raw_bytes or "")
        )
        preview = schema_payload[:1200]

    metadata = dict(ruleset.metadata or {})
    metadata["schema_type"] = schema_type
    metadata.pop("schema", None)
    ruleset.metadata = metadata
    ruleset.full_clean()
    ruleset.save()

    config = {
        "schema_source": source,
        "schema_text_preview": preview,
        "schema_type": schema_type,
        "schema_type_label": str(JSONSchemaVersion(schema_type).label),
    }
    return config, ruleset


def build_tabular_config(
    workflow: Workflow,
    form: TabularStepConfigForm,
    step: WorkflowStep | None,
) -> tuple[dict[str, Any], Ruleset | None]:
    """Persist a Tabular Validator step's config to its ruleset.

    Mirrors :func:`build_json_schema_config`: the Table Schema descriptor is
    written to ``ruleset.rules_text`` and the dialect (delimiter / encoding /
    has_header) to ``ruleset.metadata`` — the exact two places the
    TabularValidator reads at run time. The form has already validated and
    normalised everything in ``clean()``, so this only writes; ``"keep"`` means
    the author edited dialect-only and left the existing schema in place.
    """
    cleaned = form.cleaned_data
    source = cleaned.get("schema_source", "")
    delimiter = cleaned.get("delimiter") or ""
    # Encoding is pinned to UTF-8 in V1 — submitted content reaches the
    # validator already decoded as UTF-8, so there is no editable encoding
    # field. Stored as a constant so the i.encoding step input stays accurate.
    encoding = "utf-8"
    has_header = bool(cleaned.get("has_header"))

    ruleset = ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.TABULAR,
    )
    metadata = dict(ruleset.metadata or {})
    metadata["delimiter"] = delimiter
    metadata["encoding"] = encoding
    metadata["has_header"] = has_header

    if source == "keep" and step and step.ruleset_id:
        prior_display = step.display_settings or {}
        preview = prior_display.get("schema_text_preview", "")
        column_count = prior_display.get("column_count", 0)
        required_column_count = prior_display.get("required_column_count")
        if required_column_count is None:
            try:
                existing_descriptor = json.loads(ruleset.rules_text or "{}")
            except (TypeError, json.JSONDecodeError):
                existing_descriptor = {}
            required_column_count = sum(
                1
                for field in existing_descriptor.get("fields", [])
                if isinstance(field, dict)
                and isinstance(field.get("constraints"), dict)
                and field["constraints"].get("required")
            )
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save()
    else:
        descriptor_json = cleaned.get("descriptor_json", "")
        ruleset.rules_text = descriptor_json
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file = None
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save()
        preview = descriptor_json[:1200]
        descriptor = cleaned.get("descriptor") or {}
        column_count = len(descriptor.get("fields", []))
        required_column_count = sum(
            1
            for field in descriptor.get("fields", [])
            if isinstance(field, dict)
            and isinstance(field.get("constraints"), dict)
            and field["constraints"].get("required")
        )

    delimiter_labels = {
        "": str(_("Auto-detect")),
        ",": str(_("Comma")),
        "\t": str(_("Tab")),
        ";": str(_("Semicolon")),
        "|": str(_("Pipe")),
    }
    config = {
        "schema_source": source,
        "schema_text_preview": preview,
        "delimiter": delimiter,
        "delimiter_label": delimiter_labels.get(delimiter, delimiter),
        "encoding": encoding,
        "has_header": has_header,
        "column_count": column_count,
        "required_column_count": required_column_count,
    }
    return config, ruleset


def build_xml_schema_config(
    workflow: Workflow,
    form: XmlSchemaStepConfigForm,
    step: WorkflowStep | None,
) -> tuple[dict[str, Any], Ruleset | None]:
    source = form.cleaned_data.get("schema_source")
    text = (form.cleaned_data.get("schema_text") or "").strip()
    uploaded = form.cleaned_data.get("schema_file")
    schema_type = form.cleaned_data.get("schema_type")

    if schema_type not in XMLSchemaType.values:
        raise ValidationError(_("Select a valid XML schema type."))

    if source == "keep" and step and step.ruleset_id:
        preview = (step.display_settings or {}).get("schema_text_preview", "")
        ruleset = step.ruleset
        metadata = dict(ruleset.metadata or {})
        metadata["schema_type"] = schema_type
        metadata.pop("schema", None)
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save(update_fields=["metadata"])
        config = {
            "schema_source": "keep",
            "schema_type": schema_type,
            "schema_text_preview": preview,
            "schema_type_label": str(XMLSchemaType(schema_type).label),
        }
        return config, ruleset

    ruleset = ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.XML_SCHEMA,
    )

    if source == "text":
        ruleset.rules_text = text
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file = None
        preview = text[:1200]
    else:
        if uploaded is None:
            raise ValidationError(_("Upload an XML schema file."))
        uploaded.seek(0)
        raw_bytes = uploaded.read()
        uploaded.seek(0)
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file.save(uploaded.name, uploaded, save=False)
        ruleset.rules_text = ""
        schema_payload = (
            raw_bytes.decode("utf-8", errors="replace")
            if isinstance(raw_bytes, bytes)
            else str(raw_bytes or "")
        )
        preview = schema_payload[:1200]

    metadata = dict(ruleset.metadata or {})
    metadata["schema_type"] = schema_type
    metadata.pop("schema", None)
    ruleset.metadata = metadata
    ruleset.full_clean()
    ruleset.save()

    config = {
        "schema_source": source,
        "schema_type": schema_type,
        "schema_text_preview": preview,
        "schema_type_label": str(XMLSchemaType(schema_type).label),
    }
    return config, ruleset


def build_schematron_config(
    workflow: Workflow,
    form: forms.Form,
    step: WorkflowStep | None,
) -> tuple[dict[str, Any], Ruleset]:
    """Materialise a Schematron step config + Ruleset from form data.

    Parallels :func:`build_xml_schema_config`: the author's Schematron
    source (pasted or uploaded, already validated by the form) is stored in
    ``Ruleset.rules_text``; editing with both fields blank keeps the saved
    rules. ``metadata`` records the source's sha256 (the run-provenance
    identity the container echoes back) and the optional
    ``rule_doc_url_template`` for D10 deep links.
    """
    import hashlib as _hashlib

    source = form.cleaned_data.get("schematron_source")
    url_template = (form.cleaned_data.get("rule_doc_url_template") or "").strip()

    if source == "keep" and step and step.ruleset_id:
        ruleset = step.ruleset
        metadata = dict(ruleset.metadata or {})
        metadata["rule_doc_url_template"] = url_template
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save(update_fields=["metadata"])
        preview = (step.display_settings or {}).get("schematron_preview", "")
        config = {
            "schematron_source": "keep",
            "schematron_preview": preview,
        }
        return config, ruleset

    payload = form.cleaned_data.get("schematron_payload") or ""
    if not payload.strip():
        raise ValidationError(
            _("Paste Schematron rules or upload a .sch file."),
        )

    ruleset = ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.SCHEMATRON,
    )
    ruleset.rules_text = payload
    if ruleset.rules_file:
        ruleset.rules_file.delete(save=False)
    ruleset.rules_file = None

    metadata = dict(ruleset.metadata or {})
    new_sha = _hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # The upload's filename, shown as "currently assigned" on the next
    # edit. Pasted rules carry none and clear any stale filename — except
    # when the pasted text is byte-identical to the stored rules (an
    # untouched edit form resubmitting its own prefill): same bytes, same
    # provenance, so the original upload's name survives.
    filename = form.cleaned_data.get("schematron_filename") or ""
    if not filename and metadata.get("schematron_sha256") == new_sha:
        filename = metadata.get("schematron_filename", "")
    metadata["schematron_sha256"] = new_sha
    metadata["rule_doc_url_template"] = url_template
    if filename:
        metadata["schematron_filename"] = filename
    else:
        metadata.pop("schematron_filename", None)
    ruleset.metadata = metadata
    ruleset.full_clean()
    ruleset.save()

    config = {
        "schematron_source": source,
        "schematron_preview": payload[:1200],
    }
    return config, ruleset


def build_shacl_config(
    workflow: Workflow,
    form: ShaclStepConfigForm,
    step: WorkflowStep | None,
    validator: Validator | None = None,
) -> tuple[dict[str, Any], Ruleset]:
    """Materialise a SHACL step config + Ruleset from form data.

    Parallels :func:`build_json_schema_config` and
    :func:`build_xml_schema_config`. Differences:

    - **Multi-file uploads.** Shapes and ontologies each accept a list
      of files (plus optional inline text). All shapes are concatenated
      into ``Ruleset.rules_text`` with file-boundary comments; all
      ontologies go into ``Ruleset.metadata['ontology_text']``.
    - **Bundled standards.** Brick and QUDT checkboxes write the
      selected slugs to ``Ruleset.metadata['bundled_standards']``. The
      engine loads them at validation time (Phase 1 stubs them; Phase 2
      ships the static asset content).
    - **Engine knobs.** ``inference_mode``, ``advanced_shacl``, and
      ``submission_format`` live in ``Ruleset.metadata`` so the engine
      can resolve them per step without an extra DB column.

    See ADR-2026-05-18 ``SHACL Validator for RDF Graph Validation``
    section "Ruleset persistence" for the data shape.
    """
    cleaned = form.cleaned_data
    shape_files = cleaned.get("shapes_files") or []
    shape_text = (cleaned.get("shapes_text") or "").strip()
    ontology_files = cleaned.get("ontology_files") or []
    ontology_text = (cleaned.get("ontology_text") or "").strip()
    inference_mode = cleaned.get("inference_mode") or "rdfs"
    advanced_shacl = bool(cleaned.get("advanced_shacl"))
    submission_format = cleaned.get("submission_format") or "auto"
    shacl_result_handling = (
        cleaned.get("shacl_result_handling") or SHACL_RESULT_HANDLING_DEFAULT
    )
    library_shapes_text = ""
    library_metadata: dict[str, Any] = {}
    library_snapshot: dict[str, Any] | None = None
    library_ruleset = getattr(validator, "default_ruleset", None)
    if library_ruleset is not None and not getattr(validator, "is_system", False):
        library_shapes_text = getattr(library_ruleset, "rules", "") or ""
        library_metadata = dict(getattr(library_ruleset, "metadata", None) or {})
        library_snapshot = {
            "validator_id": validator.pk,
            "validator_slug": validator.slug,
            "default_ruleset_id": library_ruleset.pk,
            "default_ruleset_version": library_ruleset.version,
            "rules_sha256": sha256(
                library_shapes_text.encode("utf-8"),
            ).hexdigest(),
            "ontology_sha256": sha256(
                (library_metadata.get("ontology_text", "") or "").encode("utf-8"),
            ).hexdigest(),
        }
    bundled_standards: list[str] = []
    if cleaned.get("bundle_brick"):
        bundled_standards.append("brick-1.4")
    if cleaned.get("bundle_qudt"):
        bundled_standards.append("qudt-2.1")

    ruleset = ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.SHACL,
    )
    existing_metadata = dict(ruleset.metadata or {})
    keep_existing_shapes = bool(step and step.ruleset_id) and not (
        shape_files or shape_text
    )
    snapshot_to_persist = (
        existing_metadata.get("library_default_snapshot")
        if keep_existing_shapes
        else library_snapshot
    )
    replace_ontology = bool(ontology_files or ontology_text)

    # Fresh SHACL uploads replace the step-owned shapes. When the author is
    # editing an existing step and leaves shapes blank, keep the saved shapes
    # while still allowing ontology-only edits below.
    if keep_existing_shapes:
        shapes_concat = ruleset.rules_text
        shape_files_meta = existing_metadata.get("shape_files", []) or []
        has_inline_shapes = bool(existing_metadata.get("has_inline_shapes"))
        preview = (
            (step.display_settings or {}).get("shapes_text_preview", "") if step else ""
        )
        if not preview:
            preview = shapes_concat[:1200]
    else:
        step_shapes_concat, shape_files_meta = concatenate_uploaded_files(
            shape_files,
            shape_text,
        )
        shapes_parts = [p for p in (library_shapes_text, step_shapes_concat) if p]
        shapes_concat = FILE_SEPARATOR.join(shapes_parts)
        has_inline_shapes = bool(shape_text)
        preview = shapes_concat[:1200]

    # Ontologies have their own keep/replace semantics so authors can adjust
    # inference context without re-uploading the usually much larger shapes.
    if replace_ontology:
        step_ontology_concat, ontology_files_meta = concatenate_uploaded_files(
            ontology_files,
            ontology_text,
        )
        ontology_parts = [
            p
            for p in (
                library_metadata.get("ontology_text", "") or "",
                step_ontology_concat,
            )
            if p
        ]
        ontology_concat = FILE_SEPARATOR.join(ontology_parts)
        has_inline_ontology = bool(ontology_text)
    elif not keep_existing_shapes:
        ontology_concat = library_metadata.get("ontology_text", "") or ""
        ontology_files_meta = []
        has_inline_ontology = bool(ontology_concat)
    else:
        ontology_concat = existing_metadata.get("ontology_text", "") or ""
        ontology_files_meta = existing_metadata.get("ontology_files", []) or []
        has_inline_ontology = bool(existing_metadata.get("has_inline_ontology"))

    ruleset.rules_text = shapes_concat
    if not keep_existing_shapes and ruleset.rules_file:
        ruleset.rules_file.delete(save=False)
        ruleset.rules_file = None

    metadata = existing_metadata
    metadata.pop("sparql_assertions", None)
    metadata.update(
        {
            "shape_files": shape_files_meta,
            "has_inline_shapes": has_inline_shapes,
            "ontology_text": ontology_concat,
            "ontology_files": ontology_files_meta,
            "has_inline_ontology": has_inline_ontology,
            "bundled_standards": bundled_standards,
            "inference_mode": inference_mode,
            "advanced_shacl": advanced_shacl,
            "submission_format": submission_format,
            "shacl_result_handling": shacl_result_handling,
            "library_default_inlined": snapshot_to_persist is not None
            or bool(existing_metadata.get("library_default_inlined")),
            "library_default_snapshot": snapshot_to_persist,
        },
    )
    ruleset.metadata = metadata
    ruleset.full_clean()
    ruleset.save()

    config = {
        "shape_files": shape_files_meta,
        "ontology_files": ontology_files_meta,
        "bundled_standards": bundled_standards,
        "inference_mode": inference_mode,
        "advanced_shacl": advanced_shacl,
        "submission_format": submission_format,
        "shacl_result_handling": shacl_result_handling,
        # First 1200 chars of the merged shapes for the step editor's
        # read-only preview, mirroring build_json_schema_config.
        "shapes_text_preview": preview,
        "library_default_snapshot": metadata.get("library_default_snapshot"),
    }
    return config, ruleset


def _validate_and_scan_template(
    template_file,
    *,
    case_sensitive: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate an IDF template and return ``(variable_dicts, warnings)``.

    Reads the file, runs the validation pipeline, and converts the scan
    result into variable dicts for ``sync_step_template_io_definitions``.

    Raises:
        ValidationError: If the template fails validation checks.
    """
    from validibot.validations.utils.idf_template import validate_idf_template

    # Guard against I/O failures (temp file cleaned up, storage error).
    try:
        content = template_file.read()
    except OSError as exc:
        raise ValidationError(
            f"Could not read the uploaded template file: {exc}. "
            "Please try uploading again."
        ) from exc
    result = validate_idf_template(
        filename=template_file.name,
        content=content,
        case_sensitive=case_sensitive,
    )

    if result.errors:
        raise ValidationError(result.errors)

    template_vars = [
        {
            "name": var_ctx.name,
            "description": var_ctx.label,
            "default": "",
            "units": var_ctx.units,
            "variable_type": "text",
            "min_value": None,
            "min_exclusive": False,
            "max_value": None,
            "max_exclusive": False,
            "choices": [],
        }
        for var_ctx in result.scan_result.variables
    ]
    return template_vars, result.warnings


def build_energyplus_config(
    form: EnergyPlusStepConfigForm,
    step: WorkflowStep | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the JSON config dict for an EnergyPlus step.

    Returns a ``(config, template_vars)`` tuple.  The ``template_vars``
    list is passed to ``sync_step_template_io_definitions()`` to create/update
    ``StepIODefinition`` rows — it is **not** written to the config JSON.

    The ``validation_mode`` field determines which config keys are
    populated:

    - **direct**: Optional model-review checks and ``run_simulation`` are stored.
      Template metadata is cleared.
    - **template**: Case sensitivity and displayed step outputs are stored.
      IDF-check and simulation flags are omitted (the template pipeline
      always runs the simulation).

    Resource file references (weather files, templates) are stored
    relationally via ``WorkflowStepResource`` and are synced separately
    by ``_sync_energyplus_resources()`` after the step is saved.

    Template handling:

    - **Template upload**: Validates the IDF, scans for ``$VARIABLE_NAME``
      placeholders, and returns the scanned variables.
      Raises ``ValidationError`` if the file fails validation.
    - **Template removal** (switching to direct mode or explicit remove):
      Resets ``case_sensitive`` to True and returns an empty variable list.
    - **No change**: Returns an empty variable list (existing
      ``StepIODefinition`` rows are left unchanged).

    The template *file* itself is persisted by
    ``_sync_energyplus_resources()`` after ``step.save()``.
    """
    validation_mode = form.cleaned_data.get(
        "validation_mode",
        EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
    )

    config: dict[str, Any] = {
        "validation_mode": validation_mode,
        "review_profile": form.cleaned_data.get("review_profile", "standard"),
        "timestep_per_hour": form.cleaned_data.get("timestep_per_hour", 4),
        "show_energyplus_warnings": form.cleaned_data.get(
            "show_energyplus_warnings",
            True,
        ),
    }

    if validation_mode == EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT:
        # Direct IDF mode — store review-check/simulation settings,
        # clear any template metadata.
        config["idf_checks"] = form.cleaned_data.get("idf_checks", [])
        config["run_simulation"] = form.cleaned_data.get("run_simulation", False)
        config["case_sensitive"] = True
        config["display_step_outputs"] = []
        # Tell _sync_energyplus_resources to remove the template file
        # if one exists from a previous template-mode configuration.
        form.cleaned_data["remove_template"] = True
        return config, []

    # ── Template mode ─────────────────────────────────────────────
    config["idf_checks"] = []
    config["run_simulation"] = True

    remove_template = form.cleaned_data.get("remove_template", False)
    template_file = form.cleaned_data.get("template_file")

    template_vars: list[dict[str, Any]] = []

    if remove_template:
        # Author clicked "Remove template" — clear all template metadata.
        config["case_sensitive"] = True
        config["display_step_outputs"] = []
    elif template_file:
        # New template uploaded — validate, scan, and return vars for
        # _sync_template_step_io() to persist as StepIODefinition rows.
        template_vars, template_warnings = _validate_and_scan_template(
            template_file,
            case_sensitive=form.cleaned_data.get("case_sensitive", True),
        )

        config["case_sensitive"] = form.cleaned_data.get("case_sensitive", True)
        config["display_step_outputs"] = []

        # Attach warnings to the form so the view can display them.
        form.template_warnings = template_warnings
    elif step:
        # No upload, no removal — existing StepIODefinition rows are
        # left unchanged.  Variable annotation editing happens in the
        # dedicated template variables card on the step detail page.
        config["case_sensitive"] = form.cleaned_data.get("case_sensitive", True)
        config["display_step_outputs"] = (step.display_settings or {}).get(
            "display_step_outputs",
            [],
        )

    return config, template_vars


def _parse_optional_float(value: str) -> float | None:
    """Convert a string to float, returning None for empty values.

    Used by ``merge_template_variable_annotations()`` to parse min/max
    values from the template variable editor form fields.

    Logs a warning for non-numeric input so the author gets feedback
    via server logs.  Returns None to avoid crashing the form save,
    since template variable annotations are advisory rather than
    critical — a missing constraint is safe (no constraint applied),
    but a crash would prevent saving other valid annotations.
    """
    if not value or not value.strip():
        return None
    try:
        return float(value.strip())
    except (ValueError, TypeError):
        logger.warning(
            "Non-numeric value %r passed for a min/max constraint — "
            "treating as empty (no constraint).",
            value,
        )
        return None


def _parse_choices(value: str) -> list[str]:
    """Split a newline-separated string into a list of non-empty choices.

    Used by ``build_energyplus_config()`` to parse the "Allowed values"
    textarea from the template variable editor.
    """
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def merge_template_variable_annotations(
    existing_vars: list[dict[str, Any]],
    form_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge author annotations from the template variable editor form.

    Takes the existing template variable dicts and merges in the updated
    annotations from the form's cleaned_data. Variable **names** are
    immutable (set during IDF scan); all other fields (description,
    default, units, type, constraints, choices) can be updated.

    Returns:
        New list of template variable dicts with merged annotations.
    """
    merged = []
    for i, var in enumerate(existing_vars):
        prefix = f"tplvar_{i}"
        merged.append(
            {
                "name": var["name"],
                "description": form_data.get(
                    f"{prefix}_description",
                    var.get("description", ""),
                ),
                "default": form_data.get(
                    f"{prefix}_default",
                    var.get("default", ""),
                ),
                "units": form_data.get(
                    f"{prefix}_units",
                    var.get("units", ""),
                ),
                "variable_type": form_data.get(
                    f"{prefix}_variable_type",
                    var.get("variable_type", "text"),
                ),
                "min_value": _parse_optional_float(
                    form_data.get(f"{prefix}_min_value", ""),
                ),
                "min_exclusive": form_data.get(
                    f"{prefix}_min_exclusive",
                    False,
                ),
                "max_value": _parse_optional_float(
                    form_data.get(f"{prefix}_max_value", ""),
                ),
                "max_exclusive": form_data.get(
                    f"{prefix}_max_exclusive",
                    False,
                ),
                "choices": _parse_choices(
                    form_data.get(f"{prefix}_choices", ""),
                ),
            }
        )
    return merged


def save_template_variable_annotations(
    form: Any,
) -> None:
    """Save template variable annotations directly to StepIODefinition rows.

    Reads the form's ``_template_variable_meta`` (which carries
    ``_io_definition_pk`` and ``_binding_pk``) and ``cleaned_data`` to update
    the relational step I/O model in place.

    Args:
        form: A ``TemplateVariableAnnotationForm`` instance with
            ``cleaned_data`` and ``_template_variable_meta`` populated.
    """
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition
    from validibot.validations.step_io_metadata.metadata import TemplateStepIOMetadata

    for meta in form._template_variable_meta:
        prefix = meta["prefix"]
        io_definition_pk = meta.get("_io_definition_pk")
        binding_pk = meta.get("_binding_pk")
        if not io_definition_pk:
            continue

        variable_type = form.cleaned_data.get(
            f"{prefix}_variable_type",
            "text",
        )
        metadata = TemplateStepIOMetadata(
            variable_type=variable_type,
            min_value=_parse_optional_float(
                form.cleaned_data.get(f"{prefix}_min_value", ""),
            ),
            min_exclusive=form.cleaned_data.get(
                f"{prefix}_min_exclusive",
                False,
            ),
            max_value=_parse_optional_float(
                form.cleaned_data.get(f"{prefix}_max_value", ""),
            ),
            max_exclusive=form.cleaned_data.get(
                f"{prefix}_max_exclusive",
                False,
            ),
            choices=_parse_choices(
                form.cleaned_data.get(f"{prefix}_choices", ""),
            ),
        ).model_dump()

        StepIODefinition.objects.filter(pk=io_definition_pk).update(
            label=form.cleaned_data.get(f"{prefix}_description", ""),
            unit=form.cleaned_data.get(f"{prefix}_units", ""),
            metadata=metadata,
            provider_binding={"variable_type": variable_type},
        )

        if binding_pk:
            default_val = form.cleaned_data.get(f"{prefix}_default", "")
            StepInputBinding.objects.filter(pk=binding_pk).update(
                default_value=default_val if default_val else None,
                is_required=not bool(default_val),
            )


def step_has_template_variables(step: WorkflowStep) -> bool:
    """Condition function for the template variables step editor card.

    Returns True when the step has template-origin StepIODefinitions,
    indicating the template variables card should be rendered.
    """
    from validibot.validations.constants import StepIOOriginKind

    return step.step_io_definitions.filter(
        origin_kind=StepIOOriginKind.TEMPLATE,
    ).exists()


def _is_step_output_shown(slug: str, display_step_outputs: list[str]) -> bool:
    """Determine whether an output value should show a green check.

    An empty selection means no step outputs are shown to submitters.
    """
    return slug in display_step_outputs


def build_step_io_context(
    step: WorkflowStep,
) -> dict[str, Any]:
    """Build unified step input/output lists from ``StepIODefinition`` rows.

    Queries the unified ``StepIODefinition`` and ``StepInputBinding`` models
    to produce a single list of input and output definitions for the step detail
    card.

    Returns a dict with keys:
        input_values: List of step input dictionaries.
        output_values: List of step output dictionaries.
        has_inputs: Whether any step inputs exist.
        has_outputs: Whether any step outputs exist.
    """
    from validibot.validations.constants import StepIODirection
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.constants import ValidationType
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition
    from validibot.validations.models import WorkflowStepIOPromotion

    # ``display_step_outputs`` is cosmetic (which output values the submitter
    # sees), so it lives in the display bucket (ADR-2026-06-18).
    display_step_outputs = (step.display_settings or {}).get("display_step_outputs", [])

    # Query step-owned and validator-owned I/O definitions.
    step_io_definitions = list(
        StepIODefinition.objects.filter(workflow_step=step).order_by("order", "pk")
    )
    validator = step.validator
    validator_io_definitions = []
    allowed_output_keys: frozenset[str] | None = None
    output_group_heading = ""
    if validator:
        validator_io_definitions = list(
            validator.step_io_definitions.all().order_by("order", "pk")
        )
        if validator.validation_type == ValidationType.PORTFOLIO_MANAGER:
            from validibot.validations.validators.portfolio_manager import (
                output_groups as pm_output_groups,
            )

            structure = (step.config or {}).get(
                "submission_structure",
                "single_report",
            )
            allowed_output_keys = pm_output_groups.output_keys_for_structure(structure)
            output_group_heading = pm_output_groups.output_group_label(structure)

    # Build a binding lookup keyed by io_definition PK.
    binding_map: dict[int, StepInputBinding] = {}
    for b in StepInputBinding.objects.filter(
        workflow_step=step,
    ).select_related("io_definition"):
        binding_map[b.io_definition_id] = b

    # Build an overlay lookup keyed by io_definition PK. The
    # overlay carries workflow-scoped promoted names for
    # validator-owned StepIODefinitions only — step-owned rows are
    # forbidden from having overlays
    # (``WorkflowStepIOPromotion.clean()`` enforces this so runtime
    # never injects the same value twice). Step-owned rows use their
    # in-row ``promoted_signal_name`` field; this map only ever
    # contains entries keyed by validator-owned row pks.
    overlay_map: dict[int, str] = {}
    for overlay in WorkflowStepIOPromotion.objects.filter(workflow_step=step):
        overlay_map[overlay.io_definition_id] = overlay.promoted_signal_name

    # -- Step inputs --
    #
    # The Step Inputs table shows BOTH step-owned and validator-owned
    # input rows. Two motivating cases (per ADR-2026-05-22 + the
    # May 2026 review):
    #
    # 1. EnergyPlus template mode — step-owned rows are template
    #    variables (height, setpoint, etc.) and validator-owned rows
    #    are parser facts (zone_count, idf_version, north_axis_deg).
    #    Both populate i.*. If the table hid validator-owned inputs
    #    whenever step-owned ones existed, template-mode authors
    #    would never see the parser facts in the UI and couldn't
    #    promote them — even though autocomplete/runtime knew about
    #    them.
    # 2. FMU step — step-owned rows are FMU model inputs PLUS the
    #    seven Phase 6 parser-fact rows (model_name, fmi_version, …).
    #    The validator catalog is non-empty (parser facts come from
    #    the system FMU validator catalog), but for the system FMU
    #    case the step-owned rows cover both sets, so we still render
    #    primarily from step-owned with the catalog providing labels.
    #
    # The previous "if not input_values" guard incorrectly conflated
    # FMU's "no validator catalog" case with template mode's "both
    # sets matter". Deduplicating by contract_key lets a step-owned
    # row override a validator-owned row of the same name (rare but
    # possible) while still surfacing everything the validator
    # contributes.
    input_values: list[dict[str, Any]] = []
    seen_input_keys: set[str] = set()

    def _build_input_row(
        io_definition,
        *,
        prefer_native_label: bool,
    ) -> dict[str, Any]:
        binding = binding_map.get(io_definition.pk)
        default_val = ""
        if binding and binding.default_value is not None:
            default_val = str(binding.default_value)
        validator_managed = (
            io_definition.source_kind == "internal"
            or not io_definition.is_path_editable
        )
        is_required = (
            False if validator_managed else (binding.is_required if binding else True)
        )
        source_path = binding.source_data_path if binding else ""
        label_parts = (
            [
                io_definition.label,
                io_definition.native_name,
                io_definition.contract_key,
            ]
            if prefer_native_label
            else [io_definition.label, io_definition.contract_key]
        )
        return {
            "slug": io_definition.contract_key,
            "label": next(
                (part for part in label_parts if part),
                io_definition.contract_key,
            ),
            "source": io_definition.origin_kind,
            "required": is_required,
            "default_value": default_val,
            "source_data_path": source_path,
            "io_definition": io_definition,
            "binding": binding,
            "validator_managed": validator_managed,
            # Workflow-scoped promotion name lookup:
            # - Step-owned rows carry the name in ``promoted_signal_name``
            #   (validator-owned overlays are forbidden by
            #   ``WorkflowStepIOPromotion.clean()``).
            # - Validator-owned rows look it up in ``overlay_map``,
            #   which is keyed only by validator-owned pks.
            # The combined ``or`` works for both row types because
            # exactly one of the two sources can be set per row.
            "signal_name": (
                overlay_map.get(io_definition.pk)
                or io_definition.promoted_signal_name
                or ""
            ),
        }

    # First, step-owned inputs (template variables, FMU model inputs).
    # native_name is preferred for these because FMU rows preserve the
    # provider-original variable name.
    for io_definition in step_io_definitions:
        if io_definition.direction != StepIODirection.INPUT:
            continue
        if io_definition.io_medium == StepIOMedium.ARTIFACT:
            continue
        input_values.append(_build_input_row(io_definition, prefer_native_label=True))
        seen_input_keys.add(io_definition.contract_key)

    # Then, validator-owned inputs for any contract_key not already
    # represented step-side. This is the line the May 2026 P1 review
    # called out: validator-owned parser facts (i.zone_count) MUST
    # appear in template-mode steps even though step-owned template
    # variables already populated input_values.
    for io_definition in validator_io_definitions:
        if io_definition.direction != StepIODirection.INPUT:
            continue
        if io_definition.io_medium == StepIOMedium.ARTIFACT:
            continue
        if io_definition.contract_key in seen_input_keys:
            continue
        input_values.append(_build_input_row(io_definition, prefer_native_label=False))
        seen_input_keys.add(io_definition.contract_key)

    # Tabular dataset metadata is produced at runtime rather than stored as
    # StepIODefinition rows. It is still genuine i.* input data, so expose the
    # canonical inventory in the same card and count as persisted step inputs.
    if validator and validator.validation_type == ValidationType.TABULAR:
        from validibot.validations.validators.tabular.metadata import (
            TABULAR_DATASET_INPUTS,
        )

        for contract_key, label in TABULAR_DATASET_INPUTS:
            if contract_key in seen_input_keys:
                continue
            input_values.append(
                {
                    "slug": contract_key,
                    "label": label,
                    "source": "tabular",
                    "required": False,
                    "default_value": "",
                    "source_data_path": "",
                    "io_definition": None,
                    "binding": None,
                    "validator_managed": True,
                    "signal_name": "",
                },
            )
            seen_input_keys.add(contract_key)

    # -- Step outputs --
    output_values: list[dict[str, Any]] = []

    for io_definition in step_io_definitions:
        if io_definition.direction != StepIODirection.OUTPUT:
            continue
        if io_definition.io_medium == StepIOMedium.ARTIFACT:
            continue
        if (
            allowed_output_keys is not None
            and io_definition.contract_key not in allowed_output_keys
        ):
            continue
        show = _is_step_output_shown(
            io_definition.contract_key,
            display_step_outputs,
        )
        output_values.append(
            {
                "slug": io_definition.contract_key,
                "label": (
                    io_definition.label
                    or io_definition.native_name
                    or io_definition.contract_key
                ),
                "show_to_user": show,
                "io_definition": io_definition,
                "signal_name": io_definition.promoted_signal_name or "",
            },
        )

    for io_definition in validator_io_definitions:
        if io_definition.direction != StepIODirection.OUTPUT:
            continue
        if io_definition.io_medium == StepIOMedium.ARTIFACT:
            continue
        if (
            allowed_output_keys is not None
            and io_definition.contract_key not in allowed_output_keys
        ):
            continue
        show = _is_step_output_shown(
            io_definition.contract_key,
            display_step_outputs,
        )
        output_values.append(
            {
                "slug": io_definition.contract_key,
                "label": io_definition.label or io_definition.contract_key,
                "show_to_user": show,
                "io_definition": io_definition,
                # Validator-owned outputs read the promotion name from
                # the overlay table (workflow-scoped). The in-row
                # ``promoted_signal_name`` on validator-owned rows is
                # never written by the UI — the fall-back only matters
                # if a seeder or test fixture set it directly.
                "signal_name": (
                    overlay_map.get(io_definition.pk)
                    or io_definition.promoted_signal_name
                    or ""
                ),
            },
        )

    has_unmapped_required = any(
        s["required"] and not s["source_data_path"] for s in input_values
    )

    return {
        "input_values": input_values,
        "output_values": output_values,
        "has_inputs": bool(input_values),
        "has_outputs": bool(output_values),
        "has_unmapped_required": has_unmapped_required,
        "output_group_label": output_group_heading,
    }


def _sync_energyplus_resources(
    step: WorkflowStep,
    form: EnergyPlusStepConfigForm,
) -> None:
    """Sync the relational ``WorkflowStepResource`` rows for an EnergyPlus step.

    Called *after* ``step.save()`` so the step has a PK. Replaces the old
    approach of storing UUID strings in ``config["resource_file_ids"]``.

    Handles two resource roles:

    - **WEATHER_FILE** — catalog reference to a shared ``ValidatorResourceFile``.
    - **MODEL_TEMPLATE** — step-owned file uploaded for parameterized templates
      (Phase 2).  The template file is stored directly on the
      ``WorkflowStepResource`` via ``step_resource_file``.
    """
    from validibot.validations.constants import ENERGYPLUS_MODEL_TEMPLATE
    from validibot.validations.constants import BindingSourceScope
    from validibot.validations.models import ValidatorResourceFile

    # ── Weather file ────────────────────────────────────────────────
    weather_file_id = form.cleaned_data.get("weather_file", "")
    weather_file_source = form.cleaned_data.get("weather_file_source")

    # Remove existing weather file resources for this step
    step.step_resources.filter(role=WorkflowStepResource.WEATHER_FILE).delete()

    if (
        weather_file_source
        and weather_file_source != BindingSourceScope.WORKFLOW_RESOURCE
    ):
        weather_file_id = ""

    # Create new one if a weather file was selected
    if weather_file_id:
        try:
            vrf = ValidatorResourceFile.objects.get(pk=weather_file_id)
            WorkflowStepResource.objects.create(
                step=step,
                role=WorkflowStepResource.WEATHER_FILE,
                validator_resource_file=vrf,
            )
        except ValidatorResourceFile.DoesNotExist:
            logger.warning(
                "Weather file UUID %s not found when saving step %s.",
                weather_file_id,
                step.pk,
            )

    # ── Model template (Phase 2) ──────────────────────────────────
    remove_template = form.cleaned_data.get("remove_template", False)
    template_file = form.cleaned_data.get("template_file")

    if remove_template:
        # Author chose to remove the template — delete the resource row
        # (and the step-owned file via Django storage cleanup).
        step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        ).delete()
    elif template_file:
        # New template uploaded — replace any existing template resource.
        step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        ).delete()

        # Reset the file pointer — build_energyplus_config() already
        # called .read() for validation.
        template_file.seek(0)

        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=template_file,
            filename=template_file.name,
            resource_type=ENERGYPLUS_MODEL_TEMPLATE,
        )


def _sync_step_file_port_bindings(step: WorkflowStep, form: forms.Form) -> None:
    """Persist author-selected file-port sources into StepInputBinding rows."""
    build_updates = getattr(form, "build_file_port_binding_updates", None)
    if not callable(build_updates):
        return

    from validibot.validations.services.artifact_bindings import (
        set_artifact_input_binding,
    )

    for update in build_updates():
        set_artifact_input_binding(
            consumer_step=step,
            consumer_port=update["io_definition"],
            source_scope=update["source_scope"],
            source_data_path=update.get("source_data_path", ""),
            artifact_reference=(
                update.get("artifact_reference")
                or (
                    update.get("source_data_path", "")
                    if update["source_scope"] == BindingSourceScope.UPSTREAM_ARTIFACT
                    else ""
                )
            ),
            expected_revision=update.get("expected_revision"),
        )


def build_fmu_config(
    form: FMUValidatorStepConfigForm,
    step: WorkflowStep,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    """Build step config for an FMU step with a step-level FMU upload.

    Returns a ``(config, fmu_variables)`` tuple. ``fmu_variables`` is a
    TRI-STATE that ``_sync_fmu_step_io()`` interprets to decide
    StepIODefinition lifecycle:

      - ``None``  → no change to FMU I/O definitions (no new upload, no
                    removal; e.g., author edited simulation timing).
                    Preserves existing rows AND their parser-fact
                    StepIODefinitions.
      - ``[]``    → user explicitly removed the FMU. Clears every
                    FMU-origin StepIODefinition (variables + parser
                    facts).
      - ``[...]`` → new FMU uploaded. Reconciles per-variable rows
                    via ``sync_step_fmu_io_definitions``.

    The list-only contract (``[]`` for "no new upload") was the May
    2026 review's P1 finding: editing simulation timing without
    re-uploading the FMU cleared the step's I/O definitions and any author-
    built assertions, even though the FMU resource was untouched.

    For library FMU validators (non-system), returns ``({}, None)`` —
    I/O definitions come from the validator's ``StepIODefinition`` rows and
    are never managed at the step level.

    Mirrors ``build_energyplus_config()`` for consistency.
    """
    if not getattr(form, "is_system_validator", False):
        # Library validator — no step-level config or I/O-definition sync.
        return {}, None

    from validibot.validations.services.fmu import FMUIntrospectionError
    from validibot.validations.services.fmu import build_introspection_metadata
    from validibot.validations.services.fmu import introspect_fmu

    # Preserve existing config (simulation + introspection) if no new upload
    existing_config = step.config or {}
    fmu_file = form.cleaned_data.get("fmu_file")
    remove_fmu = form.cleaned_data.get("remove_fmu", False)

    if remove_fmu:
        # Author is removing the FMU — clear everything (including the
        # stamped introspection facts so i.* doesn't keep resolving
        # against a ghost FMU). An empty list indicates removal to
        # _sync_fmu_step_io.
        return {}, []

    # Default for the "no new upload, no removal" path: preserve.
    # The tri-state means a None here tells the sync function to
    # leave StepIODefinitions alone, even though we still rebuild
    # the config dict from form fields below.
    fmu_variables: list[dict[str, Any]] | None = None
    introspection_metadata: dict[str, Any] = {}

    if fmu_file:
        # New FMU uploaded — introspect it
        raw_bytes = fmu_file.read()
        try:
            result = introspect_fmu(payload=raw_bytes, filename=fmu_file.name)
        except FMUIntrospectionError as exc:
            raise ValidationError(str(exc)) from exc

        # Check that the FMU has at least one input or output variable
        has_io = any(v.causality in ("input", "output") for v in result.variables)
        if not has_io:
            raise ValidationError(
                _(
                    "This FMU has no input or output variables. An FMU must "
                    "have at least one input or output variable to be used "
                    "in a workflow step."
                )
            )

        # Convert FMUVariableInfo dataclasses to dicts for
        # _sync_fmu_step_io() to persist as StepIODefinition rows.
        fmu_variables = [
            {
                "name": v.name,
                "causality": v.causality,
                "variability": v.variability,
                "value_reference": v.value_reference,
                "value_type": v.value_type,
                "unit": v.unit,
                "description": v.description,
                "label": "",
            }
            for v in result.variables
        ]

        # Stamp the parser-fact dict so FMUValidator.extract_input_values
        # can resolve i.fmi_version / i.input_variable_count / etc. at
        # runtime for step-level uploads against the system FMU
        # validator. Without this, the static catalog entries on the
        # system FMU validator would declare parser facts that always
        # resolved to null — exactly the May 2026 P1 finding.
        introspection_metadata = build_introspection_metadata(result)

        # Build simulation config from DefaultExperiment defaults
        sim = result.simulation_defaults
        sim_config = {
            "start_time": sim.start_time,
            "stop_time": sim.stop_time,
            "step_size": sim.step_size,
            "tolerance": sim.tolerance,
        }
    else:
        # No new upload — keep existing simulation config + introspection
        # facts (the file hasn't changed, so the facts are still valid).
        sim_config = existing_config.get("fmu_simulation") or {}
        existing_introspection = existing_config.get("fmu_introspection")
        if isinstance(existing_introspection, dict):
            introspection_metadata = existing_introspection

    # Apply simulation setting overrides from the form
    for field_name, config_key in [
        ("sim_start_time", "start_time"),
        ("sim_stop_time", "stop_time"),
        ("sim_step_size", "step_size"),
        ("sim_tolerance", "tolerance"),
    ]:
        form_value = form.cleaned_data.get(field_name)
        if form_value is not None:
            sim_config[config_key] = form_value

    config: dict[str, Any] = {}
    if any(v is not None for v in sim_config.values()):
        config["fmu_simulation"] = sim_config
    if introspection_metadata:
        config["fmu_introspection"] = introspection_metadata

    return config, fmu_variables


def _sync_fmu_resources(
    step: WorkflowStep,
    form: FMUValidatorStepConfigForm,
) -> None:
    """Sync the ``WorkflowStepResource`` for a step-level FMU upload.

    Called *after* ``step.save()`` so the step has a PK.  Handles three cases:

    - New upload: create ``FMU_MODEL`` resource
    - Replace: delete old, create new
    - Remove: delete without replacement

    Mirrors ``_sync_energyplus_resources()`` for consistency.
    """
    remove_fmu = form.cleaned_data.get("remove_fmu", False)
    fmu_file = form.cleaned_data.get("fmu_file")

    if remove_fmu:
        step.step_resources.filter(
            role=WorkflowStepResource.FMU_MODEL,
        ).delete()
    elif fmu_file:
        # Replace any existing FMU resource
        step.step_resources.filter(
            role=WorkflowStepResource.FMU_MODEL,
        ).delete()

        # Reset file pointer — build_fmu_config() already called .read()
        fmu_file.seek(0)

        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.FMU_MODEL,
            step_resource_file=fmu_file,
            filename=fmu_file.name,
            resource_type="application/octet-stream",
        )


def build_ai_config(form: AiAssistStepConfigForm) -> dict[str, Any]:
    selectors = form.cleaned_data.get("selectors", [])
    policy_rules = form.cleaned_data.get("policy_rules", [])
    return {
        "template": form.cleaned_data.get("template"),
        "mode": form.cleaned_data.get("mode"),
        "cost_cap_cents": form.cleaned_data.get("cost_cap_cents"),
        "selectors": selectors,
        "policy_rules": [asdict(rule) for rule in policy_rules],
    }


def build_portfolio_manager_config(
    form: PortfolioManagerStepConfigForm,
) -> dict[str, Any]:
    """Build the semantic convenience settings sent to the isolated backend."""
    cleaned = form.cleaned_data
    default_euit = cleaned.get("default_euit_kbtu_ft2_yr")
    near_percent = cleaned.get("near_target_percent")
    return {
        "submission_structure": cleaned.get("submission_structure") or "single_report",
        "default_euit_kbtu_ft2_yr": (
            float(default_euit) if default_euit is not None else None
        ),
        "compare_to_euit": bool(cleaned.get("compare_to_euit")),
        "near_target_percent": (
            float(near_percent) if near_percent is not None else 10
        ),
        "require_complete_reporting_period": bool(
            cleaned.get("require_complete_reporting_period")
        ),
        "minimum_reporting_period_months": (
            cleaned.get("minimum_reporting_period_months") or 12
        ),
        "maximum_reporting_period_age_months": cleaned.get(
            "maximum_reporting_period_age_months"
        ),
        "require_benchmark_ready": bool(cleaned.get("require_benchmark_ready")),
        "require_form_c_ready": bool(cleaned.get("require_form_c_ready")),
        "require_weather_normalized_site_eui": bool(
            cleaned.get("require_weather_normalized_site_eui")
        ),
        "require_washington_standard_id": bool(
            cleaned.get("require_washington_standard_id")
        ),
        "require_energy_star_score": bool(cleaned.get("require_energy_star_score")),
        "meter_less_than_12_months_policy": cleaned.get(
            "meter_less_than_12_months_policy"
        )
        or "allow",
        "meter_gap_policy": cleaned.get("meter_gap_policy") or "allow",
        "meter_overlap_policy": cleaned.get("meter_overlap_policy") or "allow",
        "no_meters_selected_policy": cleaned.get("no_meters_selected_policy")
        or "allow",
        "long_meter_entry_policy": cleaned.get("long_meter_entry_policy") or "allow",
        "estimated_energy_policy": cleaned.get("estimated_energy_policy") or "allow",
        "other_alert_policy": cleaned.get("other_alert_policy") or "allow",
        "max_archive_members": cleaned.get("max_archive_members") or 250,
        "max_member_bytes": ((cleaned.get("max_member_size_mb") or 20) * 1_000_000),
        "max_uncompressed_bytes": (
            (cleaned.get("max_uncompressed_size_mb") or 250) * 1_000_000
        ),
    }


def build_pdf_config(form: PdfStepConfigForm) -> dict[str, Any]:
    """Build strict PDF inventory and fixed exact-selector configuration."""
    cleaned = form.cleaned_data
    config = {
        "profile": cleaned.get("profile") or "inventory_v1",
        "emit_extracted_files_bundle": bool(cleaned.get("emit_extracted_files_bundle")),
    }
    for suffix in ("xml", "json", "step_p21"):
        selector_key = f"selected_{suffix}"
        selector = None
        if cleaned.get(f"select_{suffix}"):
            selector = {
                "required": bool(cleaned.get(f"{selector_key}_required")),
                "original_filename": (
                    cleaned.get(f"{selector_key}_filename") or ""
                ).strip(),
                "declared_media_type": (
                    cleaned.get(f"{selector_key}_declared_media_type") or ""
                ).strip(),
                "af_relationship": (
                    cleaned.get(f"{selector_key}_af_relationship") or ""
                ).strip(),
                "detected_media_type": (
                    cleaned.get(f"{selector_key}_detected_media_type") or ""
                ).strip(),
                "discovery_kinds": list(
                    cleaned.get(f"{selector_key}_discovery_kinds") or []
                ),
                "rich_media_asset_name": (
                    cleaned.get(f"{selector_key}_rich_media_asset_name") or ""
                ).strip(),
            }
            if suffix == "xml":
                selector["xml_root_qname"] = (
                    cleaned.get("selected_xml_root_qname") or ""
                ).strip()
            if suffix == "step_p21":
                file_schema_text = (
                    cleaned.get("selected_step_p21_file_schema") or ""
                ).strip()
                selector["step_file_schema"] = [
                    identifier.strip()
                    for identifier in file_schema_text.splitlines()
                    if identifier.strip()
                ]
        config[selector_key] = selector
    return config


def _sync_portfolio_manager_resources(
    step: WorkflowStep,
    form: PortfolioManagerStepConfigForm,
) -> None:
    """Replace or remove the step-owned, schema-validated EBL resource."""
    from validibot.validations.constants import PORTFOLIO_MANAGER_EBL_RESOURCE

    upload = form.cleaned_data.get("expected_buildings_list")
    remove = form.cleaned_data.get("remove_expected_buildings_list", False)
    structure = form.cleaned_data.get("submission_structure")
    if remove or structure != "zip_collection":
        step.step_resources.filter(
            role=WorkflowStepResource.EXPECTED_BUILDINGS_LIST,
        ).delete()
        return
    if upload:
        step.step_resources.filter(
            role=WorkflowStepResource.EXPECTED_BUILDINGS_LIST,
        ).delete()
        upload.seek(0)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.EXPECTED_BUILDINGS_LIST,
            step_resource_file=upload,
            filename=upload.name,
            resource_type=PORTFOLIO_MANAGER_EBL_RESOURCE,
        )


def _sync_portfolio_manager_value_binding(step: WorkflowStep) -> None:
    """Persist the form's direct EUIt as the declared input's fixed value."""
    from validibot.validations.constants import BindingSourceScope
    from validibot.validations.constants import StepIODirection
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition

    if step.validator_id is None:
        msg = "Portfolio Manager steps require a validator before binding EUIt"
        raise ValueError(msg)

    io_definition = (
        StepIODefinition.objects.filter(
            validator_id=step.validator_id,
            contract_key="default_euit_kbtu_ft2_yr",
            direction=StepIODirection.INPUT,
            io_medium=StepIOMedium.VALUE,
        )
        .order_by("pk")
        .first()
    )
    if io_definition is None:
        return

    configured_value = (step.config or {}).get("default_euit_kbtu_ft2_yr")
    StepInputBinding.objects.update_or_create(
        workflow_step=step,
        io_definition=io_definition,
        defaults={
            "source_scope": BindingSourceScope.CONSTANT,
            "source_data_path": "",
            "default_value": configured_value,
            "is_required": False,
        },
    )


def _sync_fmu_step_io(
    step: WorkflowStep,
    fmu_vars: list[dict[str, Any]] | None,
) -> None:
    """Sync step-level FMU variables to ``StepIODefinition`` rows.

    Interprets the tri-state produced by ``build_fmu_config``:

      - ``None``    → no-op (no new upload, no removal — author
                      edited unrelated config like simulation timing).
                      Existing rows survive untouched.
      - ``[]``      → user removed the FMU. Clear all FMU-origin
                      step-owned I/O definitions.
      - ``[...]``   → new FMU uploaded. Reconcile via
                      ``sync_step_fmu_io_definitions``.

    The ``None`` no-op branch was the May 2026 review's P1 fix:
    previously, editing simulation timing without re-uploading the
    FMU cleared every step-owned FMU I/O definition (including parser
    facts) and cascaded any author-built assertions.
    """
    from validibot.validations.services.fmu_step_io import clear_step_fmu_io_definitions
    from validibot.validations.services.fmu_step_io import sync_step_fmu_io_definitions

    if fmu_vars is None:
        return
    if fmu_vars:
        sync_step_fmu_io_definitions(step, fmu_vars)
    else:
        clear_step_fmu_io_definitions(step)


def _sync_template_step_io(
    step: WorkflowStep,
    template_vars: list[dict[str, Any]],
) -> None:
    """Sync EnergyPlus template variables to ``StepIODefinition`` rows.

    Receives the ``template_vars`` list produced by
    ``build_energyplus_config()`` and syncs it to the relational step I/O
    model. If the list is empty (template was removed), it clears any
    existing step-owned template definitions.
    """
    from validibot.validations.services.template_step_io import (
        clear_step_template_io_definitions,
    )
    from validibot.validations.services.template_step_io import (
        sync_step_template_io_definitions,
    )

    if template_vars:
        sync_step_template_io_definitions(step, template_vars)
    else:
        clear_step_template_io_definitions(step)


def _compute_insert_order(
    workflow: Workflow,
    insert_after_step: int | None,
) -> int:
    """Determine the order value for a newly created step.

    Uses a high temporary value that avoids unique-constraint collisions.
    The caller must call ``resequence_workflow_steps()`` after saving to
    normalise the order values back to clean multiples of 10.

    If ``insert_after_step`` is given (the PK of an existing step), the
    new step is placed immediately after it by using target_order + 1.
    Since real orders are multiples of 10, this slots it between the
    target and the next step.  Resequence then normalises everything.
    """
    max_order = (
        workflow.steps.aggregate(max_order=models.Max("order"))["max_order"] or 0
    )
    if insert_after_step is not None:
        resequence_workflow_steps(workflow)
        target = (
            workflow.steps.filter(pk=insert_after_step)
            .values_list("order", flat=True)
            .first()
        )
        if target is not None:
            return target + 1
    # Append at end — use a value guaranteed to be above all existing steps.
    return max_order + 10


@transaction.atomic
def save_workflow_step(
    workflow: Workflow,
    validator: Validator,
    form: forms.Form,
    *,
    step: WorkflowStep | None = None,
    insert_after_step: int | None = None,
) -> WorkflowStep:
    """
    Persist a workflow step and all dependent rows as one database change.

    The normal editing view already holds the workflow-definition lock. This
    transaction also protects service callers, imports, management commands,
    and tests from retaining a partly-saved step when a later resource or file
    binding fails validation.
    """
    is_new = step is None
    step = step or WorkflowStep(workflow=workflow)
    step.validator = validator
    step.action = None
    step.name = form.cleaned_data.get("name", "").strip() or validator.name
    step.description = (form.cleaned_data.get("description") or "").strip()
    step.notes = (form.cleaned_data.get("notes") or "").strip()
    if "display_schema" in form.cleaned_data:
        step.display_schema = form.cleaned_data.get("display_schema", False)
    if "show_success_messages" in form.cleaned_data:
        step.show_success_messages = form.cleaned_data.get(
            "show_success_messages",
            False,
        )

    config: dict[str, Any]
    ruleset: Ruleset | None = None
    template_vars: list[dict[str, Any]] = []
    fmu_vars: list[dict[str, Any]] = []
    vtype = validator.validation_type

    if vtype == ValidationType.JSON_SCHEMA:
        config, ruleset = build_json_schema_config(workflow, form, step)
    elif vtype == ValidationType.XML_SCHEMA:
        config, ruleset = build_xml_schema_config(workflow, form, step)
    elif vtype == ValidationType.SCHEMATRON:
        config, ruleset = build_schematron_config(workflow, form, step)
    elif vtype == ValidationType.SHACL:
        config, ruleset = build_shacl_config(
            workflow,
            form,
            step,
            validator=validator,
        )
    elif vtype == ValidationType.TABULAR:
        config, ruleset = build_tabular_config(workflow, form, step)
    elif vtype == ValidationType.ENERGYPLUS:
        config, template_vars = build_energyplus_config(form, step)
        # File type enforcement: parameterized templates require JSON-only
        # submissions (the submitter sends variable values as a flat JSON
        # object, not an IDF or epJSON file).  Allowing other file types
        # alongside JSON would let users upload IDF files that the launcher
        # would attempt to parse as JSON parameters — causing a confusing
        # error downstream instead of a clear rejection at upload time.
        has_template = bool(template_vars) or (
            step is not None
            and step.pk is not None
            and step_has_template_variables(step)
        )
        if has_template:
            allowed = [ft.lower() for ft in (workflow.allowed_file_types or [])]
            if allowed != [SubmissionFileType.JSON.lower()]:
                raise ValidationError(
                    _(
                        "This step uses a parameterized template, which "
                        "requires JSON-only submissions. Please set the "
                        "workflow's allowed file types to JSON only before "
                        "activating a template."
                    )
                )
    elif vtype == ValidationType.FMU:
        config, fmu_vars = build_fmu_config(form, step)
    elif vtype == ValidationType.AI_ASSIST:
        config = build_ai_config(form)
    elif vtype == ValidationType.PORTFOLIO_MANAGER:
        config = build_portfolio_manager_config(
            cast("PortfolioManagerStepConfigForm", form),
        )
    elif vtype == ValidationType.PDF:
        config = build_pdf_config(cast("PdfStepConfigForm", form))
    else:
        config = {}

    # Container execution shape is a semantic part of the workflow contract.
    # Omit the stable default to keep legacy and newly-authored fast-response
    # steps canonical; selecting long-running adds the one explicit override.
    existing_config = getattr(step, "config", None) or {}
    execution_profile = form.cleaned_data.get("execution_profile")
    if execution_profile == ValidatorExecutionProfile.LONG_RUNNING:
        config["execution_profile"] = execution_profile
    elif (
        "execution_profile" not in form.fields
        and getattr(form, "supports_execution_profile", False)
        and existing_config.get("execution_profile")
        == ValidatorExecutionProfile.LONG_RUNNING
    ):
        # A workflow imported from a profile-capable deployment may be edited
        # on self-hosted Validibot, where the choice is intentionally hidden.
        # Preserve its versioned intent so a harmless edit does not damage a
        # later export back to a deployment that supports both routes.
        config["execution_profile"] = ValidatorExecutionProfile.LONG_RUNNING

    # Machine-authored imports may carry a narrower explicit deadline even
    # though the human UI intentionally exposes only the two simple profiles.
    # Do not silently discard that contract when an author edits another field.
    if "execution_timeout_seconds" in existing_config:
        config["execution_timeout_seconds"] = existing_config[
            "execution_timeout_seconds"
        ]

    if ruleset is not None:
        step.ruleset = ruleset
    elif validator and validator.supports_assertions:
        step.ruleset = ensure_advanced_ruleset(workflow, step, validator)
    elif vtype not in (ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA):
        step.ruleset = None

    # Split the freshly-built config into the semantic (``config``, hashed) and
    # cosmetic (``display_settings``, never hashed) buckets. Replacing both
    # wholesale mirrors the previous single-field ``step.config = config`` — the
    # per-builder keep-reads above already re-read prior cosmetic values from
    # ``display_settings`` so nothing an author set is dropped (ADR-2026-06-18).
    step.config, step.display_settings = partition_step_config(vtype, config)

    if is_new:
        step.order = _compute_insert_order(workflow, insert_after_step)

    step.save()

    # Sync relational resource bindings (weather files, templates, FMUs)
    # after step.save() gives us a PK for new steps.
    if vtype == ValidationType.ENERGYPLUS:
        _sync_energyplus_resources(step, form)
        _sync_template_step_io(step, template_vars)
    elif vtype == ValidationType.FMU and getattr(form, "is_system_validator", False):
        _sync_fmu_resources(step, form)
        _sync_fmu_step_io(step, fmu_vars)
    elif vtype == ValidationType.PORTFOLIO_MANAGER:
        _sync_portfolio_manager_resources(
            step,
            cast("PortfolioManagerStepConfigForm", form),
        )

    # File-port source persistence is validator-neutral. Forms that expose the
    # reusable component provide ``build_file_port_binding_updates``; forms
    # without declared file inputs simply produce no updates.
    _sync_step_file_port_bindings(step, form)

    # Ensure bindings exist for validator-owned step inputs so the
    # input resolution engine activates.
    from validibot.validations.services.input_bindings import ensure_step_input_bindings

    ensure_step_input_bindings(step)
    if vtype == ValidationType.PORTFOLIO_MANAGER:
        _sync_portfolio_manager_value_binding(step)

    return step


def save_workflow_action_step(
    workflow: Workflow,
    definition: ActionDefinition,
    form: forms.Form,
    *,
    step: WorkflowStep | None = None,
    insert_after_step: int | None = None,
) -> WorkflowStep:
    """Persist a workflow step that references an action definition."""

    is_new = step is None
    step = step or WorkflowStep(workflow=workflow)
    action = getattr(step, "action", None)

    if not hasattr(form, "save_action"):
        raise ValueError("Action forms must implement save_action().")

    action = form.save_action(
        definition,
        current_action=action,
    )

    step.validator = None
    step.ruleset = None
    step.action = action
    step.name = action.name
    step.description = action.description
    step.notes = (form.cleaned_data.get("notes") or "").strip()
    step.display_schema = False
    step.show_success_messages = form.cleaned_data.get("show_success_messages", False)
    summary = {}
    if hasattr(form, "build_step_summary"):
        summary = form.build_step_summary(action) or {}
    step.config = summary

    if is_new:
        step.order = _compute_insert_order(workflow, insert_after_step)

    step.save()
    return step
