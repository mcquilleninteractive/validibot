"""Step management views including the wizard and editing.

Contains the step list, add-step wizard, step form (create/update),
step edit detail page, template variable editing, step-output display
selection, and step reordering/deletion. Also includes helper functions
for step I/O stage resolution.
"""

import json
import logging
from http import HTTPStatus

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.db import models
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import CredentialActionType
from validibot.actions.models import ActionDefinition
from validibot.actions.models import SlackMessageAction
from validibot.actions.registry import get_action_form
from validibot.core.utils import reverse_with_org
from validibot.core.view_helpers import hx_trigger_response
from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.validators.base.config import get_config
from validibot.workflows.forms import TABULAR_COLUMN_FORMSET_PREFIX
from validibot.workflows.forms import TABULAR_INFER_REQUEST_MAX_BYTES
from validibot.workflows.forms import TABULAR_SAMPLE_MAX_BYTES
from validibot.workflows.forms import TABULAR_SCHEMA_MAX_BYTES
from validibot.workflows.forms import StepInputBindingEditForm
from validibot.workflows.forms import TabularColumnFormSet
from validibot.workflows.forms import WorkflowStepTypeForm
from validibot.workflows.forms import get_config_form_class
from validibot.workflows.forms import tabular_column_initial
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.services.version_context import build_workflow_version_context
from validibot.workflows.views.management import MAX_STEP_COUNT
from validibot.workflows.views_helpers import ensure_advanced_ruleset
from validibot.workflows.views_helpers import get_validator_operation_display
from validibot.workflows.views_helpers import resequence_workflow_steps
from validibot.workflows.views_helpers import save_workflow_action_step
from validibot.workflows.views_helpers import save_workflow_step

logger = logging.getLogger(__name__)


def _add_validation_error_to_form(form, exc: ValidationError) -> None:
    """Translate a model-layer ValidationError into form errors.

    Per-field entries in ``exc.message_dict`` are attached to matching
    form fields when they exist, and folded into non-field errors
    otherwise so the user still sees the message. Errors raised with no
    field key fall through to non-field errors as well.
    """
    if hasattr(exc, "message_dict"):
        for field_name, messages_list in exc.message_dict.items():
            target = field_name if field_name in form.fields else None
            for message in messages_list:
                form.add_error(target, message)
    else:
        for message in exc.messages:
            form.add_error(None, message)


def workflow_step_validator_queryset(
    workflow: Workflow,
    *,
    include_coming_soon: bool = False,
):
    """Return validators that are visible for workflow-step authoring."""

    accessible_to_workflow = (
        models.Q(is_system=True)
        | models.Q(org=workflow.org)
        | models.Q(custom_validator__org=workflow.org)
    )
    queryset = Validator.objects.filter(
        accessible_to_workflow,
        availability_state=ValidatorAvailabilityState.AVAILABLE,
        is_enabled=True,
    )
    if include_coming_soon:
        queryset = queryset.exclude(release_state=ValidatorReleaseState.DRAFT)
    else:
        queryset = queryset.filter(release_state=ValidatorReleaseState.PUBLISHED)
    return queryset.distinct()


CREDENTIAL_PLACEMENT_GUIDANCE = _(
    "Signed credential steps must come after all validation steps and "
    "blocking actions.",
)
CREDENTIAL_PLACEMENT_FOLLOWUP = _(
    "Advisory actions may appear after the signed credential step.",
)
CREDENTIAL_MOVE_GUIDANCE = _(
    "Move buttons that would break this rule are disabled.",
)


class WorkflowStepListView(WorkflowObjectMixin, View):
    template_name = "workflows/partials/workflow_step_list.html"

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        steps = (
            workflow.steps.all()
            .order_by("order", "pk")
            .select_related("validator", "ruleset", "action", "action__definition")
        )
        for step in steps:
            # In-memory display dict for the template (this list view never
            # saves the step): semantic keys from ``config`` plus cosmetic ones
            # (schema_type_label, …) from ``display_settings`` (ADR-2026-06-18).
            config = {**(step.display_settings or {}), **(step.config or {})}
            if step.validator:
                vtype = step.validator.validation_type
                if vtype == ValidationType.XML_SCHEMA:
                    schema_type = config.get("schema_type")
                    if schema_type:
                        try:
                            config["schema_type_label"] = XMLSchemaType(
                                schema_type,
                            ).label
                        except ValueError:
                            config["schema_type_label"] = schema_type
                elif vtype == ValidationType.JSON_SCHEMA:
                    schema_type = config.get("schema_type")
                    if schema_type:
                        try:
                            config["schema_type_label"] = JSONSchemaVersion(
                                schema_type,
                            ).label
                        except ValueError:
                            config["schema_type_label"] = schema_type
            elif step.action:
                definition = step.action.definition
                variant = step.action.get_variant()
                step.action_variant = variant
                step.is_signed_credential_step = _is_signed_credential_step(step)
                if not config and variant:
                    if isinstance(variant, SlackMessageAction):
                        config["message"] = variant.message
                step.action_meta = {
                    "category_label": definition.get_action_category_display(),
                    "type": definition.type,
                    "icon": definition.icon or "bi-gear",
                    "definition_name": definition.name,
                    "definition_description": definition.description,
                }
                extras = {
                    key: value
                    for key, value in config.items()
                    if key not in {"message"}
                }
                step.action_summary = {
                    "message": config.get("message"),
                    "extras": extras,
                }
            # Attach the merged display dict as a SEPARATE attribute rather than
            # overwriting ``step.config``: config must stay semantic-only so
            # ``typed_config``/forbid never sees the cosmetic keys we fold in here
            # for the template (ADR-2026-06-18). The template binds
            # ``{% with config=step.display_config %}``.
            step.display_config = config
        has_credential_step = _annotate_reorder_controls(steps)
        show_private_notes = self.user_can_manage_workflow()
        context = {
            "workflow": workflow,
            "steps": steps,
            "max_step_count": MAX_STEP_COUNT,
            "show_private_notes": show_private_notes,
            "can_view_workflow": self.user_can_view_workflow(),
            "can_manage_workflow": self.user_can_manage_workflow(),
            "can_launch_workflow": workflow.can_execute(user=request.user),
            "credential_ordering_guidance": (
                {
                    "headline": str(CREDENTIAL_PLACEMENT_GUIDANCE),
                    "details": [
                        str(CREDENTIAL_PLACEMENT_FOLLOWUP),
                        str(CREDENTIAL_MOVE_GUIDANCE),
                    ],
                }
                if has_credential_step
                else None
            ),
        }
        return render(request, self.template_name, context)


