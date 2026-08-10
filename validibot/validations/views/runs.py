"""Validation run viewing: list, detail, JSON export, delete, and guest access."""

import logging

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.http import FileResponse
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic.edit import DeleteView

from validibot.core.mixins import BreadcrumbMixin
from validibot.core.utils import reverse_with_org
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.credential_utils import (
    build_signed_credential_download_filename,
)
from validibot.validations.credential_utils import (
    extract_signed_credential_resource_label,
)
from validibot.validations.credential_utils import get_signed_credential_display_context
from validibot.validations.models import Artifact
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.artifact_display import (
    ArtifactDownloadUnavailableError,
)
from validibot.validations.services.artifact_display import build_artifact_display_item
from validibot.validations.services.artifact_display import build_artifact_display_items
from validibot.validations.services.artifact_display import open_artifact_download
from validibot.validations.services.report_layout import resolve_report_layout
from validibot.workflows.models import Workflow

logger = logging.getLogger(__name__)


# UI Views
# ------------------------------------------------------------------------------


class ValidationRunAccessMixin(LoginRequiredMixin, BreadcrumbMixin):
    """Shared queryset helpers for validation run UI views."""

    allowed_sorts = {
        "created": "created",
        "-created": "-created",
        "status": "status",
        "-status": "-status",
        "workflow": "workflow__name",
        "-workflow": "-workflow__name",
    }

    def get_base_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ValidationRun.objects.none()
        active_org = getattr(self.request, "active_org", None)
        active_org_id = (
            active_org.id if active_org else getattr(user, "current_org_id", None)
        )
        step_run_prefetch = Prefetch(
            "step_runs",
            queryset=ValidationStepRun.objects.select_related("workflow_step")
            .prefetch_related("findings", "findings__ruleset_assertion")
            .order_by("step_order", "pk"),
        )
        findings_prefetch = Prefetch(
            "findings",
            queryset=ValidationFinding.objects.select_related(
                "validation_step_run",
                "validation_step_run__workflow_step",
                "ruleset_assertion",
            ).order_by("severity", "-created"),
        )
        return (
            ValidationRun.objects.for_user(user, org=active_org_id)
            .select_related("workflow", "submission", "org")
            .prefetch_related(step_run_prefetch, findings_prefetch)
            .order_by("-created")
        )

    def get_ordering(self):
        sort = self.request.GET.get("sort", "-created")
        return self.allowed_sorts.get(sort, "-created")

    def get_queryset(self):
        return self.get_base_queryset()


class ValidationRunListView(ValidationRunAccessMixin, ListView):
    template_name = "validations/validation_list.html"
    context_object_name = "validations"
    paginate_by = 20
    page_size_options = (10, 50, 100)
    page_size_session_key = "validation_list_per_page"
    breadcrumbs = [
        {"name": _("Validations"), "url": ""},
    ]

    def get_queryset(self):
        qs = self.get_base_queryset()
        status_filter = self.request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        workflow_filter = self.request.GET.get("workflow")
        if workflow_filter:
            qs = qs.filter(workflow_id=workflow_filter)
        ordering = self.get_ordering()
        return qs.order_by(ordering)

    def get_paginate_by(self, queryset):
        per_page = self.request.GET.get("per_page")
        if per_page:
            try:
                per_page = int(per_page)
            except (TypeError, ValueError):
                per_page = None
            else:
                if per_page in self.page_size_options:
                    self.request.session[self.page_size_session_key] = per_page
                else:
                    per_page = None

        if per_page is None:
            per_page = self.request.session.get(self.page_size_session_key)

        if per_page not in self.page_size_options:
            per_page = self.paginate_by

        self.page_size = per_page
        return per_page

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        for validation in context.get("validations", []):
            can_view_all = user.has_perm(
                PermissionCode.VALIDATION_RESULTS_VIEW_ALL.value,
                validation,
            )
            can_view_own = user.has_perm(
                PermissionCode.VALIDATION_RESULTS_VIEW_OWN.value,
                validation,
            )
            validation.curr_user_can_view = bool(can_view_all or can_view_own)
            validation.curr_user_can_delete = user.has_perm(
                PermissionCode.ADMIN_MANAGE_ORG.value,
                validation,
            )
        context.update(
            {
                "current_sort": self.request.GET.get("sort", "-created"),
                "status_filter": self.request.GET.get("status", ""),
                "status_choices": ValidationRunStatus.choices,
                "workflow_options": Workflow.objects.for_user(
                    self.request.user,
                ).filter(is_tombstoned=False),
                "query_string": self._get_base_query_string(),
                "page_size_options": self.page_size_options,
                "current_page_size": getattr(self, "page_size", self.paginate_by),
            },
        )
        return context

    def _get_base_query_string(self):
        params = self.request.GET.copy()
        params.pop("page", None)
        return params.urlencode()


