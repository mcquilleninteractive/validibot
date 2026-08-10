# Doctor Check IDs

When `just self-hosted doctor` (or `just gcp doctor <stage>`) reports
an issue, it includes a stable check ID like `VB101` or `VB401`.
This page maps each ID to its meaning and the recommended fix.

The IDs are organized by category:

| Range | Category |
|---|---|
| `VB0xx` | Settings / configuration |
| `VB1xx` | Database |
| `VB2xx` | Storage |
| `VB3xx` | Docker / containers |
| `VB4xx` | Background tasks / Celery |
| `VB5xx` | Cache |
| `VB6xx` | Email |
| `VB7xx` | Validators |
| `VB8xx` | Site / roles / permissions / initial data |
| `VB9xx` | Network / TLS / signing (Phase 2 work) |

If you see an ID not listed here, it means doctor is reporting from a
newer version of Validibot than this docs page covers. Check the
release notes for that version.

## How to look up a check

Run doctor in JSON mode to see structured output you can grep:

```bash
just self-hosted doctor --json | jq '.checks[] | select(.status != "ok")'
```

Each result has an `id`, `category`, `name`, `status`, `message`,
and (for non-OK results) a `fix_hint`. The check ID matches the
sections below.

---

## VB0xx — Settings and security

### `VB001` DEBUG mode enabled
**Severity:** error in production, warn in DEBUG mode
**Trigger:** `DEBUG=True` in production settings.
**Fix:** Set `DEBUG=False` in `.envs/.production/.self-hosted/.django`.

### `VB002` Weak SECRET_KEY
**Severity:** error in production, warn in DEBUG mode
**Trigger:** `DJANGO_SECRET_KEY` is missing, contains "changeme", or is shorter than 32 characters.
**Fix:** Generate a strong secret with
`python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'`,
then set it in `.envs/.production/.self-hosted/.django` and restart.

### `VB003` ALLOWED_HOSTS misconfigured
**Severity:** error in production
**Trigger:** `DJANGO_ALLOWED_HOSTS` is empty or contains `*`.
**Fix:** Set `DJANGO_ALLOWED_HOSTS=validibot.example.com` (your real
hostname). Comma-separated for multiple hosts.

### `VB004` CSRF_TRUSTED_ORIGINS not set
**Severity:** error in production
**Trigger:** `DJANGO_CSRF_TRUSTED_ORIGINS` is empty in production.
**Fix:** Set `DJANGO_CSRF_TRUSTED_ORIGINS=https://validibot.example.com`.

### `VB005` DJANGO_ADMIN_URL is the default
**Severity:** error in production
**Trigger:** `DJANGO_ADMIN_URL` is still `admin/` in production. Bots
scrape `/admin/` looking for Django sites; randomizing reduces noise.
**Fix:** Generate a random path:
`python -c "import secrets; print(secrets.token_urlsafe(16) + '/')"`
and set `DJANGO_ADMIN_URL=` to that value (keep the trailing slash).

### `VB006` SECURE_SSL_REDIRECT is False
**Severity:** error in production
**Trigger:** `DJANGO_SECURE_SSL_REDIRECT=False` in production.
**Fix:** Set `DJANGO_SECURE_SSL_REDIRECT=True`. If your reverse proxy
already terminates TLS and strips proxy headers, also set
`SECURE_PROXY_SSL_HEADER` so Django trusts the proxy's `X-Forwarded-Proto`.

### `VB007` SESSION_COOKIE_SECURE is False
**Severity:** error in production
**Trigger:** `SESSION_COOKIE_SECURE=False` in production.
**Fix:** Set `SESSION_COOKIE_SECURE=True` and `CSRF_COOKIE_SECURE=True`
together. Both default to True in the production settings module.

