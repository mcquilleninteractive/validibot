# User Roles and Permissions

Validibot implements a comprehensive role-based access control (RBAC) system that governs how users can interact with workflows, submissions, and validation runs within their organizations.

## Overview

The permission system is built around three core concepts:

1. **Organizations**: Top-level containers that provide isolation and context
2. **Memberships**: The relationship between users and organizations
3. **Roles**: Sets of permissions that define what actions users can perform

Users can belong to multiple organizations with different roles in each, providing flexible access management across teams and projects.

## Role Catalogue

Validibot defines six organization-scoped roles. Permissions are cumulative unless otherwise noted, and organization management actions currently hinge on the **Admin** role. Use the table below as a quick reference.

| Role | Code | Primary scope | Key capabilities |
| ---- | ---- | ------------- | ---------------- |
| Owner | `OWNER` | Strategic control | Signals ultimate responsibility for the organization and automatically inherits every other role. |
| Admin | `ADMIN` | Operational management | Manage organization settings, members, and lifecycle (including deletion safeguards). |
| Author | `AUTHOR` | Workflow design | Create, edit, and retire workflows across the organization. |
| Executor | `EXECUTOR` | Validation operations | Launch runs and inspect detailed results for their own runs. |
| Analytics Viewer | `ANALYTICS_VIEWER` | Analytics review | Read-only access to analytics dashboards and reports. |
| Results Viewer | `VALIDATION_RESULTS_VIEWER` | Validation review | Read-only access to validation run details and findings across the current organization. |
| Workflow Viewer | `WORKFLOW_VIEWER` | Transparency | Read-only access to workflows and public details (no validation results access). |

Roles are cumulative: `OWNER` inherits every other role, and `ADMIN` picks up the same permissions as Author/Executor/Viewer through the permission map even if those role rows are not explicitly stored. Stack roles only when the UI needs a specific combination (for example, pair `EXECUTOR` with `VALIDATION_RESULTS_VIEWER` so operators can also review results).
The role picker mirrors these implications: selecting `ADMIN` auto-selects Author/Executor/Analytics Viewer/Validation Results Viewer/Workflow Viewer, selecting `AUTHOR` auto-selects Executor/Analytics Viewer/Validation Results Viewer/Workflow Viewer, and selecting `EXECUTOR` auto-selects Workflow Viewer. Uncheck the higher role to fine-tune lower roles.

Hybrid model in practice

- Cumulative supersets: `OWNER` and `ADMIN` exist so “full access” is one assignment and the permission map stays simple.
- Composable lower roles: `AUTHOR`, `EXECUTOR`, `ANALYTICS_VIEWER`, `VALIDATION_RESULTS_VIEWER`, and `WORKFLOW_VIEWER` remain mix-and-match (executor + results reviewer; analytics-only; workflow viewer only).
- UI pattern: checkboxes with implied roles pre-checked/disabled let us cover both use cases. Radio buttons would block the composable mixes we rely on. The form shows implied roles checked and disabled when you select a cumulative role, similar to GitHub/Discourse RBAC UIs.

## Permission Codes

Permissions are exposed as Django-style codenames (see `users.constants.PermissionCode`) and enforced through the `OrgPermissionBackend`, so code calls `user.has_perm(code, obj_with_org)` instead of inspecting roles directly. An “obj_with_org” is any object that carries organization context—typically a model instance with an `org` or `org_id` field. Examples include `Workflow`, `ValidationRun`, `Organization`, and even `Membership` when needed. Think of it as “an object the permission backend can use to figure out which org this action targets.”

- `workflow_launch`: `OWNER`, `ADMIN`, `EXECUTOR`
- `workflow_view`: `OWNER`, `ADMIN`, `AUTHOR`, `EXECUTOR`, `VALIDATION_RESULTS_VIEWER`, `WORKFLOW_VIEWER`
- `workflow_edit`: `OWNER`, `ADMIN`, `AUTHOR` (the `_edit` suffix covers both create and edit)
- `validation_results_view_all`: `OWNER`, `ADMIN`, `AUTHOR`, `VALIDATION_RESULTS_VIEWER`
- `validation_results_view_own`: `OWNER`, `ADMIN`, `AUTHOR`, `VALIDATION_RESULTS_VIEWER`, `EXECUTOR` (always true for the run’s owner)
- `validator_view`: `OWNER`, `ADMIN`, `AUTHOR`
- `validator_edit`: `OWNER`, `ADMIN`, `AUTHOR` (create and edit)
- `analytics_view`: `OWNER`, `ADMIN`, `AUTHOR`, `ANALYTICS_VIEWER`
- `analytics_review`: `OWNER`, `ADMIN`, `AUTHOR`, `ANALYTICS_VIEWER` (for approving/annotating analytics outputs)
- `admin_manage_org`: `OWNER`, `ADMIN`