class ValidationRunDetailView(ValidationRunAccessMixin, DetailView):
    template_name = "validations/validation_detail.html"
    context_object_name = "run"

    def get_queryset(self):
        return self.get_base_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        run: ValidationRun = context["run"]
        outputs_viewable = run.are_outputs_viewable
        step_runs_query = run.step_runs.select_related(
            "workflow_step",
            "workflow_step__validator",
        )
        if outputs_viewable:
            step_runs_query = step_runs_query.prefetch_related("findings")
        step_runs = list(step_runs_query)
        findings = list(run.findings.all()) if outputs_viewable else []

        # Build display step outputs and template params for each step run.
        from validibot.validations.services.step_output_display import (
            build_display_step_outputs,
        )
        from validibot.validations.services.step_output_display import (
            build_template_params_display,
        )

        step_outputs: dict[int, list] = {}
        step_params: dict[int, list] = {}
        step_template_warnings: dict[int, list] = {}
        for sr in step_runs if outputs_viewable else []:
            display_outputs = build_display_step_outputs(sr)
            if display_outputs:
                step_outputs[sr.pk] = display_outputs
            params = build_template_params_display(sr)
            if params:
                step_params[sr.pk] = params
            warnings = (sr.output or {}).get("template_warnings")
            if warnings:
                step_template_warnings[sr.pk] = warnings

        # Submission content for the "View" modal.
        # Retention policies are enforced here so the template never receives
        # content that should not be interactively displayed.
        submission_content = ""
        submission_content_can_be_viewed = False
        if run.submission:
            submission_content = run.submission.get_viewable_content()
            submission_content_can_be_viewed = bool(submission_content)

        # Flatten all displayed values for the "Workflow Outputs" summary.
        all_step_outputs = [
            output for outputs in step_outputs.values() for output in outputs
        ]

        # The evidence receipt remains downloadable after payload retention
        # hides or deletes ordinary outputs. Only a missing/failed manifest
        # removes the evidence actions from the run detail page.
        evidence_artifact = None
        try:
            artifact = run.evidence_artifact
        except Exception:
            artifact = None
        if artifact is not None:
            from validibot.validations.models import RunEvidenceArtifactAvailability

            if artifact.availability == RunEvidenceArtifactAvailability.GENERATED:
                evidence_artifact = artifact

        context.update(
            {
                "step_runs": step_runs,
                "all_findings": findings,
                "summary_record": getattr(run, "summary_record", None),
                "step_outputs": step_outputs,
                "has_step_outputs": bool(step_outputs),
                "all_step_outputs": all_step_outputs,
                "step_params": step_params,
                "step_template_warnings": step_template_warnings,
                "submission_content": submission_content,
                "submission_content_can_be_viewed": submission_content_can_be_viewed,
                "evidence_artifact": evidence_artifact,
                "artifact_items": (
                    build_artifact_display_items(
                        run=run,
                        request=self.request,
                    )
                    if outputs_viewable
                    else []
                ),
                "outputs_viewable": outputs_viewable,
            },
        )
        context.update(
            get_signed_credential_display_context(
                request=self.request,
                run=run,
            ),
        )
        # Stacked vs classic report layout — session-remembered, default stacked.
        context["report_layout"] = resolve_report_layout(self.request)
        return context

    def get_breadcrumbs(self):
        validation = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Validations"),
                "url": reverse_with_org(
                    "validations:validation_list", request=self.request
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Run #%(pk)s") % {"pk": validation.pk},
                "url": "",
            },
        )
        return breadcrumbs