### `VB008` API key digest key missing or coupled
**Severity:** error in production
**Trigger:** `DJANGO_API_KEY_DIGEST_KEY` is missing, or it reuses the same value as `DJANGO_SECRET_KEY`.
**Fix:** Generate a separate digest key with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`,
set `DJANGO_API_KEY_DIGEST_KEY=` in `.envs/.production/.self-hosted/.django`,
and restart. Do not reuse `DJANGO_SECRET_KEY`; API-token digest rotation must be
independent from Django session/signing key rotation.

### `VB030` OS version
**Severity:** error in self-hosted production, info for unsupported self-hosted distributions, skipped for local Compose and non-Linux
**Trigger:** Host OS is below the minimum supported version (Ubuntu 22.04 LTS today). Older Ubuntu ships outdated Docker packages and misses the Compose plugin.
**Fix:** Upgrade to Ubuntu 22.04 LTS or 24.04 LTS. If you're running a non-Ubuntu self-hosted distro, the check emits an info-level note — Validibot may work but isn't tested there. Local Compose skips this check because the application container's OS is not the Docker host OS.

---

## VB1xx — Database

### `VB101` Database connection
**Severity:** error if cannot connect
**Trigger:** `SELECT 1` against the database fails.
**Fix:** Verify `.envs/.production/.self-hosted/.postgres` is correct
and the postgres container is healthy:
`just self-hosted status` should show postgres "healthy."

### `VB102` Migrations not applied
**Severity:** warn
**Trigger:** Django migration plan has unapplied migrations.
**Fix:** Run `just self-hosted manage "migrate --noinput"`. The
versioned upgrade recipe runs migrations automatically, so this should
not appear long-term after a successful upgrade.

### `VB103` Cannot check migrations
**Severity:** error
**Trigger:** Django migration framework itself errors (rare — usually
means database is broken).
**Fix:** Investigate the database connection (`VB101`). If the database
is healthy but migrations introspection fails, file a support ticket
with the doctor JSON output.

### `VB120` Postgres version
**Severity:** error in self-hosted production, info on GCP, warn on dev
**Trigger:** Postgres major.minor is below the minimum (currently 14.0).
Older versions miss `pg_dump --load-via-partition-root`, which the
manifested backup workflow relies on.
**Fix:** Upgrade Postgres to 14+ (16+ recommended). Take a full
backup *before* a major-version upgrade — Postgres major upgrades
require `pg_upgrade` or dump/restore.

---

## VB2xx — Storage

### `VB200` Storage check failed
**Severity:** error
**Trigger:** Storage backend introspection threw an exception.
**Fix:** Check `DATA_STORAGE_BACKEND` and related env vars. The default
self-hosted setup uses local filesystem.

### `VB201` GCS storage access failed (cloud only)
**Severity:** error
**Trigger:** GCS bucket cannot be listed.
**Fix:** Verify `STORAGE_BUCKET` and GCP credentials.

### `VB202` Storage directory does not exist
**Severity:** error
**Trigger:** Local filesystem storage location doesn't exist on disk.
**Fix:** `mkdir -p` the path shown in the message and re-run doctor.

### `VB203` Storage directory not writable
**Severity:** error
**Trigger:** Local filesystem storage exists but can't be written.
**Fix:** Check the path printed by doctor from inside the `web` container. In the default Compose stack this is `/app/storage/private`, backed by the `validibot_storage` Docker named volume. Do not recursively chown `/srv/validibot/docker`; if ownership is wrong, fix the mounted volume from inside the container or recreate it from a verified backup.

### `VB204` Storage configured (informational OK)
**Severity:** ok
**Trigger:** Storage backend is configured and accessible.
**Fix:** No action — this is the success case.

### `VB205` Validator storage capability and isolation
**Severity:** ok for local mounts or a fully isolated downscoped-token GCS
runtime; warn while a GCP runtime may still have ambient storage authority;
error for unsupported or internally inconsistent combinations
**Trigger:** Always emitted. Doctor pairs `DATA_STORAGE_BACKEND` with
`VALIDATOR_RUNNER` and reports the effective validator I/O mode separately from
ordinary storage reachability.

The JSON output includes a matching top-level `storage_capability` object. Its
`integrity_enforced` field answers whether the runtime verifies exact immutable
bytes. Its `attempt_scoped_authority` field answers the different question:
whether a compromised validator is prevented from reaching another attempt.

- `local_attempt_mount` + `attempt_scoped`: Docker receives one attempt's input
  mount read-only and output mount read-write. This is the supported
  self-hosted mode.
- `gcs_downscoped_token` + `attempt_scoped`: the validator receives a
  short-lived read/create token limited to one attempt prefix. GCP provisioning
  forbids ambient object permissions for the attached runtime identity, and
  production acceptance proves that provider-side invariant. There is no
  reduced-isolation GCP configuration mode.
- `unsupported`: S3/S3-compatible conditional and version semantics have not
  yet been implemented and capability-tested, or the configured runner/storage
  pair has no verified contract. Doctor fails closed instead of inferring
  safety from a provider label.

**Fix:** For self-hosting, use local data storage with the Docker runner. For
GCP, run `just gcp validator-acceptance <stage> <release>` before enabling
traffic; it proves both the live token boundary and absence of ambient runtime
storage authority. Do not use an S3 or custom storage path for external
validators until its conditional writes, immutable reads, and runtime scope
are implemented and tested.

---

## VB3xx — Docker and containers

### `VB301` Docker not required by configured runner
**Severity:** skipped
**Trigger:** `VALIDATOR_RUNNER` selects a non-Docker execution backend, such
as Google Cloud Run or AWS Batch.
**Fix:** No action. Docker availability is checked only when the configured
runner actually uses Docker.

### `VB302` Docker runner availability
**Severity:** warn
**Trigger:** The configured Docker runner cannot ping Docker Engine through
the Python Docker SDK. This tests the same API path advanced validators use;
the Docker CLI does not need to be installed inside the application container.
**Fix:** Start the container engine and verify the host socket selected by
`VALIDATOR_CONTAINER_SOCKET` is mounted into the worker container at
`/var/run/docker.sock` with usable permissions.

### `VB303` Docker command timeout
**Severity:** reserved
**Trigger:** Retained as a stable historical check ID; the current doctor uses
the configured runner's SDK availability result under `VB302` instead of
invoking `docker info` with a separate timeout.
**Fix:** No current finding emits this ID.

### `VB304` Validator runner initialization error
**Severity:** warn
**Trigger:** Doctor cannot import or instantiate the runner selected by
`VALIDATOR_RUNNER` and `VALIDATOR_RUNNER_OPTIONS`.
**Fix:** Correct the runner setting or its options and restart the application.

### `VB310` Validator images present
**Severity:** ok
**Trigger:** At least one validator image is in the local Docker image
list.
**Fix:** No action — this is the success case.

### `VB311` Validator images not built (informational)
**Severity:** skipped (only emitted with `--verbose`)
**Trigger:** Some validator images aren't built locally.
**Fix:** From the `validibot-validator-backends` repo:
`just build energyplus fmu`. Optional unless those validators are in
use.

### `VB320` Docker version
**Severity:** error for an old self-hosted Docker Engine, info when Podman is
detected, skipped when the local engine is not applicable
**Trigger:** The Docker-compatible engine version reported through the same
Python SDK connection used by the worker is below the minimum (currently
24.0). Podman's independent release series is identified but not compared
numerically. The application image does not need the Docker CLI.
**Fix:** Upgrade Docker to 24+ via the official Docker repository
(NOT the OS package manager — those tend to lag and miss the
Compose plugin). For Podman, run the advanced-validator acceptance suite
against the exact installed release.

### `VB321` Docker installation source
**Severity:** warn
**Trigger:** Docker is installed from the Ubuntu snap (binary at
`/snap/bin/docker`). Snap-installed Docker has compatibility issues
with Compose named volumes (the snap sandbox confines `/var/lib/docker`)
and BuildKit secrets.
**Fix:** Reinstall Docker from the official Docker repository:
[https://docs.docker.com/engine/install/ubuntu/](https://docs.docker.com/engine/install/ubuntu/).

### `VB322` Rootless container engine
**Severity:** ok when rootless, info when rootful, warn when status cannot be
queried
**Trigger:** Doctor inspects the daemon information returned through the
worker's Docker-compatible API socket.
**Fix:** Rootful is supported for operator-reviewed validator backends only as
an explicit compatibility choice. Fresh installs default to rootless Docker.
Confirm `VALIDATOR_CONTAINER_SOCKET` resolves to the deployment user's
`$XDG_RUNTIME_DIR/docker.sock`, redeploy, and rerun doctor. The result must
change to `ok`.

---

## VB4xx — Background tasks (Celery) and backups

### `VB401` Celery broker
**Severity:** error if cannot connect; skipped if not configured
**Trigger:** Cannot reach the Redis broker.
**Fix:** Verify `REDIS_URL` and `just self-hosted status` shows redis
healthy.

This check is skipped on GCP, where Cloud Tasks replaces Celery delivery.

### `VB402` Celery Beat schedules
**Severity:** warn if no periodic tasks
**Trigger:** No `PeriodicTask` entries in the database.
**Fix:** Run `just self-hosted manage "setup_validibot"` to seed the
default scheduled tasks.

This check is skipped on GCP, where Cloud Scheduler replaces Celery Beat.

### `VB403` Celery Beat not installed
**Severity:** skipped
**Trigger:** `django_celery_beat` is missing from the environment.
**Fix:** Install it via the project's dependency manager. Should never
appear on a normal install.

### `VB411` Restore test
**Severity:** warn if missing or stale; skipped for local Compose and GCP
**Trigger:** No `.last-restore-test` marker file in `DATA_STORAGE_ROOT`,
or the marker is older than 90 days. The marker is written by the
restore recipe after a successful restore drill.
**Fix:** Run a restore drill on a clean test environment:
`just self-hosted backup` followed by `just self-hosted restore <backup-path>`
and then `just self-hosted doctor` plus `just self-hosted smoke-test`.
Principle: a backup that has never been restored is not considered valid.
Disposable local Compose data does not require a restore drill. GCP restore
drills are recorded in the hosted operator runbook because Cloud Run has no
durable local data root on which this self-hosted marker could be stored.

---

## VB5xx — Cache

### `VB501` Cache connection
**Severity:** error if cannot connect
**Trigger:** Cache backend (Redis) is unreachable, or set/get test
fails.
**Fix:** Same as `VB401` — verify Redis is running and `REDIS_URL` is
set.

### `VB502` Cache read/write
**Severity:** error
**Trigger:** Cache backend is reachable but the round-trip
write/read/match test fails.
**Fix:** Investigate cache backend internals — usually means a Redis
configuration issue or a key collision.

---

## VB6xx — Email

### `VB601` Development email backend in use
**Severity:** warn in production
**Trigger:** Email backend is `console` or `dummy` — emails won't
actually be sent.
**Fix:** Configure a real email provider (Postmark, Mailgun, SendGrid,
or SMTP). See `.envs.example/.production/.self-hosted/.django` group 5.

### `VB602` EMAIL_HOST not configured
**Severity:** error
**Trigger:** SMTP backend is set but `EMAIL_HOST` is empty.
**Fix:** Set `EMAIL_HOST=smtp.example.com` in your `.django` file.

### `VB603` SMTP server unreachable
**Severity:** warn
**Trigger:** SMTP host is set but port-level reachability test failed.
**Fix:** Check `EMAIL_HOST`, `EMAIL_PORT`, and firewall rules. Some
hosts block outbound SMTP — switch to an HTTPS-based provider
(Postmark, Mailgun, SendGrid).

### `VB604` Email backend OK
**Severity:** ok
**Trigger:** Non-SMTP backend is configured (e.g. Postmark API).
**Fix:** No action.

---

## VB7xx — Validators

### `VB701` System validators
**Severity:** error if zero, ok otherwise
**Trigger:** No validators with `is_system=True` in the database.
**Fix:** Run `just self-hosted manage "setup_validibot"` to seed the
default validators.

### `VB702` Enabled validators
**Severity:** warn if zero
**Trigger:** No validators have `is_enabled=True`.
**Fix:** Either run `setup_validibot` (seeds enabled defaults) or
manually enable validators in Django admin.

### `VB711` Validator backend image policy (invalid value)
**Severity:** error
**Trigger:** `VALIDATOR_BACKEND_IMAGE_POLICY` is set to something other
than `tag`, `digest`, or `signed-digest` on a community/self-hosted deployment.
**Fix:** Set `VALIDATOR_BACKEND_IMAGE_POLICY` to one of `tag`, `digest`,
or `signed-digest`, or leave it empty for the `tag` default.

### `VB712` Validator backend image policy (status)
**Severity:** ok for `digest`/`signed-digest`; warn when the policy is
`tag` on a production target (info on self-hosted quick-start targets)
**Trigger:** The deployment allows floating image tags for validator
backend containers, which weakens reproducibility in production.
**Fix:** Set `VALIDATOR_BACKEND_IMAGE_POLICY=digest` for production deployments
and pin validator backend images via `@sha256:<hex>` digests in the deployment
config. The GCP production template selects `digest` while preserving the same
three policy choices.

### `VB713` Signed-digest policy without cosign verification
**Severity:** error
**Trigger:** `VALIDATOR_BACKEND_IMAGE_POLICY=signed-digest` but
`COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES` is `False` — every validator
launch will be refused.
**Fix:** Set `COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True` and configure
`COSIGN_VERIFY_PUBLIC_KEY_PATH`, or relax the policy to `digest`.

### `VB720` Validator provider queue
**Severity:** skipped outside GCP; warn while Jobs remain primary; error when a
Service is primary without complete configuration
**Trigger:** The separate long-delivery queue or its dedicated Service invoker
identity is missing.
**Fix:** Set `GCP_VALIDATOR_TASK_QUEUE_NAME` and
`GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT`, upload the Django environment,
then run `just gcp init-stage <stage>` to reconcile queue and IAM policy.

### `VB721` Validator deployment routes
**Severity:** ok or error
**Trigger:** A release-enabled managed validator has no ready, unblocked primary
route, or a Service primary lacks its retained ready long-running Job route.
**Fix:** Run `just gcp validator-deployments-sync <stage>`, inspect
`just gcp validator-deployments-list <stage>`, and activate Services only after
both routes are ready.

### `VB722` Validator callback identities
**Severity:** ok or error
**Trigger:** An active deployment's recorded runtime service account is absent
from `TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS`.
**Fix:** Add every active validator runtime SA to the worker allowlist, upload
the updated secret, and redeploy the worker. Do not add the provider-task
invoker: it invokes validator Services, not the worker callback endpoint.

### `VB723` Validator Service capacity
**Severity:** info while Jobs are primary; ok once Services are primary
**Trigger:** Always reported on GCP. It projects the registered minimum,
maximum, and concurrency policy for active Service routes.
**Fix:** No action for info when the cost policy is scale-to-zero. Keep the
validator Service at minimum zero and use the retained acceptance report to
explain cold-start latency. Any decision to warm a Service must be an explicit,
separately reviewed capacity change rather than part of routine deployment.

---

## VB8xx — Site, roles, permissions

### `VB800` Site configuration
**Severity:** error
**Trigger:** Django Site object with `SITE_ID` doesn't exist.
**Fix:** Run `setup_validibot` (creates the default Site).

### `VB801` Site domain
**Severity:** warn if default value (`example.com` or `localhost`)
**Trigger:** Site domain hasn't been set to a real hostname.
**Fix:** Run `just self-hosted manage "setup_validibot --domain validibot.example.com"`.

### `VB802` Site name (informational)
**Severity:** ok
**Trigger:** Site name is set.
**Fix:** No action.

### `VB810` Roles
**Severity:** error if missing roles
**Trigger:** Some `RoleCode` values aren't represented in the database.
**Fix:** Run `setup_validibot`. With `--fix` flag, doctor will create
them automatically.

### `VB811` Permissions
**Severity:** error if missing
**Trigger:** Some custom permissions aren't in the auth_permission table.
**Fix:** Run `setup_validibot`.

---

## VB000 — Generic / internal

### `VB000` (internal)
**Severity:** error
**Trigger:** A check function threw an unexpected exception. Doctor
catches these so the rest of the run continues, but the failing
check's findings are lost.
**Fix:** This is a doctor bug. Capture the doctor JSON and the
container logs (`just self-hosted logs-service web`), then file a
support ticket with `just self-hosted collect-support-bundle`.

---

## VB9xx — Network / DigitalOcean provider overlay

These checks emit only when `--provider digitalocean` is passed
(typically by `just self-hosted doctor --provider digitalocean`).

### `VB910` DigitalOcean DNS
**Severity:** error if hostname doesn't resolve, info otherwise
**Trigger:** Doctor reads `SITE_URL`, extracts the hostname, and
resolves it via `socket.gethostbyname`. If resolution fails, this is
an error. If resolution succeeds, the result is informational —
doctor cannot verify the resolved IP matches THIS host's public IP
without making outbound calls (which violate the telemetry-off
principle). For full DNS-vs-host comparison, run `just self-hosted check-dns`
from the host shell.
**Fix:** Add a DNS A-record for `SITE_URL`'s hostname pointing at the
Droplet's public IPv4 or the Reserved IP assigned to the Droplet. Wait
for propagation, then re-run. If you use a Reserved IP, `check-dns`
may need a manual confirmation because outbound traffic can still report
the Droplet's primary public IPv4.

### `VB911` DigitalOcean volume mount
**Severity:** warn if `/srv/validibot` exists but isn't a mount point
**Trigger:** Doctor checks the historical host-mounted layout where
`DATA_STORAGE_ROOT` was under `/srv/validibot`. In the current default
Compose layout, `DATA_STORAGE_ROOT` is `/app/storage/private` inside
the container and persistence depends on Docker's data root being on
the attached Volume.
**Fix:** Create a DigitalOcean block-storage volume, attach it to
the Droplet, mount it at `/srv/validibot`, and configure Docker's data
root as `/srv/validibot/docker` before the first Compose run. Verify
with `docker info --format '{{ .DockerRootDir }}'`. See the
[DigitalOcean tutorial](providers/digitalocean.md) step 2 and step 6
for the exact commands.

### `VB912` DigitalOcean monitoring agent
**Severity:** info (always)
**Trigger:** Doctor checks for `/opt/digitalocean/bin/do-agent`.
Reports whether the optional DO monitoring agent is installed.
**Fix:** No fix required — this is purely informational. Some
operators choose not to install the agent for telemetry reasons;
others prefer the host metrics it provides. Either is fine.

### `VB913` DigitalOcean Cloud Firewall reminder
**Severity:** info (always)
**Trigger:** Doctor surfaces a reminder that Cloud Firewall rules
cannot be verified from inside the Droplet (the security model
keeps the DO API token off the production server).
**Fix:** From your operator workstation: run
`doctl compute firewall list` and verify the rules. The recommended
setup allows 22/tcp from operator IPs, 80/tcp + 443/tcp from
internet, deny everything else inbound.

---

## Reserved ranges

These IDs are reserved for upcoming check categories and aren't
emitted yet:

- `VB4xx` (above 411) — future backup checks beyond the restore-test
  marker, such as backup destination reachability, last-backup-age, and
  off-host replication status
- `VB9xx` (above the DO overlay) — Other provider overlays (AWS EC2,
  Hetzner, on-prem) when those guides ship
- `VB1000+` — upgrade pre-flight checks and signing / JWKS /
  TLS-cert checks for Pro signed credentials

When those ship, the new IDs will be added to this page in the
release notes.

---

## Related docs

- [Self-hosting overview](overview.md)
- [DigitalOcean tutorial](providers/digitalocean.md)
