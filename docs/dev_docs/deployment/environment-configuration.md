# Environment Configuration

Validibot uses environment files to configure Django settings, database credentials, and deployment-specific options. This page explains the structure and usage of these files.

## Directory Structure

Environment configuration uses a template-based approach:

```
.envs.example/              # Templates (committed to git)
├── README.md               # Quick start guide and variable reference
├── .local/
│   ├── .django             # Django settings for local development
│   ├── .build              # Optional Docker build + shared recipe/runtime knobs
│   ├── .mcp                # Optional MCP-only container env (docker-compose MCP profile)
│   └── .postgres           # Postgres credentials for local development
└── .production/
    ├── .self-hosted/       # Self-hosted (Docker Compose on a VM)
    │   ├── .build          # Docker build args + recipe knobs (Pro/Enterprise, MCP)
    │   ├── .django
    │   ├── .mcp            # MCP container env (when MCP is enabled)
    │   └── .postgres
    ├── .google-cloud/      # Google Cloud Platform deployment
    │   ├── .build          # Deploy-time knobs (MCP API URL, build flags)
    │   ├── .django         # Django runtime settings (uploaded to Secret Manager)
    │   ├── .just           # Just command runner settings (sourced locally)
    │   └── .mcp            # MCP Cloud Run env (uploaded to Secret Manager as mcp-env)
    └── .aws/               # AWS deployment (future)
        └── .django

.envs/                      # Your actual secrets (NOT committed - gitignored)
└── (same structure as above)
```

## Why This Structure?

The separation between `.envs.example/` and `.envs/` serves two purposes:

1. **Templates stay in version control**: The `.envs.example/` folder contains example configurations with placeholder values. These are committed to git so new developers can see what variables are needed.

2. **Secrets stay private**: The `.envs/` folder contains your actual credentials and is gitignored. **NEVER commit this folder to version control, especially public repositories** - it contains passwords, API keys, and other sensitive data that could compromise your deployment if exposed.

