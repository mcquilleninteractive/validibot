# Manage guest accounts

Guest accounts are external collaborators who use Validibot to launch and view validation runs without being members of any organization. This how-to covers the operator surface: classifying users, promoting and demoting accounts, and the two site-wide kill switches.

Guest management requires the `guest_management` Pro feature (installed by `validibot-pro`). In community-only deployments every user is `BASIC` and the GUEST classification doesn't exist.

## What is a guest?

Each user account has a system-wide `user_kind`:

- **`BASIC`** — regular users. Members of organizations they belong to; their per-org capabilities flow from `Membership` roles.
- **`GUEST`** — external collaborators. No `Membership` rows. Access workflows via `WorkflowAccessGrant` (per-workflow), `OrgGuestAccess` (org-wide), or workflows whose `workflow_visibility` is `ALL_USERS`.

The classification is **sticky**: it only changes when a superuser explicitly runs the `promote_user` command or the matching Django admin action. A user's kind does NOT change automatically when their grants or memberships change. This protects against silent privilege escalation — an unrelated code path adding a `Membership` row to a guest's account is rejected at the data layer.

## Promote a guest to basic

When a guest needs to graduate to a regular user account (e.g. a contractor becoming a full team member), promote them.

```bash
python manage.py promote_user --email guest@example.com --to basic
```

What this does in one atomic transaction:

1. Removes the user from the `Guests` Django Group and adds them to `Basic Users`.
2. Provisions a personal workspace for the user (creates an `Organization` + `Membership` with OWNER role) **if they have no active memberships**. Without this step a promoted user would be classified as basic but have nowhere to operate.
3. Records a single `USER_PROMOTED_TO_BASIC` audit log entry naming the operator who ran the command.

The command is **idempotent**. Running it on an already-`BASIC` user is a no-op (no audit row, no duplicate workspace).

If the operator's intent is to promote AND add the user to an existing org (rather than the auto-provisioned personal workspace), run `promote_user` first, then `add_member` (or use the existing member-invite UI) to add them to the target org. Two commands, one job each.

## Demote a basic user to guest

Less common, used in incident response when a user account needs to be downgraded.

```bash
python manage.py promote_user --email user@example.com --to guest --confirm
```

The `--confirm` flag is required for demotion. Without it the command exits with an error — a typo cannot accidentally strip operator-level capabilities.

What this does:

1. Removes the user from `Basic Users` and adds them to `Guests`.
2. Records a `USER_DEMOTED_TO_GUEST` audit log entry.

What this does NOT do:

- It does **not** remove existing `Membership` rows. The demoted user keeps any org memberships they had until you remove them separately. This is by design — a half-finished demotion is recoverable; a destructive cascade is not.
- It does **not** revoke `WorkflowAccessGrant` or `OrgGuestAccess` rows. Cross-org access stays in place unless explicitly revoked.

The follow-up matters: after demoting, an operator should also clean up any stale memberships and grants the user shouldn't retain.

## Use the Django admin action instead of the CLI

Both promotion and demotion are also available as Django admin actions on the User changelist:

1. Sign in to `/admin/` as a superuser.
2. Open **Users**.
3. Select the target users.
4. Pick **Promote selected users to Basic** or **Demote selected users to Guest** from the action dropdown.
5. Confirm on the standard Django admin "are you sure?" page.

The admin action delegates to the same code path as the management command, so the audit log, personal-workspace provisioning, and atomicity guarantees are identical. Use whichever surface fits your workflow — shell access vs. browser.

## Site-wide kill switches

Two booleans on `SiteSettings` give operators run-time control without code changes. Both default to `True` (existing deployments upgrade transparently).

### `allow_guest_access`

When `False`, GUEST users cannot log in. Existing guest accounts are not deleted — just denied access while the flag is `False`. Toggling it back on restores login without data migration.

Use case: incident response. If you suspect a compromised guest account or need to quickly cut off all guest activity, flip this flag.

### `allow_guest_invites`

When `False`, no user (other than superusers) can:

- **Create** a guest invite — `GuestInviteCreateView`, `WorkflowGuestInviteView`, etc. return 403.
- **Accept** a guest invite — `WorkflowInviteAcceptView`, `AcceptGuestInviteView` also return 403.

Two-sided enforcement is deliberate: pending invites already in the wild cannot sneak through during a temporary disable window. Pending invite rows stay `PENDING` in the database; flipping the flag back on lets unexpired invites be redeemed.

Use case: winding down a guest-invite feature, or pausing invites during a security review.

### Toggling the flags

From Django admin: open `/admin/core/sitesettings/`, edit the singleton row, flip the boolean, save.

From the Django shell:

```python
from validibot.core.site_settings import get_site_settings

settings = get_site_settings()
settings.allow_guest_access = False
settings.allow_guest_invites = False
settings.save()
```

## Rebuild user-kind classification

If a database edit, migration squash, or partial recovery has left users without a classifier group (or in the wrong one), run:

```bash
python manage.py backfill_user_kinds
```

The command is **idempotent**. It classifies every user according to the predicate "active grant AND no active membership → `Guests`; otherwise `Basic Users`." Use `--dry-run` to preview without writing.

Three scenarios where this is the right command:

1. **After a migration squash** — squashed migrations don't re-run `RunPython` operations against existing rows, so new installations won't have the original backfill.
2. **Repairing a manual edit** — an admin clicked the wrong group via Django admin (only superusers can; other staff have the field disabled).
3. **Adding the user-kind feature to a deployment that pre-dates it** — the classifier groups didn't exist before and need to be seeded.

## Audit trail

Every group-membership change on `User.groups` lands an audit log entry. The audit module is also Pro-gated; without it the log isn't recorded.

Action codes you'll see:

- `USER_PROMOTED_TO_BASIC` — operator-driven promotion
- `USER_DEMOTED_TO_GUEST` — operator-driven demotion
- `USER_GROUPS_CHANGED` — any other group flip (default classification at signup, manual fix, etc.)

The promote/demote commands suppress the generic `USER_GROUPS_CHANGED` row when they're already recording an intent-specific row, so the audit log has exactly one entry per operator action.
