from __future__ import annotations

import logging
import math
import uuid
from typing import TYPE_CHECKING

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.files.storage import storages
from django.core.validators import MinValueValidator
from django.db import models
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.actions.models import Action
from validibot.core.constants import InviteStatus
from validibot.core.mixins import FeaturedImageMixin
from validibot.core.utils import render_markdown_safe
from validibot.projects.models import Project
from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import Role
from validibot.users.models import User
from validibot.users.permissions import PermissionCode
from validibot.users.permissions import roles_for_permission
from validibot.workflows.constants import AgentBillingMode
from validibot.workflows.constants import WorkflowConstantType
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.constants import WorkflowVisibility
from validibot.workflows.constants import visibility_rank

if TYPE_CHECKING:
    from validibot.users.constants import RoleCode

logger = logging.getLogger(__name__)


def validate_workflow_version(value: int | str) -> None:
    """
    Validate that a workflow version is a positive integer.

    Workflow versions are ordering keys, not human release labels. Keeping
    them as plain positive integers avoids implying semantic-version
    compatibility rules that the product does not enforce. Historical
    migrations import this function by dotted path, so it remains module-level
    even though the current model field also enforces the rule with
    ``PositiveIntegerField`` and ``MinValueValidator``.

    A non-empty label is required: ``Workflow.version`` is part of the
    ``(org, slug, version)`` uniqueness constraint and feeds the version-
    arithmetic helpers, so a blank value would silently break both the
    family-uniqueness story and "latest version" resolution. Existing
    blank rows are backfilled to ``1`` (with collision-safe bumping)
    by migration ``0023_backfill_empty_workflow_versions`` and non-integer
    legacy labels are collapsed by migration ``0025``.
    """
    if value is None or value == "":
        raise ValidationError(
            _(
                "Workflow version is required. Use a positive integer (e.g., 1).",
            ),
        )

    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            _("Version must be a positive integer (e.g., 1)."),
        ) from exc

    if version < 1:
        raise ValidationError(_("Version must be a positive integer (e.g., 1)."))


def select_public_storage():
    """Return the explicitly public storage backend for public workflow media."""
    try:
        return storages["public"]
    except Exception:
        return storages["default"]


class WorkflowQuerySet(models.QuerySet):
    """
    A custom queryset for Workflow model to add user-specific filtering methods.
    This lets us easily get workflows a user has access to based on their membership
    to organizations or via workflow access grants (for guests).
    """

    def for_user(
        self,
        user: User,
        required_role_code: RoleCode | None = None,
    ) -> WorkflowQuerySet:
        """Return workflows accessible to ``user`` via any access path.

        Access is granted by ANY of these paths (unioned):

        1. **Membership**: an active ``Membership`` in the workflow's
           org with a role that grants ``WORKFLOW_VIEW`` (or the
           specific ``required_role_code``, if supplied).
        2. **Creator**: the workflow's ``user`` field matches.
        3. **Per-workflow grant**: an active ``WorkflowAccessGrant`` on
           any workflow row in the same family (matching ``(org_id,
           slug)``). The family-level join is what lets a guest's
           access carry across version bumps without manual re-granting.
        4. **Org-wide guest access**: an active ``OrgGuestAccess`` row
           for the workflow's org. Authorises every CURRENT and FUTURE
           workflow in that org with no per-workflow maintenance — the
           "100 workflows, 10 new a month" simplification.
        5. **Visibility tier** (``workflow_visibility``): ``ALL_USERS``
           is visible to every authenticated user; ``ORG`` additionally
           surfaces it to org members + org-wide guests (paths 1 & 4);
           ``PRIVATE`` restricts to the creator + explicit per-workflow
           grants only (paths 2 & 3). Capability checks
           (``required_role_code``) ignore this tiering — see below.

        If ``required_role_code`` is supplied, ONLY the membership path
        is used and it must match that specific role. The grant /
        OrgGuestAccess / public branches are intentionally excluded
        because role-specific queries are for org-member capability
        checks ("does this user have AUTHOR in this workflow's org?"),
        not for read-side narrowing ("can this user see this workflow
        at all?"). Mixing them would let a guest with a grant pass a
        check meant to gate org-member-only actions.
        """

        if not getattr(user, "is_authenticated", False):
            return self.none()

        # Org membership subquery
        allowed_view_roles = roles_for_permission(PermissionCode.WORKFLOW_VIEW)
        membership_subq = Membership.objects.filter(
            org=OuterRef("org_id"),
            user=user,
            is_active=True,
        )
        if required_role_code:
            membership_subq = membership_subq.filter(roles__code=required_role_code)
        else:
            membership_subq = membership_subq.filter(roles__code__in=allowed_view_roles)

        qs = self.annotate(
            _has_membership=Exists(membership_subq),
        )

        # Role-specific queries are CAPABILITY checks ("is this user an
        # AUTHOR in this workflow's org?"), not read-visibility. They are
        # deliberately NOT narrowed by ``workflow_visibility`` — an org
        # member's role-based capability does not depend on how widely
        # the workflow is shared. The creator always qualifies.
        if required_role_code:
            return qs.filter(
                Q(_has_membership=True) | Q(user_id=user.id),
            ).distinct()

        # Read/visibility path — tier by ``workflow_visibility``:
        #   • creator + explicit per-workflow grant  → ALL tiers (incl PRIVATE)
        #   • org membership + org-wide guest access → ORG and ALL_USERS only
        #   • ALL_USERS                              → any authenticated user
        # PRIVATE therefore surfaces a workflow only to its creator and
        # the people explicitly invited to it (grants) — NOT to every
        # org member, which is the new behaviour the old ``is_public``
        # boolean could not express.

        # Per-workflow grant: family-scoped to ``(org_id, slug)`` so any
        # active grant on any version of the workflow family makes every
        # version visible. Without the family expansion, cloning a
        # workflow to v2 would silently strip a guest's access until
        # someone re-granted manually.
        grant_subq = WorkflowAccessGrant.objects.filter(
            user=user,
            is_active=True,
            workflow__org_id=OuterRef("org_id"),
            workflow__slug=OuterRef("slug"),
        )
        qs = qs.annotate(_has_grant=Exists(grant_subq))

        # Org-wide guest access: one row authorises every workflow in the
        # org, current and future. The subquery joins back by ``org_id``
        # so the read-side picks up new workflows as they're added, with
        # no acceptance-time snapshot.
        org_guest_subq = OrgGuestAccess.objects.filter(
            user=user,
            is_active=True,
            org_id=OuterRef("org_id"),
        )
        qs = qs.annotate(_has_org_guest_access=Exists(org_guest_subq))

        # Effective visibility is stored visibility masked by the org
        # ceiling at READ time. In SQL that means:
        #   effective >= ORG       iff both stored value and cap are >= ORG
        #   effective == ALL_USERS iff both stored value and cap are ALL_USERS
        # This mirrors ``Workflow.effective_visibility()`` so lowering an
        # org cap immediately narrows existing rows without overwriting the
        # workflow's stored intent.
        org_or_wider = Q(
            workflow_visibility__in=[
                WorkflowVisibility.ORG,
                WorkflowVisibility.ALL_USERS,
            ],
            org__workflow_visibility_cap__in=[
                WorkflowVisibility.ORG,
                WorkflowVisibility.ALL_USERS,
            ],
        )
        all_users_effective = Q(
            workflow_visibility=WorkflowVisibility.ALL_USERS,
            org__workflow_visibility_cap=WorkflowVisibility.ALL_USERS,
        )
        access_filter = (
            Q(user_id=user.id)
            | Q(_has_grant=True)
            | (Q(_has_membership=True) & org_or_wider)
            | (Q(_has_org_guest_access=True) & org_or_wider)
            | all_users_effective
        )

        return qs.filter(access_filter).distinct()


class WorkflowManager(models.Manager):
    def get_queryset(self):
        return WorkflowQuerySet(self.model, using=self._db)

    def for_user(self, user: User, required_role_code: RoleCode | None = None):
        return self.get_queryset().for_user(user, required_role_code=required_role_code)


def _default_workflow_file_types() -> list[str]:
    return [SubmissionFileType.JSON]


