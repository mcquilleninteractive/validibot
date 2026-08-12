"""Legacy adapter views for workflow discovery, detail, and runs.

These Community routes are retained for Cloud compatibility code that resolves
an authenticated user's workflows across organizations. The embedded MCP
endpoint bypasses this HTTP layer and calls typed application services.

Cloud's ``/api/v1/agent/*`` routes serve the separate anonymous x402 flow
and are not affected by these views.
"""

from __future__ import annotations

from http import HTTPStatus

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.db.models import QuerySet
from django.urls import reverse
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from validibot.mcp_api.authentication import MCPUserRouteAuthentication
from validibot.mcp_api.constants import MCPHelperErrorCode
from validibot.mcp_api.refs import build_member_run_ref
from validibot.mcp_api.refs import build_workflow_ref
from validibot.mcp_api.refs import parse_member_run_ref
from validibot.mcp_api.refs import parse_workflow_ref
from validibot.users.models import Membership
from validibot.validations.constants import ValidationRunSource
from validibot.validations.models import ValidationRun
from validibot.validations.serializers import ValidationRunSerializer
from validibot.validations.serializers import ValidationRunStartSerializer
from validibot.workflows.models import Workflow
from validibot.workflows.serializers import WorkflowFullSerializer
from validibot.workflows.version_utils import get_latest_workflow_ids
from validibot.workflows.views_helpers import resolve_project
from validibot.workflows.views_launch_helpers import LaunchValidationError
from validibot.workflows.views_launch_helpers import build_submission_from_api
from validibot.workflows.views_launch_helpers import launch_api_validation_run


class MCPHelperAPIException(APIException):
    """Return consistent error payloads for the MCP helper API.

    Wraps DRF's ``APIException`` so helper views can raise a single
    object and consistently return ``detail`` / ``code`` / ``errors``
    payloads matching the retained HTTP adapter contract.
    """

    status_code = HTTPStatus.BAD_REQUEST
    default_detail = "The MCP helper request could not be processed."
    default_code = MCPHelperErrorCode.INVALID_PARAMS

    def __init__(
        self,
        *,
        detail: str,
        code: str,
        status_code: int,
        errors: list[dict[str, object]] | None = None,
    ) -> None:
        self.status_code = int(status_code)
        payload: dict[str, object] = {
            "detail": detail,
            "code": code,
        }
        if errors:
            payload["errors"] = errors
        super().__init__(detail=payload, code=code)


def _raise_invalid_reference(field_name: str, exc: ValueError) -> None:
    """Raise a consistent invalid-parameter error for malformed refs."""

    raise MCPHelperAPIException(
        detail=f"{field_name} is invalid.",
        code=MCPHelperErrorCode.INVALID_PARAMS,
        status_code=HTTPStatus.BAD_REQUEST,
        errors=[{"field": field_name, "message": str(exc)}],
    ) from exc


def _raise_not_found(detail: str) -> None:
    """Raise a consistent not-found error for helper endpoints."""

    raise MCPHelperAPIException(
        detail=detail,
        code=MCPHelperErrorCode.NOT_FOUND,
        status_code=HTTPStatus.NOT_FOUND,
    )


def _raise_launch_validation_error(
    exc: LaunchValidationError,
    *,
    status_code: int,
) -> None:
    """Re-raise a workflow launch error using the helper API error contract.

    The user-facing detail is taken from the exception's structured
    ``payload["detail"]`` when present, falling back to a generic message.
    The exception's ``str()`` representation is intentionally NOT used as a
    fallback — that path could leak unredacted internal text if a future
    code site ever raises ``LaunchValidationError(<some raw error>)``
    without a curated payload.
    """

    payload = exc.payload if isinstance(exc.payload, dict) else {}
    detail = str(payload.get("detail") or "Workflow launch failed.")
    code = str(payload.get("code") or MCPHelperErrorCode.INVALID_PARAMS)
    raw_errors = payload.get("errors")
    errors = raw_errors if isinstance(raw_errors, list) else None
    raise MCPHelperAPIException(
        detail=detail,
        code=code,
        status_code=status_code,
        errors=errors,
    ) from exc


def _member_org_ids_for_user(user) -> set[int]:
    """Return the active organization memberships for an authenticated user."""

    return set(
        Membership.objects.filter(
            user=user,
            is_active=True,
        ).values_list("org_id", flat=True),
    )