class WorkflowStepWizardView(WorkflowObjectMixin, View):
    """Present the validator selector in the add-step modal."""

    template_select = "workflows/partials/workflow_step_wizard_select.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.headers.get("HX-Request"):
            return HttpResponse(status=400)
        return super().dispatch(request, *args, **kwargs)

    def _get_insert_after_step(self, request) -> int | None:
        """Extract and validate the insert_after_step parameter.

        This is the PK of the step to insert after. Using the step ID
        (rather than order value) is robust against concurrent
        resequencing operations.
        """
        raw = request.GET.get("insert_after_step") or request.POST.get(
            "insert_after_step"
        )
        if raw is None:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = self._get_step()
        if step is not None:
            edit_url = reverse_with_org(
                "workflows:workflow_step_edit",
                request=request,
                kwargs={"pk": workflow.pk, "step_id": step.pk},
            )
            response = HttpResponse(status=204)
            response["HX-Redirect"] = edit_url
            return response
        if workflow.steps.count() >= MAX_STEP_COUNT:
            context = {
                "workflow": workflow,
                "form": None,
                "validators_by_type": [],
                "max_step_count": MAX_STEP_COUNT,
                "step": None,
                "limit_reached": True,
            }
            return render(request, self.template_select, context, status=409)
        return self._render_select(request, workflow)

    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = self._get_step()
        stage = request.POST.get("stage", "select")
        insert_after_step = self._get_insert_after_step(request)

        if stage != "select":
            if step is not None:
                redirect_url = reverse_with_org(
                    "workflows:workflow_step_edit",
                    request=request,
                    kwargs={"pk": workflow.pk, "step_id": step.pk},
                )
            else:
                redirect_url = reverse_with_org(
                    "workflows:workflow_detail",
                    request=request,
                    kwargs={"pk": workflow.pk},
                )
            response = HttpResponse(status=204)
            response["HX-Redirect"] = redirect_url
            return response

        validators = self._available_validators(workflow)
        action_definitions = self._available_action_definitions()
        tabs, options = self._build_step_tabs(
            validators,
            action_definitions,
        )
        form = WorkflowStepTypeForm(request.POST, options=options)
        if form.is_valid():
            if workflow.steps.count() >= MAX_STEP_COUNT:
                message = _("You can add up to %(count)s steps per workflow.") % {
                    "count": MAX_STEP_COUNT,
                }
                return hx_trigger_response(message, level="warning", status_code=409)
            selection = form.get_selection()
            if selection["kind"] == "validator":
                validator = selection["object"]
                create_url = reverse_with_org(
                    "workflows:workflow_step_create",
                    request=request,
                    kwargs={"pk": workflow.pk, "validator_id": validator.pk},
                )
            else:
                definition: ActionDefinition = selection["object"]
                create_url = reverse_with_org(
                    "workflows:workflow_step_action_create",
                    request=request,
                    kwargs={
                        "pk": workflow.pk,
                        "action_definition_id": definition.pk,
                    },
                )
            if insert_after_step is not None:
                create_url += f"?insert_after_step={insert_after_step}"
            response = HttpResponse(status=204)
            response["HX-Redirect"] = create_url
            response["HX-Trigger"] = json.dumps(
                {
                    "close-modal": "workflowStepModal",
                },
            )
            return response
        return self._render_select(request, workflow, form=form)

    # Helper methods ---------------------------------------------------------

    def _get_step(self) -> WorkflowStep | None:
        step_id = self.kwargs.get("step_id")
        if not step_id:
            return None
        workflow = self.get_workflow()
        return get_object_or_404(WorkflowStep, workflow=workflow, pk=step_id)

    def _available_validators(self, workflow: Workflow) -> list[Validator]:
        """
        Return validators visible to this workflow's organization.

        The workflow's original submission types deliberately do not filter or
        disable this list. A later step can consume a typed file exposed by an
        earlier step, so compatibility belongs to the selected file-port source
        rather than to the workflow as a whole.

        Excludes DRAFT validators (not ready for display). COMING_SOON validators
        are included but will be disabled in the UI.

        Multi-version validator families are collapsed to their latest version
        per slug, so the picker shows each validator once. Validators are
        integer-versioned per slug (``uq_validator_slug_version``); without this
        collapse every published version would render as its own card (e.g. the
        JSON Schema validator's v1 *and* v2). ``DISTINCT ON (slug)`` ordered by
        ``slug, -version, -pk`` keeps the highest version of each slug (and also
        absorbs any row duplication from the org/custom-validator joins). The
        final display order is restored in Python afterwards.
        """
        latest_per_slug = (
            workflow_step_validator_queryset(workflow, include_coming_soon=True)
            .order_by("slug", "-version", "-pk")
            .distinct("slug")
        )
        validators = sorted(
            latest_per_slug,
            key=lambda v: (v.validation_type, v.name.lower(), v.pk),
        )
        for validator in validators:
            self._ensure_validator_defaults(validator)
        return validators

    def _available_action_definitions(self) -> list[ActionDefinition]:
        """Return action definitions the current user can add.

        Filters out definitions whose ``required_commercial_feature`` is not
        enabled
        and whose action plugins are not registered in the current
        process.
        """
        from validibot.core.features import is_feature_enabled

        definitions = ActionDefinition.objects.filter(is_active=True).order_by(
            "action_category",
            "name",
        )
        return [
            d
            for d in definitions
            if (
                get_action_form(d.type) is not None
                and (
                    not d.required_commercial_feature
                    or is_feature_enabled(d.required_commercial_feature)
                )
            )
        ]

    def _render_select(self, request, workflow: Workflow, form=None, status=200):
        validators = self._available_validators(workflow)
        action_definitions = self._available_action_definitions()

        tabs, options = self._build_step_tabs(
            validators,
            action_definitions,
        )

        selected_value = None
        if form is not None:
            selected_value = form.data.get("choice") or form.initial.get("choice")
        else:
            selected_value = request.GET.get("selected")

        selected_tab = self._resolve_selected_tab(tabs, selected_value)
        form = form or WorkflowStepTypeForm(options=options)

        context = {
            "workflow": workflow,
            "form": form,
            "validator_tabs": tabs,
            "selected_tab": selected_tab,
            "max_step_count": MAX_STEP_COUNT,
            "step": None,
            "limit_reached": False,
            "selected_value": str(selected_value) if selected_value else None,
            "insert_after_step": self._get_insert_after_step(request),
        }
        return render(request, self.template_select, context, status=status)

    def _build_step_tabs(
        self,
        validators: list[Validator],
        action_definitions: list[ActionDefinition],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        tabs: list[dict[str, object]] = []
        options: list[dict[str, object]] = []

        validator_groups: list[tuple[str, str, set[str] | None, str]] = [
            (
                "basic",
                str(_("Validators")),
                {
                    ValidationType.BASIC,
                    ValidationType.JSON_SCHEMA,
                    ValidationType.SHACL,
                    ValidationType.XML_SCHEMA,
                    # Tabular runs in-process (no container backend), so it
                    # belongs with the built-in validators, not "Advanced".
                    ValidationType.TABULAR,
                },
                str(_("No validators available yet.")),
            ),
            (
                "advanced",
                str(_("Advanced Validators")),
                {
                    ValidationType.AI_ASSIST,
                    ValidationType.ENERGYPLUS,
                    ValidationType.FMU,
                    ValidationType.PORTFOLIO_MANAGER,
                    ValidationType.PDF,
                },
                str(_("No advanced validators available yet.")),
            ),
            (
                "custom",
                str(_("Custom Validators")),
                {
                    ValidationType.CUSTOM_VALIDATOR,
                },
                str(_("No custom validators available yet.")),
            ),
        ]

        handled: list[Validator] = []
        for slug, label, types, empty_message in validator_groups:
            if types:
                filtered = [
                    v
                    for v in validators
                    if v.validation_type in types and v not in handled
                ]
                handled.extend(filtered)
            else:
                filtered = []
            members = [self._serialize_validator(v) for v in filtered]
            tabs.append(
                {
                    "slug": slug,
                    "label": label,
                    "entries": members,
                    "empty_message": empty_message,
                },
            )
            options.extend(members)

        remaining_validators = [v for v in validators if v not in handled]
        if remaining_validators:
            advanced_tab = next(
                (tab for tab in tabs if tab["slug"] == "advanced"),
                None,
            )
            if advanced_tab is not None:
                serialized = [
                    self._serialize_validator(v) for v in remaining_validators
                ]
                advanced_tab["entries"].extend(serialized)
                options.extend(serialized)

        integration_entries = [
            self._serialize_action_definition(defn)
            for defn in action_definitions
            if defn.action_category == ActionCategoryType.INTEGRATION
        ]
        credential_entries = [
            self._serialize_action_definition(defn)
            for defn in action_definitions
            if defn.action_category == ActionCategoryType.CREDENTIAL
        ]

        tabs.append(
            {
                "slug": "integrations",
                "label": str(_("Integrations")),
                "entries": integration_entries,
                "empty_message": str(_("No integrations available yet.")),
            },
        )
        tabs.append(
            {
                "slug": "credentials",
                "label": str(_("Credentials")),
                "entries": credential_entries,
                "empty_message": str(_("No credentials available yet.")),
            },
        )
        options.extend(integration_entries)
        options.extend(credential_entries)

        return tabs, options

    def _ensure_validator_defaults(self, validator: Validator) -> None:
        """
        Backfill expected supported formats/file types for validators created
        before defaults expanded (notably FMU, which now accepts JSON/TEXT).
        """
        if validator.validation_type != ValidationType.FMU:
            return
        changed = False
        if validator.supported_file_types is None:
            validator.supported_file_types = []
            changed = True
        if validator.supported_data_formats is None:
            validator.supported_data_formats = []
            changed = True
        for ft in (SubmissionFileType.JSON, SubmissionFileType.TEXT):
            if ft not in validator.supported_file_types:
                validator.supported_file_types.append(ft)
                changed = True
        for fmt in (SubmissionDataFormat.JSON, SubmissionDataFormat.TEXT):
            if fmt not in validator.supported_data_formats:
                validator.supported_data_formats.append(fmt)
                changed = True
        # Only persist on a mutating request. A GET that renders the step
        # wizard (plus crawlers, link-preview bots, and browser prefetch) must
        # be side-effect-free per HTTP semantics. The in-memory backfill above
        # already makes the rendered validator show the right types; the row
        # is persisted the next time a POST actually touches this validator.
        if changed and self.request.method == "POST":
            validator.save(
                update_fields=["supported_file_types", "supported_data_formats"],
            )

    def _serialize_validator(
        self,
        validator: Validator,
    ) -> dict[str, object]:
        cfg = get_config(validator.validation_type)
        disabled_reason = None
        is_disabled = False
        short_description = validator.short_description
        if not short_description and cfg is not None:
            short_description = cfg.short_description

        # Coming-soon validators remain visible for product discovery but are
        # not authorable. Available validators are selectable regardless of the
        # workflow's original submission type because file ports can bind them
        # to typed outputs from earlier steps.
        if validator.is_coming_soon:
            is_disabled = True
            disabled_reason = _("Coming soon")

        return {
            "value": f"validator:{validator.pk}",
            "label": validator.name,
            "name": validator.name,
            "subtitle": validator.get_validation_type_display(),
            "description": validator.description,
            "short_description": short_description,
            "icon": getattr(validator, "display_icon", "bi-sliders"),
            "kind": "validator",
            "object": validator,
            "disabled": is_disabled,
            "disabled_reason": disabled_reason,
        }

    def _serialize_action_definition(
        self,
        definition: ActionDefinition,
    ) -> dict[str, object]:
        return {
            "value": f"action:{definition.pk}",
            "label": definition.name,
            "name": definition.name,
            "subtitle": definition.get_action_category_display(),
            "description": definition.description,
            "icon": definition.icon or "bi-gear",
            "kind": "action",
            "object": definition,
        }

    def _resolve_selected_tab(
        self,
        tabs: list[dict[str, object]],
        selected_value: str | None,
    ) -> str:
        if selected_value:
            for tab in tabs:
                for entry in tab["entries"]:
                    if str(entry["value"]) == str(selected_value):
                        return tab["slug"]
        for tab in tabs:
            if tab["entries"]:
                return tab["slug"]
        return tabs[0]["slug"] if tabs else "basic"


class TabularSettingsEndpointMixin(WorkflowObjectMixin):
    """Shared authorization and rendering for the tabular HTMx endpoints."""

    def dispatch(self, request, *args, **kwargs):
        self.workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        self.step = self._get_tabular_step()
        self.validator = self._get_tabular_validator()
        return super().dispatch(request, *args, **kwargs)

    def _get_tabular_step(self) -> WorkflowStep | None:
        step_id = self.kwargs.get("step_id")
        if step_id is None:
            return None
        return get_object_or_404(
            WorkflowStep,
            workflow=self.workflow,
            pk=step_id,
            validator__validation_type=ValidationType.TABULAR,
        )

    def _get_tabular_validator(self) -> Validator:
        if self.step is not None:
            return self.step.validator
        validator_id = self.kwargs.get("validator_id")
        return get_object_or_404(
            workflow_step_validator_queryset(self.workflow),
            pk=validator_id,
            validation_type=ValidationType.TABULAR,
        )

    def _endpoint_url(self, route_name: str) -> str:
        kwargs: dict[str, int] = {"pk": self.workflow.pk}
        if self.step is not None:
            kwargs["step_id"] = self.step.pk
            route_name = f"{route_name}_existing"
        else:
            kwargs["validator_id"] = self.validator.pk
        return reverse_with_org(
            f"workflows:{route_name}",
            request=self.request,
            kwargs=kwargs,
        )

    def _workspace_context(
        self,
        *,
        descriptor: dict,
        column_formset=None,
        table_schema_value: str = "",
        import_error: str = "",
        sample_error: str = "",
        success_message: str = "",
        pending_descriptor: dict | None = None,
        pending_source: str = "",
        resolved_delimiter: str | None = None,
        resolved_has_header: bool | None = None,
    ) -> dict[str, object]:
        if column_formset is None:
            initial = tabular_column_initial(descriptor) or [{}]
            column_formset = TabularColumnFormSet(
                initial=initial,
                prefix=TABULAR_COLUMN_FORMSET_PREFIX,
            )
        return {
            "column_formset": column_formset,
            "schema_base": json.dumps(descriptor),
            "table_schema_value": table_schema_value,
            "import_error": import_error,
            "sample_error": sample_error,
            "schema_success_message": success_message,
            "pending_schema": (
                json.dumps(pending_descriptor) if pending_descriptor else ""
            ),
            "pending_schema_source": pending_source,
            "pending_schema_column_count": len(
                (pending_descriptor or {}).get("fields", []),
            ),
            "tabular_columns_url": self._endpoint_url("workflow_tabular_columns"),
            "tabular_import_url": self._endpoint_url(
                "workflow_tabular_schema_import",
            ),
            "tabular_infer_url": self._endpoint_url(
                "workflow_tabular_schema_infer",
            ),
            "tabular_apply_url": self._endpoint_url(
                "workflow_tabular_schema_apply",
            ),
            "tabular_export_url": (
                self._endpoint_url("workflow_tabular_schema_export")
                if self.step
                else ""
            ),
            "resolved_delimiter": resolved_delimiter,
            "resolved_has_header": resolved_has_header,
            "has_resolved_dialect": resolved_delimiter is not None,
        }

    def _current_descriptor(self) -> dict:
        raw = self.request.POST.get("schema_base", "")
        if raw:
            try:
                descriptor = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                descriptor = {}
            if isinstance(descriptor, dict):
                return descriptor
        if self.step and self.step.ruleset_id and self.step.ruleset.rules_text:
            try:
                descriptor = json.loads(self.step.ruleset.rules_text)
            except (TypeError, json.JSONDecodeError):
                return {}
            return descriptor if isinstance(descriptor, dict) else {}
        return {}

    def _posted_column_formset(self):
        if f"{TABULAR_COLUMN_FORMSET_PREFIX}-TOTAL_FORMS" not in self.request.POST:
            return None
        return TabularColumnFormSet(
            data=self.request.POST,
            prefix=TABULAR_COLUMN_FORMSET_PREFIX,
        )

    def _render_workspace(self, context: dict[str, object]) -> HttpResponse:
        return render(
            self.request,
            "workflows/partials/tabular_schema_workspace.html",
            context,
        )

    def _render_input_error(self, message: str, target: str) -> HttpResponse:
        """Render a one-line error onto a modal's *input* screen.

        Used by import/infer when the submitted descriptor/sample is bad. The
        HTMx response is retargeted to the input screen's error slot (instead of
        the review body the button normally targets) so the author stays on the
        input screen and sees what to fix, rather than advancing to review.
        """
        response = render(
            self.request,
            "workflows/partials/tabular_modal_error.html",
            {"error": str(message)},
        )
        response["HX-Retarget"] = target
        response["HX-Reswap"] = "innerHTML"
        return response

    def _render_review(
        self,
        *,
        pending_descriptor: dict,
        source: str,
        modal_id: str,
        resolved_delimiter: str | None = None,
        resolved_has_header: bool | None = None,
    ) -> HttpResponse:
        """Render the modal's *review* screen for a validated proposal.

        Returns the review body (compatibility report + column-count summary +
        the hidden ``pending_schema`` the Apply button submits) and fires an
        ``HX-Trigger`` so the page script flips the modal from input to review.
        For inference, the resolved dialect rides along as out-of-band swaps so
        the Settings-tab delimiter/header reflect what was detected.
        """
        from validibot.validations.validators.tabular.schema import (
            table_schema_compatibility_notices,
        )

        context = {
            "pending_schema": json.dumps(pending_descriptor),
            "pending_schema_source": source,
            "pending_schema_column_count": len(
                pending_descriptor.get("fields", []),
            ),
            "schema_compatibility_notices": list(
                table_schema_compatibility_notices(pending_descriptor),
            ),
            "resolved_delimiter": resolved_delimiter,
            "resolved_has_header": resolved_has_header,
            "has_resolved_dialect": resolved_delimiter is not None,
        }
        response = render(
            self.request,
            "workflows/partials/tabular_schema_review.html",
            context,
        )
        response["HX-Trigger"] = json.dumps(
            {"tabular-review-ready": {"modalId": modal_id}},
        )
        return response


class TabularColumnCreateView(TabularSettingsEndpointMixin, View):
    """Return one correctly-prefixed column row for HTMx insertion."""

    MAX_COLUMNS = 1024

    def get(self, request, *args, **kwargs):
        try:
            index = int(
                request.GET.get(
                    f"{TABULAR_COLUMN_FORMSET_PREFIX}-TOTAL_FORMS",
                    "0",
                ),
            )
        except (TypeError, ValueError):
            index = 0
        if index < 0 or index >= self.MAX_COLUMNS:
            return HttpResponse(
                _("A tabular schema can contain at most 1,024 columns."),
                status=400,
            )
        empty_formset = TabularColumnFormSet(
            prefix=TABULAR_COLUMN_FORMSET_PREFIX,
        )
        form = empty_formset.empty_form
        form.prefix = f"{TABULAR_COLUMN_FORMSET_PREFIX}-{index}"
        return render(
            request,
            "workflows/partials/tabular_column_row.html",
            {
                "column_form": form,
                "management_total": index + 1,
                "is_new_column": True,
            },
        )


class TabularSchemaImportView(TabularSettingsEndpointMixin, View):
    """Validate an imported descriptor and preview it before replacement."""

    #: Input-screen error slot for this modal (HX-Retarget target on failure).
    ERROR_TARGET = "#tabular-import-input-error"

    def post(self, request, *args, **kwargs):
        from validibot.validations.validators.tabular.schema import parse_table_schema

        raw = (request.POST.get("table_schema") or "").strip()
        uploaded = request.FILES.get("schema_file")
        if raw and uploaded:
            return self._render_input_error(
                _("Paste a descriptor or upload one, not both."),
                self.ERROR_TARGET,
            )
        if uploaded:
            if uploaded.size > TABULAR_SCHEMA_MAX_BYTES:
                return self._render_input_error(
                    _("Descriptor files must be 2 MB or smaller."),
                    self.ERROR_TARGET,
                )
            try:
                raw = uploaded.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                return self._render_input_error(
                    _("Descriptor files must be UTF-8 JSON."),
                    self.ERROR_TARGET,
                )
        if not raw:
            return self._render_input_error(
                _("Paste a Table Schema descriptor first."),
                self.ERROR_TARGET,
            )
        try:
            imported = json.loads(raw)
            parse_table_schema(imported)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._render_input_error(
                _("Could not import the descriptor: %(error)s") % {"error": exc},
                self.ERROR_TARGET,
            )
        return self._render_review(
            pending_descriptor=imported,
            source="import",
            modal_id="tabularImportModal",
        )


class TabularSchemaInferView(TabularSettingsEndpointMixin, View):
    """Infer a starting descriptor from bounded delimited text.

    The endpoint checks the complete multipart request size before touching
    ``request.FILES``. This complements the uploaded-file size check: Django
    otherwise has to parse and spool an arbitrarily large request before the
    application can inspect ``UploadedFile.size``.
    """

    #: Input-screen error slot for this modal (HX-Retarget target on failure).
    ERROR_TARGET = "#tabular-infer-input-error"

    def post(self, request, *args, **kwargs):
        from validibot.validations.validators.tabular.infer import infer_table_schema
        from validibot.validations.validators.tabular.preflight import TabularDialect
        from validibot.validations.validators.tabular.preflight import TabularReadError

        raw_content_length = request.headers.get("content-length", "")
        try:
            content_length = int(raw_content_length)
        except (TypeError, ValueError):
            content_length = 0
        if content_length > TABULAR_INFER_REQUEST_MAX_BYTES:
            return self._render_input_error(
                _("Inference requests must be 10 MB or smaller."),
                self.ERROR_TARGET,
            )

        sample = request.FILES.get("sample_file")
        if sample is None:
            return self._render_input_error(
                _("Choose a delimited text sample first."),
                self.ERROR_TARGET,
            )
        if sample.size > TABULAR_SAMPLE_MAX_BYTES:
            return self._render_input_error(
                _("Sample files must be 5 MB or smaller."),
                self.ERROR_TARGET,
            )

        raw_content = sample.read()
        delimiter = request.POST.get("delimiter") or None
        dialect = TabularDialect(
            delimiter=delimiter,
            encoding="utf-8",
            has_header=request.POST.get("has_header") in {"on", "true", "1"},
        )
        try:
            inferred = infer_table_schema(raw_content, dialect=dialect)
        except TabularReadError as exc:
            return self._render_input_error(
                _("Could not read the sample: %(error)s") % {"error": exc},
                self.ERROR_TARGET,
            )

        return self._render_review(
            pending_descriptor=inferred.descriptor,
            source="infer",
            modal_id="tabularInferModal",
            resolved_delimiter=inferred.dialect.delimiter,
            resolved_has_header=inferred.dialect.has_header,
        )


class TabularSchemaApplyView(TabularSettingsEndpointMixin, View):
    """Apply a previously previewed imported or inferred descriptor."""

    def post(self, request, *args, **kwargs):
        from validibot.validations.validators.tabular.schema import parse_table_schema

        raw = (request.POST.get("pending_schema") or "").strip()
        current = self._current_descriptor()
        try:
            descriptor = json.loads(raw)
            parse_table_schema(descriptor)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._render_workspace(
                self._workspace_context(
                    descriptor=current,
                    column_formset=self._posted_column_formset(),
                    import_error=str(
                        _("Could not apply the proposed schema: %(error)s")
                        % {"error": exc},
                    ),
                ),
            )
        # The compatibility report is shown on the modal's review screen before
        # the author applies, so the post-apply workspace does not repeat it.
        return self._render_workspace(
            self._workspace_context(
                descriptor=descriptor,
                success_message=str(
                    _("Proposed columns applied. Save changes to persist them."),
                ),
            ),
        )


class TabularSchemaExportView(TabularSettingsEndpointMixin, View):
    """Download the saved Table Schema descriptor for an existing step."""

    def get(self, request, *args, **kwargs):
        if (
            not self.step
            or not self.step.ruleset_id
            or not self.step.ruleset.rules_text
        ):
            return HttpResponse(
                _("Save a column schema before downloading it."),
                status=404,
            )
        filename = f"{slugify(self.step.name) or 'tabular-schema'}.json"
        response = HttpResponse(
            self.step.ruleset.rules_text,
            content_type="application/json; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class WorkflowStepFormView(WorkflowObjectMixin, FormView):
    """Render the full-screen workflow step editor for create/update."""

    template_name = "workflows/workflow_step_form.html"
    mode: str = "create"
    validator_url_kwarg = "validator_id"
    action_definition_url_kwarg = "action_definition_id"
    step_url_kwarg = "step_id"
    saved_step: WorkflowStep | None = None

    def dispatch(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        if self.mode == "create" and workflow.steps.count() >= MAX_STEP_COUNT:
            messages.warning(
                request,
                _("You can add up to %(count)s steps per workflow.")
                % {
                    "count": MAX_STEP_COUNT,
                },
            )
            detail_url = reverse_with_org(
                "workflows:workflow_detail",
                request=request,
                kwargs={"pk": workflow.pk},
            )
            return HttpResponseRedirect(detail_url)
        return super().dispatch(request, *args, **kwargs)

    def _is_tabular_settings(self) -> bool:
        """Whether this request edits the rich Tabular Validator settings page.

        The Tabular Validator has a bespoke full-screen editor (Columns /
        Settings tabs with a sticky action footer) and its own template,
        distinct from the generic step form used by every other validator and
        by action steps.
        """
        return (
            not self.is_action_step()
            and self.get_validator().validation_type == ValidationType.TABULAR
        )

    def get_template_names(self):
        # Route the Tabular Validator to its dedicated two-tab editor; every
        # other validator and all action steps keep the shared step form.
        if self._is_tabular_settings():
            return ["workflows/tabular_step_settings.html"]
        return [self.template_name]

    def get_step(self) -> WorkflowStep | None:
        if self.mode != "update":
            return None
        if not hasattr(self, "_step"):
            workflow = self.get_workflow()
            step_id = self.kwargs.get(self.step_url_kwarg)
            self._step = get_object_or_404(
                WorkflowStep,
                workflow=workflow,
                pk=step_id,
            )
        return getattr(self, "_step", None)

    def _validator_queryset(self):
        """Return validators that can be selected for workflow steps.

        Only PUBLISHED validators can be used in workflows. DRAFT and
        COMING_SOON validators are excluded.
        """
        workflow = self.get_workflow()
        return workflow_step_validator_queryset(workflow)

    def get_validator(self) -> Validator:
        if self.is_action_step():
            raise Http404
        if not hasattr(self, "_validator"):
            if self.mode == "update":
                step = self.get_step()
                if step is None:
                    raise Http404
                self._validator = step.validator
            else:
                validator_id = self.kwargs.get(self.validator_url_kwarg)
                self._validator = get_object_or_404(
                    self._validator_queryset(),
                    pk=validator_id,
                )
        return self._validator

    def get_action_definition(self) -> ActionDefinition:
        """Look up the ActionDefinition for create or update mode.

        For create mode, also enforces ``required_commercial_feature`` gating so
        that Pro-only actions cannot be added to a workflow when the
        required commercial package is not installed.  This is the
        server-side companion to the UI filtering in
        ``_available_action_definitions()`` — both are necessary for
        defense in depth.
        """
        if not hasattr(self, "_action_definition"):
            if self.mode == "update":
                step = self.get_step()
                if step is None or not step.action:
                    raise Http404
                self._action_definition = step.action.definition
            else:
                definition_id = self.kwargs.get(self.action_definition_url_kwarg)
                self._action_definition = get_object_or_404(
                    ActionDefinition,
                    pk=definition_id,
                    is_active=True,
                )
                # Server-side enforcement: reject action types whose
                # required commercial feature is not enabled.
                required = self._action_definition.required_commercial_feature
                if required:
                    from validibot.core.features import is_feature_enabled

                    if not is_feature_enabled(required):
                        raise Http404
                if get_action_form(self._action_definition.type) is None:
                    raise Http404
        return self._action_definition

    def is_action_step(self) -> bool:
        if self.mode == "update":
            step = self.get_step()
            return bool(step and step.action_id)
        return bool(self.kwargs.get(self.action_definition_url_kwarg))

    def get_form_class(self):
        if self.is_action_step():
            definition = self.get_action_definition()
            form_class = get_action_form(definition.type)
            if form_class is None:
                raise Http404("Unsupported action type.")
            return form_class
        validator = self.get_validator()
        return get_config_form_class(validator.validation_type)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["step"] = self.get_step()
        if self.is_action_step():
            kwargs["definition"] = self.get_action_definition()
        else:
            # Pass org and validator for forms that need them (e.g., EnergyPlus)
            workflow = self.get_workflow()
            kwargs["workflow"] = workflow
            kwargs["org"] = workflow.org
            kwargs["validator"] = self.get_validator()
            step = self.get_step()
            if step is not None:
                kwargs["proposed_order"] = step.order
            else:
                insert_after = self._get_insert_after_step()
                target_order = (
                    workflow.steps.filter(pk=insert_after)
                    .values_list("order", flat=True)
                    .first()
                    if insert_after is not None
                    else None
                )
                if target_order is None:
                    target_order = (
                        workflow.steps.order_by("-order")
                        .values_list("order", flat=True)
                        .first()
                        or 0
                    )
                    kwargs["proposed_order"] = target_order + 10
                else:
                    kwargs["proposed_order"] = target_order + 1
        return kwargs

    def _get_insert_after_step(self) -> int | None:
        """Read the insert_after_step query param for mid-list insertion."""
        raw = self.request.GET.get("insert_after_step")
        if raw is None:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    def form_valid(self, form):
        """Persist one complete step edit under the workflow definition lock."""

        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )
        from validibot.workflows.services.editing_policy import (
            workflow_step_form_has_semantic_changes,
        )

        workflow = self.get_workflow()
        semantic_change = workflow_step_form_has_semantic_changes(
            changed_fields=form.changed_data,
            is_new=self.mode == "create",
        )
        with guard_workflow_definition_mutation(
            workflow.pk,
            semantic_change=semantic_change,
        ):
            return self._form_valid_under_definition_lock(form)

    def _form_valid_under_definition_lock(self, form):
        """Save the step and its dependent rows while the lock is held."""

        workflow = self.get_workflow()
        insert_after_step = (
            self._get_insert_after_step() if self.mode == "create" else None
        )
        if self.is_action_step():
            definition = self.get_action_definition()
            saved_step = save_workflow_action_step(
                workflow,
                definition,
                form,
                step=self.get_step(),
                insert_after_step=insert_after_step,
            )
        else:
            validator = self.get_validator()
            try:
                saved_step = save_workflow_step(
                    workflow,
                    validator,
                    form,
                    step=self.get_step(),
                    insert_after_step=insert_after_step,
                )
            except ValidationError as exc:
                # Model-level invariants (e.g. Ruleset's lock when the
                # workflow has prior runs) raise during full_clean inside
                # the per-validator config builders. Surface them as form
                # errors instead of letting the 500 bubble up.
                _add_validation_error_to_form(form, exc)
                return self.form_invalid(form)
        resequence_workflow_steps(workflow)
        self.saved_step = saved_step
        if self.mode == "create":
            message = _("Workflow step added.")
        else:
            message = _("Workflow step updated.")
        messages.success(self.request, message)
        # Surface schema compatibility warnings only when the schema was just
        # *imported* (paste/upload/infer), never on an ordinary editor save.
        # Otherwise every unrelated edit (e.g. changing the description) would
        # re-toast the whole compatibility report, which is shown at import time
        # in the review step. See the Tabular import flow.
        if form.cleaned_data.get("schema_source") in {"text", "upload", "infer"}:
            for warning in form.cleaned_data.get("schema_warnings", []):
                messages.warning(self.request, warning)
        return HttpResponseRedirect(self.get_success_url())

    def form_invalid(self, form):
        return self.render_to_response(
            self.get_context_data(form=form),
            status=400,
        )

    def get_success_url(self):
        workflow = self.get_workflow()
        detail_url = reverse_with_org(
            "workflows:workflow_detail",
            request=self.request,
            kwargs={"pk": workflow.pk},
        )
        if hasattr(self, "saved_step") and self.saved_step:
            validator = self.saved_step.validator
            supports_assertions = validator and validator.supports_assertions
            # Validators without an assertion surface are fully configured by
            # the settings form, so return to the workflow detail.
            if not supports_assertions:
                return f"{detail_url}#workflow-steps-col"

            anchor = (
                "#workflow-step-assertions"
                if supports_assertions
                else "#workflow-step-details"
            )
            return (
                reverse_with_org(
                    "workflows:workflow_step_edit",
                    request=self.request,
                    kwargs={"pk": workflow.pk, "step_id": self.saved_step.pk},
                )
                + anchor
            )
        return f"{detail_url}#workflow-steps-col"

    def get_neighbor_steps(self) -> tuple[WorkflowStep | None, WorkflowStep | None]:
        step = self.get_step()
        if step is None:
            return (None, None)
        steps = list(self.get_workflow().steps.all().order_by("order", "pk"))
        previous_step = None
        next_step = None
        for index, current in enumerate(steps):
            if current.pk == step.pk:
                if index > 0:
                    previous_step = steps[index - 1]
                if index < len(steps) - 1:
                    next_step = steps[index + 1]
                break
        return previous_step, next_step

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        step = self.get_step()
        form = context.get("form")
        details: dict[str, object]
        icon = "bi-sliders"
        if self.is_action_step():
            definition = self.get_action_definition()
            icon = definition.icon or icon
            details = {
                "name": definition.name,
                "description": definition.description,
                "type_label": definition.get_action_category_display(),
                "icon": icon,
            }
        else:
            validator = self.get_validator()
            icon = getattr(validator, "display_icon", icon)
            details = {
                "name": validator.name,
                "description": validator.description,
                "short_description": validator.short_description,
                "type_label": validator.get_validation_type_display(),
                "icon": icon,
            }
        prev_step, next_step = self.get_neighbor_steps()
        context.update(
            build_workflow_version_context(
                request=self.request,
                workflow=workflow,
            ),
        )
        context.update(
            {
                "workflow": workflow,
                "step": step,
                "subject_details": details,
                "validator_details": details,
                "is_action_step": self.is_action_step(),
                "is_create": self.mode == "create",
                "max_step_count": MAX_STEP_COUNT,
                "previous_step": prev_step,
                "next_step": next_step,
                "steps_count": workflow.steps.count(),
                "show_assertion_link": bool(
                    not self.is_action_step()
                    and step
                    and step.validator
                    and step.validator.supports_assertions,
                ),
                "credential_step_guidance": self._get_credential_step_guidance(),
            },
        )
        if self._is_tabular_settings():
            context.update(self._get_tabular_settings_context(form, step))
        return context

    def _get_tabular_settings_context(self, form, step) -> dict[str, object]:
        """Build the schema workspace URLs for the rich editor."""
        workflow = self.get_workflow()
        if step is not None:
            route_suffix = "_existing"
            route_kwargs = {"pk": workflow.pk, "step_id": step.pk}
        else:
            route_suffix = ""
            route_kwargs = {
                "pk": workflow.pk,
                "validator_id": self.get_validator().pk,
            }

        def endpoint(name: str) -> str:
            return reverse_with_org(
                f"workflows:{name}{route_suffix}",
                request=self.request,
                kwargs=route_kwargs,
            )

        active_tab = self._tabular_active_tab(form)
        return {
            "is_tabular_settings": True,
            "tabular_active_tab": active_tab,
            # Single boolean lets the template pick tab state with `yesno`
            # filters (one-line attributes) instead of inline `{% if %}` blocks,
            # which djLint reflows into whitespace-padded class/aria values.
            "tabular_settings_tab_active": active_tab == "settings",
            "column_formset": getattr(form, "column_formset", None),
            "tabular_columns_url": endpoint("workflow_tabular_columns"),
            "tabular_import_url": endpoint("workflow_tabular_schema_import"),
            "tabular_infer_url": endpoint("workflow_tabular_schema_infer"),
            "tabular_apply_url": endpoint("workflow_tabular_schema_apply"),
            "tabular_export_url": (
                endpoint("workflow_tabular_schema_export") if step else ""
            ),
        }

    def _tabular_active_tab(self, form) -> str:
        """Pick which editor tab opens first.

        Columns lead by default — that is where authors spend their time, and
        where column/schema problems surface (an invalid column, or a schema
        change that orphans assertions). We open Settings first *only* when a
        Settings-tab field actually errored, so those field errors are not
        hidden behind the Columns tab. Crucially, non-field errors
        (``form.errors['__all__']``, added by ``clean()`` for column/schema
        issues) and column-formset errors must NOT pull the author onto
        Settings — they belong on Columns, next to the inputs at fault.
        """
        settings_tab_fields = {
            "name",
            "description",
            "delimiter",
            "has_header",
            "display_schema",
            "show_success_messages",
            "notes",
        }
        if (
            form is not None
            and getattr(form, "is_bound", False)
            and settings_tab_fields & set(form.errors)
        ):
            return "settings"
        return "columns"

    def _get_credential_step_guidance(self) -> dict[str, str] | None:
        """Return UI guidance for signed credential action steps."""

        if not self.is_action_step():
            return None
        definition = self.get_action_definition()
        if definition.type != CredentialActionType.SIGNED_CREDENTIAL:
            return None

        summary = str(
            _(
                "When this step is present, the workflow editor keeps it after "
                "all validation steps and blocking actions."
            )
        )
        if self.mode == "create":
            status = str(
                _(
                    "New steps are added at the end of the workflow. You can "
                    "still place advisory actions after this one later."
                )
            )
        else:
            step = self.get_step()
            workflow = self.get_workflow()
            step_number = step.step_number_display if step else "?"
            status = str(
                _("You are editing %(step)s in a workflow with %(count)s steps.")
                % {
                    "step": step_number,
                    "count": workflow.steps.count(),
                }
            )

        return {
            "headline": str(CREDENTIAL_PLACEMENT_GUIDANCE),
            "details": [
                str(CREDENTIAL_PLACEMENT_FOLLOWUP),
                summary,
                status,
            ],
        }

    def _has_dedicated_step_edit_view(self) -> bool:
        """Return whether the step has a distinct overview page.

        Action steps use the settings form as their only edit surface.
        Their ``workflow_step_edit`` route redirects back to the same
        settings page, so breadcrumbs should not include a self-link for
        the step number.
        """

        return not self.is_action_step()

    def get_breadcrumbs(self):
        workflow = self.get_workflow()
        is_tabular_settings = self._is_tabular_settings()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        if self.mode == "create":
            breadcrumbs.append(
                self.workflow_breadcrumb_item(
                    workflow,
                    url=reverse_with_org(
                        "workflows:workflow_detail",
                        request=self.request,
                        kwargs={"pk": workflow.pk},
                    ),
                ),
            )
            breadcrumbs.append(
                {
                    "name": (
                        _("Tabular settings") if is_tabular_settings else _("Add step")
                    ),
                    "url": "",
                },
            )
        else:
            step = self.get_step()
            breadcrumbs.append(
                self.workflow_breadcrumb_item(
                    workflow,
                    url=reverse_with_org(
                        "workflows:workflow_detail",
                        request=self.request,
                        kwargs={"pk": workflow.pk},
                    ),
                ),
            )
            if self._has_dedicated_step_edit_view():
                step_url = reverse_with_org(
                    "workflows:workflow_step_edit",
                    request=self.request,
                    kwargs={"pk": workflow.pk, "step_id": step.pk if step else ""},
                )
                breadcrumbs.append(
                    {
                        "name": step.step_number_display,
                        "url": step_url,
                    },
                )
                breadcrumbs.append(
                    {
                        "name": (
                            _("Tabular settings")
                            if is_tabular_settings
                            else _("Edit Step Detail")
                        ),
                        "url": "",
                    },
                )
            else:
                breadcrumbs.append(
                    {
                        "name": _("%(step)s: Edit Step Detail")
                        % {"step": step.step_number_display},
                        "url": "",
                    },
                )
        return breadcrumbs


# ── Step I/O stage helpers ───────────────────────────────────────────
# These helpers let the step detail view detect I/O stages and group
# assertions correctly.


def _step_has_io_stages(step) -> bool:
    """True when the step's validator has a processor (input → process → output).

    This checks the validator's *capability* (``has_processor``), not whether
    step I/O definitions actually exist yet. A validator with a processor
    always shows the three-section layout (input assertions, process divider,
    output assertions) even when no definitions have been created yet, so the
    user sees the structural slots where inputs and outputs will appear.
    """
    validator = getattr(step, "validator", None)
    return bool(validator and validator.has_processor)


def _resolve_assertion_stage(assertion) -> CatalogRunStage:
    """Determine which stage bucket an assertion belongs to.

    Delegates to the model's ``resolved_run_stage`` property which checks
    ``target_io_definition``, then defaults to OUTPUT.
    """
    return assertion.resolved_run_stage


def _tabular_assertion_counts(assertions) -> dict[str, int]:
    """Count tabular assertions by their persisted execution stage."""
    counts = {"dataset": 0, "row": 0, "column": 0}
    for assertion in assertions:
        stage = (assertion.options or {}).get("tabular_stage", "dataset")
        counts[stage if stage in counts else "dataset"] += 1
    return counts


def _tabular_summary_config(step: WorkflowStep) -> dict[str, object]:
    """Build summary values with Ruleset fallbacks for pre-editor steps.

    Merges both step-config buckets: the semantic dialect (delimiter / encoding /
    has_header) from ``config`` and the cosmetic labels / counts from
    ``display_settings`` (ADR-2026-06-18). Missing values are still recomputed via
    the ``setdefault`` fallbacks below, so pre-editor and imported steps work too.
    """
    config = {**(step.display_settings or {}), **(step.config or {})}
    ruleset = step.ruleset if step.ruleset_id else None
    metadata = dict(ruleset.metadata or {}) if ruleset else {}

    delimiter = config.get("delimiter", metadata.get("delimiter", ""))
    delimiter_labels = {
        "": str(_("Auto-detect")),
        ",": str(_("Comma")),
        "\t": str(_("Tab")),
        ";": str(_("Semicolon")),
        "|": str(_("Pipe")),
    }
    config.setdefault("delimiter", delimiter)
    config.setdefault(
        "delimiter_label",
        delimiter_labels.get(str(delimiter), str(delimiter)),
    )
    config.setdefault("has_header", metadata.get("has_header", True))
    config.setdefault("encoding", metadata.get("encoding", "utf-8"))

    if ruleset and (
        "column_count" not in config or "required_column_count" not in config
    ):
        try:
            descriptor = json.loads(ruleset.rules_text or "{}")
        except (TypeError, json.JSONDecodeError):
            descriptor = {}
        if not isinstance(descriptor, dict):
            descriptor = {}
        fields = [
            field for field in descriptor.get("fields", []) if isinstance(field, dict)
        ]
        config.setdefault("column_count", len(fields))
        config.setdefault(
            "required_column_count",
            sum(
                1
                for field in fields
                if isinstance(field.get("constraints"), dict)
                and field["constraints"].get("required")
            ),
        )

    config.setdefault("column_count", 0)
    config.setdefault("required_column_count", 0)
    return config


class WorkflowStepEditView(WorkflowObjectMixin, TemplateView):
    """Two-column overview for validator-based steps."""

    template_name = "workflows/workflow_step_detail.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        if self.step.action_id:
            return HttpResponseRedirect(
                reverse_with_org(
                    "workflows:workflow_step_settings",
                    request=request,
                    kwargs={
                        "pk": self.get_workflow().pk,
                        "step_id": self.step.pk,
                    },
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        validator = self.step.validator
        ruleset = None
        assertions = []
        allow_assertions = validator and validator.supports_assertions
        if allow_assertions:
            ruleset = self.step.ruleset or ensure_advanced_ruleset(
                workflow,
                self.step,
                validator,
            )
            assertions = list(ruleset.assertions.all().order_by("order", "pk"))
        # Group assertions by run stage so input/output sections render
        # separately in the step detail template.
        grouped_assertions = {
            "input": [],
            "output": [],
        }
        for assertion in assertions:
            stage = _resolve_assertion_stage(assertion)
            key = "input" if stage == CatalogRunStage.INPUT else "output"
            grouped_assertions[key].append(assertion)
        uses_io_stages = bool(
            validator and _step_has_io_stages(self.step) and allow_assertions,
        )
        uses_tabular_stages = bool(
            validator
            and validator.validation_type == ValidationType.TABULAR
            and allow_assertions,
        )
        tabular_assertion_groups = {
            "dataset": [],
            "row": [],
            "column": [],
        }
        for assertion in assertions:
            stage = (assertion.options or {}).get("tabular_stage", "dataset")
            group = tabular_assertion_groups.get(
                stage,
                tabular_assertion_groups["dataset"],
            )
            group.append(assertion)
        validator_operation = get_validator_operation_display(validator)
        default_assertions_count = (
            validator.default_ruleset.assertions.count()
            if validator and validator.default_ruleset_id
            else 0
        )

        # Build unified step inputs and outputs for the right-column card.
        from validibot.workflows.views_helpers import build_step_io_context

        step_io_context = build_step_io_context(self.step)

        # Workflow-level signal mappings — shown as "Available Signals"
        # on every step editor so authors know what s.name values exist.
        from validibot.workflows.models import WorkflowSignalMapping

        available_signals = list(
            WorkflowSignalMapping.objects.filter(
                workflow=workflow,
            )
            .order_by("position")
            .values("name", "source_path"),
        )

        # Promoted step inputs/outputs from upstream steps — these also
        # appear in the s.* namespace, so authors need to see them
        # alongside workflow-level mappings.
        #
        # Two sources, matching the runtime injection in
        # _inject_promotions and the autocomplete in
        # get_catalog_choices:
        #
        # 1. In-row promotions on step-owned StepIODefinitions.
        # 2. WorkflowStepIOPromotion overlay rows on validator-owned
        #    StepIODefinitions (May 2026 P3 fix — previously this
        #    panel listed only in-row promotions, so authors saw
        #    overlay promotions in autocomplete and at runtime but
        #    not in the visible "Available Data" panel).
        from validibot.validations.models import StepIODefinition
        from validibot.validations.models import WorkflowStepIOPromotion

        promoted_outputs = list(
            StepIODefinition.objects.filter(
                workflow_step__workflow=workflow,
                workflow_step__order__lt=self.step.order,
            )
            .exclude(promoted_signal_name="")
            .values_list("promoted_signal_name", "contract_key")
        )
        for signal_name, contract_key in promoted_outputs:
            available_signals.append(
                {
                    "name": signal_name,
                    "source_path": f"(promoted from {contract_key})",
                }
            )
        overlay_promoted = list(
            WorkflowStepIOPromotion.objects.filter(
                workflow_step__workflow=workflow,
                workflow_step__order__lt=self.step.order,
            )
            .select_related("io_definition")
            .values_list(
                "promoted_signal_name",
                "io_definition__contract_key",
            )
        )
        for signal_name, contract_key in overlay_promoted:
            available_signals.append(
                {
                    "name": signal_name,
                    "source_path": f"(promoted from {contract_key})",
                }
            )

        # Workflow Constants (c.* namespace, ADR-2026-06-18) — shown in the
        # "Available Data" panel WITH their values. Constants are the one
        # namespace whose values are known at authoring time, so unlike signals
        # (name + path only) the panel can show the actual value the author will
        # be asserting against.
        from validibot.workflows.models import WorkflowConstant

        available_constants = list(
            WorkflowConstant.objects.filter(workflow=workflow).order_by("position"),
        )

        # Upstream step outputs and generated files — shown so authors know
        # what steps.<key>.output.<name> and steps.<key>.artifact.<name> paths
        # are available.
        from validibot.workflows.models import WorkflowStep

        upstream_outputs = []
        upstream_artifacts = []
        for ws in (
            WorkflowStep.objects.filter(
                workflow=workflow,
                order__lt=self.step.order,
            )
            .exclude(pk=self.step.pk)
            .order_by("order")
        ):
            from validibot.validations.constants import StepIODirection
            from validibot.validations.constants import StepIOMedium
            from validibot.validations.models import StepIODefinition

            output_query = StepIODefinition.objects.filter(
                workflow_step=ws,
                direction=StepIODirection.OUTPUT,
            ).exclude(io_medium=StepIOMedium.ARTIFACT)
            if ws.validator_id:
                output_query = output_query.union(
                    StepIODefinition.objects.filter(
                        validator=ws.validator,
                        direction=StepIODirection.OUTPUT,
                    ).exclude(io_medium=StepIOMedium.ARTIFACT),
                )
            outputs = list(output_query.values_list("contract_key", flat=True))
            if outputs:
                step_key = ws.step_key or str(ws.pk)
                upstream_outputs.append(
                    {
                        "step_name": ws.name,
                        "step_key": step_key,
                        "outputs": [
                            f"steps.{step_key}.output.{name}" for name in outputs
                        ],
                    },
                )
            artifact_query = StepIODefinition.objects.filter(
                workflow_step=ws,
                direction=StepIODirection.OUTPUT,
                io_medium=StepIOMedium.ARTIFACT,
            )
            if ws.validator_id:
                artifact_query = artifact_query.union(
                    StepIODefinition.objects.filter(
                        validator=ws.validator,
                        direction=StepIODirection.OUTPUT,
                        io_medium=StepIOMedium.ARTIFACT,
                    ),
                )
            artifact_ports = list(artifact_query.values_list("contract_key", flat=True))
            if artifact_ports:
                step_key = ws.step_key or str(ws.pk)
                upstream_artifacts.append(
                    {
                        "step_name": ws.name,
                        "step_key": step_key,
                        "artifacts": [
                            f"steps.{step_key}.artifact.{name}"
                            for name in artifact_ports
                        ],
                    },
                )

        context.update(
            {
                "workflow": workflow,
                "step": self.step,
                "validator": validator,
                "assertions": assertions,
                "assertion_groups": grouped_assertions,
                "uses_io_stages": uses_io_stages,
                "uses_tabular_stages": uses_tabular_stages,
                "tabular_assertion_groups": tabular_assertion_groups,
                "tabular_stage_choices": [
                    (
                        "dataset",
                        _("Dataset assertion"),
                        _("Dataset shape, row count, and column presence."),
                        "bi-table",
                    ),
                    (
                        "row",
                        _("Row assertion"),
                        _("Relationships and conditions within each row."),
                        "bi-list-check",
                    ),
                    (
                        "column",
                        _("Column assertion"),
                        _("Cross-row aggregates for one or more columns."),
                        "bi-bar-chart",
                    ),
                ],
                "tabular_assertion_counts": _tabular_assertion_counts(assertions),
                "tabular_config": (
                    _tabular_summary_config(self.step) if uses_tabular_stages else {}
                ),
                "validator_operation": validator_operation,
                "ruleset": ruleset,
                "can_manage_assertions": self.user_can_manage_workflow()
                and allow_assertions,
                "supports_assertions": allow_assertions,
                "catalog_tab_prefix": f"workflow-step-{self.step.pk}-catalog",
                "validator_default_assertions_count": default_assertions_count,
                "can_manage_workflow": self.user_can_manage_workflow(),
                "step_io_context": step_io_context,
                "available_signals": available_signals,
                "available_constants": available_constants,
                "upstream_outputs": upstream_outputs,
                "upstream_artifacts": upstream_artifacts,
            },
        )
        context.update(
            build_workflow_version_context(
                request=self.request,
                workflow=workflow,
            ),
        )
        return context

    def get_breadcrumbs(self):
        workflow = self.get_workflow()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            self.workflow_breadcrumb_item(
                workflow,
                url=reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            ),
        )
        breadcrumbs.append(
            {
                "name": self.step.step_number_display,
                "url": reverse_with_org(
                    "workflows:workflow_step_edit",
                    request=self.request,
                    kwargs={"pk": workflow.pk, "step_id": self.step.pk},
                ),
            },
        )
        return breadcrumbs


class WorkflowStepTemplateVariablesView(WorkflowObjectMixin, FormView):
    """HTMx endpoint for editing template variable annotations.

    Renders and processes the template variables card that appears in
    the step detail page's right column.  This view is declared as the
    ``view_class`` in the EnergyPlus ``StepEditorCardSpec``.

    GET returns the rendered card partial (for initial page load).
    POST validates the form, merges annotations into ``step.config``,
    and returns the re-rendered card with a toast trigger.
    """

    template_name = "workflows/partials/template_variables_card.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        from validibot.workflows.forms import TemplateVariableAnnotationForm

        return TemplateVariableAnnotationForm(
            data=self.request.POST or None,
            step=self.step,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "workflow": self.get_workflow(),
                "step": self.step,
                "tplvar_form": context.get("form"),
                "display_step_outputs": (self.step.display_settings or {}).get(
                    "display_step_outputs",
                    [],
                ),
            },
        )
        return context

    def form_valid(self, form):
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )
        from validibot.workflows.views_helpers import save_template_variable_annotations

        semantic_fields = {
            field_name
            for field_name in form.changed_data
            if not field_name.endswith("_description")
        }
        with guard_workflow_definition_mutation(
            self.step.workflow_id,
            semantic_change=bool(semantic_fields),
        ):
            save_template_variable_annotations(form)

        if self.request.headers.get("HX-Request"):
            # Re-render the card with updated data and a toast trigger
            context = self.get_context_data(form=self.get_form())
            html = render_to_string(
                self.template_name,
                context,
                request=self.request,
            )
            response = HttpResponse(html)
            response["HX-Trigger"] = json.dumps(
                {
                    "showToast": {
                        "message": str(
                            _("Template variable annotations saved."),
                        ),
                        "level": "success",
                    },
                },
            )
            return response

        return HttpResponseRedirect(
            reverse_with_org(
                "workflows:workflow_step_edit",
                request=self.request,
                kwargs={
                    "pk": self.get_workflow().pk,
                    "step_id": self.step.pk,
                },
            ),
        )

    def form_invalid(self, form):
        context = self.get_context_data(form=form)
        return render(self.request, self.template_name, context)


class WorkflowStepDisplayStepOutputsView(WorkflowObjectMixin, FormView):
    """HTMx modal endpoint for editing which step outputs are shown to users.

    GET returns the modal form content (loaded into displayStepOutputsModal).
    POST validates and saves the selection to
    ``step.display_settings["display_step_outputs"]`` (the cosmetic bucket —
    ADR-2026-06-18), then triggers a page reload via HX-Trigger.
    """

    template_name = "workflows/partials/display_step_outputs_modal_content.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        from validibot.workflows.forms import DisplayStepOutputsForm

        return DisplayStepOutputsForm(
            data=self.request.POST or None,
            step=self.step,
            validator=self.step.validator,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "workflow": self.get_workflow(),
                "step": self.step,
            },
        )
        return context

    def form_valid(self, form):
        selected = form.cleaned_data.get("display_step_outputs", [])
        display_settings = dict(self.step.display_settings or {})
        display_settings["display_step_outputs"] = selected
        self.step.display_settings = display_settings
        self.step.save(update_fields=["display_settings"])

        response = HttpResponse(status=HTTPStatus.NO_CONTENT)
        response["HX-Trigger"] = json.dumps(
            {
                "close-modal": "displayStepOutputsModal",
                "showToast": {
                    "message": str(_("Displayed step outputs updated.")),
                    "level": "success",
                },
            },
        )
        response["HX-Refresh"] = "true"
        return response

    def form_invalid(self, form):
        context = self.get_context_data(form=form)
        return render(self.request, self.template_name, context)


