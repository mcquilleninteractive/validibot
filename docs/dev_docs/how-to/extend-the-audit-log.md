# How to extend the audit log

The audit log (``validibot/audit/``) is an append-only record of
configuration changes, security events, and admin actions. This
page covers the moves a developer makes day-to-day:

* adding a new action code
* extending the field whitelist for an existing model
* adding audit capture for a new model
* writing an ad-hoc entry from service code

It does *not* cover the cross-cutting architecture — that's the
four-pillar observability taxonomy that the audit log is one Pillar of.

## Mental model

Every audit entry has four moving parts:

* An ``AuditAction`` code (think ``WORKFLOW_UPDATED``, ``LOGIN_FAILED``).
* An ``AuditActor`` row (who, with IP / user-agent; purgeable for GDPR).
* A target (``target_type`` label + ``target_id`` string + human
  ``target_repr``).
* An optional whitelisted ``changes`` diff + free-form ``metadata``.

The write path is always ``AuditLogService.record(...)``. Capture
happens via three layers — direct service calls, signal handlers, and
the admin LogEntry bridge — but they all converge on the service.

## Add a new action code

Actions are a ``TextChoices`` enum in
``validibot/audit/constants.py::AuditAction``. Adding one is a
one-line edit plus a retention-window entry if the new action needs a
different default than the generic "config change" tier.

```python
# validibot/audit/constants.py
class AuditAction(TextChoices):
    # ... existing actions ...
    WEBHOOK_INVOKED = "webhook_invoked", _("Webhook Invoked")


# Optional — only if the default 2-year cold retention is wrong:
RETENTION_COLD_DAYS: dict[AuditAction, int] = {
    # ... existing ...
    AuditAction.WEBHOOK_INVOKED: 365,  # 1 year cold, like LOGIN_*
}
```

No migration is required — ``TextChoices`` stores the raw string in
the ``action`` column. Existing entries keep working.

## Extend the whitelist for an existing model

``AUDITABLE_FIELDS`` in ``constants.py`` declares which columns the
audit layer is allowed to snapshot into ``changes``. Anything outside
the list is either silently ignored (the snapshot helper reads only
whitelisted attributes) or recorded as ``<redacted>`` (the service's
sanitiser when a caller passes a non-whitelisted field explicitly).

```python
AUDITABLE_FIELDS = {
    "workflows.Workflow": (
        "name",
        "description",
        "workflow_visibility",
        "mcp_enabled",
        "x402_enabled",
        "new_field_to_audit",  # ← add here
    ),
    # ...
}
```

The rule of thumb:

* **Add** a field if it's config-level and a change is forensically
  interesting (workflow name, API-key scopes, org role).
* **Don't add** fields that carry PII beyond what the actor layer
  already covers (email/name), never add secrets (tokens, passwords,
  webhook-signing keys), never add customer content (validation
  payloads, file uploads).

## Audit a new model

The model-audit registry in ``validibot/audit/model_audit.py`` drives
pre_save / post_save / pre_delete dispatch. Registering a model:

```python
# validibot/audit/model_audit.py, register_builtin_model_audits():
from validibot.notifications.models import WebhookEndpoint

model_audit_registry.register(
    WebhookEndpoint,
    create=AuditAction.WEBHOOK_CREATED,
    update=AuditAction.WEBHOOK_UPDATED,
    delete=AuditAction.WEBHOOK_DELETED,
)
```

Then add an entry to ``AUDITABLE_FIELDS`` for the new model's
``_meta.label`` so the diff capture knows which fields to snapshot.

That's it. The generic pre_save / post_save / pre_delete receivers
already attached by ``AuditConfig.ready()`` pick up the new
registration. No per-model signal wiring needed.

### Registering only some events

A model that should only audit certain lifecycle events can pass
``None`` for the rest. For example, ``Membership`` audits updates
only (join/leave is captured via dedicated invite/removal hooks):

```python
model_audit_registry.register(
    Membership,
    update=AuditAction.MEMBER_ROLE_CHANGED,
)
```

## Write an entry from service code

When a signal doesn't fit (e.g. a business rule triggers the event,
not a DB row change), call the service directly:

```python
from validibot.audit.constants import AuditAction
from validibot.audit.context import get_current_actor_spec
from validibot.audit.context import get_current_request_id
from validibot.audit.services import AuditLogService

AuditLogService.record(
    action=AuditAction.USER_ERASURE_REQUESTED,
    actor=get_current_actor_spec(),
    target=target_user,
    metadata={"requested_via": "admin_ui"},
    request_id=get_current_request_id(),
)
```

``get_current_actor_spec()`` and ``get_current_request_id()`` read the
per-request context installed by ``AuditContextMiddleware``. Outside
a request (Celery task, management command) they return empty
defaults, which is the correct outcome — the entry is attributed to
the system rather than a forged user identity.

## Confirm an entry landed

Three ways:

1. **Django admin** — ``/admin/audit/auditlogentry/``. Read-only;
   searchable by actor email, target id, request id.
2. **Pro UI** — ``/app/audit/`` when the deployment has
   ``AUDIT_LOG``. Org-scoped list + detail + filters +
   CSV / JSONL export (see below).
3. **Shell**:

   ```python
   from validibot.audit.models import AuditLogEntry

   AuditLogEntry.objects.filter(
       action="workflow_updated",
   ).order_by("-occurred_at")[:5]
   ```

## Filters and exports (Pro UI)

The list view at ``/app/audit/`` accepts these GET params, defined
in ``validibot.audit.forms.AuditLogFilterForm``:

| Param | Type | Behaviour |
|---|---|---|
| ``action`` | ``AuditAction`` value | Exact match; unknown values are a form error. |
| ``actor`` | string | ``icontains`` match on both ``actor.email`` (captured at write time) and the live ``actor.user.email``. |
| ``target_type`` | string | Exact match on ``app.Model`` label (e.g. ``workflows.Workflow``). |
| ``date_from`` / ``date_to`` | ``YYYY-MM-DD`` | Start-of-day to end-of-day in the server timezone. Reversed ranges produce a form error. |

Filters stack as logical AND. The same querystring drives the
``/app/audit/export/?format=<csv|jsonl>`` endpoint — the "Export CSV"
and "Export JSONL" buttons on the list page carry the current
filters through so "export current view" does what you'd expect.

**Export format.** Both formats emit the same flat row shape: one
record per ``AuditLogEntry`` with actor fields denormalised into the
row. CSV uses ``json.dumps`` for the two dict-shaped columns
(``changes``, ``metadata``) so a downstream ``pandas.read_csv`` can
round-trip the nested structure. JSONL emits one JSON object per
line, compatible with streaming ``jq .`` and BigQuery ingest.

**Streaming.** Both formats use ``StreamingHttpResponse`` with
``queryset.iterator(chunk_size=500)`` so memory footprint stays
bounded even for multi-year exports.

**Rate limit.** The export endpoint is capped at **10 requests per
hour per organisation**, keyed by org id — the budget is shared
across every admin on the team. Over the limit returns ``429`` with
``Retry-After: 3600``. Rationale: make bulk scraping an unattractive
exfiltration channel while leaving normal filtered exports
unaffected.

## Retention and archival

The audit table grows monotonically — without a retention policy, a
busy org will eventually push it into the "this query is slow now"
bracket. The ``enforce_audit_retention`` management command handles
pruning; a pluggable :class:`~validibot.audit.archive.AuditArchiveBackend`
decides what (if anything) happens to entries before they're deleted.

### How it runs

The command is wired into the scheduled-task registry:

| Registry entry | Celery task name | Schedule | API endpoint |
|---|---|---|---|
| ``enforce-audit-retention`` | ``validibot.enforce_audit_retention`` | ``30 2 * * *`` (daily 02:30) | ``/api/v1/scheduled/enforce-audit-retention/`` |

Running ``setup_validibot`` (or ``just self-hosted bootstrap``)
picks up the registry entry automatically via
``sync_schedules --backend=celery``. No data migration, no manual
``PeriodicTask`` row to create. See
[configure-scheduled-tasks.md](configure-scheduled-tasks.md) for the
full registry contract.

On GCP the same registry entry is reconciled with Cloud Scheduler
(``sync_schedules --backend=cloud-scheduler``), which posts to the
declared API endpoint on the worker service.

### Settings matrix

| Setting | Default | What it controls |
|---|---|---|
| ``AUDIT_HOT_RETENTION_DAYS`` | ``90`` | Rows older than this are candidates for archive + delete. |
| ``AUDIT_RETENTION_ENABLED`` | ``True`` | Kill-switch. When ``False`` the scheduled task becomes a logged no-op — useful during incident investigation. |
| ``AUDIT_ARCHIVE_BACKEND`` | ``"validibot.audit.archive.NullArchiveBackend"`` | Dotted path to the backend class. Must satisfy the :class:`AuditArchiveBackend` protocol. |
| ``AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH`` | ``""`` | Only used by the filesystem backend. Must be set when that backend is configured; the backend constructor raises otherwise. |

All four settings live in ``config/settings/base.py`` near the
audit block and read from env vars so deployments can override
without forking settings.

### What the shipped backends do

| Backend | What it does | Who uses it |
|---|---|---|
| ``NullArchiveBackend`` | Returns a verified receipt naming every input id without writing anything. Retention still prunes the table, but the rows are gone for good. | Community deployments that only want "stop the table from growing". The default. |
| ``FilesystemArchiveBackend`` | Writes ``org_<id>/YYYY/MM/DD.jsonl.gz`` partitions under ``AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH`` with a SHA-256 sidecar. Atomic write (tempfile + fsync + rename). | Self-hosted Pro deployments with a persistent volume. Reference implementation of the contract. |
| ``GCSArchiveBackend`` (commercial add-on) | Same file format as the filesystem backend, written to a CMEK-encrypted GCS bucket; verification re-reads the object and compares SHA-256. Shipped as part of the hosted offering. | The Validibot Cloud deployment. |