class Workflow(FeaturedImageMixin, TimeStampedModel):
    """
    Reusable, versioned definition of a sequence of validation steps.

    A workflow normally remains editable and launchable until it is archived or
    disabled. The break-glass delete flow adds a stronger historical-record
    state, ``is_tombstoned``, which removes the workflow from normal product
    surfaces while keeping the row available for old runs and signed
    credentials.
    """

    objects = WorkflowManager()

    featured_image = models.FileField(
        null=True,
        blank=True,
        storage=select_public_storage,
        help_text=_(
            "Optional image to represent the workflow Shown on the 'info' page.",
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "slug",
                    "version",
                ],
                name="uq_workflow_org_slug_version",
            ),
            # ── Public-x402 publishing invariants (DB-level guards) ──
            #
            # ``Workflow.clean()`` enforces a set of trust-critical
            # invariants for the public-x402 publishing contract, but
            # ``clean()`` does not run on:
            #   • ``QuerySet.update()`` (admin bulk edits, data fixes)
            #   • Fixtures and ``loaddata``
            #   • Raw SQL writes
            # So a row can be persisted that satisfies
            # ``x402_enabled=True`` while violating the rest of the x402
            # publishing predicate. The defensive resolver filter
            # (``_public_x402_predicate``) hides such rows from the
            # catalog, but a row that exists in a contradictory state is
            # still a bug — the constraints below close that last gap by
            # enforcing each invariant at the database level for every
            # write path.
            #
            # Each constraint mirrors a clause in
            # ``_public_x402_predicate`` and the corresponding
            # ValidationError raised in ``clean()``.
            #
            # NOTE: x402 is INDEPENDENT of MCP — there is deliberately no
            # constraint coupling ``x402_enabled`` to ``mcp_enabled``. A
            # workflow may be private to its org for identity-scoped use
            # while still being paid-public to anonymous agents via x402.
            #
            # x402 requires a positive, NON-NULL price.  SQL CHECK
            # constraints treat ``NULL > 0`` as UNKNOWN (not FALSE),
            # so the original ``agent_price_cents__gt=0`` clause
            # silently passed for x402 rows with NULL prices.  The
            # explicit ``isnull=False`` closes that hole.
            models.CheckConstraint(
                condition=(
                    ~Q(agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402)
                    | (Q(agent_price_cents__isnull=False) & Q(agent_price_cents__gt=0))
                ),
                name="ck_workflow_x402_requires_positive_price",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402)
                    | (
                        Q(input_retention=SubmissionRetention.DO_NOT_STORE)
                        & Q(output_retention=OutputRetention.DO_NOT_STORE)
                    )
                ),
                name="ck_workflow_x402_requires_do_not_store_retention",
            ),
            # Three additional invariants enforced at the DB layer
            # for the same reason — ``clean()`` covers them but bulk
            # paths bypass it.
            #
            # ① x402 publish implies x402 billing.  A paid-public
            # workflow must accept x402 — that is the anonymous payment
            # surface.  Without this a row with ``x402_enabled=True`` and
            # ``agent_billing_mode=AUTHOR_PAYS`` could persist via
            # ``QuerySet.update`` (clean() would have rejected it,
            # but clean() doesn't run on bulk paths).
            models.CheckConstraint(
                condition=(
                    Q(x402_enabled=False)
                    | Q(agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402)
                ),
                name="ck_workflow_x402_enabled_requires_x402_billing",
            ),
            # ② x402 publish implies the row is alive (not archived, not
            # tombstoned).  An archived row that still carries
            # ``x402_enabled=True`` would be a contradiction the resolver
            # hides from the catalog but x402 run-creation could still
            # hit before the publish-invariants re-check (P1 #5) catches
            # it.  The legacy NULL handling (treating ``is_archived=NULL``
            # and ``is_tombstoned=NULL`` as "not archived" / "not
            # tombstoned") matches ``_public_x402_predicate``.
            models.CheckConstraint(
                condition=(
                    Q(x402_enabled=False)
                    | (
                        (Q(is_archived=False) | Q(is_archived__isnull=True))
                        & (Q(is_tombstoned=False) | Q(is_tombstoned__isnull=True))
                    )
                ),
                name="ck_workflow_x402_enabled_requires_alive_row",
            ),
        ]
        ordering = [
            "slug",
            "-version",
        ]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="workflows",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflows",
        help_text=_(
            "Default project to associate with runs triggered from this workflow.",
        ),
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="workflows",
        help_text=_("The user who created this workflow."),
    )

    name = models.CharField(
        max_length=200,
        blank=False,
        null=False,
        help_text=_("Name of the workflow, e.g. 'My Workflow'"),
    )

    description = models.TextField(
        blank=True,
        default="",
        max_length=5000,
        help_text=_(
            "Short description of what this workflow validates. "
            "Shown to authenticated users in the web UI, CLI, and API. "
            "For a longer public-facing description, use the workflow's "
            "public info page instead."
        ),
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique identifier for the workflow."),
    )

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_(
            "A unique identifier for the workflow, used in URLs. "
            "(Leave blank to auto-generate from name.)",
        ),
    )

    allow_submission_name = models.BooleanField(
        default=True,
        help_text=_(
            "Allow users to submit a custom name along with their data for validation.",
        ),
    )

    allow_submission_meta_data = models.BooleanField(
        default=False,
        help_text=_(
            "Allow users to submit meta-data along with their data for validation.",
        ),
    )

    allow_submission_short_description = models.BooleanField(
        default=False,
        help_text=_(
            "Allow users to submit a short description along with "
            "their data for validation.",
        ),
    )
    version = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text=_(
            "Positive integer version number. Every workflow row must carry "
            "a version so the family identity ``(org, slug, version)`` stays "
            "unique and 'latest version' resolution stays deterministic."
        ),
    )

    history_policy = models.CharField(
        max_length=16,
        choices=WorkflowHistoryPolicy.choices,
        default=WorkflowHistoryPolicy.VERSIONED,
        help_text=_(
            "Controls whether this workflow preserves historical run "
            "reproducibility by requiring new versions for semantic edits, "
            "or allows in-place changes after runs."
        ),
    )

    is_locked = models.BooleanField(
        default=False,
    )

    is_active = models.BooleanField(
        default=True,
        help_text=_("Inactive workflows stay visible but cannot run validations."),
    )
    is_archived = models.BooleanField(
        default=False,
        help_text=_(
            "Archived workflows are disabled and hidden unless explicitly shown."
        ),
    )
    is_tombstoned = models.BooleanField(
        default=False,
        help_text=_(
            "Tombstoned workflows are removed from normal product surfaces but "
            "kept for historical run and credential continuity."
        ),
    )
    tombstoned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the workflow entered the break-glass tombstone state."),
    )
    tombstoned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tombstoned_workflows",
        help_text=_("The user who initiated the break-glass tombstone flow."),
    )
    tombstone_reason = models.TextField(
        blank=True,
        default="",
        help_text=_("Human-entered reason recorded during break-glass delete."),
    )
    tombstone_workflow_definition_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("Workflow definition digest captured at the time of tombstoning."),
    )

    make_info_page_public = models.BooleanField(
        default=False,
        help_text=_(
            "Allows non-logged in users to see the workflow's info page.",
        ),
    )

    # ── Access: identity-scoped visibility (the WHO dial) ───────────────
    # Who, by Validibot identity, may run this workflow for FREE. Applies
    # uniformly to the web UI and to authenticated MCP agents (an agent
    # acts on behalf of its user). INDEPENDENT of x402 paid access below.
    # Capped by ``Organization.workflow_visibility_cap``.
    workflow_visibility = models.CharField(
        max_length=20,
        choices=WorkflowVisibility.choices,
        default=WorkflowVisibility.PRIVATE,
        help_text=_(
            "Who, by Validibot identity, may run this workflow for free: "
            "PRIVATE (you and people you invite — the default), ORG (your "
            "organization), or ALL_USERS (any Validibot user). Capped by the "
            "organization ceiling. Independent of x402 paid access.",
        ),
    )

    # ── Access: agent channels (the HOW dials) ──────────────────────────
    # ``mcp_enabled`` and ``x402_enabled`` are INDEPENDENT channels.
    # mcp_enabled = authenticated agents (on behalf of a user with
    # identity access above), billed to that user's plan quota.
    # x402_enabled = paid anonymous access to anyone on the internet who
    # pays, regardless of ``workflow_visibility``. They are dormant in the
    # community edition — the cloud layer / a self-hosted MCP server reads
    # them via the REST API.

    mcp_enabled = models.BooleanField(
        default=False,
        help_text=_(
            "Allow authenticated AI agents to run this workflow via MCP, on "
            "behalf of a user who already has identity access through "
            "'workflow_visibility'. Billed to that user's plan quota. "
            "Independent of x402 paid access.",
        ),
    )

    x402_enabled = models.BooleanField(
        default=False,
        help_text=_(
            "Publish this workflow for PAID, ANONYMOUS access via x402: "
            "anyone on the internet who pays the per-call price can run it, "
            "regardless of 'workflow_visibility'. Automatically bills via "
            "x402 and requires a price and 'Do not store' retention.",
        ),
    )

    agent_max_launches_per_hour = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Maximum agent launches per hour per wallet. "
            "Null means use the platform default.",
        ),
    )

    agent_billing_mode = models.CharField(
        max_length=30,
        choices=AgentBillingMode.choices,
        default=AgentBillingMode.AUTHOR_PAYS,
        help_text=_(
            "Who pays when an agent invokes this workflow. "
            "AUTHOR_PAYS uses your plan quota (authenticated agents only). "
            "AGENT_PAYS_X402 requires agents to pay per call via x402.",
        ),
    )

    agent_price_cents = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Price per agent invocation in US cents (USDC equivalent). "
            "Required when billing mode is AGENT_PAYS_X402.",
        ),
    )

    featured_image_alt_candidates = ("name",)

    def __str__(self) -> str:  # pragma: no cover - display helper
        version = f" v{self.version}" if self.version else ""
        return f"{self.name}{version}"

    allowed_file_types = ArrayField(
        base_field=models.CharField(
            max_length=32,
            choices=SubmissionFileType.choices,
        ),
        default=_default_workflow_file_types,
        help_text=_(
            "Logical file types (JSON, XML, text, etc.) this workflow can accept.",
        ),
    )

    # Input retention: how long to keep user-uploaded input files.
    # Mirrors ``output_retention`` below — both fields carry the
    # workflow author's retention choice for one of the two data
    # streams a run touches. Renamed from ``input_retention`` in
    # the trust ADR's Phase 4 closing housekeeping (the old name
    # was ambiguous: "data" could mean input or output).
    input_retention = models.CharField(
        max_length=32,
        choices=SubmissionRetention.choices,
        default=SubmissionRetention.DO_NOT_STORE,
        help_text=_(
            "How long to keep user-submitted input files after validation "
            "completes. DO_NOT_STORE deletes the submission immediately "
            "after any terminal outcome. The submission record is "
            "preserved for audit."
        ),
    )

    # Output retention: how long to keep validator results, artifacts, findings
    output_retention = models.CharField(
        max_length=32,
        choices=OutputRetention.choices,
        default=OutputRetention.DO_NOT_STORE,
        help_text=_(
            "How long to keep validation outputs (results, artifacts, findings) "
            "after validation reaches a terminal state. DO_NOT_STORE queues "
            "detailed output deletion immediately while preserving the minimal "
            "permanent evidence receipt and aggregate run record."
        ),
    )

    success_message = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Custom message displayed when validation succeeds. "
            "Leave blank for the default message."
        ),
    )

    # ── Input contract ───────────────────────────────────────────────────
    # Author-declared JSON Schema defining the expected submission shape.
    # When set on a JSON-only workflow, the launch form shows a structured
    # form as an additional input mode.

    input_schema = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "JSON Schema defining the expected submission shape. "
            "When set on a JSON-only workflow, the launch form shows "
            "a structured form as an additional input mode."
        ),
    )

    input_schema_source_mode = models.CharField(
        max_length=32,
        choices=[
            ("json_schema", "JSON Schema"),
            ("pydantic", "Pydantic"),
        ],
        blank=True,
        default="",
        help_text=_(
            "How the workflow author last edited the input contract. "
            "Authoring metadata only; not part of the runtime contract."
        ),
    )

    input_schema_source_text = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Original authoring text for the input contract. "
            "Used to repopulate the workflow settings form."
        ),
    )

    # Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": _("Name is required.")})
        # Every workflow must belong to a project. The column stays nullable for
        # historical rows and for SET_NULL on project deletion, but creating or
        # saving a workflow with no project is not allowed: runs default to the
        # workflow's project and project-scoped surfaces assume it is present.
        if not self.project_id:
            raise ValidationError(
                {"project": _("A workflow must belong to a project.")},
            )
        if self.project_id and self.org_id and self.project.org_id != self.org_id:
            raise ValidationError(
                {"project": _("Project must belong to the workflow's organization.")},
            )
        allowed = [value for value in (self.allowed_file_types or []) if value]
        if not allowed:
            raise ValidationError(
                {
                    "allowed_file_types": _(
                        "Select at least one submission file type.",
                    ),
                },
            )
        normalized: list[str] = []
        for value in allowed:
            if value not in SubmissionFileType.values:
                raise ValidationError(
                    {
                        "allowed_file_types": _(
                            "'%(value)s' is not a supported submission file type.",
                        )
                        % {"value": value},
                    },
                )
            if value not in normalized:
                normalized.append(value)
        self.allowed_file_types = normalized

        # Validate input_schema against the supported v1 subset so that
        # direct saves outside WorkflowForm cannot persist contracts the
        # runtime adapters would silently ignore.
        if self.input_schema:
            # Input contracts require the workflow to accept only JSON
            # submissions — other file types have no structured schema.
            if set(normalized) != {SubmissionFileType.JSON}:
                raise ValidationError(
                    {
                        "input_schema": _(
                            "Input contracts are only supported when the "
                            "sole allowed file type is JSON."
                        ),
                    },
                )

            from validibot.workflows.schema_authoring import validate_schema_subset

            try:
                validate_schema_subset(self.input_schema)
            except ValidationError as exc:
                raise ValidationError(
                    {"input_schema": exc.messages},
                ) from exc

        # ── Cascade: enabling x402 selects the x402 billing rail ──────
        # x402 and MCP are INDEPENDENT channels — there is deliberately
        # no cascade between ``mcp_enabled`` and ``x402_enabled``.
        # Enabling paid public access selects the x402 billing mode; the
        # price and retention guards below then apply.
        #
        # This cascade is INTENTIONALLY one-directional. ``agent_billing_mode``
        # is the source of truth for the billing rail and can be configured
        # independently of ``x402_enabled`` (price + retention staged BEFORE
        # publishing, then kept staged after un-publishing for easy
        # republish — see ``test_disabled_with_x402_billing_configured_is_
        # valid``). Disabling x402 must therefore NOT auto-clear the rail
        # here. To fully decommission the rail, set ``agent_billing_mode``
        # back to ``AUTHOR_PAYS`` explicitly via the edit form.
        if self.x402_enabled:
            self.agent_billing_mode = AgentBillingMode.AGENT_PAYS_X402

        # ── x402 billing requires a price ─────────────────────────────
        if (
            self.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402
            and not self.agent_price_cents
        ):
            raise ValidationError(
                {
                    "agent_price_cents": _(
                        "A price per invocation is required when agents pay "
                        "via x402 micropayments.",
                    ),
                },
            )

        # ── x402 billing requires DO_NOT_STORE data retention ──────────
        # x402 is anonymous per-call micropayment access for agents.
        # Storing agent submissions would undermine the privacy model
        # that x402 enables, so retention MUST be DO_NOT_STORE.  Enforced
        # on the model (not just the form) so the API and admin can't
        # bypass it.
        if (
            self.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402
            and self.input_retention != SubmissionRetention.DO_NOT_STORE
        ):
            raise ValidationError(
                {
                    "input_retention": _(
                        "Data retention must be 'Do not store' when agents "
                        "pay via x402 micropayments — x402 is anonymous "
                        "per-call access and storing submissions is "
                        "incompatible with its privacy model.",
                    ),
                },
            )
        if (
            self.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402
            and self.output_retention != OutputRetention.DO_NOT_STORE
        ):
            raise ValidationError(
                {
                    "output_retention": _(
                        "Output retention must be 'Do not retain' when agents "
                        "pay via x402 micropayments — anonymous per-call access "
                        "must not leave detailed results behind.",
                    ),
                },
            )

    def save(self, *args, **kwargs):
        # Auto-generate slug BEFORE validation so uniqueness checks work correctly
        if not self.slug:
            candidate = slugify(self.name) if self.name else ""
            if not candidate:
                # Fallback for names that don't slugify (e.g., only punctuation)
                candidate = f"wf-{uuid.uuid4().hex[:8]}"
            self.slug = candidate

        # ALL_USERS-visible workflows surface their info page publicly too
        if (
            self.workflow_visibility == WorkflowVisibility.ALL_USERS
            and not self.make_info_page_public
        ):
            self.make_info_page_public = True

        self.full_clean()
        super().save(*args, **kwargs)

    def effective_visibility(self) -> str:
        """Return stored visibility masked by the organization ceiling.

        The org cap is a runtime mask, not a write-time rewrite. A workflow
        can remember that its author intended ``ALL_USERS`` while the org
        temporarily caps access at ``ORG`` or ``PRIVATE``. Every read path
        must use this method, or the SQL-equivalent predicate in
        ``WorkflowQuerySet.for_user``.
        """
        visibility = self.workflow_visibility or WorkflowVisibility.PRIVATE
        org = getattr(self, "org", None)
        cap = (
            getattr(org, "workflow_visibility_cap", None)
            or WorkflowVisibility.ALL_USERS
        )
        try:
            visibility_rank_value = visibility_rank(visibility)
            cap_rank_value = visibility_rank(cap)
        except (TypeError, ValueError):
            return WorkflowVisibility.PRIVATE
        if visibility_rank_value <= cap_rank_value:
            return visibility
        return cap

    def mcp_effective(self) -> bool:
        """Return whether authenticated MCP access is currently effective."""
        org = getattr(self, "org", None)
        return bool(self.mcp_enabled and getattr(org, "mcp_allowed", False))

    def x402_effective(self) -> bool:
        """Return whether paid anonymous x402 access is currently effective."""
        org = getattr(self, "org", None)
        return bool(self.x402_enabled and getattr(org, "x402_allowed", False))

    def enable_x402(self, *, price_cents: int | None = None) -> None:
        """Guard the dangerous transition into paid anonymous access.

        Enabling x402 has prerequisites: x402 billing, a positive price, and
        ``DO_NOT_STORE`` input retention. This method applies the billing
        cascade and validates the full model before callers save, restoring
        the prior in-memory values if validation fails so a re-rendered form
        does not display a state that never persisted.
        """
        previous = {
            "x402_enabled": self.x402_enabled,
            "agent_billing_mode": self.agent_billing_mode,
            "agent_price_cents": self.agent_price_cents,
        }
        self.x402_enabled = True
        self.agent_billing_mode = AgentBillingMode.AGENT_PAYS_X402
        if price_cents is not None:
            self.agent_price_cents = price_cents
        try:
            self.full_clean()
        except ValidationError:
            self.x402_enabled = previous["x402_enabled"]
            self.agent_billing_mode = previous["agent_billing_mode"]
            self.agent_price_cents = previous["agent_price_cents"]
            raise

    def can_view(self, *, user: User) -> bool:
        """
        Check if the given user can view this workflow.

        Access is granted if either:
        - User has WORKFLOW_VIEW permission in the workflow's org (org member), OR
        - User has an active WorkflowAccessGrant for ANY version of this
          workflow's family (guest, family-grant expansion)
        """
        if not user or not user.is_authenticated:
            return False

        effective_visibility = self.effective_visibility()

        # ALL_USERS effective visibility: any authenticated user can view.
        if effective_visibility == WorkflowVisibility.ALL_USERS:
            return True

        # The creator can always view.
        if self.user_id == user.id:
            return True

        # Org admins/owners administer every workflow in their org,
        # PRIVATE ones included (ADR 2026-06-27, "admins administer
        # everything"). This keeps ``can_view`` consistent with the admin
        # row-access branch in
        # ``WorkflowAccessMixin.get_workflow_queryset_for_access``: both
        # must agree, or an admin could open a detail page that
        # ``can_view`` would then deny. ``has_perm`` is object-scoped — it
        # derives the org from the workflow, so this grants nothing in
        # orgs the user does not administer.
        if user.has_perm(PermissionCode.ADMIN_MANAGE_ORG.value, self):
            return True

        # Org member with VIEW — only when visibility is ORG, not
        # PRIVATE. A PRIVATE workflow is visible solely to its creator
        # and the people explicitly invited to it (grants, below).
        if effective_visibility == WorkflowVisibility.ORG and user.has_perm(
            PermissionCode.WORKFLOW_VIEW.value, self
        ):
            return True

        # Guest grant check.
        #
        # A grant on ANY version of this workflow's family (same
        # ``(org_id, slug)`` pair) authorises view of every version,
        # matching the visibility resolver's semantics in
        # :meth:`WorkflowQuerySet.for_user`.
        #
        # Without this expansion, ``Workflow.objects.for_user(user)``
        # surfaces v2 in the catalog (correct), then ``can_view()``
        # rejects v2 on the detail page when only v1 has a grant —
        # because the FK reverse manager ``self.access_grants`` matches
        # the exact row only. Visibility and per-row checks must agree.
        return WorkflowAccessGrant.objects.filter(
            user=user,
            is_active=True,
            workflow__org_id=self.org_id,
            workflow__slug=self.slug,
        ).exists()

    def can_delete(self, *, user: User) -> bool:
        """
        Check if the given user can delete this workflow.
        Requires the ``workflow_edit`` permission in the workflow's org.
        """
        if not user or not user.is_authenticated:
            return False

        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, self)

    def can_execute(self, *, user: User) -> bool:
        """
        Check if the given user can execute (run) this workflow.

        Access is granted if any of these conditions are met:
        - Workflow is public (any authenticated user)
        - User has WORKFLOW_LAUNCH permission in the workflow's org (org member)
        - User has an active WorkflowAccessGrant for ANY version of this
          workflow's family (guest, family-grant expansion)

        The workflow must also be active for execution to be allowed.
        """
        if not self.is_active or self.is_tombstoned:
            return False
        if not user or not user.is_authenticated:
            return False

        effective_visibility = self.effective_visibility()

        # ALL_USERS effective visibility: any authenticated user can execute.
        if effective_visibility == WorkflowVisibility.ALL_USERS:
            return True

        # The creator can always execute.
        if self.user_id == user.id:
            return True

        # Org member with LAUNCH — only when visibility is ORG, not
        # PRIVATE (PRIVATE = creator + explicitly-invited grants only).
        if effective_visibility == WorkflowVisibility.ORG and user.has_perm(
            PermissionCode.WORKFLOW_LAUNCH.value, self
        ):
            return True

        # Guest grant check — family-scoped.
        #
        # A grant on ANY version of this workflow's family (same
        # ``(org_id, slug)`` pair) authorises execution of every
        # version, matching the visibility resolver's semantics in
        # :meth:`WorkflowQuerySet.for_user`.
        #
        # Without this expansion: a guest granted v1 sees v2 in their
        # catalog (the queryset expands by family), clicks Launch on v2,
        # and gets "permission denied" here — because ``self.access_grants``
        # only matches the exact row. ``can_view`` carries the same
        # rule for symmetry; tightening one without the other would
        # just shift the bug from "deny on launch" to "show in catalog
        # then 404 on click".
        return WorkflowAccessGrant.objects.filter(
            user=user,
            is_active=True,
            workflow__org_id=self.org_id,
            workflow__slug=self.slug,
        ).exists()

    def can_edit(self, *, user: User) -> bool:
        """
        Check if the given user can edit this workflow.
        Requires the ``workflow_edit`` permission in the workflow's organization.
        """
        if self.is_tombstoned or not user or not user.is_authenticated:
            return False

        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, self)

    @transaction.atomic
    def tombstone(
        self,
        *,
        deleted_by: User | None,
        reason: str,
        workflow_definition_hash: str = "",
    ) -> None:
        """Place the workflow into the break-glass tombstone state.

        Tombstoning is intentionally stronger than ordinary archiving. It
        removes the workflow from normal listing, launch, sharing, and public
        surfaces while preserving the row so historical runs and issued
        credentials still have a stable target.
        """
        now = timezone.now()
        cleaned_reason = (reason or "").strip()
        self.is_tombstoned = True
        self.is_archived = True
        self.is_active = False
        self.workflow_visibility = WorkflowVisibility.PRIVATE
        self.make_info_page_public = False
        self.mcp_enabled = False
        self.x402_enabled = False
        self.tombstoned_at = now
        self.tombstoned_by = deleted_by
        self.tombstone_reason = cleaned_reason
        self.tombstone_workflow_definition_hash = workflow_definition_hash or ""
        self.save(
            update_fields=[
                "is_tombstoned",
                "is_archived",
                "is_active",
                "workflow_visibility",
                "make_info_page_public",
                "mcp_enabled",
                "x402_enabled",
                "tombstoned_at",
                "tombstoned_by",
                "tombstone_reason",
                "tombstone_workflow_definition_hash",
                "modified",
            ],
        )
        self.access_grants.filter(is_active=True).update(
            is_active=False,
            modified=now,
        )
        self.invites.filter(status=InviteStatus.PENDING).update(
            status=InviteStatus.CANCELED,
            modified=now,
        )

    @property
    def workflow_type(self) -> str:
        """
        Classification for metering: BASIC or ADVANCED.

        A workflow is ADVANCED if any of its validator steps uses a
        HIGH-compute validator. Used by the cloud billing system to
        determine whether a run consumes basic launch quota or compute
        credits.
        """
        from validibot.validations.constants import ComputeTier

        has_high_compute = self.steps.filter(
            validator__compute_tier=ComputeTier.HIGH,
        ).exists()
        return "ADVANCED" if has_high_compute else "BASIC"

    def allowed_file_type_labels(self) -> list[str]:
        labels: list[str] = []
        for value in self.allowed_file_types or []:
            try:
                labels.append(str(SubmissionFileType(value).label))
            except Exception:
                labels.append(str(value))
        return labels

    def supports_file_type(self, file_type: str) -> bool:
        normalized = (file_type or "").lower()
        return normalized in {ft.lower() for ft in (self.allowed_file_types or [])}

    def first_unavailable_validator_step(self):
        """Return the first step whose validator cannot run in this process."""
        from validibot.validations.validators.base.config import get_validator_class

        steps = self.steps.select_related("validator").all()
        for step in steps:
            validator = step.validator
            if validator is None:
                continue
            if not validator.is_runtime_available:
                return step
            try:
                get_validator_class(validator.validation_type)
            except KeyError:
                return step
        return None

    def has_runs(self) -> bool:
        """Return True if any validation run targets this workflow.

        A workflow with runs is "in use" — its launch contract is
        the rules that those runs ran under, and any contract edit
        from this point should produce a new version rather than
        silently mutating in place.

        See :meth:`requires_new_version_for_contract_edits`.
        """
        return self.validation_runs.exists()

    def editing_policy_is_fixed(self) -> bool:
        """Return whether ordinary authors may still change editing policy."""

        from validibot.workflows.services.editing_policy import editing_policy_is_fixed

        return editing_policy_is_fixed(self)

    def has_unreleased_definition_users(self) -> bool:
        """Return whether any run may still read this live definition."""

        return self.validation_runs.filter(
            definition_released_at__isnull=True,
        ).exists()

    def requires_new_version_for_contract_edits(self) -> bool:
        """Return True if contract-field edits require a new version.

        Contract fields are listed in
        :data:`validibot.workflows.services.versioning.CONTRACT_FIELDS`
        — they're the fields that change *what* a workflow does
        when launched (allowed file types, retention, agent
        publication state, etc.). Non-contract fields (name,
        description, lifecycle flags) can always be edited in place.

        Versioned-history workflows reject unsafe contract edits once
        locked OR once runs exist, and the operator should be directed
        to clone the workflow to a new version using
        :meth:`WorkflowVersioningService.clone`. Mutable-history workflows
        opt out of this reproducibility gate.
        """
        if self.history_policy == WorkflowHistoryPolicy.MUTABLE:
            return False
        return self.is_locked or self.has_runs()

    def changed_contract_fields(self, proposed: dict) -> set[str]:
        """Return contract-field names whose proposed value differs from current.

        Pure helper for forms/serializers/scripts that need to detect
        in-place contract edits before deciding whether to allow them.

        The comparison is done against the *current* in-memory values
        on this instance — callers that want to compare against the
        database row should refresh first (``self.refresh_from_db()``).
        Forms get this for free because Django's ``ModelForm.clean()``
        runs before ``_post_clean()`` merges ``cleaned_data`` into the
        instance, so at that point ``self.instance`` still carries the
        DB values.

        Only fields *present* in ``proposed`` are considered — a caller
        editing a subset of contract fields (e.g. only ``input_retention``)
        does not need to pass every contract field. ``ArrayField`` values
        like ``allowed_file_types`` are compared as sets so re-ordering
        without a real change isn't flagged.

        Args:
            proposed: A dict of ``{field_name: proposed_value}``.
                Typically this is ``cleaned_data`` from a ModelForm or
                ``serializer.validated_data`` from a DRF serializer.

        Returns:
            The subset of contract field names whose proposed value is
            different from the current value. Empty set if everything
            matches (or if no contract fields appear in ``proposed``).
        """
        # Local import: services.versioning imports models, models can't
        # import services at module-load time without circularity.
        from validibot.workflows.services.versioning import CONTRACT_FIELDS

        changed: set[str] = set()
        for field_name in CONTRACT_FIELDS:
            if field_name not in proposed:
                continue
            current = getattr(self, field_name, None)
            new_value = proposed[field_name]
            # Order-insensitive comparison for list-shaped fields like
            # allowed_file_types — reordering checkboxes shouldn't count
            # as a contract change.
            if isinstance(current, list) and isinstance(new_value, (list, tuple)):
                if set(current) != set(new_value):
                    changed.add(field_name)
            elif current != new_value:
                changed.add(field_name)
        return changed

    def unsafely_changed_contract_fields(self, proposed: dict) -> set[str]:
        """Return contract-field names whose change would invalidate past runs.

        Layers safety semantics on top of :meth:`changed_contract_fields`.
        A change is "unsafe" only when it narrows the execution contract or
        expands the workflow's published privacy scope:

        - ``allowed_file_types``: removing a previously-allowed type is
          unsafe; adding a new type is safe in place.
        - ``input_retention`` / ``output_retention``: shortening retention
          is privacy-safe in place; extending it requires a new version.

        Callers (form gates, API serializers) use this to allow safe
        edits without forcing a workflow clone. The blanket
        :meth:`changed_contract_fields` remains for callers that want
        the raw drift answer regardless of direction (e.g. the cloning
        service that mirrors every change into the new version).

        Returns the subset of contract field names whose proposed value
        would unsafely change the contract. Empty set if every change is
        either safe in place or absent.
        """
        from validibot.workflows.services.versioning import CONTRACT_FIELD_SAFETY

        changed = self.changed_contract_fields(proposed)
        unsafe: set[str] = set()
        for field_name in changed:
            safety_check = CONTRACT_FIELD_SAFETY.get(field_name)
            if safety_check is None:
                # A contract field with no safety classifier is treated
                # as always unsafe — the default protects the integrity
                # story when new contract fields are added without
                # explicit per-direction reasoning.
                unsafe.add(field_name)
                continue
            current = getattr(self, field_name, None)
            new_value = proposed[field_name]
            if not safety_check(current, new_value):
                unsafe.add(field_name)
        return unsafe

    @transaction.atomic
    def clone_to_new_version(self, user) -> Workflow:
        """
        Create an identical workflow with version+1 and copied steps.
        Locks old version.

        Phase 3 of ADR-2026-04-27: this method now delegates to
        :class:`validibot.workflows.services.versioning.WorkflowVersioningService`
        so the clone copies the **complete** workflow contract
        (steps + step resources + public info + role access + signal
        mappings), not just steps and resources. The service returns
        a structured :class:`CloneReport` that callers can inspect;
        for backward compatibility this method continues to return
        just the new ``Workflow`` row.
        """
        # Local import to avoid circular dependency at module load.
        from validibot.workflows.services.versioning import WorkflowVersioningService

        report = WorkflowVersioningService.clone(self, user=user)
        return Workflow.objects.get(pk=report.new_workflow_id)

    def _determine_next_version_label(self, versions) -> int:
        """
        Return the next positive integer version for the cloned workflow.

        Existing rows should already carry integers, but the helper remains
        defensive so clone creation can still recover cleanly if legacy
        fixture data reaches it before migrations normalize the database.
        """
        max_version = 0
        for raw in versions:
            if raw is None:
                continue
            try:
                candidate = int(raw)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                max_version = max(max_version, candidate)

        return max_version + 1

    @property
    def has_signed_credential_action(self) -> bool:
        """Whether any step in this workflow issues a signed credential."""
        from validibot.actions.constants import CredentialActionType

        return self.steps.filter(
            action__definition__type=CredentialActionType.SIGNED_CREDENTIAL,
        ).exists()

    @property
    def is_advanced(self) -> bool:
        """
        Check if this workflow uses any advanced (high-compute) validators.
        Returns:
            True if any step uses an advanced validator type.
        """
        from validibot.validations.constants import ADVANCED_VALIDATION_TYPES

        return self.steps.filter(
            validator__validation_type__in=ADVANCED_VALIDATION_TYPES,
        ).exists()

    @property
    def get_public_info(self) -> WorkflowPublicInfo:
        public_info, _ = WorkflowPublicInfo.objects.get_or_create(workflow=self)
        return public_info

    def who_can_run_summary(self) -> str:
        """Return a short, human-readable description of who may run this.

        Combines the two independent access ideas into one sentence for
        the sharing UI's "Who can run this?" line:

        - ``workflow_visibility`` answers WHO, by Validibot identity, may
          run the workflow for free (PRIVATE / ORG / ALL_USERS).
        - ``x402_enabled`` is the INDEPENDENT paid-anonymous channel: when
          on, anyone on the internet who pays the per-call price may run
          it too, regardless of the visibility tier. We append that as a
          separate clause so the reader sees it is additive, not a
          replacement for the identity audience.

        This is a read-only display helper — it never changes state and
        carries no authorization weight (the real gates live on
        ``can_execute`` / the resolvers).
        """
        summaries = {
            WorkflowVisibility.PRIVATE: _("Private — only you and people you invite"),
            WorkflowVisibility.ORG: _("Your organization"),
            WorkflowVisibility.ALL_USERS: _("All Validibot users"),
        }
        # Fall back to the most restrictive description for any unexpected
        # value so the line never renders blank on a malformed row.
        base = summaries.get(
            self.effective_visibility(),
            summaries[WorkflowVisibility.PRIVATE],
        )
        if self.x402_effective():
            base = str(base) + str(_(" · plus anyone who pays via x402"))
        return str(base)