class WorkflowStepToggleStepOutputDisplayView(WorkflowObjectMixin, View):
    """HTMx endpoint that toggles a single step output's visibility.

    POST adds or removes the output key from the step's
    ``display_settings["display_step_outputs"]`` list (the cosmetic bucket —
    ADR-2026-06-18) and returns the updated toggle button HTML fragment.

    An empty ``display_step_outputs`` list means no outputs are shown.
    """

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        slug = self.kwargs["output_key"]
        display_settings = dict(self.step.display_settings or {})
        display_step_outputs = list(display_settings.get("display_step_outputs", []))

        from validibot.validations.constants import StepIODirection

        owner_filter = models.Q(workflow_step=self.step)
        if self.step.validator_id:
            owner_filter |= models.Q(validator=self.step.validator)

        all_slugs = list(
            StepIODefinition.objects.filter(
                owner_filter,
                direction=StepIODirection.OUTPUT,
            ).values_list("contract_key", flat=True),
        )
        if slug not in all_slugs:
            raise Http404("Step output not found.")

        if slug in display_step_outputs:
            display_step_outputs.remove(slug)
        else:
            display_step_outputs.append(slug)

        display_settings["display_step_outputs"] = display_step_outputs
        self.step.display_settings = display_settings
        self.step.save(update_fields=["display_settings"])

        is_shown = slug in display_step_outputs
        return HttpResponse(
            render_to_string(
                "workflows/partials/step_output_show_toggle.html",
                {
                    "output_key": slug,
                    "is_shown": is_shown,
                    "step": self.step,
                    "workflow": self.get_workflow(),
                },
                request=request,
            ),
        )


