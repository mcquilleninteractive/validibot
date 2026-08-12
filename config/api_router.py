"""
Public API router (available on APP_ROLE=web).

These routes are the regular API surface used by clients to launch workflows,
manage users, etc. Internal-only endpoints (e.g., validator callbacks) live in
config/api_internal_router.py and are exposed only on the worker service.

URL structure:
    /api/v1/users/                               - User management (root-level)
    /api/v1/auth/me/                             - Auth verification (root-level)
    /api/v1/orgs/                                - List user's organizations
    /api/v1/orgs/<org_slug>/workflows/           - Org-scoped workflows
    /api/v1/orgs/<org_slug>/runs/                - Org-scoped validation runs
"""

from django.conf import settings
from django.urls import include
from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from validibot.core.api.auth_views import AuthMeView
from validibot.users.api.views import OrganizationViewSet
from validibot.users.api.views import UserViewSet
from validibot.validations.api_views import OrgScopedRunViewSet
from validibot.workflows.api_views import OrgScopedWorkflowViewSet
from validibot.workflows.api_views import WorkflowVersionViewSet

# Root-level router for user endpoints (not org-scoped)
root_router = DefaultRouter() if settings.DEBUG else SimpleRouter()
root_router.register("users", UserViewSet)
root_router.register("orgs", OrganizationViewSet, basename="orgs")

# Org-scoped router for workflows and runs
org_router = DefaultRouter() if settings.DEBUG else SimpleRouter()
org_router.register("workflows", OrgScopedWorkflowViewSet, basename="org-workflows")
org_router.register("runs", OrgScopedRunViewSet, basename="org-runs")

# Nested router for workflow versions
# URL: /orgs/<org_slug>/workflows/<workflow_slug>/versions/
version_router = DefaultRouter() if settings.DEBUG else SimpleRouter()
version_router.register(
    "versions",
    WorkflowVersionViewSet,
    basename="workflow-versions",
)

app_name = "api"
urlpatterns = [
    # Auth endpoint for token verification and user identification
    path("auth/me/", AuthMeView.as_view(), name="auth-me"),
    # Legacy compatibility surface retained for Cloud x402 agent adapters.
    # Embedded MCP does not call these routes; it uses application services.
    path("mcp/", include("validibot.mcp_api.urls", namespace="mcp")),
    # Root-level routes (users)
    *root_router.urls,
    # Org-scoped routes
    path(
        "orgs/<slug:org_slug>/",
        include(org_router.urls),
    ),
    # Nested workflow versions
    path(
        "orgs/<slug:org_slug>/workflows/<slug:workflow_slug>/",
        include(version_router.urls),
    ),
]