class ValidationRunJsonView(ValidationRunAccessMixin, DetailView):
    """Renders the full DRF serializer output as pretty-printed JSON.

    Opens in a new browser tab with syntax-highlighted JSON, using the
    same ``ValidationRunSerializer`` that the REST API returns.
    """

    template_name = "validations/validation_json.html"
    context_object_name = "run"

    def get_queryset(self):
        return self.get_base_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from validibot.validations.serializers import ValidationRunSerializer

        serializer = ValidationRunSerializer(context["run"])
        context["json_data"] = serializer.data
        return context

    def get_breadcrumbs(self):
        """Build the standard breadcrumb trail for the run JSON page."""
        validation = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Validations"),
                "url": reverse_with_org(
                    "validations:validation_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Run #%(pk)s") % {"pk": validation.pk},
                "url": reverse_with_org(
                    "validations:validation_detail",
                    request=self.request,
                    kwargs={"pk": validation.pk},
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("JSON"),
                "url": "",
            },
        )
        return breadcrumbs


class ValidationRunDeleteView(ValidationRunAccessMixin, DeleteView):
    template_name = "validations/partials/validation_confirm_delete.html"

    def get_success_url(self):
        return reverse_with_org("validations:validation_list", request=self.request)

    def get_breadcrumbs(self):
        validation = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Validations"),
                "url": reverse_with_org(
                    "validations:validation_list", request=self.request
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Run #%(pk)s") % {"pk": validation.pk},
                "url": reverse_with_org(
                    "validations:validation_detail",
                    request=self.request,
                    kwargs={"pk": validation.pk},
                ),
            },
        )
        breadcrumbs.append({"name": _("Delete"), "url": ""})
        return breadcrumbs

    def post(self, request, *args, **kwargs):
        return self.delete(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        success_url = self.get_success_url()
        if not request.user.has_perm(
            PermissionCode.ADMIN_MANAGE_ORG.value,
            self.object,
        ):
            raise PermissionDenied(
                "You do not have permission to delete this validation run."
            )
        self.object.delete()
        messages.success(request, "Validation run removed.")
        if request.headers.get("HX-Request"):
            target = request.headers.get("HX-Target", "")
            response = HttpResponse("")
            response["HX-Trigger"] = "validationDeleted"
            if target.startswith("validation-row-"):
                return response
            response["HX-Redirect"] = success_url
            return response
        if request.method == "DELETE":
            return HttpResponse(status=204)
        return HttpResponseRedirect(success_url)


# Artifact Detail and Download
# ------------------------------------------------------------------------------


class ValidationRunArtifactMixin(ValidationRunAccessMixin):
    """Resolve artifacts through the same access boundary as their parent run."""

    artifact_url_kwarg = "artifact_pk"

    def get_queryset(self):
        return self.get_base_queryset()

    def get_artifact(self, run: ValidationRun) -> Artifact:
        """Return the requested artifact only when it belongs to ``run``."""

        if not run.are_outputs_viewable:
            raise Http404(_("Artifact bytes are no longer retained."))
        return get_object_or_404(
            Artifact.objects.select_related("workflow_step", "step_run"),
            pk=self.kwargs[self.artifact_url_kwarg],
            validation_run=run,
        )


class ArtifactDetailView(ValidationRunArtifactMixin, DetailView):
    """Show template-safe metadata for one artifact emitted by a run."""

    template_name = "validations/artifact_detail.html"
    context_object_name = "run"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        artifact = self.get_artifact(context["run"])
        context["artifact"] = artifact
        context["artifact_item"] = build_artifact_display_item(
            artifact=artifact,
            request=self.request,
        )
        return context

    def get_breadcrumbs(self):
        run = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Validations"),
                "url": reverse_with_org(
                    "validations:validation_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Run #%(pk)s") % {"pk": run.pk},
                "url": reverse_with_org(
                    "validations:validation_detail",
                    request=self.request,
                    kwargs={"pk": run.pk},
                ),
            },
        )
        breadcrumbs.append({"name": _("Generated file"), "url": ""})
        return breadcrumbs


class ArtifactDownloadView(ValidationRunArtifactMixin, DetailView):
    """Download one artifact when it has a directly supported byte source."""

    def get(self, request, *args, **kwargs):
        run = self.get_object()
        artifact = self.get_artifact(run)
        try:
            source = open_artifact_download(artifact)
        except ArtifactDownloadUnavailableError as exc:
            raise Http404(_("Artifact bytes are not available.")) from exc

        response = FileResponse(
            source.fileobj,
            as_attachment=True,
            filename=source.filename,
            content_type=source.content_type,
        )
        response["Cache-Control"] = "no-store"
        if source.sha256:
            response["X-Validibot-Artifact-Sha256"] = source.sha256
        return response


# Guest Validation Views
# ------------------------------------------------------------------------------