Enforcement pattern: call `user.has_perm(PermissionCode.<code>.value, obj_with_org)` and let the backend map roles to permissions and handle object scoping (including “own” semantics).

For clarity: `_edit` permissions include create and edit flows; `analytics_*` cover dashboards and approvals; `validator_*` govern access to the Validator Library and custom validator CRUD. Resource file management (upload, edit, delete) uses `admin_manage_org` rather than `validator_edit`, since resource files consume storage and are shared org resources.

Only Owners, Admins, Authors, and Results Viewers can open validation results. Executors without those roles only see the runs they launched. Workflow Viewers without additional roles see a compact menu limited to Workflows and Validation Runs.

### Integrity-only role checks

Direct role lookups remain for a few data-integrity safeguards (not authorization):

- Prevent removing the last `OWNER` or last `ADMIN` from an organization.
- Block removing yourself from an organization.
- Enforce single-owner transfer rules.

All request-level access control should use `user.has_perm(PermissionCode.<code>.value, obj_with_org)` so behavior stays consistent across UI and API.

### Owner (`OWNER`)

**What it represents:** The accountable owner of the organization (billing contact, contractual responsibility).

**Permissions in practice:**

- Automatically inherits every other role (Admin, Author, Executor, Results Viewer, Workflow Viewer) so the UI surfaces all related controls.
- Primary contact for billing, contractual, and audit workflows; doubles as the escalation path for support.

**Typical holders:** Executive sponsor, procurement lead, customer of record.

**Notes:**

- Exactly one member holds the Owner role at any time; transfers require platform support and automatically demote the previous owner.
- Personal workspaces create a membership flagged as Owner which immediately unlocks every other role.
- Owner memberships cannot be removed or reassigned within the UI to protect billing continuity.
- Comparable platforms follow the same cumulative pattern: GitHub org Owners and Discourse admins automatically carry every lower privilege. We mirror that to keep expectations simple.

### Admin (`ADMIN`)

**What it represents:** Operational administrator empowered to configure the organization day to day.

**Permissions:**

- All Author, Executor, Results Viewer, and Workflow Viewer permissions (if those roles are also assigned).
- Access to organization management UI/API (`OrganizationAdminRequiredMixin` checks this role explicitly).
- Invite or remove members, edit organization metadata, and reassign roles.
- Delete non-personal organizations (guarded so at least one other active admin remains).
- Switch active organization context and seed new organizations via the UI.

**Typical holders:** Team lead, platform administrator, senior engineer responsible for tooling.

**Notes:**

- Keep at least two admins per organization; the deletion flow enforces this.
- Grant Admin to any Owner who needs to manage people or settings.
- Access to the Dashboard and Validator Library is included via the admin privilege set.

### Author (`AUTHOR`)

**What it represents:** Designer and maintainer of validation workflows.

**Permissions:**

- Configure workflows: create, edit, clone, lock, and delete steps.
- Manage workflow-level access controls.
- See run histories and operational metrics.

**Typical holders:** Validation engineers, quality specialists, workflow authors.

**Notes:**

- Authors cannot change organization membership or settings unless they are also Admins.
- Workflow UI treats Owner, Admin, and Author memberships as managers (`WorkflowAccessMixin.manager_role_codes`).
- Authors automatically see the Dashboard, Designer nav sections, and the Validator Library.

### Executor (`EXECUTOR`)

**What it represents:** Operator who runs validations and investigates outcomes.

**Permissions:**

- Start validation runs (UI and API).
- Upload submissions and metadata.
- Inspect detailed run results, logs, and generated artifacts.
- Re-run eligible workflows.

**Typical holders:** Application developers, QA engineers, CI/CD service accounts.