class WorkflowPublicInfo(TimeStampedModel):
    workflow = models.OneToOneField(
        Workflow,
        on_delete=models.CASCADE,
        related_name="public_info",
    )
    title = models.CharField(
        max_length=200,
        default="",
        help_text=_(
            "Optional title to show on the public info page. "
            "If blank, the Workflow name will be used.",
        ),
    )
    content_md = models.TextField()  # user-authored Markdown
    content_html = models.TextField(editable=False)  # cached sanitized HTML

    show_steps = models.BooleanField(
        default=True,
        help_text=_("Whether to show the workflow steps on the public info page."),
    )

    def __str__(self):
        return f"Public info for {self.workflow}"

    def save(self, *args, **kwargs):
        self.compile_content()
        super().save(*args, **kwargs)

    def compile_content(self):
        try:
            self.content_html = render_markdown_safe(self.content_md)
        except Exception:
            logger.exception("Error rendering markdown for workflow public info")
            self.content_html = ""

    def get_title(self) -> str:
        if self.title and self.title.strip():
            return self.title.strip()
        return self.workflow.name

    def get_html_content(self) -> str:
        return self.content_html or ""


class WorkflowStep(TimeStampedModel):
    """
    Ordered unit of work within a workflow.

    Each step is either a validator execution or an action (never both). Validator
    steps may optionally link a `Ruleset` to override the validator's default
    assertions; action steps skip rulesets and instead reference a concrete
    `Action` subclass (Slack message, signed credential, etc.) that performs a
    side effect. `config` stores per-step JSON tweaks consumed by the validator or
    action at runtime such as severity thresholds or templated text.
    """

    class Meta:
        unique_together = [
            (
                "workflow",
                "order",
            ),
        ]
        ordering = ["order"]
        constraints = [
            models.CheckConstraint(
                name="workflowstep_validator_xor_action",
                condition=(
                    Q(validator__isnull=False, action__isnull=True)
                    | Q(validator__isnull=True, action__isnull=False)
                ),
            ),
            # step_key is the stable namespace for cross-step value
            # references in CEL and APIs. Must be unique within a workflow.
            models.UniqueConstraint(
                fields=["workflow", "step_key"],
                condition=~Q(step_key=""),
                name="uq_workflowstep_workflow_step_key",
            ),
        ]

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="steps",
    )

    order = models.PositiveIntegerField()  # 10,20,30... leave gaps for inserts

    name = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )

    step_key = models.SlugField(
        max_length=100,
        blank=True,
        default="",
        help_text=_(
            "Stable identifier for this step within the workflow. "
            "Used to reference output data from this step in "
            "downstream assertions (e.g., "
            "steps.simulation.output.site_eui_kwh_m2). "
            "Auto-generated from the step name on creation. "
            "Immutable once set."
        ),
    )

    description = models.CharField(
        max_length=2000,
        blank=True,
        default="",
        help_text=_("Brief description to help users understand what this step does."),
    )
    notes = models.CharField(
        max_length=2000,
        blank=True,
        default="",
        help_text=_(
            "Author notes about this step (visible only by you and other users "
            "with author permissions for this workflow).",
        ),
    )
    display_schema = models.BooleanField(
        default=False,
        help_text=_("Allow launchers to view this schema in public workflow pages."),
    )
    show_success_messages = models.BooleanField(
        default=False,
        help_text=_(
            "When enabled, assertions display their success message when they pass. "
            "If an assertion has no success message defined, a default is generated."
        ),
    )

    validator = models.ForeignKey(
        "validations.Validator",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )

    action = models.ForeignKey(
        Action,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="workflow_steps",
    )

    ruleset = models.ForeignKey(
        "validations.Ruleset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    # ── The two step-config buckets (ADR-2026-06-18) ──
    # ``config`` is the SEMANTIC bucket: only settings that change what
    # validation *does* or how its compute is planned (schema_type, delimiter,
    # execution_profile, case_sensitive, FMU sim settings, …). Its models forbid
    # keys, so the workflow-definition digest can hash this field WHOLESALE and
    # stay provably free of cosmetic or run-injected data.
    # ``display_settings`` is the COSMETIC + runtime-injected bucket
    # (schema_type_label, previews, column counts, display_step_outputs, and
    # keys the runner injects like primary_file_uri). It is NEVER hashed.
    # Use ``typed_config`` / ``display_settings_typed`` for type-safe access —
    # see step_configs.py, which is the single source of truth for which key
    # belongs in which bucket.
    config = models.JSONField(default=dict, blank=True)
    display_settings = models.JSONField(default=dict, blank=True)

    SEMANTIC_DEFINITION_FIELDS = (
        "action_id",
        "config",
        "order",
        "ruleset_id",
        "validator_id",
    )

    @property
    def typed_config(self):
        """Return this step's config as a typed Pydantic model.

        Parses the raw config JSONField into the appropriate Pydantic model
        based on the step's validator or action type. For example, an
        EnergyPlus step returns an EnergyPlusStepConfig instance with typed
        fields like ``idf_checks: list[str]``.

        Resource file references (weather files, templates) are stored
        relationally via ``self.step_resources`` rather than in config.

        Returns BaseStepConfig for unknown types (still gives dict-like access
        via ``extra="allow"``).

        See Also:
            validibot.workflows.step_configs for available config models.
            WorkflowStepResource for relational resource bindings.
        """
        from validibot.workflows.step_configs import get_step_config

        return get_step_config(self)

    @property
    def display_settings_typed(self):
        """Return this step's ``display_settings`` as a typed Pydantic model.

        Mirror of :attr:`typed_config` for the **cosmetic** bucket. Parses the
        raw ``display_settings`` JSONField into the appropriate display model
        (e.g. a Tabular step returns a ``TabularDisplaySettings`` with
        ``delimiter_label``/``column_count``). Falls back to
        ``BaseDisplaySettings`` (still exposing ``display_step_outputs``) for
        types without extra display fields.

        See Also:
            validibot.workflows.step_configs for available display models.
        """
        from validibot.workflows.step_configs import get_step_display_settings

        return get_step_display_settings(self)

    @property
    def step_number(self) -> int:
        """Return the display position for this step based on its order."""
        if not self.order:
            return 1
        return max(1, math.ceil(self.order / 10))

    @property
    def step_number_display(self) -> str:
        """Return a localized display string for this step's number."""
        step_number = self.step_number
        return _("Step") + f" {step_number}"

    def save(self, *args, **kwargs):
        """Auto-generate step_key on first save; prevent mutation after.

        The step_key is the stable workflow contract identifier used for
        cross-step value references in CEL and APIs. It must not change
        after creation because assertions and API consumers may reference
        it. Auto-generated from the step name via slugify() if not set.

        Tenant relationships are checked here as well as in ``clean()``
        because Django does not call model validation for direct ORM writes.
        """
        from slugify import slugify

        self._validate_tenant_relationships()
        is_new = self._state.adding

        if is_new and not self.step_key and self.name:
            # Auto-generate from name, ensuring uniqueness within workflow
            base_key = slugify(self.name, separator="_") or "step"
            candidate = base_key
            counter = 2
            while (
                WorkflowStep.objects.filter(
                    workflow=self.workflow,
                    step_key=candidate,
                )
                .exclude(pk=self.pk)
                .exists()
            ):
                candidate = f"{base_key}_{counter}"
                counter += 1
            self.step_key = candidate

        if not is_new and self.pk:
            # Prevent mutation of step_key after initial creation
            try:
                existing = WorkflowStep.objects.values_list(
                    "step_key",
                    flat=True,
                ).get(pk=self.pk)
            except WorkflowStep.DoesNotExist:
                existing = ""

            if existing and self.step_key != existing:
                self.step_key = existing

        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )
        from validibot.workflows.services.editing_policy import (
            model_instance_has_semantic_changes,
        )

        semantic_change = model_instance_has_semantic_changes(
            self,
            semantic_fields=self.SEMANTIC_DEFINITION_FIELDS,
        )
        with guard_workflow_definition_mutation(
            self.workflow_id,
            semantic_change=semantic_change,
        ):
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Delete a step only while no Mutable run uses its definition."""

        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )

        with guard_workflow_definition_mutation(self.workflow_id):
            return super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        self._validate_tenant_relationships()

        if (
            WorkflowStep.objects.filter(workflow=self.workflow, order=self.order)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError({"order": _("Order already used in this workflow.")})

        # Ensure the ruleset chosen matches the validator's type
        if bool(self.validator_id) == bool(self.action_id):
            raise ValidationError(
                {
                    "validator": _(
                        "Specify either a validator or an action for this step.",
                    ),
                    "action": _(
                        "Specify either a validator or an action for this step.",
                    ),
                },
            )

        if (
            self.validator
            and self.ruleset
            and (self.ruleset.ruleset_type != self.validator.validation_type)
        ):
            raise ValidationError(
                {
                    "ruleset": _("Ruleset type must match validator type."),
                },
            )

        if self.action and self.display_schema:
            self.display_schema = False

        # ── Credential step placement rules ──
        # At most one SignedCredentialAction per workflow, and it must
        # come after all blocking steps.
        if self.action_id:
            self._validate_credential_step_placement()

        # Validate config against the typed Pydantic model for this step type.
        # This catches typos and type mismatches at save time rather than at
        # runtime during validation execution.
        if self.config:
            from pydantic import ValidationError as PydanticValidationError

            from validibot.workflows.step_configs import get_step_config

            try:
                get_step_config(self)
            except PydanticValidationError as exc:
                raise ValidationError(
                    {"config": str(exc)},
                ) from exc

    def _validate_credential_step_placement(self):
        """Enforce credential step uniqueness and ordering rules.

        Rules:
            - At most one SignedCredentialAction step per workflow.
            - The credential step must come after all validator steps
              and all BLOCKING action steps.
            - ADVISORY action steps may appear after the credential step.

        This method is a no-op for non-credential action steps.
        """
        from validibot.actions.constants import ActionFailureMode
        from validibot.actions.constants import CredentialActionType

        # Only apply to signed credential actions.
        action = self.action
        if not action or not action.definition_id:
            return
        if action.definition.type != CredentialActionType.SIGNED_CREDENTIAL:
            return

        # Rule 1: At most one credential step per workflow.
        existing_credential_steps = WorkflowStep.objects.filter(
            workflow=self.workflow,
            action__definition__type=CredentialActionType.SIGNED_CREDENTIAL,
        ).exclude(pk=self.pk)
        if existing_credential_steps.exists():
            raise ValidationError(
                {
                    "action": _(
                        "A workflow can have at most one signed credential step."
                    ),
                },
            )

        # Rule 2: No validator steps may appear after the credential
        # step.  The ADR says: "the credential step must come after
        # all blocking work whose failure would change the meaning of
        # the claim."  Validators are implicitly blocking.
        validators_after_us = WorkflowStep.objects.filter(
            workflow=self.workflow,
            order__gt=self.order,
            validator__isnull=False,
        ).exclude(pk=self.pk)

        if validators_after_us.exists():
            raise ValidationError(
                {
                    "order": _(
                        "The signed credential step must come after all "
                        "validation steps. Move it to the end of the "
                        "workflow, or move the validator steps before it."
                    ),
                },
            )

        # Rule 3: No BLOCKING action steps may appear after the
        # credential step.  ADVISORY actions after it are fine.
        blocking_actions_after_us = WorkflowStep.objects.filter(
            workflow=self.workflow,
            order__gt=self.order,
            action__isnull=False,
            action__failure_mode=ActionFailureMode.BLOCKING,
        ).exclude(pk=self.pk)

        if blocking_actions_after_us.exists():
            raise ValidationError(
                {
                    "order": _(
                        "The signed credential step must come after all "
                        "blocking action steps."
                    ),
                },
            )

    def _validate_tenant_relationships(self) -> None:
        """Reject custom validator or ruleset links from another organization."""

        if not self.workflow_id:
            return

        workflow_org_id = self.workflow.org_id
        errors: dict[str, str] = {}
        if (
            self.validator_id
            and self.validator.org_id is not None
            and self.validator.org_id != workflow_org_id
        ):
            errors["validator"] = _(
                "Custom validator must belong to the workflow organization.",
            )
        if (
            self.ruleset_id
            and self.ruleset.org_id is not None
            and self.ruleset.org_id != workflow_org_id
        ):
            errors["ruleset"] = _(
                "Ruleset must belong to the workflow organization.",
            )
        if errors:
            raise ValidationError(errors)


class WorkflowStepResource(models.Model):
    """Links a resource to a workflow step.

    Supports two mutually exclusive modes:

    1. **Catalog reference** — points to a shared ValidatorResourceFile from the
       validator library (e.g., weather files). Uses PROTECT on delete so you
       can't delete a catalog file that steps still need.

    2. **Step-owned file** — stores the file directly on this record (e.g., a
       template IDF uploaded for this specific step). The file cascades
       with the step — no orphaned files, no catalog clutter.

    Exactly one of ``validator_resource_file`` or ``step_resource_file`` must be
    populated (DB-level XOR constraint).

    See Also:
        ADR 2026-03-04: EnergyPlus Parameterized Model Templates
        (WorkflowStepResource — Relational Resource Binding)
    """

    # Role constants — the purpose of this resource in the step.
    WEATHER_FILE = "WEATHER_FILE"
    MODEL_TEMPLATE = "MODEL_TEMPLATE"
    FMU_MODEL = "FMU_MODEL"
    EXPECTED_BUILDINGS_LIST = "EXPECTED_BUILDINGS_LIST"

    step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="step_resources",
    )
    role = models.CharField(
        max_length=50,
        help_text="Purpose of this resource (e.g., WEATHER_FILE, MODEL_TEMPLATE).",
    )

    # ── Mode 1: Catalog reference (shared resources) ──────────────

    validator_resource_file = models.ForeignKey(
        "validations.ValidatorResourceFile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="step_usages",
        help_text="Shared resource from the validator library.",
    )

    # ── Mode 2: Step-owned file (step-specific resources) ─────────

    step_resource_file = models.FileField(
        upload_to="step_resources/",
        max_length=500,
        blank=True,
        help_text="File owned by this step. Deleted when the step is deleted.",
    )
    filename = models.CharField(
        max_length=255,
        blank=True,
        help_text="Original filename of the step-owned file.",
    )
    resource_type = models.CharField(
        max_length=32,
        blank=True,
        help_text="Resource type identifier for step-owned files.",
    )

    # ADR-2026-04-27 Phase 3, task 11: SHA-256 of the step-owned
    # file's content (only meaningful in step-owned mode; catalog
    # references inherit the hash from their ValidatorResourceFile).
    # Auto-populated on save. Mutation gating happens at save time:
    # if this resource's step lives on a locked workflow and the
    # bytes change, save() raises.
    content_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "SHA-256 of step_resource_file content. Only used for "
            "step-owned files; catalog references look up the hash "
            "via validator_resource_file.content_hash."
        ),
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="ck_step_resource_xor_file",
                condition=(
                    models.Q(
                        validator_resource_file__isnull=False,
                        step_resource_file="",
                    )
                    | (
                        models.Q(validator_resource_file__isnull=True)
                        & ~models.Q(step_resource_file="")
                    )
                ),
            ),
        ]

    def __str__(self):
        if self.is_catalog_reference:
            return (
                f"StepResource({self.step_id}, {self.role},"
                f" catalog={self.validator_resource_file_id})"
            )
        return f"StepResource({self.step_id}, {self.role}, file={self.filename})"

    def save(self, *args, **kwargs):
        """Compute ``content_hash`` for step-owned files, gate drift on lock.

        Catalog references (``validator_resource_file`` set) are
        immutable links to another row — their hash story belongs to
        ``ValidatorResourceFile.content_hash``; we leave
        ``self.content_hash`` empty in that mode.

        For step-owned files, the on-save hash recompute is the
        choke point: a different hash than what's stored, AND a
        locked workflow on the other side, raises before persistence.
        """
        from validibot.core.filesafety import sha256_field_file

        if self.is_step_owned:
            from validibot.validations.constants import PORTFOLIO_MANAGER_EBL_RESOURCE

            if self.resource_type == PORTFOLIO_MANAGER_EBL_RESOURCE:
                from django.core.exceptions import ValidationError
                from validibot_shared.portfolio_manager import (
                    validate_expected_buildings_list_json,
                )
                from validibot_shared.portfolio_manager.envelopes import MAX_EBL_BYTES

                try:
                    self.step_resource_file.open("rb")
                    content = self.step_resource_file.read(MAX_EBL_BYTES + 1)
                    self.step_resource_file.seek(0)
                    validate_expected_buildings_list_json(content)
                except (OSError, ValueError) as exc:
                    raise ValidationError(
                        {
                            "step_resource_file": (
                                f"Expected Buildings List is invalid: {exc}"
                            )
                        }
                    ) from exc
            new_hash = sha256_field_file(self.step_resource_file)
            if (
                self.pk
                and self.content_hash
                and new_hash != self.content_hash
                and self.is_used_by_locked_workflow()
            ):
                from django.core.exceptions import ValidationError

                raise ValidationError(
                    {
                        "step_resource_file": (
                            "This step-owned file is part of a locked "
                            "workflow's contract; its bytes cannot "
                            "change in place. Clone the workflow to a "
                            "new version to upload a different file."
                        ),
                    },
                )
            self.content_hash = new_hash
        else:
            # Catalog reference: leave hash blank, the source row owns it.
            self.content_hash = ""
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )
        from validibot.workflows.services.editing_policy import (
            model_instance_has_semantic_changes,
        )

        semantic_change = model_instance_has_semantic_changes(
            self,
            semantic_fields=(
                "content_hash",
                "filename",
                "resource_type",
                "role",
                "step_resource_file",
                "validator_resource_file_id",
            ),
        )
        with guard_workflow_definition_mutation(
            self.step.workflow_id,
            semantic_change=semantic_change,
        ):
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Delete a resource only while no Mutable run uses its workflow."""

        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )

        with guard_workflow_definition_mutation(self.step.workflow_id):
            return super().delete(*args, **kwargs)

    @property
    def is_catalog_reference(self) -> bool:
        """True when this resource points to a shared ValidatorResourceFile."""
        return self.validator_resource_file_id is not None

    @property
    def is_step_owned(self) -> bool:
        """True when this resource stores a file directly on this record."""
        return self.validator_resource_file_id is None

    def is_used_by_locked_workflow(self) -> bool:
        """Return True if our step lives on a versioned locked/used workflow.

        Step-owned resources are scoped to a single step (one row,
        one workflow). The check therefore walks ``step.workflow``
        and applies the same history-policy-aware locked-or-has-runs
        gate as rulesets and catalog resources.
        """
        if not self.step_id:
            return False
        workflow = self.step.workflow
        return workflow.history_policy == WorkflowHistoryPolicy.VERSIONED and (
            workflow.is_locked or workflow.has_runs()
        )

    def get_storage_uri(self) -> str:
        """Return the storage URI for this resource, regardless of mode.

        For catalog references, delegates to the ValidatorResourceFile's
        ``get_storage_uri()`` method (which handles GCS vs local paths).
        For step-owned files, constructs a proper storage URI from the
        FileField's storage backend — ``gs://`` for GCS or ``file://``
        for local filesystems.

        This follows the same pattern as
        ``ValidatorResourceFile.get_storage_uri()``.
        """
        if self.is_catalog_reference:
            return self.validator_resource_file.get_storage_uri()

        # Step-owned file: construct proper URI for the storage backend.
        # Previously returned `.url` which gives a media URL (e.g.,
        # "/media/files/...") instead of a storage URI that containers
        # can use to download the file.
        file_storage = self.step_resource_file.storage
        storage_class_name = file_storage.__class__.__name__
        if storage_class_name == "GoogleCloudStorage":
            bucket_name = getattr(file_storage, "bucket_name", "")
            location = getattr(file_storage, "location", "")
            if location:
                return f"gs://{bucket_name}/{location}/{self.step_resource_file.name}"
            return f"gs://{bucket_name}/{self.step_resource_file.name}"

        # Local filesystem storage
        return f"file://{self.step_resource_file.path}"