class GuestValidationRunListView(LoginRequiredMixin, ListView):
    """
    List validation runs for workflow guests.

    This view is for workflow guests (users with grants but no org memberships).
    It shows only the user's own validation runs across all workflows they have
    access to, with an organization filter.
    """

    template_name = "validations/guest_validation_list.html"
    context_object_name = "validations"
    paginate_by = 20
    page_size_options = (10, 50, 100)

    allowed_sorts = {
        "created": "created",
        "-created": "-created",
        "status": "status",
        "-status": "-status",
        "workflow": "workflow__name",
        "-workflow": "-workflow__name",
    }

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ValidationRun.objects.none()

        # Get workflows the guest has access to via grants
        from validibot.workflows.models import Workflow

        accessible_workflows = Workflow.objects.for_user(user)

        # Only show the user's own runs on those workflows
        qs = ValidationRun.objects.filter(
            user=user,
            workflow__in=accessible_workflows,
        ).select_related("workflow", "workflow__org", "submission", "org")

        # Apply filters
        status_filter = self.request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        workflow_filter = self.request.GET.get("workflow")
        if workflow_filter:
            qs = qs.filter(workflow_id=workflow_filter)

        org_filter = self.request.GET.get("org")
        if org_filter:
            qs = qs.filter(org_id=org_filter)

        ordering = self._get_ordering()
        return qs.order_by(ordering)

    def _get_ordering(self):
        sort = self.request.GET.get("sort", "-created")
        return self.allowed_sorts.get(sort, "-created")

    def get_paginate_by(self, queryset):
        per_page = self.request.GET.get("per_page")
        if per_page:
            try:
                per_page = int(per_page)
            except (TypeError, ValueError):
                per_page = None
            else:
                if per_page not in self.page_size_options:
                    per_page = None
        if per_page is None:
            per_page = self.paginate_by
        self.page_size = per_page
        return per_page

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Mark all runs as viewable since these are the user's own runs
        for validation in context.get("validations", []):
            validation.curr_user_can_view = True
            validation.curr_user_can_delete = False  # Guests can't delete

        # Get org options for filter - orgs the user has workflow grants for
        from validibot.users.models import Organization
        from validibot.workflows.models import Workflow

        accessible_workflows = Workflow.objects.for_user(user)
        org_ids = accessible_workflows.values_list("org_id", flat=True).distinct()
        org_options = Organization.objects.filter(id__in=org_ids)

        context.update(
            {
                "current_sort": self.request.GET.get("sort", "-created"),
                "status_filter": self.request.GET.get("status", ""),
                "status_choices": ValidationRunStatus.choices,
                "workflow_options": accessible_workflows,
                "org_options": org_options,
                "org_filter": self.request.GET.get("org", ""),
                "query_string": self._get_base_query_string(),
                "page_size_options": self.page_size_options,
                "current_page_size": getattr(self, "page_size", self.paginate_by),
            },
        )
        return context

    def _get_base_query_string(self):
        params = self.request.GET.copy()
        params.pop("page", None)
        return params.urlencode()


# Credential Download
# ------------------------------------------------------------------------------


class CredentialDownloadView(ValidationRunAccessMixin, DetailView):
    """Download the compact JWS credential for a validation run.

    Returns the ``application/vc+jwt`` compact JWS string as a
    downloadable file.  Requires the same access permissions as the
    run detail page.

    If no credential was issued for this run (no Pro, feature disabled,
    or the run didn't succeed), returns 404.
    """

    def get_queryset(self):
        return self.get_base_queryset()

    def get(self, request, *args, **kwargs):
        run = self.get_object()

        # Pro is required for issued credentials; gate on the installed
        # apps so we don't try to query an unregistered model.
        if apps.is_installed("validibot_pro"):
            from validibot_pro.credentials.models import IssuedCredential

            credential = IssuedCredential.objects.filter(workflow_run=run).first()
        else:
            credential = None

        if credential is None:
            raise Http404(_("No credential issued for this run."))

        resource_label = extract_signed_credential_resource_label(
            credential.payload_json,
        )
        download_name = build_signed_credential_download_filename(
            resource_label=resource_label,
            workflow_slug=run.workflow.slug if run.workflow else "",
            fallback_identifier=str(run.pk),
        )
        response = HttpResponse(
            credential.credential_jws,
            content_type="application/vc+jwt",
        )
        response["Content-Disposition"] = f'attachment; filename="{download_name}"'
        return response