This pattern follows the [cookiecutter-django](https://github.com/cookiecutter/cookiecutter-django) convention, which is widely used in the Django community.

## Setup Workflow

### Local Development

1. Create the directory structure:

    ```bash
    mkdir -p .envs/.local
    ```

2. Copy the templates:

    ```bash
    cp .envs.example/.local/.django .envs/.local/.django
    cp .envs.example/.local/.postgres .envs/.local/.postgres
    # Optional for Pro/Enterprise
    cp .envs.example/.local/.build .envs/.local/.build
    ```

3. Edit the files to set real values (especially `SUPERUSER_PASSWORD`).

4. Start Docker Compose:

    ```bash
    just local up
    ```

### Docker Compose Production

1. Create the directory structure:

    ```bash
    mkdir -p .envs/.production/.self-hosted
    ```

2. Copy both template files:

    ```bash
    cp .envs.example/.production/.self-hosted/.django .envs/.production/.self-hosted/.django
    cp .envs.example/.production/.self-hosted/.postgres .envs/.production/.self-hosted/.postgres
    # Optional for Pro/Enterprise
    cp .envs.example/.production/.self-hosted/.build .envs/.production/.self-hosted/.build
    ```

3. Edit with your production values (generate a proper secret key, set your domain, etc.).

4. Validate the env files and bootstrap the deployment:

    ```bash
    just self-hosted check-env
    just self-hosted bootstrap
    ```

### Google Cloud Platform

GCP deployments don't use local `.envs/` files. Instead, secrets are stored in Google Secret Manager and mounted as environment files at runtime.

Start with [Deploy to GCP](deploy-gcp.md) for the high-level path, then use [Google Cloud Deployment](../google_cloud/deployment.md) for the full Cloud Run runbook.

## How Environment Files Work

### The Runtime Two-File Pattern

Each deployment environment uses two files:

| File | Purpose | Example Variables |
|------|---------|-------------------|
| `.postgres` | Database credentials only | `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| `.django` | Everything else | `DJANGO_SECRET_KEY`, `DJANGO_API_KEY_DIGEST_KEY`, `REDIS_URL`, `SITE_URL` |

This separation keeps database credentials isolated and makes it clear which variables configure which service.

### The `.build` file — build, recipe, and shared runtime knobs

The `.build` file plays three roles, all loaded from the same file:

1. **Docker build-time vars.** Passed to `docker compose --env-file` for
   YAML interpolation of `${FOO}` references in the compose files. This
   is where you bake a commercial package into the image:

    - `VALIDIBOT_COMMERCIAL_PACKAGE` — must be an exact version like
      `validibot-pro==0.5.0` or a quoted exact wheel URL on
      `pypi.validibot.com` that includes `#sha256=<hash>`
    - `VALIDIBOT_COMMERCIAL_NETRC` — absolute path to a mode-0600 netrc;
      Compose mounts it only as a BuildKit secret

2. **Recipe/deploy/runtime knobs.** The `just local up` /
   `just local-pro up` / `just local-cloud up` recipes (and the
   production deploy recipes) source this file at the top, so shell-level
   variables drive recipe logic **before** `docker compose` or `gcloud`
   is invoked. The canonical example is `ENABLE_MCP_SERVER`, which
   decides whether to activate the `mcp` Compose profile or MCP Cloud Run
   deploy path. The shared **GCP** MCP URLs live here too: the deploy
   recipe stamps them onto the web and MCP Cloud Run services via
   `--set-env-vars`. (Local stacks don't need them — the MCP URL defaults
   to `http://localhost:8001`.)

    - `ENABLE_MCP_SERVER=true` — include the FastMCP container in the
      stack. Flip to `true` for `local-pro` and `local-cloud`, where
      validibot-pro is installed and satisfies the runtime license
      gate. Ignored by `just local up` because the community compose
      file defines no `mcp` service.

3. **Shared non-secret values stamped onto GCP services.** On GCP the deploy
   recipe stamps a few public values from this file onto the relevant Cloud Run
   services via `--set-env-vars`, so the web and MCP revisions agree on them.
   The MCP URLs are the concrete example: `.build` holds `VALIDIBOT_MCP_BASE_URL`
   and `VALIDIBOT_MCP_API_BASE_URL`, stamped onto both revisions so each side
   agrees on where the MCP server lives. (x402 payment config is **not** among
   these — it moved to `.django` when the MCP server stopped handling payments.
   And `.build` is no longer mounted into any container via `env_file`; local
   stacks read it only at the recipe level.)

These categories are optional — if `.build` is absent, recipes that do not
need it no-op cleanly. For MCP or cloud-local work, the file is worth copying
because it activates MCP and (on GCP) carries the shared MCP URLs.

Pro / Enterprise reminder: installing the wheel via category (1)
gets the package into the image, but Django still needs the app in
`INSTALLED_APPS`. Use `config.settings.local_pro` /
`config.settings.production_pro` settings modules for that.

### The `.just` file — host-side GCP command context

The `.just` file is GCP-only and is sourced into your local shell before running
`just gcp ...`. It contains the information the recipe runner needs before it
can talk to Google Cloud: project ID, deploy region, app name prefix, scheduler
timezone, and Cloud Run timeout.

It is not uploaded to Secret Manager, not mounted into containers, and not
runtime application configuration. If a value must reach Django or MCP at
runtime, it does not belong in `.just`: put service-specific runtime values in
`.django` or `.mcp`, and put non-secret values shared by both services in
`.build` so the recipes can stamp them into both places. Shared secrets are the
exception: keep them in Secret Manager-backed service files until a deliberate
shared-secret workflow exists.

### The `.mcp` file — MCP container env

The MCP server runs in its own container (docker-compose) or Cloud Run
service (GCP) with its own env mount. The `.mcp` file is where its
settings live, separate from `.django` so the MCP image never sees
Django-only secrets (database passwords, Stripe keys, etc.) it doesn't
need. Contains things like `VALIDIBOT_API_BASE_URL` for local/self-hosted
deployments and `VALIDIBOT_OAUTH_CLIENT_SECRET`. The OAuth client secret is a
paired secret: it and `IDP_OIDC_MCP_SERVER_CLIENT_SECRET` in `.django` are two
Secret Manager-backed copies of one generated value, rotated together. It does
**not** carry any x402 payment config — the MCP server no longer handles
payments, so the Coinbase CDP API credentials and the rest of the x402 settings
live in the cloud Django secret (`.django`) instead. The public MCP URLs are
shared across both services and live in `.build`.

The MCP OAuth proxy builds the small provider map it needs from the configured
authorization-server URL and Validibot's stable django-allauth routes. It does
not download `/.well-known/openid-configuration` during process startup. This
keeps maintenance deployments independent of public Django ingress. If a
self-hosted reverse proxy exposes compatible endpoints on different paths,
set `VALIDIBOT_OAUTH_AUTHORIZATION_ENDPOINT`,
`VALIDIBOT_OAUTH_TOKEN_ENDPOINT`, `VALIDIBOT_OAUTH_REVOCATION_ENDPOINT`, and
`VALIDIBOT_OAUTH_JWKS_URL` in `.mcp`; otherwise leave them unset.

On GCP, `just gcp mcp secrets <stage>` uploads this file to Secret
Manager as `mcp-env` and Cloud Run mounts it at `/secrets/.env` on
the MCP service.

### Variable-to-file reference

The quick version of "where does each variable go":

| Variable | File | Why |
|---|---|---|
| `DJANGO_SECRET_KEY`, `DJANGO_API_KEY_DIGEST_KEY`, `DATABASE_URL`, `SITE_URL` | `.django` | Read by Django at process startup |
| `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | `.django` | Hostnames accepted by Django and full HTTPS origins allowed to submit CSRF-protected requests. GCP production must include its public application origin in both forms. |
| `IDP_OIDC_PRIVATE_KEY_B64` | `.django` | Signs JWT access tokens |
| `IDP_OIDC_MCP_SERVER_CLIENT_SECRET` | `.django` | Paired OAuth client secret; Django verifies it when MCP exchanges codes for tokens |
| `VALIDIBOT_MCP_BASE_URL` | `.build` on GCP; `.django`/`.mcp` for local/self-hosted runtime files | Public MCP URL. GCP stamps one `.build` value into both services; local/self-hosted compose still passes it through runtime env files. |
| `MCP_OIDC_AUDIENCE` | `.build` (GCP, via `VALIDIBOT_MCP_API_BASE_URL`) | GCP deploy stamps the same Django API URL onto web as the MCP OIDC audience |
| `MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS` | `.django` | Django allowlists MCP Cloud Run service accounts for MCP → Django identity tokens |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` | `.postgres` | Database credentials, kept isolated |
| `VALIDIBOT_API_BASE_URL` | `.mcp` / `.build` (GCP) | MCP server's target for REST calls; GCP deploy stamps `VALIDIBOT_MCP_API_BASE_URL` into `VALIDIBOT_API_BASE_URL` |
| `VALIDIBOT_OAUTH_CLIENT_SECRET` | `.mcp` | Paired OAuth client secret; same generated value as `IDP_OIDC_MCP_SERVER_CLIENT_SECRET`, stored in the MCP secret file |
| `VALIDIBOT_OAUTH_AUTHORIZATION_SERVER_URL` | `.mcp` | Canonical Django/OIDC issuer base URL; required by the standalone MCP server |
| `VALIDIBOT_OAUTH_AUTHORIZATION_ENDPOINT`, `VALIDIBOT_OAUTH_TOKEN_ENDPOINT`, `VALIDIBOT_OAUTH_REVOCATION_ENDPOINT`, `VALIDIBOT_OAUTH_JWKS_URL` | `.mcp` | Optional complete-URL overrides for non-standard compatible OIDC routing; normal Validibot deployments derive these locally from the issuer URL |
| `VALIDIBOT_COMMERCIAL_PACKAGE`, `VALIDIBOT_COMMERCIAL_NETRC` | `.build` | Exact package reference plus a BuildKit-mounted credential file (Docker Compose only) |
| `ENABLE_MCP_SERVER` | `.build` | Recipe-level knob; decides whether `just gcp deploy-all` and the compose MCP profile activate MCP |
| `DRF_NUM_PROXIES` | `.django` | Trusted-proxy count for client-IP resolution in DRF throttles; must equal the inbound proxy hop count or IP rate-limits can be spoofed (too high) / over-applied (too low). Community default 1; hosted cloud 2 (behind the LB). See [reverse-proxy.md](reverse-proxy.md). |
| `VALIDIBOT_MCP_API_BASE_URL` | `.build` (GCP) | Stamped onto MCP as `VALIDIBOT_API_BASE_URL` and onto Django as `MCP_OIDC_AUDIENCE` |
| `VALIDIBOT_X402_*`, `VALIDIBOT_TEST_X402_*` (enabled, test-mode, network, asset, pay-to, facilitator URL, CDP key id/secret) | `.django` | **All** x402 config. x402 is cloud-only — only validibot-cloud's Django reads it (the MCP server no longer handles payments), so every value lives in the cloud Django secret alongside Stripe/audit. Only the CDP key id/secret are true secrets; the rest are non-secret but kept here so x402 has one authoring file. Not in `.build` and not stamped via `--set-env-vars`. |
| `GCP_PROJECT_ID`, `GCP_REGION` | `.just` (GCP) | Sourced into the shell before running `just gcp` recipes |

Avoid adding a non-secret variable to two files. If Django and MCP both need a
public/runtime value, author it in `.build` and have the recipe inject or stamp
it into both runtimes. If both sides need the same secret, do not move it to
`.build` because `.build` becomes Cloud Run `--set-env-vars`, not Secret
Manager. Keep paired secrets in their service-specific secret files and rotate
them together from one generated value.

### DATABASE_URL Construction

You'll notice that `DATABASE_URL` is not in the example environment files
by default. The entrypoint script constructs it from the individual
`POSTGRES_*` variables when it isn't already set:

```bash
# From compose/production/django/entrypoint.sh
if [ -z "${DATABASE_URL:-}" ] && [ -z "${CLOUD_SQL_CONNECTION_NAME:-}" ]; then
  export DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
fi
```

So for the **bundled Postgres** stack — the one running inside Docker
Compose — `.postgres` is the single source of truth for credentials and
you never duplicate connection details.

If you're pointing at an **external managed database** (DigitalOcean
Managed Postgres, AWS RDS, etc.), set `DATABASE_URL` directly in
`.envs/.production/.self-hosted/.django` with the connection string from
your provider. The entrypoint detects that it's already set and skips
the construction step. Cloud SQL is handled the same way via
`CLOUD_SQL_CONNECTION_NAME` (Unix-socket connection rather than TCP).