**Notes:**

- Minimum role required for active validation work (`Workflow.can_execute` checks this role specifically).
- Executors cannot alter workflow configuration or organization settings.
- Without an Author/Admin/Owner/Results Viewer role, Executors only see the Workflows and Validation Runs links in the app navigation and only the runs they launched.

### Analytics Viewer (`ANALYTICS_VIEWER`)

**What it represents:** Read-only consumer of analytics dashboards and reports.

**Permissions:**

- View analytics dashboards (no edit or approval capabilities).
- No implicit access to validation results unless paired with `VALIDATION_RESULTS_VIEWER`.

**Typical holders:** Product stakeholders, analytics reviewers, leadership needing dashboards without edit/run access.

**Notes:**

- Authors/Admins/Owners automatically imply Analytics Viewer via role implications.
- Combine with `VALIDATION_RESULTS_VIEWER` when a user needs both analytics and validation findings; combine with `EXECUTOR` for operators who also need dashboards.

### Results Viewer (`VALIDATION_RESULTS_VIEWER`)

**What it represents:** Read-only reviewer focused on validation outcomes.

**Permissions:**

- Browse validation runs and inspect findings across the current organization.
- No ability to launch workflows or edit configuration.

**Typical holders:** Product stakeholders, compliance reviewers, QA leads.

**Notes:**

- Pair with Executor when a user needs to both launch and review runs.
- Does not grant workflow editing rights; combine with Author/Admin as needed.

### Workflow Viewer (`WORKFLOW_VIEWER`)

**What it represents:** Read-only participant who needs visibility without edit rights.

**Permissions:**

- Browse workflow catalog and step configuration.
- Review public workflow info pages when permitted.

**Typical holders:** Product stakeholders, compliance reviewers, onboarding teammates.

**Notes:**

- Default role for new invitees when no role is specified.
- Cannot run workflows, upload content, or view detailed validation results unless combined with Results Viewer.
- Nav is restricted to Workflows and Validation Runs unless the Workflow Viewer is also an Author, Admin, or Owner.

## Organization Model

### Organization Types

**Regular Organizations**

- Multi-user workspaces for teams and companies
- Managed by Admins (often also Owners) who can invite other users
- Support all role types and collaborative workflows
- Can have custom billing and usage limits

**Personal Organizations**

- Single-user workspaces automatically created for each user
- User is automatically assigned Owner, Admin, and Executor roles (Owner already implies the others; we keep the explicit rows so the UI matches invited-member defaults and any legacy integrity checks stay simple)
- Provides a private space for personal validation work
- Can be used for prototyping before sharing with teams

### Membership Management

Users join organizations through:

1. **Invitation**: Existing Admins (often also Owners) invite users via email
2. **Automatic Creation**: Personal organizations are created automatically
3. **Self-Service**: Organizations can enable open registration (optional)

Each membership tracks:

- **Join Date**: When the user became a member
- **Active Status**: Whether the membership is currently active
- **Role History**: Audit trail of role changes over time

Only members with the `ADMIN` role (Owners automatically satisfy this requirement) can perform invitations, role changes, or other organization-level management tasks in the UI and API. The UI does not permit assigning or removing the Owner role; ownership transfers remain a support task and continue to demote the previous Owner automatically to keep the single-owner rule intact.

## Workflow Access Control

Beyond organization-level roles, Validibot provides workflow-specific access control:

### Workflow Visibility

By default, workflows inherit organization-level permissions:

- **Owners**: See all workflows in the organization
- **Admins**: See all workflows in the organization
- **Authors**: See all workflows in the organization
- **Executors**: See workflows they have access to execute
- **Workflow Viewers**: See workflows they have permission to view

### Fine-Grained Workflow Permissions

Authors can configure additional access restrictions:

**Role-Based Workflow Access**

- Restrict workflow access to specific roles within the organization
- Example: Limit sensitive financial validation workflows to Owners only
- Implemented via `WorkflowRoleAccess` model

**Future Enhancements** (Planned)

- User-specific workflow permissions
- Project-based access control
- Time-limited access grants

## Permission Enforcement

### API Level

All REST API endpoints enforce authentication and role-based permissions:

```python
# Example: Starting a validation requires EXECUTOR role
def can_execute(self, *, user: User) -> bool:
    """Check if user can execute this workflow."""
    return (
        Workflow.objects.for_user(user, required_role_code=RoleCode.EXECUTOR)
        .filter(pk=self.pk)
        .exists()
    )
```

### Database Level

Queries are automatically filtered based on user permissions:

```python
# Users only see workflows they have access to
def get_queryset(self):
    return Workflow.objects.for_user(self.request.user)
```

### UI Level

Templates and forms adapt based on user roles:

- Admins (often also Owners) see organization management options
- Authors see workflow creation buttons
- Executors see validation execution controls
- Workflow Viewers see read-only interfaces

## Security Features

### Multi-Tenancy Isolation

- Organizations provide complete data isolation
- Users cannot access resources outside their member organizations
- Queries are automatically scoped to prevent data leakage

### Audit Logging

- All role changes are logged with timestamps and actors
- Workflow access attempts are tracked
- Failed permission checks generate security events

### Session Management

- Users can switch between organizations they belong to
- Current organization context affects all operations
- Session state tracks active organization for security

## Common Use Cases

### Enterprise Team Structure

```
Organization: "Acme Corp Data Team"
├── Owner: Alice (Head of Data)
├── Authors: Bob (Senior Engineer), Carol (Data Architect)
├── Executors: Dave (Developer), Eve (QA Engineer)
└── Workflow Viewers: Frank (Product Manager), Grace (Compliance)
```

### Multi-Project Environment

```
User: John Smith
├── Personal Org: "John's Workspace" (Owner)
├── Work Org: "Tech Corp" (Executor)
└── Client Org: "Customer Inc" (Workflow Viewer)
```

### API Integration Pattern

```
Service Account: "production-validator"
└── Role: Executor in "Production Data Org"
    └── Purpose: Automated validation in CI/CD pipeline
```

## Role Assignment Best Practices

### Principle of Least Privilege

- Start with Workflow Viewer role for new users
- Promote to higher roles as responsibilities increase
- Regularly audit role assignments for appropriateness
- Owners automatically receive every other role, so no additional pairing is required

### Separation of Duties

- Authors design workflows but may not execute them in production
- Executors run validations but cannot modify workflow logic
- Workflow or Results Viewers provide oversight without operational access

### Emergency Access

- Maintain multiple Admins per organization; ownership stays unique and transfers happen through support
- Document emergency access procedures
- Consider temporary role escalation for incident response

## Future Enhancements

### Planned Features

- **Custom Roles**: Organization-defined roles with granular permissions
- **Conditional Access**: Time-based and IP-based access restrictions
- **Project-Level Permissions**: Sub-organization permission scoping
- **Advanced Audit**: Enhanced logging and compliance reporting
- **Single Sign-On**: Integration with enterprise identity providers
- **API Keys**: Service account management with scoped permissions

### Integration Points

- **External Identity Providers**: SAML, OAuth, LDAP integration
- **Workflow Automation**: Role-based workflow triggers and notifications
- **Compliance Frameworks**: SOC 2, GDPR, HIPAA compliance features

## Troubleshooting Common Issues

### "Permission Denied" Errors

1. Verify user has active membership in the organization
2. Check if user has the required role for the operation
3. Confirm the workflow allows access for the user's role
4. Ensure user is operating in the correct organization context

### Role Assignment Problems

1. Confirm the acting user holds the `ADMIN` role—Owners automatically satisfy this requirement but cannot alter their own ownership status through the UI.
2. The UI blocks removing the final Admin; promote another member before demoting the last admin.
3. New role grants may require the user to reselect the organization or refresh the session.
4. Double-check invitations include every role the user needs (Owners are excluded from invitations and already include all other roles).

### Access Control Debugging

```python
# Check user's roles in an organization
membership = user.membership_for_current_org()
roles = membership.roles.all() if membership else []

# Verify workflow access
can_access = workflow.can_execute(user=user)

# Debug organization membership
orgs_with_roles = [
    (membership.org.name, list(membership.roles.values_list("code", flat=True)))
    for membership in user.memberships.filter(is_active=True)
]
```

This role-based access control system provides Validibot with enterprise-grade security while maintaining the flexibility needed for diverse organizational structures and workflows.