def _latest_accessible_workflow_queryset(
    *,
    user,
    member_org_ids: set[int] | None = None,
) -> QuerySet[Workflow]:
    """Return the latest MCP-accessible workflows for ``user``.

    A workflow is MCP-accessible when ALL of these hold:

    1. **Identity access.** ``Workflow.objects.for_user`` resolves
       membership, creator, family-scoped grants, OrgGuestAccess, and
       the PRIVATE / ORG / ALL_USERS visibility tiers in ONE place — so
       a future fix to any access path propagates to MCP automatically.
    2. **Effective MCP access.** ``mcp_enabled=True`` AND
       ``org.mcp_allowed=True``. This is the SQL equivalent of
       ``Workflow.mcp_effective()``.

    x402 paid-public workflows are deliberately NOT in this catalog.
    Anonymous x402 discovery is a separate, cloud-only surface
    (``/api/v1/agent/*``). MCP here is always authenticated — acting on
    behalf of a user, billed to that user's plan quota. ``mcp_enabled``
    and ``x402_enabled`` are INDEPENDENT channels (the 2026-06-27
    access-control refactor decoupled them), so a workflow's x402
    publication state has no bearing on its MCP catalog visibility.

    The function returns the *latest* version of each matching family.

    ``member_org_ids`` is accepted for caller compatibility but is no
    longer used here — ``for_user`` computes membership internally.
    """

    base_filters = Q(
        is_active=True,
        is_archived=False,
        is_tombstoned=False,
    )

    # Identity access (for_user — already none() for anonymous) ∩ the
    # workflow's agent opt-in ∩ the org-level MCP guardrail. No more
    # cross-org "public discovery" branch: that path is x402-only and
    # lives on the anonymous cloud agent surface, not here.
    eligible = Workflow.objects.for_user(user).filter(
        base_filters,
        mcp_enabled=True,
        org__mcp_allowed=True,
    )

    latest_ids = set(get_latest_workflow_ids(eligible))
    return Workflow.objects.filter(pk__in=latest_ids).filter(base_filters).distinct()


def _prefetch_workflow_detail_relations(
    queryset: QuerySet[Workflow],
) -> QuerySet[Workflow]:
    """Attach the relations needed by ``WorkflowFullSerializer``."""

    return queryset.select_related("org").prefetch_related(
        "steps__validator__default_ruleset__assertions__target_io_definition",
        "steps__ruleset__assertions__target_io_definition",
    )


def _resolve_accessible_workflow(
    *,
    user,
    member_org_ids: set[int],
    workflow_ref: str,
) -> Workflow:
    """Resolve an MCP ``workflow_ref`` to the latest accessible workflow."""

    try:
        org_slug, workflow_slug = parse_workflow_ref(workflow_ref)
    except ValueError as exc:
        _raise_invalid_reference("workflow_ref", exc)

    workflow = _prefetch_workflow_detail_relations(
        _latest_accessible_workflow_queryset(
            user=user,
            member_org_ids=member_org_ids,
        ).filter(
            org__slug=org_slug,
            slug=workflow_slug,
        ),
    ).first()
    if workflow is None:
        _raise_not_found("Workflow not found for this MCP user.")
    return workflow


def _resolve_member_run(
    *,
    user,
    run_ref: str,
) -> ValidationRun:
    """Resolve a member ``run_ref`` through canonical run visibility."""

    try:
        org_slug, run_id = parse_member_run_ref(run_ref)
    except ValueError as exc:
        _raise_invalid_reference("run_ref", exc)

    validation_run = (
        ValidationRun.objects.for_user(user)
        .select_related("org", "workflow", "submission")
        .prefetch_related(
            "step_runs__findings",
            "step_runs__workflow_step",
        )
        .filter(
            pk=run_id,
            org__slug=org_slug,
        )
        .first()
    )
    if validation_run is None:
        _raise_not_found("Run not found for this MCP user.")
    return validation_run


