"""Custom validator CRUD: create, update, and delete operations."""

import json
import logging

from django import forms
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from django.http import HttpResponse
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from django.views.generic import View
from django.views.generic.edit import FormView

from validibot.core.utils import reverse_with_org
from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import CustomValidatorType
from validibot.validations.constants import FMUProbeStatus
from validibot.validations.constants import ValidationType
from validibot.validations.forms import CustomValidatorCreateForm
from validibot.validations.forms import CustomValidatorUpdateForm
from validibot.validations.forms import FMUValidatorCreateForm
from validibot.validations.forms import ShaclLibraryValidatorCreateForm
from validibot.validations.forms import ShaclLibraryValidatorUpdateForm
from validibot.validations.models import Validator
from validibot.validations.services.custom_validator_contracts import (
    custom_validator_data_format,
)
from validibot.validations.services.fmu import FMUIntrospectionError
from validibot.validations.services.fmu import create_fmu_validator
from validibot.validations.services.fmu import run_fmu_probe
from validibot.validations.utils import create_custom_validator
from validibot.validations.utils import create_shacl_library_validator
from validibot.validations.utils import update_custom_validator
from validibot.validations.utils import update_shacl_library_validator
from validibot.validations.views.library import ValidatorLibraryMixin
from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


class CustomValidatorManageMixin(ValidatorLibraryMixin):
    """Require author/admin access for CRUD operations."""

    def dispatch(self, request, *args, **kwargs):
        if not self.require_manage_permission():
            return redirect(
                reverse_with_org(
                    "validations:validation_library",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self, validator):
        return reverse_with_org(
            "validations:validator_detail",
            request=self.request,
            kwargs={"slug": validator.slug},
        )

    def get_latest_custom_validator_row(self, **filters):
        """Resolve slug-based custom CRUD routes to the latest validator row."""
        validator = (
            Validator.objects.filter(
                slug=self.kwargs["slug"],
                org=self.get_active_org(),
                is_system=False,
                **filters,
            )
            .order_by("-version", "-pk")
            .first()
        )
        if validator is None:
            raise Http404("No Validator matches the given query.")
        return validator


class FMUValidatorCreateView(CustomValidatorManageMixin, FormView):
    template_name = "validations/library/fmu_validator_form.html"
    form_class = FMUValidatorCreateForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["org"] = self.get_active_org()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create FMU Validator")
        context["can_manage_validators"] = self.can_manage_validators()
        context["validator"] = None
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Create FMU validator"),
                "url": "",
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        org = self.get_active_org()
        try:
            validator = create_fmu_validator(
                org=org,
                project=form.cleaned_data.get("project"),
                name=form.cleaned_data["name"],
                short_description=form.cleaned_data.get("short_description") or "",
                description=form.cleaned_data.get("description") or "",
                upload=form.cleaned_data["fmu_file"],
            )
        except FMUIntrospectionError as exc:
            form.add_error("fmu_file", str(exc))
            return self.form_invalid(form)
        messages.success(
            self.request,
            _("Created FMU validator \u201c%(name)s\u201d.") % {"name": validator.name},
        )
        return redirect(self.get_success_url(validator))


class FMUProbeStartView(CustomValidatorManageMixin, View):
    """HTMX endpoint to kick off an FMU probe inline."""

    def post(self, request, *args, **kwargs):
        # SECURITY: scope the validator lookup to the caller's active org so
        # an FMU probe cannot be started against (or its status read from)
        # another org's validator by enumerating its primary key.
        validator = get_object_or_404(
            Validator,
            pk=kwargs["pk"],
            is_system=False,
            org=self.get_active_org(),
        )
        fmu = getattr(validator, "fmu_model", None)
        if not fmu:
            return JsonResponse(
                {"status": "error", "message": _("No FMU attached to this validator.")},
                status=400,
            )
        probe = getattr(fmu, "probe_result", None)
        if probe:
            probe.status = FMUProbeStatus.PENDING
            probe.last_error = ""
            probe.save(update_fields=["status", "last_error", "modified"])
        result = run_fmu_probe(fmu)
        # Refresh probe record to reflect latest status written by run_fmu_probe
        fmu.refresh_from_db(fields=["probe_result"])
        probe = getattr(fmu, "probe_result", None)
        payload = {
            "status": getattr(probe, "status", getattr(result, "status", "unknown")),
            "last_error": getattr(probe, "last_error", ""),
            "details": getattr(probe, "details", {}),
        }
        return JsonResponse(payload)


class FMUProbeStatusView(CustomValidatorManageMixin, View):
    """Return the latest probe status for polling."""

    def get(self, request, *args, **kwargs):
        # SECURITY: scope the validator lookup to the caller's active org so
        # an FMU probe cannot be started against (or its status read from)
        # another org's validator by enumerating its primary key.
        validator = get_object_or_404(
            Validator,
            pk=kwargs["pk"],
            is_system=False,
            org=self.get_active_org(),
        )
        fmu = getattr(validator, "fmu_model", None)
        probe = getattr(fmu, "probe_result", None) if fmu else None
        if not probe:
            return JsonResponse(
                {"status": "missing", "message": _("Probe has not been requested.")},
                status=404,
            )
        data = {
            "status": probe.status,
            "last_error": probe.last_error,
            "details": probe.details,
        }
        return JsonResponse(data)


class CustomValidatorCreateView(CustomValidatorManageMixin, FormView):
    template_name = "validations/library/custom_validator_form.html"
    form_class = CustomValidatorCreateForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create Custom Basic Validator")
        context["can_manage_validators"] = self.can_manage_validators()
        context["validator"] = None
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Create new validator"),
                "url": "",
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        org = self.get_active_org()
        custom_validator = create_custom_validator(
            org=org,
            user=self.request.user,
            name=form.cleaned_data["name"],
            short_description=form.cleaned_data.get("short_description") or "",
            description=form.cleaned_data.get("description") or "",
            custom_type=CustomValidatorType.SIMPLE,
            notes=form.cleaned_data.get("notes") or "",
            allow_custom_assertion_targets=form.cleaned_data.get(
                "allow_custom_assertion_targets",
                False,
            ),
            input_data_format=(
                form.cleaned_data.get("input_data_format") or SubmissionDataFormat.JSON
            ),
        )
        messages.success(
            self.request,
            _("Created custom validator \u201c%(name)s\u201d.")
            % {"name": custom_validator.validator.name},
        )
        return redirect(self.get_success_url(custom_validator.validator))


class CustomValidatorUpdateView(CustomValidatorManageMixin, FormView):
    template_name = "validations/library/custom_validator_form.html"
    form_class = CustomValidatorUpdateForm

    def dispatch(self, request, *args, **kwargs):
        try:
            self.custom_validator = self.get_object()
        except ObjectDoesNotExist:
            messages.error(
                request,
                _(
                    "This custom validator is missing its configuration. "
                    "Please recreate it from the Validator Library."
                ),
            )
            return redirect(
                reverse_with_org(
                    "validations:validation_library",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        return self.get_latest_custom_validator_row().custom_validator

    def get_initial(self):
        validator = self.custom_validator.validator
        return {
            "name": validator.name,
            "short_description": validator.short_description,
            "description": validator.description,
            "allow_custom_assertion_targets": validator.allow_custom_assertion_targets,
            "input_data_format": custom_validator_data_format(validator),
            "notes": self.custom_validator.notes,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validator = self.custom_validator.validator
        context.update(
            {
                "form_title": _("Edit %(name)s Settings") % {"name": validator.name},
                "validator": validator,
                "can_manage_validators": self.can_manage_validators(),
            }
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        validator = self.custom_validator.validator
        label = validator.name or validator.slug
        breadcrumbs.append(
            {
                "name": _("Edit \u201c%(name)s\u201d") % {"name": label},
                "url": reverse_with_org(
                    "validations:validator_detail",
                    request=self.request,
                    kwargs={"slug": validator.slug},
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Edit Settings"),
                "url": "",
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        custom = update_custom_validator(
            self.custom_validator,
            name=form.cleaned_data["name"],
            short_description=form.cleaned_data.get("short_description") or "",
            description=form.cleaned_data.get("description") or "",
            notes=form.cleaned_data.get("notes") or "",
            allow_custom_assertion_targets=form.cleaned_data.get(
                "allow_custom_assertion_targets",
            ),
            input_data_format=(
                form.cleaned_data.get("input_data_format") or SubmissionDataFormat.JSON
            ),
        )
        messages.success(
            self.request,
            _("Updated custom validator \u201c%(name)s\u201d.")
            % {"name": custom.validator.name},
        )
        return redirect(self.get_success_url(custom.validator))


class CustomValidatorDeleteView(CustomValidatorManageMixin, TemplateView):
    template_name = "validations/library/custom_validator_confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        self.custom_validator = self.get_object()
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        return self.get_latest_custom_validator_row().custom_validator

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validator = self.custom_validator.validator
        blockers = self._list_delete_blockers(validator)
        context.update(
            {
                "validator": validator,
                "can_manage_validators": self.can_manage_validators(),
                "delete_blockers": blockers,
                "can_delete": not blockers,
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        return self.handle_delete(request)

    def delete(self, request, *args, **kwargs):
        return self.handle_delete(request)

    def handle_delete(self, request):
        validator = self.custom_validator.validator
        blocker = self._get_delete_blocker(validator)
        if blocker:
            return self._delete_blocked_response(request, blocker)

        success_message = _("Deleted custom validator \u201c%(name)s\u201d.") % {
            "name": validator.name
        }
        validator.delete()
        if request.headers.get("HX-Request"):
            return self._hx_toast_response(success_message, status=200)
        messages.success(request, success_message)
        return redirect(
            reverse_with_org(
                "validations:validation_library",
                request=request,
            ),
        )

    def _get_delete_blocker(self, validator):
        if WorkflowStep.objects.filter(validator=validator).exists():
            return _(
                "Cannot delete %(name)s because workflow steps "
                "still reference this validator.",
            ) % {"name": validator.name}
        return None

    def _list_delete_blockers(self, validator):
        blockers: list[dict[str, str]] = []
        steps = WorkflowStep.objects.filter(validator=validator).select_related(
            "workflow",
        )
        for step in steps:
            workflow_name = step.workflow.name if step.workflow else _("Unknown")
            blockers.append(
                {
                    "label": _(
                        "Workflow step \u201c%(step)s\u201d (workflow: %(workflow)s)"
                    )
                    % {
                        "step": step.name,
                        "workflow": workflow_name,
                    },
                    "url": reverse_with_org(
                        "workflows:workflow_detail",
                        request=self.request,
                        kwargs={"pk": step.workflow_id},
                    )
                    if step.workflow_id
                    else "",
                }
            )
        return blockers

    def _delete_blocked_response(self, request, message):
        if request.headers.get("HX-Request"):
            return self._hx_toast_response(
                message,
                level="danger",
                status=400,
                reswap="none",
            )
        form = forms.Form(data={})
        form.full_clean()
        form.add_error(None, message)
        context = self.get_context_data()
        context["error_message"] = message
        context["form"] = form
        return render(
            request,
            self.template_name,
            context,
            status=200,
        )

    def _hx_toast_response(self, message, *, level="success", status=200, reswap=None):
        response = HttpResponse("", status=status)
        response["HX-Trigger"] = json.dumps(
            {
                "toast": {
                    "level": level,
                    "message": str(message),
                }
            }
        )
        if reswap:
            response["HX-Reswap"] = reswap
        return response


# ════════════════════════════════════════════════════════════════════════════
# SHACL library validators
# ════════════════════════════════════════════════════════════════════════════
#
# Org-owned, named SHACL validators an organisation creates once and
# references from many workflows. Mirror the existing Custom + FMU
# validator UI surfaces so the validator-library experience is
# consistent regardless of validator type. The underlying data shape
# differs (SHACL uses the existing Validator + default_ruleset, no
# wrapper model) but the operator-facing view contract is identical:
# create / update / delete from the library page.
#
# See ADR-2026-05-18 ``SHACL Validator for RDF Graph Validation``
# section "Library-level custom SHACL validators".


class ShaclLibraryValidatorCreateView(CustomValidatorManageMixin, FormView):
    """Create a new SHACL library validator from the validator library page."""

    template_name = "validations/library/custom_validator_form.html"
    form_class = ShaclLibraryValidatorCreateForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create SHACL Library Validator")
        context["can_manage_validators"] = self.can_manage_validators()
        context["validator"] = None
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Create SHACL library validator"),
                "url": "",
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        org = self.get_active_org()
        validator = create_shacl_library_validator(
            org=org,
            user=self.request.user,
            form=form,
            notes=form.cleaned_data.get("notes") or "",
        )
        messages.success(
            self.request,
            _("Created SHACL library validator “%(name)s”.") % {"name": validator.name},
        )
        return redirect(self.get_success_url(validator))


class ShaclLibraryValidatorUpdateView(CustomValidatorManageMixin, FormView):
    """Edit metadata + shapes for an existing SHACL library validator."""

    template_name = "validations/library/custom_validator_form.html"
    form_class = ShaclLibraryValidatorUpdateForm

    def dispatch(self, request, *args, **kwargs):
        try:
            self.validator = self.get_validator()
        except ObjectDoesNotExist:
            messages.error(
                request,
                _(
                    "This SHACL library validator could not be loaded. "
                    "It may have been deleted.",
                ),
            )
            return redirect(
                reverse_with_org(
                    "validations:validation_library",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_validator(self):
        return self.get_latest_custom_validator_row(
            validation_type=ValidationType.SHACL,
        )

    def get_initial(self):
        validator = self.validator
        ruleset = validator.default_ruleset
        metadata = (ruleset.metadata if ruleset else {}) or {}
        return {
            "name": validator.name,
            "short_description": validator.short_description,
            "description": validator.description,
            "inference_mode": metadata.get("inference_mode", "rdfs"),
            "advanced_shacl": metadata.get("advanced_shacl", False),
            "submission_format": metadata.get("submission_format", "auto"),
            "bundle_brick": "brick-1.4" in (metadata.get("bundled_standards") or []),
            "bundle_qudt": "qudt-2.1" in (metadata.get("bundled_standards") or []),
            "notes": metadata.get("library_validator_notes", ""),
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "form_title": _("Edit %(name)s") % {"name": self.validator.name},
                "validator": self.validator,
                "can_manage_validators": self.can_manage_validators(),
            },
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        label = self.validator.name or self.validator.slug
        breadcrumbs.append(
            {
                "name": _("Edit “%(name)s”") % {"name": label},
                "url": reverse_with_org(
                    "validations:validator_detail",
                    request=self.request,
                    kwargs={"slug": self.validator.slug},
                ),
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        validator = update_shacl_library_validator(
            self.validator,
            form=form,
            notes=form.cleaned_data.get("notes") or "",
        )
        messages.success(
            self.request,
            _("Updated SHACL library validator “%(name)s”.") % {"name": validator.name},
        )
        return redirect(self.get_success_url(validator))


class ShaclLibraryValidatorDeleteView(CustomValidatorManageMixin, TemplateView):
    """Delete a SHACL library validator (blocked if workflow steps reference it)."""

    template_name = "validations/library/custom_validator_confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        self.validator = self.get_validator()
        return super().dispatch(request, *args, **kwargs)

    def get_validator(self):
        return self.get_latest_custom_validator_row(
            validation_type=ValidationType.SHACL,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        blockers = self._list_delete_blockers(self.validator)
        context.update(
            {
                "validator": self.validator,
                "can_manage_validators": self.can_manage_validators(),
                "delete_blockers": blockers,
                "can_delete": not blockers,
            },
        )
        return context

    def post(self, request, *args, **kwargs):
        return self.handle_delete(request)

    def delete(self, request, *args, **kwargs):
        return self.handle_delete(request)

    def handle_delete(self, request):
        if WorkflowStep.objects.filter(validator=self.validator).exists():
            message = _(
                "Cannot delete %(name)s because workflow steps "
                "still reference this validator.",
            ) % {"name": self.validator.name}
            messages.error(request, message)
            return redirect(
                reverse_with_org(
                    "validations:validator_detail",
                    request=request,
                    kwargs={"slug": self.validator.slug},
                ),
            )

        success_message = _(
            "Deleted SHACL library validator “%(name)s”.",
        ) % {"name": self.validator.name}
        default_ruleset = self.validator.default_ruleset
        self.validator.delete()
        if (
            default_ruleset is not None
            and not Validator.objects.filter(default_ruleset=default_ruleset).exists()
            and not WorkflowStep.objects.filter(ruleset=default_ruleset).exists()
        ):
            default_ruleset.delete()
        messages.success(request, success_message)
        return redirect(
            reverse_with_org(
                "validations:validation_library",
                request=request,
            ),
        )

    def _list_delete_blockers(self, validator):
        blockers: list[dict[str, str]] = []
        steps = WorkflowStep.objects.filter(validator=validator).select_related(
            "workflow",
        )
        for step in steps:
            workflow_name = step.workflow.name if step.workflow else _("Unknown")
            blockers.append(
                {
                    "label": _(
                        "Workflow step “%(step)s” (workflow: %(workflow)s)",
                    )
                    % {"step": step.name, "workflow": workflow_name},
                    "url": reverse_with_org(
                        "workflows:workflow_detail",
                        request=self.request,
                        kwargs={"pk": step.workflow_id},
                    )
                    if step.workflow_id
                    else "",
                },
            )
        return blockers