The cloud backend is a layer above the community scaffolding — the
retention command doesn't know or care which backend it's driving.

### Verified-upload-before-delete invariant

This is the one contract the command enforces and tests cover
explicitly:

```python
# Simplified pseudocode of the inner loop.
receipt = backend.archive(chunk)  # may raise → command aborts
if receipt.archived_ids:
    AuditLogEntry.objects.filter(pk__in=receipt.archived_ids).delete()
# Rows not in receipt.archived_ids stay in the DB for the next run.
```

A backend that fails to archive row 42 simply omits it from
``receipt.archived_ids`` — row 42 survives and gets retried on the
next scheduled run. A backend that raises aborts the run without
deleting anything; the scheduler's retry picks it up later.

### Writing your own backend

Implement the protocol and point the setting at it. Nothing else
to register:

```python
# my_project/audit_archive.py
from collections.abc import Iterable

from validibot.audit.archive import ArchiveReceipt
from validibot.audit.models import AuditLogEntry


class S3ArchiveBackend:
    """Example: write gzipped JSONL to S3 with SHA-256 verification."""

    def __init__(self) -> None:
        # Read settings / env here — the command instantiates with
        # zero args, so any config must come from Django settings or
        # os.environ.
        ...

    def archive(self, entries: Iterable[AuditLogEntry]) -> ArchiveReceipt:
        materialised = list(entries)
        # 1. Serialise + upload.
        # 2. Re-read + verify checksum.
        # 3. Return a receipt naming only the ids that verified.
        return ArchiveReceipt(
            archived_ids=[e.pk for e in materialised],
            location="s3://audit-archive/...",
            verified=True,
        )
```

```python
# settings.py
AUDIT_ARCHIVE_BACKEND = "my_project.audit_archive.S3ArchiveBackend"
```

Raise on unrecoverable errors; return a partial ``archived_ids``
list for per-row failures. Do **not** return ``verified=False`` —
the command treats that the same as "nothing archived" but it's
a foot-gun because the backend has the only authoritative view of
whether the bytes actually landed.

### CLI flags

The command accepts a few overrides for ad-hoc operator use:

| Flag | Purpose |
|---|---|
| ``--dry-run`` | Count eligible rows and report. No backend call, no delete. Output is prefixed with ``[DRY-RUN]`` so logs are unambiguous. |
| ``--retention-days N`` | Override ``AUDIT_HOT_RETENTION_DAYS`` for one invocation. Useful for a one-off cleanup after an incident (``--retention-days 30`` to free space). |
| ``--chunk-size N`` | Override the default 500-row chunks. Lower for a backend with slow writes; higher for a fast local filesystem. |
| ``--limit N`` | Stop after processing N rows. Testing only — production runs should let the whole eligible set through. |

Manual invocation (host or Docker):

```bash
# Dry-run with the configured backend.
docker compose exec web python manage.py enforce_audit_retention --dry-run

# Ad-hoc narrow window after an incident.
docker compose exec web python manage.py enforce_audit_retention --retention-days 30
```

## Security guardrails — tests you must write

For any new capture point, write at least three tests:

1. **Positive capture** — trigger the event, assert exactly one entry
   appears with the expected ``action``.
2. **Sanitisation** — pass a plausibly dangerous field in ``changes``
   (credential, token, secret URL) and assert it lands as
   ``<redacted>``. The regression guard here prevents OWASP-grade
   credential leaks if someone later widens ``AUDITABLE_FIELDS``
   without thinking.
3. **Actor attribution** — verify the entry's ``actor.user`` is set
   (for authenticated events) or ``None`` (for system / failed-auth
   events), never the wrong user.

See ``validibot/audit/tests/test_signals.py`` for working examples
that follow this pattern.

## Don't do

* **Don't mutate an audit entry post-hoc.** Rows are append-only
  outside the Phase-3 erasure-sanitisation workflow (itself audited
  via ``AUDIT_ENTRY_SANITISED``). If you need to correct something,
  write a new entry describing the correction.
* **Don't use the audit log for product analytics.** Use
  ``validibot/tracking/`` — it's cheaper, retention-tuned for hot
  dashboard queries, and ships in community for every tier.
* **Don't skip the field whitelist.** If a new event's ``changes``
  diff doesn't fit any existing whitelist entry, add a whitelist
  entry for the model. Do not bypass the sanitiser — that's the
  barrier between "structured audit log" and "ad-hoc secret dump".

## See also

* [Audit archive (GCS)](audit-archive-gcs.md) — operational guide for the
  archive backend that complements this how-to.