class WorkflowStepTemplateVariableEditView(WorkflowObjectMixin, FormView):
    """HTMx modal endpoint for editing a single template variable's annotations.

    Replaces the old "save all at once" template variables form with a
    per-variable modal pattern.  The variable is identified by its
    ``StepIODefinition`` row.

    GET returns the modal form content for the specified variable.
    POST validates and saves annotations for that single variable,
    then triggers a page reload.
    """

    template_name = "workflows/partials/template_variable_edit_modal_content.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        self.var_index = int(self.kwargs.get("var_index", 0))

        # Look up the template input by index in the same ordering used
        # by the annotation form and step output display.
        from validibot.validations.constants import StepIOOriginKind
        from validibot.validations.models import StepInputBinding

        bindings = list(
            StepInputBinding.objects.filter(
                workflow_step=self.step,
                io_definition__origin_kind=StepIOOriginKind.TEMPLATE,
            )
            .select_related("io_definition")
            .order_by(
                "io_definition__order",
                "io_definition__contract_key",
            )
        )
        if self.var_index < 0 or self.var_index >= len(bindings):
            return HttpResponse(status=HTTPStatus.NOT_FOUND)

        binding = bindings[self.var_index]
        io_definition = binding.io_definition
        meta = io_definition.metadata or {}
        default_val = binding.default_value
        self.variable = {
            "name": io_definition.native_name or io_definition.contract_key,
            "description": io_definition.label or "",
            "default": str(default_val) if default_val is not None else "",
            "units": io_definition.unit or "",
            "variable_type": meta.get("variable_type", "text"),
            "min_value": meta.get("min_value"),
            "min_exclusive": meta.get("min_exclusive", False),
            "max_value": meta.get("max_value"),
            "max_exclusive": meta.get("max_exclusive", False),
            "choices": meta.get("choices", []),
        }
        self._io_definition_pk = io_definition.pk
        self._binding_pk = binding.pk
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        from validibot.workflows.forms import SingleTemplateVariableForm

        return SingleTemplateVariableForm(
            data=self.request.POST or None,
            variable=self.variable,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "workflow": self.get_workflow(),
                "step": self.step,
                "variable": self.variable,
                "var_index": self.var_index,
            },
        )
        return context

    def form_valid(self, form):
        from validibot.validations.models import StepInputBinding
        from validibot.validations.models import StepIODefinition
        from validibot.validations.step_io_metadata.metadata import (
            TemplateStepIOMetadata,
        )
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )
        from validibot.workflows.views_helpers import _parse_choices
        from validibot.workflows.views_helpers import _parse_optional_float

        variable_type = form.cleaned_data.get("variable_type", "text")
        metadata = TemplateStepIOMetadata(
            variable_type=variable_type,
            min_value=_parse_optional_float(
                form.cleaned_data.get("min_value", ""),
            ),
            min_exclusive=form.cleaned_data.get("min_exclusive", False),
            max_value=_parse_optional_float(
                form.cleaned_data.get("max_value", ""),
            ),
            max_exclusive=form.cleaned_data.get("max_exclusive", False),
            choices=_parse_choices(
                form.cleaned_data.get("choices", ""),
            ),
        ).model_dump()

        semantic_change = bool(set(form.changed_data).difference({"description"}))
        with guard_workflow_definition_mutation(
            self.step.workflow_id,
            semantic_change=semantic_change,
        ):
            StepIODefinition.objects.filter(pk=self._io_definition_pk).update(
                label=form.cleaned_data.get("description", ""),
                unit=form.cleaned_data.get("units", ""),
                metadata=metadata,
                provider_binding={"variable_type": variable_type},
            )
            default_val = form.cleaned_data.get("default", "")
            StepInputBinding.objects.filter(pk=self._binding_pk).update(
                default_value=default_val if default_val else None,
                is_required=not bool(default_val),
            )

        response = HttpResponse(status=HTTPStatus.NO_CONTENT)
        response["HX-Trigger"] = json.dumps(
            {
                "close-modal": "templateVariableEditModal",
                "showToast": {
                    "message": str(
                        _("Variable %(name)s updated.")
                        % {"name": f"${self.variable['name']}"},
                    ),
                    "level": "success",
                },
            },
        )
        response["HX-Refresh"] = "true"
        return response

    def form_invalid(self, form):
        context = self.get_context_data(form=form)
        return render(self.request, self.template_name, context)


