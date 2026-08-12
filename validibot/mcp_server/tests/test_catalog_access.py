"""Tests for the MCP catalog access logic after the 2026-06-27 refactor.

The MCP catalog is the surface authenticated agents use to discover and
run workflows on behalf of a user. After the access-control refactor a
workflow appears in the catalog iff ALL of these hold:

1. **Identity access** — ``Workflow.objects.for_user(user)`` resolves the
   user's reachable workflows via membership, creator, family-scoped
   grants, OrgGuestAccess, and the PRIVATE / ORG / ALL_USERS visibility
   tiers. This is the single source of truth for WHO can reach a
   workflow, so any future fix to an access path propagates to MCP.
2. **Workflow opts into MCP** — ``mcp_enabled=True``.
3. **Org permits MCP** — ``org.mcp_allowed=True`` (the org-level master
   switch / guardrail).

What is GONE relative to the old model:

* There is no separate "agent_access_enabled" member-gate. The org-level
  guardrail is now ``org.mcp_allowed``, and the per-workflow opt-in is
  ``mcp_enabled``.
* There is no cross-org "public discovery" branch. x402 paid-public
  access is a SEPARATE, anonymous, cloud-only surface
  (``/api/v1/agent/*``) — it does NOT surface workflows on the
  authenticated MCP catalog. ``mcp_enabled`` and ``x402_enabled`` are
  independent dials, so a workflow's x402 state has no bearing here.

Because the catalog is just ``for_user ∩ mcp_enabled ∩ org.mcp_allowed``,
the visibility tier is what controls WHO sees a given mcp-enabled
workflow: a grant / OrgGuestAccess / ORG membership / ALL_USERS tier each
brings a user into ``for_user`` for that workflow. Every access mode the
catalog advertises is therefore ``member_access`` (authenticated,
on-behalf-of-user) — there is no ``public_x402`` mode anymore.

Pinning these behaviours so the catalog stays in sync with ``for_user``.
"""

from __future__ import annotations

import pytest

from validibot.mcp_server.services import _accessible_workflows
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.constants import WorkflowVisibility
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _visible_pks(user):
    return set(
        _accessible_workflows(user=user).values_list(
            "pk",
            flat=True,
        ),
    )


class TestMCPCatalogFamilyScopedGrant:
    """A grant on any version in a family makes the latest version visible.

    The previous implementation joined through ``access_grants`` on
    the *current* row, so a grant pinned to v1 didn't surface the
    latest v2 row in the MCP catalog. Family scoping is implemented
    by ``WorkflowQuerySet.for_user``, and the catalog now delegates
    to that — these tests pin the resulting behaviour.

    The grant brings the guest into ``for_user``; the workflow must
    still be ``mcp_enabled`` in an ``mcp_allowed`` org to appear, which
    is the MCP opt-in the catalog requires of every row.
    """

    def test_grant_on_v1_surfaces_latest_version_in_catalog(self):
        """A guest with a v1 grant sees v2 in the MCP catalog."""

        # mcp_allowed so MCP-enabled workflows in this org can appear.
        org = OrganizationFactory(mcp_allowed=True)
        author = UserFactory(orgs=[org])
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        # v1 has a grant; v2 is the latest version of the same family.
        # Both opt into MCP so the catalog's mcp_enabled requirement is met.
        v1 = WorkflowFactory(
            org=org,
            user=author,
            slug="my-workflow",
            version="1",
            mcp_enabled=True,
        )
        v2 = WorkflowFactory(
            org=org,
            user=author,
            slug="my-workflow",
            version="2",
            mcp_enabled=True,
        )
        WorkflowAccessGrant.objects.create(
            workflow=v1,
            user=guest,
            is_active=True,
        )

        visible = _visible_pks(guest)
        # The latest version (v2) MUST be visible — that's the family-
        # scoped behaviour. The catalog filters to "latest per family"
        # so v1 itself wouldn't be in the result anyway.
        assert v2.pk in visible