### Docker Compose Loading

Docker Compose loads both files via the `env_file` directive:

```yaml
services:
  django:
    env_file:
      - ./.envs/.local/.django
      - ./.envs/.local/.postgres
```

The order matters only if you have duplicate variables (later files override earlier ones).

## Platform-Specific Configuration

### Local Development (Docker Compose)

The local configuration is designed for simplicity:

- Uses Docker service names for hostnames (`postgres`, `redis`)
- Simple default credentials (fine for local dev)
- Console email backend (emails print to terminal)
- Debug mode enabled

Key files:
- `.envs/.local/.django` - Django configuration
- `.envs/.local/.postgres` - Postgres credentials

### Docker Compose Production

Docker Compose deployments run with production-grade settings:

- `DJANGO_SETTINGS_MODULE=config.settings.production`
- `DEPLOYMENT_TARGET=self_hosted`
- Real SSL certificates (via reverse proxy)
- Proper secret key and passwords

The `DEPLOYMENT_TARGET=self_hosted` setting tells Django to:
- Use Celery for background tasks (not Cloud Tasks)
- Use Docker socket for running validator containers
- Use local filesystem or S3/GCS for file storage

### Google Cloud Platform

GCP deployments use completely different infrastructure:

- `DEPLOYMENT_TARGET=gcp`
- Cloud SQL for database (via Unix socket)
- Cloud Tasks for background work
- Private Cloud Run Services for request-shaped validator work, with retained
  Cloud Run Jobs for author-selected long-running attempts and rollback