class WorkflowStepIOEditView(WorkflowObjectMixin, FormView):
    """Edit a step I/O definition and its binding via an HTMx modal.

    Handles both step-owned definitions (all fields editable) and library-owned
    definitions (definition fields read-only, binding fields editable).

    The definition must belong to either the current step (step-owned) or the
    step's validator (library-owned). This prevents editing definitions from
    other steps or workflows.

    Uses WorkflowObjectMixin (not WorkflowStepAssertionsMixin) because
    step I/O editing doesn't require assertion support — that mixin
    would reject steps whose validators don't support assertions,
    which is unrelated.
    """

    template_name = "workflows/partials/step_io_edit_modal_content.html"
    form_class = StepInputBindingEditForm

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        # Resolve the step early so the definition and binding lookups can use it.
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def _get_io_definition(self):
        """Fetch the I/O definition, scoped to this step or its validator."""
        if hasattr(self, "_io_definition"):
            return self._io_definition
        io_definition_id = self.kwargs.get("io_definition_id")
        # Scope the lookup to definitions owned by the step or its validator.
        io_definition = (
            StepIODefinition.objects.filter(pk=io_definition_id)
            .filter(
                models.Q(workflow_step=self.step)
                | models.Q(validator=self.step.validator),
            )
            .first()
        )
        if not io_definition:
            raise Http404
        self._io_definition = io_definition
        return io_definition

    def _get_binding(self):
        """Get a binding, or build an unsaved default without mutating on GET."""
        if hasattr(self, "_binding"):
            return self._binding
        io_definition = self._get_io_definition()
        self._binding = StepInputBinding.objects.filter(
            workflow_step=self.step,
            io_definition=io_definition,
        ).first()
        if self._binding is None:
            is_artifact_input = (
                io_definition.direction == "input"
                and io_definition.io_medium == "artifact"
            )
            self._binding = StepInputBinding(
                workflow_step=self.step,
                io_definition=io_definition,
                source_scope=(
                    BindingSourceScope.SUBMISSION_FILE
                    if is_artifact_input
                    else BindingSourceScope.SUBMISSION_PAYLOAD
                ),
                source_data_path=(
                    "primary" if is_artifact_input else io_definition.native_name or ""
                ),
                is_required=io_definition.min_items > 0 if is_artifact_input else True,
            )
        return self._binding

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["io_definition"] = self._get_io_definition()
        kwargs["binding"] = self._get_binding()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        io_definition = self._get_io_definition()
        from validibot.validations.constants import StepIODirection

        is_input = io_definition.direction == StepIODirection.INPUT
        title_prefix = _("Edit input") if is_input else _("Edit output")
        context.update(
            {
                "io_definition": io_definition,
                "binding": self._get_binding(),
                "is_library_definition": bool(io_definition.validator_id),
                "is_path_editable": io_definition.is_path_editable,
                "source_kind_display": io_definition.get_source_kind_display(),
                "is_artifact_input": bool(
                    is_input and io_definition.io_medium == "artifact"
                ),
                "modal_title": (
                    f"{title_prefix}: "
                    f"{io_definition.label or io_definition.contract_key}"
                ),
            },
        )

        # Build source path suggestions for the datalist: workflow-level
        # signals (s.name) so the user can easily bind an input to a signal.
        if is_input and io_definition.io_medium != "artifact":
            from validibot.workflows.models import WorkflowSignalMapping

            workflow = self.get_workflow()
            signal_suggestions = [
                {
                    "value": f"s.{m['name']}",
                    "label": m["source_path"],
                }
                for m in WorkflowSignalMapping.objects.filter(
                    workflow=workflow,
                )
                .order_by("position")
                .values("name", "source_path")
            ]
            context["source_path_suggestions"] = signal_suggestions

        return context

    def form_valid(self, form):
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )

        binding_is_new = bool(form.binding and form.binding._state.adding)
        semantic_change = binding_is_new or bool(
            set(form.changed_data).difference({"description", "label"}),
        )
        with guard_workflow_definition_mutation(
            self.step.workflow_id,
            semantic_change=semantic_change,
        ):
            try:
                form.save()
            except ValidationError as exc:
                # The form performs an early stale-write check for a useful
                # field-level error. The binding service repeats that check
                # after taking the workflow lock, closing the small race where
                # another editor saves between validation and persistence.
                _add_validation_error_to_form(form, exc)
                return self.form_invalid(form)
        messages.success(self.request, _("Step I/O definition updated."))
        response = HttpResponse(status=200)
        response["HX-Refresh"] = "true"
        return response

    def render_to_response(self, context, **response_kwargs):
        if self.request.headers.get("HX-Request"):
            return render(
                self.request,
                self.template_name,
                context,
                status=response_kwargs.get("status", 200),
            )
        return super().render_to_response(context, **response_kwargs)