class MCPWorkflowAccessSerializerMixin:
    """Serialize workflow access metadata shared by catalog and detail views."""

    def _get_member_org_ids(self) -> set[int]:
        """Return ``member_org_ids`` from serializer context or fail clearly."""

        member_org_ids = self.context.get("member_org_ids")
        if member_org_ids is None:
            msg = "MCP workflow serializers require member_org_ids in context."
            raise ImproperlyConfigured(msg)
        return member_org_ids

    def get_workflow_ref(self, obj: Workflow) -> str:
        """Expose the stable workflow reference used by the MCP contract."""

        return build_workflow_ref(obj)

    def get_access_modes(self, obj: Workflow) -> list[str]:
        """List how the current user can access this workflow via MCP.

        Every workflow in the MCP catalog is reachable via authenticated,
        on-behalf-of-user access (resolved by the catalog queryset). x402
        paid access is NOT exposed on the MCP surface — it is a separate
        anonymous cloud endpoint — so ``public_x402`` is no longer
        advertised here.
        """

        return ["member_access"]

    def get_preferred_access_mode(self, obj: Workflow) -> str:
        """The MCP surface only offers authenticated (member) access."""

        return "member_access"


class MCPWorkflowCatalogView(APIView):
    """Return the authenticated workflow catalog to a trusted adapter.

    The caller uses a trusted service-to-service channel after validating the
    end user's bearer credential. The
    response is the set of workflows the user has identity access to (any
    visibility tier) that are ``mcp_enabled`` and whose org has
    ``mcp_allowed`` on. x402 paid-public workflows are NOT included — they
    live on the separate anonymous cloud agent surface.

    Each workflow includes a stable ``workflow_ref`` plus compatibility
    fields like ``org_slug`` and ``slug`` so the current tool contract
    can continue to work while the MCP surface migrates away from
    org-first inputs.
    """

    authentication_classes = [MCPUserRouteAuthentication]
    permission_classes = [AllowAny]

    class WorkflowSerializer(
        MCPWorkflowAccessSerializerMixin,
        serializers.ModelSerializer,
    ):
        """Serialize the aggregated MCP workflow catalog."""

        workflow_ref = serializers.SerializerMethodField()
        org_slug = serializers.SlugRelatedField(
            source="org",
            slug_field="slug",
            read_only=True,
        )
        org_name = serializers.CharField(source="org.name", read_only=True)
        access_modes = serializers.SerializerMethodField()
        preferred_access_mode = serializers.SerializerMethodField()

        class Meta:
            model = Workflow
            fields = [
                "workflow_ref",
                "slug",
                "name",
                "description",
                "version",
                "org_slug",
                "org_name",
                "allowed_file_types",
                "agent_price_cents",
                "agent_billing_mode",
                "access_modes",
                "preferred_access_mode",
            ]
            read_only_fields = fields

    def get(self, request):
        """Return the combined catalog for the authenticated MCP user."""

        member_org_ids = _member_org_ids_for_user(request.user)
        workflows = (
            _latest_accessible_workflow_queryset(
                user=request.user,
                member_org_ids=member_org_ids,
            )
            .select_related("org")
            .order_by("org__name", "name", "slug")
        )

        serializer = self.WorkflowSerializer(
            workflows,
            many=True,
            context={"member_org_ids": member_org_ids},
        )
        return Response(serializer.data)


class MCPWorkflowDetailView(APIView):
    """Return authenticated workflow detail keyed by the opaque workflow ref.

    This view exists so the MCP server can resolve a workflow chosen
    from the aggregated catalog without asking the user for an org slug.
    The view accepts either member-access workflows from the caller's
    org memberships or public x402 workflows visible on the
    authenticated surface.
    """

    authentication_classes = [MCPUserRouteAuthentication]
    permission_classes = [AllowAny]

    class WorkflowDetailSerializer(
        MCPWorkflowAccessSerializerMixin,
        WorkflowFullSerializer,
    ):
        """Serialize workflow detail plus MCP access metadata."""

        workflow_ref = serializers.SerializerMethodField()
        org_slug = serializers.SlugRelatedField(
            source="org",
            slug_field="slug",
            read_only=True,
        )
        org_name = serializers.CharField(source="org.name", read_only=True)
        access_modes = serializers.SerializerMethodField()
        preferred_access_mode = serializers.SerializerMethodField()

        class Meta(WorkflowFullSerializer.Meta):
            fields = [
                "workflow_ref",
                *WorkflowFullSerializer.Meta.fields,
                "org_slug",
                "org_name",
                "access_modes",
                "preferred_access_mode",
            ]
            read_only_fields = fields

    def get(self, request, workflow_ref: str):
        """Return full workflow detail for the authenticated MCP user."""

        member_org_ids = _member_org_ids_for_user(request.user)
        workflow = _resolve_accessible_workflow(
            user=request.user,
            member_org_ids=member_org_ids,
            workflow_ref=workflow_ref,
        )

        serializer = self.WorkflowDetailSerializer(
            workflow,
            context={
                "member_org_ids": member_org_ids,
                "org_slug": workflow.org.slug,
                "request": request,
            },
        )
        return Response(serializer.data, status=HTTPStatus.OK)