# ── Storage cleanup for step-owned files ──────────────────────────────
#
# Django's FileField does NOT auto-delete files from storage when a model
# instance is deleted.  For step-owned files, the WorkflowStepResource
# row is the *only* reference to the file — without this signal, deleting
# the row (via cascade, template replacement, or explicit delete) leaves
# orphaned files in storage (local filesystem or GCS bucket).


def _cleanup_step_resource_file(sender, instance, **kwargs):
    """Delete the physical file from storage when a WorkflowStepResource is removed."""
    if instance.step_resource_file:
        try:
            instance.step_resource_file.delete(save=False)
        except Exception:
            logger.warning(
                "Failed to delete step-owned file for WorkflowStepResource %s",
                instance.pk,
                exc_info=True,
            )


models.signals.post_delete.connect(
    _cleanup_step_resource_file,
    sender=WorkflowStepResource,
)


class WorkflowRoleAccess(models.Model):
    """
    Grants access to a workflow to users holding specific roles in the workflow's org.
    Example: allow all 'ADMIN' or 'OWNER' members of the org.
    """

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="role_access",
    )

    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="workflow_role_access",
    )

    class Meta:
        unique_together = [("workflow", "role")]
        indexes = [models.Index(fields=["workflow", "role"])]

    def __str__(self):
        return f"{self.workflow_id}:{self.role}"