class WorkflowStepIOAutoLinkView(WorkflowObjectMixin, View):
    """POST: auto-link a step input to a matching workflow-level signal.

    Looks for a ``WorkflowSignalMapping`` whose ``name`` matches the
    input definition's ``contract_key``. When found, sets the
    ``StepInputBinding.source_data_path`` to ``s.<name>`` and refreshes
    the page.  When no match exists, adds a warning message.
    """

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        from validibot.workflows.models import WorkflowSignalMapping

        workflow = self.get_workflow()
        io_definition_id = self.kwargs.get("io_definition_id")

        # Scope the input definition to this step or its validator.
        io_definition = (
            StepIODefinition.objects.filter(pk=io_definition_id)
            .filter(
                models.Q(workflow_step=self.step)
                | models.Q(validator=self.step.validator),
            )
            .first()
        )
        if not io_definition:
            raise Http404

        contract_key = io_definition.contract_key

        mapping = WorkflowSignalMapping.objects.filter(
            workflow=workflow,
            name=contract_key,
        ).first()

        if not mapping:
            messages.warning(
                request,
                _(
                    "No matching workflow signal found. "
                    "Create a signal named '%(name)s' first."
                )
                % {"name": contract_key},
            )
            response = HttpResponse(status=HTTPStatus.OK)
            response["HX-Refresh"] = "true"
            return response

        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )

        with guard_workflow_definition_mutation(workflow.pk):
            binding, created = StepInputBinding.objects.get_or_create(
                workflow_step=self.step,
                io_definition=io_definition,
                defaults={
                    "source_scope": BindingSourceScope.SIGNAL,
                    "source_data_path": mapping.name,
                    "is_required": True,
                },
            )
            if not created:
                binding.source_scope = BindingSourceScope.SIGNAL
                binding.source_data_path = mapping.name
                binding.save(update_fields=["source_scope", "source_data_path"])

        messages.success(
            request,
            _("Linked '%(key)s' to signal s.%(name)s.")
            % {"key": contract_key, "name": mapping.name},
        )
        response = HttpResponse(status=HTTPStatus.OK)
        response["HX-Refresh"] = "true"
        return response