- GCS for file storage (media + submissions)

Infrastructure is provisioned idempotently via
`just gcp init-stage {dev|staging|prod}`. After it finishes, the
recipe prints the env var values (e.g. `STORAGE_BUCKET`) to paste
into `.envs/.production/.google-cloud/.django` before the first
`just gcp secrets` upload. Commercial add-ons (e.g. the GCS
audit-archive backend) provision their own resources via their own
recipes on top of this baseline.

**Two types of environment files**:

| File | Purpose | Usage |
|------|---------|-------|
| `.django` | Django runtime settings | Uploaded to Secret Manager, mounted at `/secrets/.env` |
| `.just` | Host-side GCP command context | Sourced locally before running `just gcp` commands |
| `.build` | Build/deploy knobs and shared runtime values | Sourced by deploy recipes, not uploaded to Secret Manager |

The `.just` file contains your GCP project ID and region, which the justfile needs to run deployment commands. Source it before running any `just gcp` commands:

```bash
# Source your GCP config
source .envs/.production/.google-cloud/.just

# Now you can run GCP commands
just gcp deploy prod
```

The `.django` file contains Django settings and is uploaded to Secret Manager:

```bash
just gcp secrets dev   # Upload secrets for dev environment
just gcp secrets prod  # Upload secrets for production
```