class TestMCPCatalogOrgGuestAccess:
    """OrgGuestAccess gives the catalog visibility into all org workflows.

    OrgGuestAccess brings the guest into ``for_user`` for every workflow
    in the org. The MCP opt-in (``mcp_enabled`` + ``org.mcp_allowed``)
    still decides which of those reach the catalog.
    """

    def test_org_guest_access_makes_mcp_enabled_org_workflows_visible(self):
        org = OrganizationFactory(mcp_allowed=True)
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)

        wf1 = WorkflowFactory(org=org, mcp_enabled=True)
        wf2 = WorkflowFactory(org=org, mcp_enabled=True)

        visible = _visible_pks(guest)
        assert wf1.pk in visible
        assert wf2.pk in visible

    def test_workflow_not_mcp_enabled_is_excluded_even_with_guest_access(self):
        """OrgGuestAccess grants identity reach, but a workflow that has
        NOT opted into MCP (``mcp_enabled=False``) still stays out of the
        catalog.

        This pins that the per-workflow MCP opt-in is required for *every*
        catalog row regardless of how the user reached it — the catalog is
        ``for_user ∩ mcp_enabled ∩ org.mcp_allowed``, not just ``for_user``.
        """

        org = OrganizationFactory(mcp_allowed=True)
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)

        # Reachable via OrgGuestAccess, but the workflow itself is not
        # MCP-enabled, so it must not appear in the agent catalog.
        wf = WorkflowFactory(org=org, mcp_enabled=False)

        visible = _visible_pks(guest)
        assert wf.pk not in visible


class TestMCPCatalogOrgMcpGate:
    """The catalog requires both the workflow opt-in and the org guardrail.

    A workflow appears only when ``mcp_enabled=True`` AND its org has
    ``mcp_allowed=True``. These tests pin both halves of that AND.
    """

    def test_member_workflow_with_mcp_enabled_is_visible(self):
        org = OrganizationFactory(mcp_allowed=True)
        member = UserFactory(orgs=[org])
        # ``for_user`` requires the membership to hold a role that
        # grants ``WORKFLOW_VIEW``; the AUTHOR role does.
        grant_role(member, org, RoleCode.AUTHOR)
        # Default visibility is ORG, which surfaces to org members via
        # for_user; mcp_enabled opts the row into the catalog.
        wf = WorkflowFactory(org=org, mcp_enabled=True)

        visible = _visible_pks(member)
        assert wf.pk in visible

    def test_member_workflow_without_mcp_enabled_is_excluded(self):
        """An org member can reach an ORG-visible workflow via ``for_user``,
        but if the workflow has not opted into MCP it is excluded.

        ``mcp_enabled`` is the per-workflow agent opt-in — without it, the
        workflow is runnable in the web UI but not advertised to agents.
        """

        org = OrganizationFactory(mcp_allowed=True)
        member = UserFactory(orgs=[org])
        grant_role(member, org, RoleCode.AUTHOR)
        wf = WorkflowFactory(
            org=org,
            mcp_enabled=False,
            workflow_visibility=WorkflowVisibility.ORG,
        )

        visible = _visible_pks(member)
        assert wf.pk not in visible

    def test_mcp_enabled_workflow_excluded_when_org_mcp_disallowed(self):
        """The org-level guardrail is decisive: even an ``mcp_enabled``
        workflow stays out of the catalog when ``org.mcp_allowed`` is False.

        This is the master switch an operator uses to keep an org's
        workflows off all agent surfaces regardless of per-workflow flags.
        """

        org = OrganizationFactory(mcp_allowed=False)
        member = UserFactory(orgs=[org])
        grant_role(member, org, RoleCode.AUTHOR)
        wf = WorkflowFactory(org=org, mcp_enabled=True)

        visible = _visible_pks(member)
        assert wf.pk not in visible

    def test_member_with_grant_still_needs_mcp_opt_in(self):
        """A grant brings the user into ``for_user`` but does not bypass the
        MCP opt-in.

        Under the old model a grant overrode the agent master switch. Now
        the master switch is the org guardrail and the per-workflow opt-in
        is ``mcp_enabled``; a grant only affects identity reach. So a
        granted-but-not-mcp-enabled workflow is excluded, while the same
        workflow with ``mcp_enabled=True`` appears.
        """

        org = OrganizationFactory(mcp_allowed=True)
        user = UserFactory(orgs=[org])
        grant_role(user, org, RoleCode.AUTHOR)

        # Granted but NOT mcp-enabled → excluded.
        wf_off = WorkflowFactory(org=org, mcp_enabled=False)
        WorkflowAccessGrant.objects.create(
            workflow=wf_off,
            user=user,
            is_active=True,
        )

        # Granted AND mcp-enabled → included.
        wf_on = WorkflowFactory(org=org, mcp_enabled=True)
        WorkflowAccessGrant.objects.create(
            workflow=wf_on,
            user=user,
            is_active=True,
        )

        visible = _visible_pks(user)
        assert wf_off.pk not in visible
        assert wf_on.pk in visible