class WorkflowStepOutputsPartialView(WorkflowObjectMixin, View):
    """GET: return the re-rendered validator outputs table partial.

    Used by HTMx to refresh just the outputs table after a promote/demote
    action, preserving the active tab state on the step detail page.
    """

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = get_object_or_404(
            WorkflowStep,
            workflow=workflow,
            pk=self.kwargs.get("step_id"),
        )
        from validibot.workflows.views_helpers import build_step_io_context

        step_io_context = build_step_io_context(step)
        return render(
            request,
            "workflows/partials/step_outputs_table.html",
            {
                "step_io_context": step_io_context,
                "step": step,
                "workflow": workflow,
                "can_manage_workflow": self.user_can_manage_workflow(),
            },
        )


class WorkflowStepInputsPartialView(WorkflowObjectMixin, View):
    """GET: return the re-rendered step inputs table partial.

    Symmetric to ``WorkflowStepOutputsPartialView`` per the May 2026
    P2 review: input promotion via "Copy to Signal" succeeded server-
    side but the input table didn't refresh, leaving the row showing
    the empty form until a full page reload. This endpoint lets the
    input table re-render via the same ``signals-changed`` HTMx
    event the output table listens for.
    """

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = get_object_or_404(
            WorkflowStep,
            workflow=workflow,
            pk=self.kwargs.get("step_id"),
        )
        from validibot.workflows.views_helpers import build_step_io_context

        step_io_context = build_step_io_context(step)
        return render(
            request,
            "workflows/partials/step_inputs_table.html",
            {
                "step_io_context": step_io_context,
                "step": step,
                "workflow": workflow,
                "can_manage_workflow": self.user_can_manage_workflow(),
            },
        )