### AWS (Future)

AWS deployment support is planned but not yet implemented. The configuration will use:

- `DEPLOYMENT_TARGET=aws`
- AWS Batch for validator containers
- SQS for task queue
- S3 for file storage

## Environment Variable Reference

For a complete list of environment variables and their descriptions, see:

- `.envs.example/README.md` in the project root - Quick reference table with all variables
- [Configuration Settings](../overview/settings.md) - Django-specific settings documentation

## Internal API Security (Worker Endpoints)

Validibot's worker service exposes internal API endpoints for validation
execution, callbacks, and scheduled tasks. These endpoints need protection
against unauthorized access. The authentication backend is selected
automatically based on `DEPLOYMENT_TARGET`.

| Deployment target | Primary auth | Application-layer auth |
|---|---|---|
| **Docker Compose** | Network isolation | `WORKER_API_KEY` (shared secret) — **required** |
| **GCP** | Cloud Run IAM + private ingress | `CloudTasksOIDCAuthentication` — **required** |
| **AWS** *(not yet implemented)* | — | `WORKER_API_KEY` fallback |
| **Local dev / test** | — | None (skipped when key unset) |

Worker views inherit from `WorkerOnlyAPIView`, whose
`get_authenticators()` delegates to the deployment-aware factory in
`validibot/core/api/task_auth.py::get_worker_auth_classes()`. Adding a
new deployment target means writing one DRF `BaseAuthentication`
subclass and extending the factory — no view code changes.

### Shared-secret (`WORKER_API_KEY`) — Docker Compose

When the shared-secret backend is active, all requests to worker
endpoints must include the key in the `Authorization` header:

```
Authorization: Worker-Key <key>
```

When `WORKER_API_KEY` is empty (the default outside production), the
check is skipped. This allows local dev to run without provisioning a
key.

For Docker Compose deployments, add `WORKER_API_KEY` to
`.envs/.production/.self-hosted/.django`:

```bash
# Generate a key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Add to .django env file
WORKER_API_KEY=your-generated-key-here
```

Since all Docker Compose services (web, worker, scheduler) share the
same env file, the key is automatically available to every service.
The Celery worker uses it when making internal API calls.

**Why this matters:** in Docker Compose, all containers share the
Docker bridge network. Without `WORKER_API_KEY`, an SSRF vulnerability
in the web container could let an attacker call worker endpoints
directly, potentially spoofing validation results or triggering data
deletion.

### OIDC identity tokens — GCP

On `DEPLOYMENT_TARGET=gcp`, worker endpoints verify a Google-signed
OIDC identity token on every request. The token is provided as
`Authorization: Bearer <jwt>` by the caller (Cloud Tasks, Cloud
Scheduler, or a validator Cloud Run Service/Job via the metadata server).

Two settings control verification:

| Setting | What it does |
|---|---|
| `TASK_OIDC_AUDIENCE` | Expected `aud` claim. Must equal the worker service URL origin (scheme + host, **no path**). Falls back to `WORKER_URL` when unset. |
| `TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS` | Comma-separated list of service-account emails authorised to sign tokens. **Empty list → reject everything.** Legacy fallback: `CLOUD_TASKS_SERVICE_ACCOUNT`. |

Both settings live in the Secret Manager-mounted `.env` on the worker
service. A typical `.envs/.production/.google-cloud/.django`:

```bash
DEPLOYMENT_TARGET=gcp
WORKER_URL=https://validibot-worker-xxxx.run.app
# Optional — defaults to WORKER_URL
TASK_OIDC_AUDIENCE=https://validibot-worker-xxxx.run.app
TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=validibot-cloudrun-prod@PROJECT.iam.gserviceaccount.com,validibot-validator-prod@PROJECT.iam.gserviceaccount.com
```

**Set both explicitly in production.** The fallbacks (`WORKER_URL` for
audience, `CLOUD_TASKS_SERVICE_ACCOUNT` for the allowlist) are there so
the settings module doesn't raise `ImproperlyConfigured` on a minimal
`.env`, but leaving them implicit couples worker authentication to
settings whose primary purpose is something else. Rotating the Cloud
Tasks dispatcher SA, changing `WORKER_URL` to add a custom domain, or
splitting Cloud Scheduler onto its own SA all become accidentally
breaking changes if the auth layer is piggy-backing on them. Setting
`TASK_OIDC_AUDIENCE` and `TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS` directly
makes the auth contract self-documenting and lets you rotate the two
concerns independently.

**Boot-time validation.** `config/settings/production.py` raises
`ImproperlyConfigured` at Cloud Run startup when `DEPLOYMENT_TARGET=gcp`
and the resolved audience or allowlist (after applying fallbacks) is
empty. Misconfiguration surfaces in the deploy log, not in production
traffic.

### Task delivery bounds

Validibot treats both supported task transports as at-least-once delivery.
PostgreSQL run/attempt state provides idempotency; these settings bound how
quickly the transport decides a delivery was lost:

| Setting | Default | Deployment | Purpose |
|---|---:|---|---|
| `CELERY_VISIBILITY_TIMEOUT_SECONDS` | `3600` | Self-hosted Redis | Must exceed `CELERY_TASK_TIME_LIMIT` (1800 seconds), otherwise Redis can deliver a healthy long task to another worker. |
| `CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS` | `600` | GCP | Bounds the short application-worker HTTP orchestration request; accepted range is 15–1800 seconds. Validator compute runs separately in a selected Service or retained Job. |
| `VALIDATION_CALLBACK_PROCESSING_STALE_SECONDS` | `600` | All | Bounds ownership of callback storage download and verification. A duplicate delivery receives a retryable conflict before this age and may fence a stale processor afterward. Must be greater than zero. |
| `VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS` | `300` | All | Delay before the watchdog may take over a continuation producer that claimed work but did not record queue acceptance. Must be greater than zero. |
| `VALIDATION_CONTINUATION_EXECUTION_STALE_SECONDS` | `2100` | All | Delay before the watchdog may redeliver a continuation whose worker disappeared. It must exceed `CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS`; the default also exceeds the self-hosted Celery hard task limit. |
| `GCP_VALIDATOR_TASK_QUEUE_NAME` | empty | GCP | Separate stage-level queue used only for bounded validator Service deliveries. Production convention: `<app>-validator-provider`. |
| `GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT` | empty | GCP | Dedicated OIDC principal with `run.invoker` only on registered private validator Services. Use `<app>-val-invoker-<stage>@<project>.iam.gserviceaccount.com`; `init-stage` derives this abbreviated name to stay within Google's 30-character service-account ID limit. Upload the Django environment after setting it. |
| `GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS` | `1800` | GCP | Exact Cloud Tasks HTTP deadline for validator Service requests; changing it requires revisiting the Service transport contract. |
| `VALIDATOR_STATUS_LOOKUP_GRACE_SECONDS` | `300` | GCP | Bounded retry window after an attempt deadline when a status-capable provider API is temporarily unavailable. After the grace period, the attempt's absolute deadline remains authoritative. |

Transport retries never authorize a second provider launch after an attempt
has reached `DISPATCHING`, `RUNNING`, or `UNKNOWN`.

Callback-driven resumption follows the same at-least-once rule. PostgreSQL
commits a `ValidationRunContinuation` with the completed callback step;
`transaction.on_commit()` provides the fast delivery path, and the scheduled
`cleanup_stuck_runs` watchdog repairs pending or stale claims. Duplicate
workers stop at an active step rather than re-running or skipping past it.

GCP validator storage controls are deliberately absent from this table because
they are not operator choices. Every GCS + Cloud Run attempt uses a
prefix-bound Credential Access Boundary token, and the runtime identity must
have no ambient object permission. `just gcp init-stage` reconciles the IAM
posture and `just gcp validator-acceptance` proves it before traffic is enabled.

`VALIDATOR_BACKEND_IMAGE_POLICY` remains an operator choice on every target:
`tag`, `digest`, or `signed-digest`. The committed GCP production template sets
`digest`; production release recipes also verify the signed release before
registering its exact Artifact Registry digest.

**Audience contract.** Cloud Tasks and Cloud Scheduler sign tokens
with `aud = <service URL origin>` — path and query are NOT included.
Django's strict verification enforces exact match. Validator
containers derive the audience the same way (see
`validibot-validator-backends/validator_backends/core/callback_auth.py`). If you front
the worker behind a load balancer with a different signed audience,
override with `TASK_OIDC_AUDIENCE`.

**Failure modes.** Missing header, missing signature, audience
mismatch, un-allowlisted signer, or unverified email all return 401
with an audit log line identifying the presented audience and signing
account. These are diagnosable from worker logs alone.

## Security Reminders

!!! danger "Never Commit Secrets"
    The `.envs/` folder must **NEVER** be committed to version control, especially public repositories. This folder is gitignored for a reason - it contains passwords, API keys, database credentials, and other sensitive data. Committing these files could expose your entire deployment to attackers.

1. **Generate proper secrets for production** - Use the commands in `.envs.example/README.md` to generate `DJANGO_SECRET_KEY`, `DJANGO_API_KEY_DIGEST_KEY`, and passwords.

2. **Randomize the admin URL** - Change `DJANGO_ADMIN_URL` from the default `admin/` to a random path. This prevents automated scanners from finding your admin login page. Generate one with: `python -c "import secrets; print(secrets.token_urlsafe(16))"`.

3. **Use different secrets per environment** - Dev, staging, and production should have completely different credentials.

4. **Rotate secrets periodically** - Especially after team member departures or security incidents.

5. **Use Secret Manager in cloud deployments** - GCP Secret Manager (or AWS Secrets Manager) provides audit logging and access controls that local files can't.