class TestMCPCatalogVisibilityTiers:
    """Visibility tiers decide WHO sees an ``mcp_enabled`` workflow.

    The catalog is ``for_user ∩ mcp_enabled ∩ org.mcp_allowed``. Because
    ``for_user`` already encodes the PRIVATE / ORG / ALL_USERS tiers, the
    visibility tier controls which users an mcp-enabled workflow reaches.
    """

    def test_all_users_visible_workflow_reaches_unrelated_user(self):
        """An ALL_USERS-visible, mcp-enabled workflow is in the catalog for
        any authenticated user.

        ALL_USERS is the new home of the old ``is_public=True`` behaviour:
        every authenticated user has identity reach via ``for_user``, so
        the only extra requirement for the catalog is the MCP opt-in.
        """

        author = UserFactory()
        org = author.get_current_org()
        org.mcp_allowed = True
        org.save(update_fields=["mcp_allowed"])

        public_wf = WorkflowFactory(
            org=org,
            user=author,
            workflow_visibility=WorkflowVisibility.ALL_USERS,
            mcp_enabled=True,
        )

        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        visible = _visible_pks(unrelated)
        assert public_wf.pk in visible

    def test_lowered_visibility_cap_hides_all_users_from_unrelated_user(self):
        """The MCP catalog follows effective visibility, not the stored enum."""
        author = UserFactory()
        org = author.get_current_org()
        org.mcp_allowed = True
        org.workflow_visibility_cap = WorkflowVisibility.ORG
        org.save(update_fields=["mcp_allowed", "workflow_visibility_cap"])

        public_wf = WorkflowFactory(
            org=org,
            user=author,
            workflow_visibility=WorkflowVisibility.ALL_USERS,
            mcp_enabled=True,
        )

        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        visible = _visible_pks(unrelated)
        assert public_wf.effective_visibility() == WorkflowVisibility.ORG
        assert public_wf.pk not in visible

    def test_private_workflow_hidden_from_unrelated_user(self):
        """A PRIVATE, mcp-enabled workflow is NOT in the catalog for a user
        with no membership/grant.

        PRIVATE restricts identity reach to the creator + explicit grants,
        so ``for_user`` excludes the unrelated user and the catalog follows
        — the MCP opt-in alone does not widen the audience.
        """

        author = UserFactory()
        org = author.get_current_org()
        org.mcp_allowed = True
        org.save(update_fields=["mcp_allowed"])

        private_wf = WorkflowFactory(
            org=org,
            user=author,
            workflow_visibility=WorkflowVisibility.PRIVATE,
            mcp_enabled=True,
        )

        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        visible = _visible_pks(unrelated)
        assert private_wf.pk not in visible

    def test_x402_published_workflow_not_on_mcp_catalog(self):
        """x402 paid-public state does NOT put a workflow on the MCP catalog.

        x402 is the separate, anonymous, cloud-only agent surface. A
        workflow that is ``x402_enabled`` but not ``mcp_enabled`` (and
        unreachable by the user via ``for_user``) must stay out of the
        authenticated MCP catalog — the two channels are independent.
        """
        from validibot.submissions.constants import SubmissionRetention
        from validibot.workflows.constants import AgentBillingMode

        author = UserFactory()
        org = author.get_current_org()
        # mcp_allowed on to prove it's the per-workflow mcp_enabled (off)
        # that keeps this row out, not the org guardrail.
        org.mcp_allowed = True
        org.save(update_fields=["mcp_allowed"])

        x402_wf = WorkflowFactory(
            org=org,
            user=author,
            workflow_visibility=WorkflowVisibility.PRIVATE,
            mcp_enabled=False,
            x402_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=100,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )

        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        visible = _visible_pks(unrelated)
        assert x402_wf.pk not in visible