class WorkflowStepCreateView(WorkflowStepFormView):
    """Create a new workflow step for the given validator."""

    mode = "create"


class WorkflowActionStepCreateView(WorkflowStepFormView):
    """Create a new workflow step for the selected action definition."""

    mode = "create"


class WorkflowStepUpdateView(WorkflowStepFormView):
    """Edit an existing workflow step in full-page mode."""

    mode = "update"


class WorkflowStepDeleteView(WorkflowObjectMixin, View):
    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        step = get_object_or_404(
            WorkflowStep,
            workflow=workflow,
            pk=self.kwargs.get("step_id"),
        )
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )

        artifact_consumers = list(
            step.artifact_output_consumers.select_related("workflow_step").order_by(
                "workflow_step__order",
            )
        )
        if artifact_consumers:
            consumer_names = ", ".join(
                binding.workflow_step.name or binding.workflow_step.step_key
                for binding in artifact_consumers
            )
            return hx_trigger_response(
                status_code=400,
                message=_(
                    "This step supplies a file to: %(consumers)s. Change those "
                    "inputs before deleting it."
                )
                % {"consumers": consumer_names},
                level="warning",
            )

        with guard_workflow_definition_mutation(workflow.pk):
            try:
                step.delete()
            except (models.ProtectedError, models.RestrictedError):
                messages.warning(
                    request,
                    _(
                        "This step cannot be deleted because it has "
                        "existing validation runs. Remove the runs first."
                    ),
                )
                response = HttpResponse(status=200)
                response["HX-Refresh"] = "true"
                return response
            resequence_workflow_steps(workflow)
        message = _("Workflow step removed.")
        return hx_trigger_response(message, close_modal=None)


class WorkflowStepMoveView(WorkflowObjectMixin, View):
    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        step = get_object_or_404(
            WorkflowStep,
            workflow=workflow,
            pk=self.kwargs.get("step_id"),
        )
        direction = request.POST.get("direction")
        steps = list(workflow.steps.all().order_by("order", "pk"))
        try:
            index = steps.index(step)
        except ValueError:
            return hx_trigger_response(
                status_code=400,
                message=_("Step not found."),
                level="warning",
            )
        if direction == "up" and index > 0:
            steps[index - 1], steps[index] = steps[index], steps[index - 1]
        elif direction == "down" and index < len(steps) - 1:
            steps[index], steps[index + 1] = steps[index + 1], steps[index]
        else:
            return hx_trigger_response(status_code=204)
        # Validate credential step placement before persisting the
        # new order.  The clean() method on WorkflowStep only fires
        # on full_clean(), but the reorder uses raw .update() for
        # performance.  We check the proposed order here instead.
        placement_error = _validate_credential_step_order(steps)
        if placement_error:
            return hx_trigger_response(
                status_code=400,
                message=placement_error,
                level="warning",
            )

        from validibot.validations.services.artifact_bindings import (
            validate_workflow_dependencies,
        )

        proposed_order = {
            item.pk: position * 10 for position, item in enumerate(steps, start=1)
        }
        try:
            validate_workflow_dependencies(
                workflow,
                proposed_order=proposed_order,
            )
        except ValidationError as exc:
            return hx_trigger_response(
                status_code=400,
                message=" ".join(exc.messages),
                level="warning",
            )

        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )

        with guard_workflow_definition_mutation(workflow.pk):
            for pos, item in enumerate(steps, start=1):
                WorkflowStep.objects.filter(pk=item.pk).update(order=1000 + pos)
            resequence_workflow_steps(workflow)
        message = _("Workflow step order updated.")
        return hx_trigger_response(message, close_modal=None)


def _validate_credential_step_order(
    steps: list[WorkflowStep],
) -> str | None:
    """Check proposed step order for credential step placement violations.

    Returns an error message string if the proposed order violates the
    placement rules, or ``None`` if the order is valid.

    Rules:
        - All validator steps must appear before any credential step.
        - All BLOCKING action steps must appear before any credential step.
        - ADVISORY action steps may appear after the credential step.
    """
    from validibot.actions.constants import ActionFailureMode

    credential_index = None
    for i, step in enumerate(steps):
        if (
            step.action_id
            and step.action.definition_id
            and step.action.definition.type == CredentialActionType.SIGNED_CREDENTIAL
        ):
            credential_index = i
            break

    if credential_index is None:
        return None  # No credential step — no placement rules to check.

    # Check for validators or BLOCKING actions after the credential step.
    for step in steps[credential_index + 1 :]:
        if step.validator_id:
            return str(
                _("The signed credential step must come after all validation steps.")
            )
        if step.action_id and step.action.failure_mode == ActionFailureMode.BLOCKING:
            return str(
                _(
                    "The signed credential step must come after all "
                    "blocking action steps."
                )
            )

    return None


def _annotate_reorder_controls(steps: list[WorkflowStep]) -> bool:
    """Add move-button state to step objects for the workflow editor.

    The workflow detail page renders move up/down buttons inline. This helper
    simulates each move in memory and disables buttons that would violate the
    signed-credential placement rule, so authors get guidance before they click.
    """

    has_credential_step = False
    last_index = len(steps) - 1

    for index, step in enumerate(steps):
        step.move_up_disabled = index == 0
        step.move_down_disabled = index == last_index
        step.move_up_reason = str(_("This step is already first."))
        step.move_down_reason = str(_("This step is already last."))
        step.credential_ordering_hint = ""

        if _is_signed_credential_step(step):
            has_credential_step = True
            step.credential_ordering_hint = str(
                _(
                    "This step must remain after all validation steps and "
                    "blocking actions."
                )
            )

        if index > 0:
            proposed = list(steps)
            proposed[index - 1], proposed[index] = proposed[index], proposed[index - 1]
            placement_error = _validate_credential_step_order(proposed)
            if placement_error:
                step.move_up_disabled = True
                step.move_up_reason = placement_error

        if index < last_index:
            proposed = list(steps)
            proposed[index], proposed[index + 1] = proposed[index + 1], proposed[index]
            placement_error = _validate_credential_step_order(proposed)
            if placement_error:
                step.move_down_disabled = True
                step.move_down_reason = placement_error

    return has_credential_step


def _is_signed_credential_step(step: WorkflowStep) -> bool:
    """Return True when a workflow step is the signed credential action."""

    return bool(
        step.action_id
        and step.action
        and step.action.definition_id
        and step.action.definition.type == CredentialActionType.SIGNED_CREDENTIAL
    )