class MCPWorkflowRunCreateView(APIView):
    """Create a member-access validation run keyed by ``workflow_ref``.

    This view is the authenticated counterpart to the anonymous x402
    run endpoint. The trusted adapter resolves the end user first, then calls
    this helper over a service channel so Django never
    has to accept the MCP OAuth token as a generic REST API credential.
    """

    authentication_classes = [MCPUserRouteAuthentication]
    permission_classes = [AllowAny]

    def post(self, request, workflow_ref: str):
        """Launch an authenticated MCP run for the current user.

        Authorization is fully decided by ``_resolve_accessible_workflow``
        (identity access ∩ ``mcp_enabled`` ∩ ``org.mcp_allowed``), so a
        workflow that resolves is one the user may launch via MCP. There
        is no separate member-org restriction now that ALL_USERS-visible
        ``mcp_enabled`` workflows are legitimately launchable by any
        authenticated user (billed to that user's plan quota).
        """

        workflow = _resolve_accessible_workflow(
            user=request.user,
            member_org_ids=_member_org_ids_for_user(request.user),
            workflow_ref=workflow_ref,
        )

        project = resolve_project(workflow=workflow, request=request)
        try:
            submission_build = build_submission_from_api(
                request=request,
                workflow=workflow,
                user=request.user,
                project=project,
                serializer_factory=ValidationRunStartSerializer,
                multipart_payload=lambda: request.data,
            )
        except LaunchValidationError as exc:
            status_code = exc.status_code
            if status_code == HTTPStatus.FORBIDDEN:
                status_code = HTTPStatus.NOT_FOUND
            _raise_launch_validation_error(exc, status_code=status_code)

        # Trust ADR 2026-05-03 review (P1 #4): source is derived
        # from the route, not from a caller-controlled header. This
        # view IS the MCP helper API entry point, so we pass MCP
        # explicitly. The previous implementation read
        # X-Validibot-Source from the request, which a regular API
        # caller could spoof to mint MCP-looking runs.
        response = launch_api_validation_run(
            request=request,
            workflow=workflow,
            submission_build=submission_build,
            source=ValidationRunSource.MCP,
        )
        if not isinstance(response.data, dict):
            return response

        run_id = str(response.data.get("id") or "").strip()
        if not run_id:
            return response

        run_ref = build_member_run_ref(
            org_slug=workflow.org.slug,
            run_id=run_id,
        )
        response.data.setdefault("run_id", run_id)
        response.data["run_ref"] = run_ref
        response.data.pop("url", None)
        response.data.pop("poll", None)
        # The mcp_api URLs are included from config.api_router under the
        # outer ``api`` namespace, so the fully qualified reverse name is
        # ``api:mcp:run-detail``.
        response["Location"] = request.build_absolute_uri(
            reverse("api:mcp:run-detail", kwargs={"run_ref": run_ref}),
        )
        return response


class MCPRunDetailView(APIView):
    """Return member-access run status keyed by the opaque ``run_ref``."""

    authentication_classes = [MCPUserRouteAuthentication]
    permission_classes = [AllowAny]

    class RunSerializer(ValidationRunSerializer):
        """Expose ValidationRun status plus the opaque MCP ``run_ref``."""

        run_ref = serializers.SerializerMethodField()

        def get_run_ref(self, obj: ValidationRun) -> str:
            """Return the opaque member run handle for this validation run."""

            return build_member_run_ref(
                org_slug=obj.org.slug,
                run_id=str(obj.pk),
            )

        class Meta(ValidationRunSerializer.Meta):
            fields = [
                "run_ref",
                *ValidationRunSerializer.Meta.fields,
            ]
            read_only_fields = fields

    def get(self, request, run_ref: str):
        """Return validation run detail for the authenticated MCP user."""

        validation_run = _resolve_member_run(
            user=request.user,
            run_ref=run_ref,
        )
        serializer = self.RunSerializer(
            validation_run,
            context={"request": request},
        )
        return Response(serializer.data, status=HTTPStatus.OK)