class WorkflowAccessGrant(TimeStampedModel):
    """
    Grants a user (typically external) access to a specific workflow without
    requiring org membership. Used for cross-organization workflow sharing.

    Workflow Guests are users who have access grants but no org membership.
    Their usage is billed/metered against the workflow owner's org.

    This is distinct from WorkflowRoleAccess which grants access based on
    org membership roles. WorkflowAccessGrant is for external users who
    are not members of the workflow's organization.
    """

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="access_grants",
        help_text=_("The workflow this grant provides access to."),
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="workflow_grants",
        help_text=_("The user who has been granted access."),
    )
    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_workflow_access",
        help_text=_("The user who created this grant."),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Whether this grant is currently active."),
    )
    notes = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional notes about this access grant."),
    )

    class Meta:
        unique_together = [("workflow", "user")]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["workflow", "is_active"]),
        ]
        verbose_name = _("workflow access grant")
        verbose_name_plural = _("workflow access grants")

    def __str__(self):
        return f"{self.user} -> {self.workflow.name}"

    def revoke(self) -> None:
        """Revoke this access grant."""
        self.is_active = False
        self.save(update_fields=["is_active", "modified"])


class OrgGuestAccess(TimeStampedModel):
    """Grant a guest user access to all current AND future workflows in an org.

    Created when a ``GuestInvite`` with ``scope=ALL`` is accepted. The
    guest sees every workflow in the org's catalog as it grows, with
    no per-workflow grant maintenance. This solves the "100 workflows,
    10 new a month" problem inherent to expanding ``scope=ALL`` into N
    individual ``WorkflowAccessGrant`` rows at acceptance time only.

    A user can simultaneously hold an ``OrgGuestAccess`` row for one
    org AND per-workflow ``WorkflowAccessGrant`` rows for other orgs
    (or for specific workflows in the same org that pre-existed the
    org-wide grant). The read-side queryset (``Workflow.objects.for_user``)
    unions all access paths.

    Revocation is a flag flip rather than a delete so the audit trail
    stays intact and a temporarily-disabled access can be restored
    without losing the original grant timestamp.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="org_guest_accesses",
        help_text=_("The user who has been granted org-wide guest access."),
    )
    org = models.ForeignKey(
        "users.Organization",
        on_delete=models.CASCADE,
        related_name="guest_accesses",
        help_text=_("The organization this guest can access workflows in."),
    )
    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_org_guest_accesses",
        help_text=_("The user who created this grant."),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Whether this org-wide guest access is currently active."),
    )
    notes = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional notes about this access grant."),
    )

    class Meta:
        unique_together = [("user", "org")]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["org", "is_active"]),
        ]
        verbose_name = _("organization guest access")
        verbose_name_plural = _("organization guest accesses")

    def __str__(self):
        return f"{self.user} -> all workflows in {self.org}"

    def revoke(self) -> None:
        """Revoke this org-wide guest access (flag flip, not delete)."""
        self.is_active = False
        self.save(update_fields=["is_active", "modified"])


class WorkflowInvite(TimeStampedModel):
    """
    Invitation for an external user to access a specific workflow as a guest.

    Unlike MemberInvite (for org membership), accepting this invite creates a
    WorkflowAccessGrant but NOT a Membership. The invited user operates as a
    Workflow Guest without an organization context.

    Workflow invites enable cross-org sharing where:
    - The inviter is an author in the workflow's org
    - The invitee may or may not have an existing account
    - Upon acceptance, the invitee gets access to the specific workflow only
    - Usage is billed to the workflow owner's org
    """

    # Keep Status as alias for backward compatibility
    Status = InviteStatus

    # Default invite expiry: 7 days
    DEFAULT_EXPIRY_DAYS = 7

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="invites",
        help_text=_("The workflow this invite grants access to."),
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_workflow_invites",
        help_text=_("The user who sent this invite."),
    )
    invitee_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="received_workflow_invites",
        null=True,
        blank=True,
        help_text=_("The invited user, if they already have an account."),
    )
    invitee_email = models.EmailField(
        blank=True,
        help_text=_("Email address of invitee (used when inviting non-users)."),
    )
    status = models.CharField(
        max_length=16,
        choices=InviteStatus.choices,
        default=InviteStatus.PENDING,
    )
    token = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique token for invite acceptance URL."),
    )
    expires_at = models.DateTimeField(
        help_text=_("When this invite expires."),
    )

    class Meta:
        ordering = ["-created"]
        verbose_name = _("workflow invite")
        verbose_name_plural = _("workflow invites")
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["invitee_email", "status"]),
            models.Index(fields=["workflow", "status"]),
        ]

    def __str__(self):
        target = self.invitee_user or self.invitee_email
        return f"Invite to {self.workflow.name} for {target}"

    @classmethod
    def create_with_expiry(
        cls,
        *,
        workflow: Workflow,
        inviter: User,
        invitee_email: str,
        invitee_user: User | None = None,
        expiry_days: int | None = None,
        send_email: bool = True,
    ) -> WorkflowInvite:
        """
        Create a new workflow invite with default expiry.

        Args:
            workflow: The workflow to grant access to.
            inviter: The user sending the invite.
            invitee_email: Email of the person being invited.
            invitee_user: Optional existing user if email matches.
            expiry_days: Days until expiry (default: 7).
            send_email: Whether to send an invitation email (default: True).

        Returns:
            The created WorkflowInvite instance.
        """
        from datetime import timedelta

        from django.utils import timezone

        days = expiry_days or cls.DEFAULT_EXPIRY_DAYS
        expires_at = timezone.now() + timedelta(days=days)

        invite = cls.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email=invitee_email,
            invitee_user=invitee_user,
            expires_at=expires_at,
        )

        if send_email:
            from validibot.workflows.emails import send_workflow_invite_email

            send_workflow_invite_email(invite)

        return invite

    def mark_expired_if_needed(self) -> bool:
        """
        Check if invite has expired and update status if so.

        Returns:
            True if the invite was marked as expired, False otherwise.
        """
        from django.utils import timezone

        if self.status != InviteStatus.PENDING:
            return False

        if timezone.now() >= self.expires_at:
            self.status = InviteStatus.EXPIRED
            self.save(update_fields=["status", "modified"])
            return True

        return False

    def accept(self, user: User | None = None) -> WorkflowAccessGrant:
        """
        Accept this invite and create a WorkflowAccessGrant.

        Args:
            user: The user accepting the invite. If not provided, uses
                  invitee_user. Required if invitee_user is not set.

        Returns:
            The created WorkflowAccessGrant.

        Raises:
            ValueError: If invite is not in PENDING status or no user provided.
        """
        # Check for expiry first
        if self.mark_expired_if_needed():
            raise ValueError("Invite has expired")

        if self.status != InviteStatus.PENDING:
            msg = f"Cannot accept invite with status {self.status}"
            raise ValueError(msg)

        accepting_user = user or self.invitee_user
        if not accepting_user:
            msg = "No user provided to accept invite"
            raise ValueError(msg)

        # SECURITY INVARIANT: a *bound* invite (``invitee_user`` set) may only
        # be redeemed by that exact account. The token-acceptance views check
        # email ownership only for *unbound* (email-only) invites, and the
        # post-signup adapter does the same — so without this model-level guard
        # a logged-in User A holding a link to an invite bound to User B could
        # call ``accept(user=A)`` and be granted B's workflow access
        # (cross-account escalation). Enforcing it here covers every acceptance
        # surface — token view, signup adapter, notification view — at the one
        # chokepoint they all funnel through. Unbound invites
        # (``invitee_user_id`` falsy) are intentionally exempt: their correct
        # gate is email ownership, enforced by the callers.
        if self.invitee_user_id and accepting_user.pk != self.invitee_user_id:
            msg = "This invite is addressed to a different user."
            raise ValueError(msg)

        # Create the access grant
        grant, _created = WorkflowAccessGrant.objects.get_or_create(
            workflow=self.workflow,
            user=accepting_user,
            defaults={
                "granted_by": self.inviter,
                "is_active": True,
            },
        )

        # If grant already existed but was inactive, reactivate it
        if not _created and not grant.is_active:
            grant.is_active = True
            grant.granted_by = self.inviter
            grant.save(update_fields=["is_active", "granted_by", "modified"])

        # Update invite status
        self.status = InviteStatus.ACCEPTED
        if not self.invitee_user:
            self.invitee_user = accepting_user
        self.save(update_fields=["status", "invitee_user", "modified"])

        return grant

    def decline(self) -> None:
        """Decline this invite."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.DECLINED
        self.save(update_fields=["status", "modified"])

    def cancel(self) -> None:
        """Cancel this invite (called by the inviter)."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.CANCELED
        self.save(update_fields=["status", "modified"])

    @property
    def is_expired(self) -> bool:
        """Check if invite has expired without updating status."""
        from django.utils import timezone

        return self.status == InviteStatus.EXPIRED or timezone.now() >= self.expires_at

    @property
    def is_pending(self) -> bool:
        """Check if invite is still pending and not expired."""
        return self.status == InviteStatus.PENDING and not self.is_expired


class GuestInvite(TimeStampedModel):
    """
    Invitation for an external user to access multiple workflows in an org as a guest.

    Unlike WorkflowInvite (for a single workflow), this invite grants access to either:
    - All current workflows in the org (scope=ALL), or
    - A selected subset of workflows (scope=SELECTED)

    When accepted, the invite expands into individual WorkflowAccessGrant rows
    for each workflow in the resolved set. New workflows created after acceptance
    are NOT automatically shared.

    This model enables the org-level guest management UI where admins can invite
    guests to multiple workflows at once.
    """

    # Keep Status as alias for backward compatibility
    Status = InviteStatus

    class Scope(models.TextChoices):
        ALL = "ALL", _("All workflows in org")
        SELECTED = "SELECTED", _("Selected workflows")

    # Default invite expiry: 7 days
    DEFAULT_EXPIRY_DAYS = 7

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    org = models.ForeignKey(
        "users.Organization",
        on_delete=models.CASCADE,
        related_name="guest_invites",
        help_text=_("The organization this invite is for."),
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_guest_invites",
        help_text=_("The user who sent this invite."),
    )
    invitee_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="received_guest_invites",
        null=True,
        blank=True,
        help_text=_("The invited user, if they already have an account."),
    )
    invitee_email = models.EmailField(
        blank=True,
        help_text=_("Email address of invitee (used when inviting non-users)."),
    )
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.SELECTED,
        help_text=_("Whether to grant access to all current workflows or a selection."),
    )
    workflows = models.ManyToManyField(
        Workflow,
        blank=True,
        related_name="guest_invites",
        help_text=_("Workflows to grant access to (used when scope=SELECTED)."),
    )
    status = models.CharField(
        max_length=16,
        choices=InviteStatus.choices,
        default=InviteStatus.PENDING,
    )
    token = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique token for invite acceptance URL."),
    )
    expires_at = models.DateTimeField(
        help_text=_("When this invite expires."),
    )

    class Meta:
        ordering = ["-created"]
        verbose_name = _("guest invite")
        verbose_name_plural = _("guest invites")
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["invitee_email", "status"]),
            models.Index(fields=["org", "status"]),
        ]

    def __str__(self):
        target = self.invitee_user or self.invitee_email
        return f"Guest invite to {self.org.name} for {target}"

    def get_resolved_workflows(self) -> models.QuerySet[Workflow]:
        """
        Get the workflows this invite grants access to.

        For scope=ALL, returns all active, non-archived, non-tombstoned
        workflows in the org.
        For scope=SELECTED, returns the explicitly selected workflows.
        """
        if self.scope == self.Scope.ALL:
            return Workflow.objects.filter(
                org=self.org,
                is_active=True,
                is_archived=False,
                is_tombstoned=False,
            )
        return self.workflows.filter(
            is_active=True,
            is_archived=False,
            is_tombstoned=False,
        )

    @classmethod
    def create_with_expiry(
        cls,
        *,
        org,
        inviter: User,
        invitee_email: str,
        scope: str,
        workflows: list | None = None,
        invitee_user: User | None = None,
        expiry_days: int | None = None,
        send_email: bool = True,
    ) -> GuestInvite:
        """
        Create a new guest invite with default expiry.

        Args:
            org: The organization to grant guest access to.
            inviter: The user sending the invite.
            invitee_email: Email of the person being invited.
            scope: Either 'ALL' or 'SELECTED'.
            workflows: List of workflows (required if scope=SELECTED).
            invitee_user: Optional existing user if email matches.
            expiry_days: Days until expiry (default: 7).
            send_email: Whether to send an invitation email (default: True).

        Returns:
            The created GuestInvite instance.
        """
        from datetime import timedelta

        from django.utils import timezone

        days = expiry_days or cls.DEFAULT_EXPIRY_DAYS
        expires_at = timezone.now() + timedelta(days=days)

        invite = cls.objects.create(
            org=org,
            inviter=inviter,
            invitee_email=invitee_email,
            invitee_user=invitee_user,
            scope=scope,
            expires_at=expires_at,
        )

        if scope == cls.Scope.SELECTED and workflows:
            invite.workflows.set(workflows)

        if send_email:
            from validibot.workflows.emails import send_guest_invite_email

            send_guest_invite_email(invite)

        return invite

    def mark_expired_if_needed(self) -> bool:
        """
        Check if invite has expired and update status if so.

        Returns:
            True if the invite was marked as expired, False otherwise.
        """
        from django.utils import timezone

        if self.status != InviteStatus.PENDING:
            return False

        if timezone.now() >= self.expires_at:
            self.status = InviteStatus.EXPIRED
            self.save(update_fields=["status", "modified"])
            return True

        return False

    def accept(
        self,
        user: User | None = None,
    ) -> list[WorkflowAccessGrant] | OrgGuestAccess:
        """Accept this invite and create the appropriate access record(s).

        Two return shapes depending on the invite's ``scope``:

        * ``Scope.SELECTED`` → a list of ``WorkflowAccessGrant`` rows
          (one per resolved workflow). Behaviour matches the
          per-workflow ``WorkflowInvite.accept()`` for the selected
          subset.
        * ``Scope.ALL`` → a single ``OrgGuestAccess`` row that
          authorises the user against every current AND future workflow
          in the org. Acceptance time no longer snapshots the workflow
          list, so a workflow added next month is automatically visible
          to the guest with no admin action required.

        Args:
            user: The user accepting the invite. If not provided, uses
                  ``invitee_user``. Required if ``invitee_user`` is not set.

        Returns:
            Either a list of ``WorkflowAccessGrant`` (SELECTED scope)
            or a single ``OrgGuestAccess`` (ALL scope).

        Raises:
            ValueError: If invite is not in PENDING status or no user provided.
        """

        # Check for expiry first
        if self.mark_expired_if_needed():
            raise ValueError("Invite has expired")

        if self.status != InviteStatus.PENDING:
            msg = f"Cannot accept invite with status {self.status}"
            raise ValueError(msg)

        accepting_user = user or self.invitee_user
        if not accepting_user:
            msg = "No user provided to accept invite"
            raise ValueError(msg)

        # SECURITY INVARIANT: see ``WorkflowInvite.accept`` — a bound guest
        # invite (``invitee_user`` set) may only be redeemed by that exact
        # account, so a leaked/forwarded link cannot grant another person's
        # org guest access to whoever opens it. Unbound (email-only) invites
        # are exempt; their gate is email ownership, enforced by the callers.
        if self.invitee_user_id and accepting_user.pk != self.invitee_user_id:
            msg = "This invite is addressed to a different user."
            raise ValueError(msg)

        if self.scope == GuestInvite.Scope.ALL:
            access, created = OrgGuestAccess.objects.get_or_create(
                user=accepting_user,
                org=self.org,
                defaults={
                    "granted_by": self.inviter,
                    "is_active": True,
                },
            )
            # Reactivate a previously-revoked row so a guest who was
            # cut off and then re-invited regains access via the same
            # OrgGuestAccess row (preserving the original grant
            # timestamp + audit trail).
            if not created and not access.is_active:
                access.is_active = True
                access.granted_by = self.inviter
                access.save(
                    update_fields=["is_active", "granted_by", "modified"],
                )
            result: list[WorkflowAccessGrant] | OrgGuestAccess = access
        else:
            # SELECTED scope: per-workflow grants (the legacy path).
            grants: list[WorkflowAccessGrant] = []
            for workflow in self.get_resolved_workflows():
                grant, created = WorkflowAccessGrant.objects.get_or_create(
                    workflow=workflow,
                    user=accepting_user,
                    defaults={
                        "granted_by": self.inviter,
                        "is_active": True,
                    },
                )
                if not created and not grant.is_active:
                    grant.is_active = True
                    grant.granted_by = self.inviter
                    grant.save(
                        update_fields=["is_active", "granted_by", "modified"],
                    )
                grants.append(grant)
            result = grants

        # Update invite status (same for both scopes)
        self.status = InviteStatus.ACCEPTED
        if not self.invitee_user:
            self.invitee_user = accepting_user
        self.save(update_fields=["status", "invitee_user", "modified"])

        return result

    def decline(self) -> None:
        """Decline this invite."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.DECLINED
        self.save(update_fields=["status", "modified"])

    def cancel(self) -> None:
        """Cancel this invite (called by the inviter)."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.CANCELED
        self.save(update_fields=["status", "modified"])

    @property
    def is_expired(self) -> bool:
        """Check if invite has expired without updating status."""
        from django.utils import timezone

        return self.status == InviteStatus.EXPIRED or timezone.now() >= self.expires_at

    @property
    def is_pending(self) -> bool:
        """Check if invite is still pending and not expired."""
        return self.status == InviteStatus.PENDING and not self.is_expired


def _guard_contract_member_write(
    instance,
    *,
    immutable_fields: tuple[str, ...],
    member_label: str,
) -> None:
    """Block an unsafe add/edit of a workflow-scoped contract member.

    Shared by :class:`WorkflowSignalMapping` and :class:`WorkflowConstant`
    (ADR-2026-06-18). Both are part of the versioned trust contract: a signal
    mapping's resolved value and a constant's literal value each determine
    pass/fail, so editing them after runs exist (or once the workflow is locked)
    would silently rewrite what "passed against workflow vX" meant. On a
    ``versioned`` workflow with runs/lock, this rejects:

    * **adding** a new member (no ``pk``), and
    * **editing** any *semantic* field in ``immutable_fields`` (cosmetic fields
      like ``description``/``position`` are intentionally excluded and stay
      freely editable).

    The author is directed to clone the workflow to a new version instead. Runs
    only when the workflow's ``requires_new_version_for_contract_edits()`` gate
    is set, so it has no effect during normal authoring or on mutable-history
    workflows. (Deletion is guarded separately in each model's ``delete()``.)

    NOTE: like the assertion immutability guard this lives on the per-instance
    write path (``clean()``), so it covers form/service/VAF saves but not bulk
    ``QuerySet.update()/delete()`` — clone uses ``bulk_create`` on the *new*
    (unlocked) workflow, which is the intended escape hatch.
    """
    workflow_id = getattr(instance, "workflow_id", None)
    if not workflow_id:
        return
    workflow = instance.workflow
    if not workflow.requires_new_version_for_contract_edits():
        return

    if instance.pk is None:
        raise ValidationError(
            _(
                "Cannot add a new %(member)s: this workflow has runs (or is "
                "locked), so its contract is fixed. Clone it to a new version "
                "to change %(member)s.",
            )
            % {"member": member_label},
        )

    try:
        original = type(instance).objects.get(pk=instance.pk)
    except type(instance).DoesNotExist:
        return
    changed = [
        field
        for field in immutable_fields
        if getattr(instance, field, None) != getattr(original, field, None)
    ]
    if changed:
        raise ValidationError(
            _(
                "Cannot change %(member)s on a workflow that has runs (or is "
                "locked): clone it to a new version to change this.",
            )
            % {"member": member_label},
        )


def _guard_contract_member_delete(instance, *, member_label: str) -> None:
    """Block deleting a workflow-scoped contract member after runs/lock.

    The delete-side companion to :func:`_guard_contract_member_write`: removing a
    signal mapping or constant changes the workflow contract and its digest
    exactly as an edit does, so it is blocked on a ``versioned`` workflow with
    runs/lock. Only fires for a direct ``instance.delete()`` — Django's cascade
    on workflow deletion uses the collector's bulk delete and does not call this,
    so deleting the whole workflow is unaffected.
    """
    workflow_id = getattr(instance, "workflow_id", None)
    if not workflow_id:
        return
    if instance.workflow.requires_new_version_for_contract_edits():
        raise ValidationError(
            _(
                "Cannot delete this %(member)s: the workflow has runs (or is "
                "locked). Clone it to a new version to change %(member)s.",
            )
            % {"member": member_label},
        )


class WorkflowSignalMapping(TimeStampedModel):
    """A named signal defined at the workflow level.

    Each row maps a signal name (the author's domain vocabulary) to a
    source path in the submission data.  Resolved once before any step
    runs.  Available in CEL expressions as ``s.<name>`` (or
    ``signal.<name>``).

    The ``on_missing`` field controls what happens when the source path
    cannot be resolved against the submission data:

    - ``error`` (default): the validation run fails immediately with a
      clear message before any step is attempted.
    - ``null``: the signal is injected as ``null`` and the author must
      guard with ``s.name != null`` in CEL expressions.  Accessing a
      null signal without a guard produces a fail-fast evaluation error
      with a message explaining how to fix it.
    """

    workflow = models.ForeignKey(
        "Workflow",
        on_delete=models.CASCADE,
        related_name="signal_mappings",
    )
    name = models.CharField(
        max_length=100,
        help_text="Signal name.  Valid CEL identifier.  Used as s.<name>.",
    )
    source_path = models.CharField(
        max_length=500,
        help_text="Data path resolved against the submission payload.",
    )
    default_value = models.JSONField(
        null=True,
        blank=True,
        help_text="Fallback value when the source path resolves to nothing.",
    )
    on_missing = models.CharField(
        max_length=10,
        choices=[("error", "Fail the run"), ("null", "Inject null")],
        default="error",
    )
    data_type = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Expected type: number, string, boolean.  Empty = infer.",
    )
    position = models.PositiveIntegerField(
        default=0,
        help_text="Display order in the signal mapping editor.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "name"],
                name="unique_signal_name_per_workflow",
            ),
        ]
        ordering = ["position"]

    # Semantic fields whose mutation changes how the signal resolves and thus
    # what a run was checked against. ADR-2026-06-18 adds signal mappings to the
    # edit-after-runs protected family (closing a pre-existing gap). ``position``
    # is cosmetic (display order) and intentionally excluded.
    IMMUTABLE_SIGNAL_FIELDS = (
        "name",
        "source_path",
        "on_missing",
        "default_value",
        "data_type",
    )

    def clean(self) -> None:
        """Validate signal name is a valid CEL identifier, not reserved,
        and unique across both workflow mappings and promoted outputs.

        Also enforces the versioned trust contract: on a ``versioned`` workflow
        that has runs (or is locked), adding a mapping or editing a semantic
        field is rejected and the author is directed to clone a new version
        (ADR-2026-06-18).
        """
        from validibot.validations.services.signal_resolution import (
            validate_signal_name,
        )
        from validibot.validations.services.signal_resolution import (
            validate_signal_name_unique,
        )

        errors: dict[str, list[str]] = {}

        # Validate name is valid CEL identifier + not reserved
        name_errors = validate_signal_name(self.name)
        if name_errors:
            errors["name"] = name_errors

        # Cross-table uniqueness check (only if we have a workflow)
        if self.workflow_id:
            unique_errors = validate_signal_name_unique(
                workflow_id=self.workflow_id,
                name=self.name,
                exclude_mapping_id=self.pk,
            )
            if unique_errors:
                errors.setdefault("name", []).extend(unique_errors)

        if errors:
            raise ValidationError(errors)

        # Versioned trust-contract guard (after name validation so the message
        # ordering matches the constant model).
        _guard_contract_member_write(
            self,
            immutable_fields=self.IMMUTABLE_SIGNAL_FIELDS,
            member_label="signal mapping",
        )

    def save(self, *args, **kwargs):
        """Run full model validation on every save.

        Django's ``Model.save()`` does NOT call ``clean()`` by default,
        so ORM-level creates (``objects.create()``) would bypass the
        reserved-name and cross-table uniqueness checks.  This override
        ensures those guards always fire.
        """
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )
        from validibot.workflows.services.editing_policy import (
            model_instance_has_semantic_changes,
        )

        semantic_change = model_instance_has_semantic_changes(
            self,
            semantic_fields=self.IMMUTABLE_SIGNAL_FIELDS,
            update_fields=kwargs.get("update_fields"),
        )
        with guard_workflow_definition_mutation(
            self.workflow_id,
            semantic_change=semantic_change,
        ):
            self.full_clean()
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Block deletion once the workflow's contract is locked by runs.

        Removing a mapping changes the contract just as editing one does
        (ADR-2026-06-18), so it is gated the same way.
        """
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )

        with guard_workflow_definition_mutation(self.workflow_id):
            _guard_contract_member_delete(self, member_label="signal mapping")
            return super().delete(*args, **kwargs)

    def __str__(self) -> str:
        return f"s.{self.name} → {self.source_path}"


class WorkflowConstant(TimeStampedModel):
    """A named, fixed value defined at the workflow level (the ``c.*`` namespace).

    ADR-2026-06-18. A constant is the first value that comes from the *workflow
    definition* instead of the run — workflow-definition-derived, fixed at
    authoring time. That is what distinguishes it from a
    :class:`WorkflowSignalMapping` (``s.<name>``), which is *resolved from the
    submission payload* at a source path. A constant therefore has **no**
    ``source_path``, ``on_missing``, or ``default_value`` — it can never be
    "missing" and never needs resolution; at runtime it is just a literal map
    injected once before any step. Referenced in assertions as ``c.<name>``
    (long form ``const.<name>``).

    Type is explicit (no "Auto"): the chosen ``data_type`` coerces and validates
    ``value`` at save time via
    :func:`validibot.workflows.services.constants.coerce_constant_value`. A
    ``NUMBER`` is stored as a canonical decimal *string* (e.g. ``"0.40"``) so its
    exact value and precision survive into the evidence manifest, the
    workflow-definition digest, and the Pro signed credential — CEL has no
    decimal type, so the value is coerced to ``double`` only at evaluation time.
    """

    workflow = models.ForeignKey(
        "Workflow",
        on_delete=models.CASCADE,
        related_name="constants",
    )
    name = models.CharField(
        max_length=100,
        help_text="Constant name.  Valid CEL identifier.  Used as c.<name>.",
    )
    value = models.JSONField(
        help_text=(
            "The literal value.  NUMBER is stored as a canonical decimal "
            "string; LIST/OBJECT as JSON; STRING/BOOLEAN directly."
        ),
    )
    data_type = models.CharField(
        max_length=20,
        choices=WorkflowConstantType.choices,
        help_text="Explicit value type (required; no inference).",
    )
    description = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Optional human note (e.g. 'agreed €/kWh per the 2026 contract').",
    )
    position = models.PositiveIntegerField(
        default=0,
        help_text="Display order in the constants editor.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "name"],
                name="unique_constant_name_per_workflow",
            ),
        ]
        ordering = ["position"]

    # Semantic fields whose mutation changes pass/fail. ``description`` and
    # ``position`` are cosmetic (ADR-2026-06-18) and stay freely editable even
    # after runs.
    IMMUTABLE_CONSTANT_FIELDS = (
        "name",
        "value",
        "data_type",
    )

    def clean(self) -> None:
        """Validate the constant name and coerce/validate its value by type.

        Name rules (valid CEL identifier, not a reserved root, unique *among
        constants*) reuse the constant-scoped helpers — NOT the signal
        uniqueness helper, because a constant sharing a bare name with a signal
        is allowed (the ``c.``/``s.`` prefix disambiguates). The value is run
        through ``coerce_constant_value`` so the stored form is canonical and
        type-correct before it ever reaches a run. Finally, the versioned
        trust-contract guard blocks add/edit of semantic fields once the
        workflow has runs (or is locked).
        """
        from validibot.workflows.services.constants import ConstantValueError
        from validibot.workflows.services.constants import coerce_constant_value
        from validibot.workflows.services.constants import validate_constant_name
        from validibot.workflows.services.constants import validate_constant_name_unique

        errors: dict[str, list[str]] = {}

        name_errors = validate_constant_name(self.name)
        if name_errors:
            errors["name"] = name_errors

        if self.workflow_id:
            unique_errors = validate_constant_name_unique(
                workflow_id=self.workflow_id,
                name=self.name,
                exclude_constant_id=self.pk,
            )
            if unique_errors:
                errors.setdefault("name", []).extend(unique_errors)

        if self.data_type:
            try:
                # Coerce in place so the canonical (e.g. decimal-string) form is
                # what gets persisted.
                self.value = coerce_constant_value(self.data_type, self.value)
            except ConstantValueError as exc:
                errors["value"] = [str(exc)]
        else:
            errors["data_type"] = [str(_("A constant type is required."))]

        if errors:
            raise ValidationError(errors)

        # Versioned trust-contract guard — blocks add/edit of name/value/
        # data_type after runs exist (or once locked).
        _guard_contract_member_write(
            self,
            immutable_fields=self.IMMUTABLE_CONSTANT_FIELDS,
            member_label="constant",
        )

    def save(self, *args, **kwargs):
        """Run full model validation on every save.

        Mirrors :class:`WorkflowSignalMapping`: Django's ``save()`` does not call
        ``clean()``, so ORM-level creates would otherwise bypass the
        reserved-name, uniqueness, and value-coercion guards.
        """
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )
        from validibot.workflows.services.editing_policy import (
            model_instance_has_semantic_changes,
        )

        semantic_change = model_instance_has_semantic_changes(
            self,
            semantic_fields=self.IMMUTABLE_CONSTANT_FIELDS,
            update_fields=kwargs.get("update_fields"),
        )
        with guard_workflow_definition_mutation(
            self.workflow_id,
            semantic_change=semantic_change,
        ):
            self.full_clean()
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Block deletion once the workflow's contract is locked by runs.

        Removing a constant changes the workflow contract and digest just as
        editing one does (ADR-2026-06-18).
        """
        from validibot.workflows.services.editing_policy import (
            guard_workflow_definition_mutation,
        )

        with guard_workflow_definition_mutation(self.workflow_id):
            _guard_contract_member_delete(self, member_label="constant")
            return super().delete(*args, **kwargs)

    @property
    def display_value(self) -> str:
        """Human-facing value for templates (JSON for LIST/OBJECT, not repr).

        Templates should render ``{{ c.display_value }}`` rather than
        ``{{ c.value }}`` so a list shows as ``["EUR", "GBP"]`` instead of the
        Python ``['EUR', 'GBP']`` repr (ADR-2026-06-18).
        """
        from validibot.workflows.services.constants import format_constant_value

        return format_constant_value(self)

    def __str__(self) -> str:
        return f"c.{self.name} = {self.value!r}"
