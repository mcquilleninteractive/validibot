# Deploy to GCP

Choose this target when you want a managed cloud deployment on Google Cloud instead of a self-managed single host.

This page is the high-level entry point for GCP deployments. For the deeper Cloud Run runbook, see [Google Cloud Deployment](../google_cloud/deployment.md).

## When to choose this target

Choose GCP if you want:

- managed application hosting on Cloud Run
- managed PostgreSQL with Cloud SQL
- Secret Manager, Artifact Registry, and Cloud Scheduler integration
- a cleaner fit for teams already standardised on Google Cloud

Choose [Deploy with Docker Compose](deploy-docker-compose.md) instead if you want the simplest self-hosted production path on infrastructure you control directly.

## What this target runs

The GCP deployment uses:

- Cloud Run for the web service
- Cloud Run for the worker service
- Cloud SQL for PostgreSQL
- Cloud Storage for file storage
- Secret Manager for runtime configuration
- Artifact Registry for container images
- Cloud Scheduler for recurring jobs

Advanced validators are deployed separately from the main web and worker services.

## Environment model

The GCP setup is designed around three stages:

| Stage | Purpose | Typical use |
| --- | --- | --- |
| `dev` | development testing | deploy new changes first |
| `staging` | pre-production verification | optional but useful for larger changes |
| `prod` | production | customer-facing environment |

Each stage gets its own Cloud Run services, Cloud SQL instance, secrets, and queueing resources.

## Signed credentials on GCP

GCP deployments should use Google Cloud KMS rather than a local PEM file.
Set both the credential-signing key and one explicit active version in your
stage `.django` env file:

```bash
GCP_KMS_SIGNING_KEY=projects/your-project/locations/your-region/keyRings/your-app-name-keys/cryptoKeys/credential-signing
GCP_KMS_SIGNING_KEY_VERSION=1
CREDENTIAL_ISSUER_URL=https://validibot.example.com
```

Validibot never chooses the highest enabled KMS version automatically. The
explicit version prevents a newly created version from signing before its
public key has reached the application's JWKS. The Cloud Run service account
needs these key-scoped roles:

- `roles/cloudkms.viewer`
- `roles/cloudkms.publicKeyViewer`
- `roles/cloudkms.signerVerifier`

Use a different KMS key per stage so dev, staging, and prod credentials do not
share the same issuer key material.

After the database migration has run, register the active version's public key
before enabling workflows that issue credentials:

```bash
python manage.py register_signing_key --gcp-version 1
python manage.py signing_key_status
```

The registration command asks KMS only for the public key and stores a public
JWK in the normal application database. Private key bytes remain in KMS.

### Rotate a credential-signing key

Rotation is publish-before-use:

1. Create a new version in the existing asymmetric signing key.
2. Leave `GCP_KMS_SIGNING_KEY_VERSION` on the old version.
3. Run `python manage.py register_signing_key --gcp-version NEW_VERSION` in the
   deployed application environment.
4. Confirm `/.well-known/jwks.json` contains the reported `kid`, allowing for
   its five-minute cache.
5. Change `GCP_KMS_SIGNING_KEY_VERSION` in the stage `.django` file, upload the
   secret, and redeploy every credential-issuing service.
6. Run `python manage.py signing_key_status`, issue a test credential, and
   confirm both the new credential and an older credential verify.

Old public JWKs stay in the registry indefinitely so existing credentials keep
verifying. Disabling an old private KMS version later does not remove its public
verification key from Validibot.

## Set up the env files

Before any `just gcp ...` recipe will work, copy the env templates and
fill in the values:

```bash
mkdir -p .envs/.production/.google-cloud
cp .envs.example/.production/.google-cloud/.just    .envs/.production/.google-cloud/.just
cp .envs.example/.production/.google-cloud/.django  .envs/.production/.google-cloud/.django
cp .envs.example/.production/.google-cloud/.build   .envs/.production/.google-cloud/.build
```

If you plan to deploy MCP as well, copy the MCP template:

```bash
cp .envs.example/.production/.google-cloud/.mcp     .envs/.production/.google-cloud/.mcp
```

Then edit the new files. The `.just` file holds deployment-time
configuration (GCP project, region, app name) and is sourced into your
shell — it never leaves your machine. The `.django` file holds runtime
configuration and is uploaded to Secret Manager. The `.build` file holds
build/deploy knobs, including `ENABLE_MCP_SERVER`, public MCP URLs, and hosted
x402 values that the recipes stamp onto the services that need them.

## Typical first-time flow

Most first-time GCP setups follow this order:

```bash
source .envs/.production/.google-cloud/.just

just gcp init-stage dev
# Edit .envs/.dev/.google-cloud/.django using the values from init-stage.
just gcp secrets dev
just gcp deploy-all dev
crane version  # preferred; otherwise ensure `docker info` succeeds
just gcp validator-status dev
just gcp validator-setup dev
```

`just gcp deploy-all` runs migrations and the guarded, complete application
initializer before any new service revision receives traffic. The initializer
owns site/default data, validators and Step I/O, help content, and bundled
validator resources. It does **not** install the independently released Cloud
Run validator backends: `validator-setup` verifies, deploys, accepts, and
activates those Service/Job pairs after the application is ready. There is no
single command that performs the entire first-time sequence today.

Validator setup and later updates use shared Cloud Tasks queues for private
acceptance. They enter maintenance, pause both queues, and continue only when
the queues are empty and every managed execution attempt is terminal. Queued
tasks are preserved and the previous lifecycle mode is restored if this idle
check fails. Each stage reuses one zero-idle management Job, and each backend's
Service and Job acceptance phases run in one remote execution rather than a
series of temporary Jobs. Each phase uses three concurrent attempts per
compatible semantic Validator. Larger percentile samples come from the
separate latency report over accumulated executions, not from every release.

There are no separate migration, setup-data, help-sync, weather-seed, or
scheduler commands in the first-time flow. You can still run `just gcp migrate
dev` or `just gcp setup-data dev` explicitly for recovery. Every managed
migration path first runs `python manage.py
check_migration_history`. A pre-reset migration-history refusal is a hard stop:
back up the database and rebuild it through the documented cutover path rather
than forcing `migrate` over an incompatible schema.

After that, verify the environment, then repeat the same process for `staging` or `prod` as needed.

### Secrets checklist

Before `just gcp secrets dev`, make sure `.envs/.production/.google-cloud/.django`
defines:

- `DJANGO_SECRET_KEY` — Django session / signed-cookie key.
- `DJANGO_API_KEY_DIGEST_KEY` — HMAC key for stored API/user bearer-token
  digests. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`
  and keep it separate from `DJANGO_SECRET_KEY`.
- `DJANGO_MFA_ENCRYPTION_KEY` — Fernet key for MFA secret material. The
  app refuses to start without this, and the startup check validates
  the format (not just presence). Generate with:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `DATABASE_URL`, `POSTGRES_*` — Cloud SQL connection.
- `DJANGO_ALLOWED_HOSTS` — exact Cloud Run and custom hostnames; do not use a
  wildcard `.run.app` suffix.
- `DJANGO_CSRF_TRUSTED_ORIGINS` — full HTTPS origins for CSRF-protected
  requests, including the public `SITE_URL` origin.
- `MFA_TOTP_ISSUER` — authenticator-app label (e.g. "Validibot Cloud").
- `STORAGE_BUCKET` — media / submission bucket, printed at the end of
  `init-stage`.

Commercial add-ons may introduce additional env vars (for example, a
GCS audit-archive bucket with CMEK encryption). Each add-on's own
deployment docs lists the env vars it expects — a community GCP
deployment uses the null / filesystem audit-archive backends and
needs nothing beyond the list above.

### Provisioned resources

`just gcp init-stage {stage}` is idempotent and creates, among other
things:

- Runtime and validator service accounts with IAM bindings.
- Cloud SQL instance and database.
- Cloud Tasks queue and Cloud Scheduler-ready KMS permissions.
- Media/submissions GCS bucket (`{app}-storage[-stage]`) with
  public/private prefix IAM.
- Secret Manager placeholder for `django-env[-stage]`.

A community-only deployment uses the ``NullArchiveBackend`` for audit
log retention, which needs no extra GCP resources. Deployments that
layer on a commercial add-on with the GCS audit-archive backend provision
the bucket, CMEK key, and IAM separately — see the add-on's own
deployment docs.

See [configure-mfa.md](../how-to/configure-mfa.md) for key-generation
and rotation procedures. The encryption key is stored in Secret Manager
via `just gcp secrets`, never committed.

### Cache table

Production uses Django's `DatabaseCache` backend by default (rather
than Memorystore/Redis) — a zero-marginal-cost option that reuses
the Cloud SQL instance for allauth rate limiting and TOTP replay
protection. The `just gcp migrate` step runs `createcachetable`
automatically on every deploy (idempotent — no-op after the first
run). If you ever need higher cache throughput, set `REDIS_URL` to a
Memorystore instance and the settings module switches backends
automatically — see
[configure-mfa.md](../how-to/configure-mfa.md#upgrade-path-redis-via-memorystore)
for the full upgrade path.

## Routine deployment flow

For normal updates:

```bash
source .envs/.production/.google-cloud/.just

just gcp deploy-all dev
```

`deploy-all` runs migrations as part of its dependency chain, so a
separate migrate step is not needed for a routine deploy. Promote to
production only after the lower stage looks healthy.

## Include the MCP server

The standalone FastMCP container exposes validation workflows to AI
agents over the Model Context Protocol. On GCP it runs as its own
Cloud Run service (`validibot-mcp` in prod, `validibot-mcp-<stage>`
otherwise) with its own Artifact Registry image and service account,
deployed independently from the main Django web service.

**Source and image.** The MCP code lives in this repo at `mcp/` and
is built from `compose/production/mcp/Dockerfile`. The image is a
lightweight Python container (~80 MB) with FastMCP, httpx, and
pydantic-settings only — no Django, no database drivers.

**License gate.** Whenever an enabled MCP revision starts, it calls
`GET /api/v1/license/features/` against the Django API and refuses
to serve traffic unless `mcp_server` is advertised. This only
happens when `validibot-pro` (or enterprise) is installed. So a
community-only deployment can build and deploy the image but the
enabled container will exit during the license check. A
`deploy-maintenance` revision is instead internal and explicitly disabled, so
it can become ready while Django is offline. `mode-live` exposes Django
first and then re-enables MCP, which creates an enabled revision that performs
the same fail-closed license check before serving traffic.

### Configure the knobs

The MCP deploy tooling reads its public Cloud Run config from
`.envs/.production/.google-cloud/.build`:

```bash
# Include the MCP container in ``just gcp deploy-all`` and unlock
# the ``just gcp mcp ...`` recipes. Requires validibot-pro to be
# installed so the runtime license check passes.
ENABLE_MCP_SERVER=true

# Public URL of YOUR Validibot Django API — the MCP server proxies
# tool calls here. There is no default; setting this wrong could
# accidentally proxy your users' traffic to another operator's API.
VALIDIBOT_MCP_API_BASE_URL=https://app.your-domain.example

# Public URL of YOUR MCP service. The deploy recipe stamps this onto both
# Django and MCP; do not repeat it in .django or .mcp.
VALIDIBOT_MCP_BASE_URL=https://mcp.your-domain.example

# Hosted x402 is cloud-only and disabled by default. Its runtime settings live
# in the cloud Django .django secret, not in .build or .mcp.
```

See `.envs.example/.production/.google-cloud/.build` for the full
documented template.

### Configure MCP auth

MCP has two independent auth chains, both of which need their own
settings in `.envs/.production/.google-cloud/.django`:

**1. End user → MCP server (OAuth 2.1).** When an OAuth-capable MCP
client (Claude Desktop, Cursor, Windsurf, Continue, Zed, etc.)
connects, the MCP server proxies a Dynamic Client Registration flow
to Django's OIDC provider. Required settings:

```bash
# Signing key for JWT access tokens (base64-encoded PEM). Generate
# once and back up securely — rotating invalidates every live session.
IDP_OIDC_PRIVATE_KEY_B64=<base64 of a fresh openssl genrsa 2048 -out key.pem>

# Paired secret for the confidential OAuth client the MCP server registers as.
# Use the same generated value in .mcp as VALIDIBOT_OAUTH_CLIENT_SECRET
# (openssl rand -hex 32), then rotate both secret files together.
IDP_OIDC_MCP_SERVER_CLIENT_SECRET=<hex random secret>
```

In `.envs/.production/.google-cloud/.build`:
```bash
# Public URL of your MCP server. The deploy recipe stamps this onto both
# Django and MCP so the OIDC audience, redirect URI, and MCP metadata come
# from one value.
VALIDIBOT_MCP_BASE_URL=https://mcp.your-domain.example
```

**2. MCP server → Django API (Cloud Run OIDC identity token).** Every
tool call reaches Django via `/api/v1/mcp/*`, which requires a Google-
signed identity token minted by the MCP service account. Required
settings:

```bash
# The deploy recipe stamps MCP_OIDC_AUDIENCE onto Django from
# VALIDIBOT_MCP_API_BASE_URL in .build. Keep only the service-account
# allowlist in .django.
MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS=validibot-mcp-prod@your-project.iam.gserviceaccount.com
```

Django refuses to boot if `MCP_OIDC_AUDIENCE` is stamped but the allowlist is
empty — a safety guard against accepting tokens from any Google service account
that can mint to the audience.

See `.envs.example/.production/.google-cloud/.django` for the fully
commented template.

### Deploy

First-time setup provisions the MCP service account, IAM bindings,
and Artifact Registry access:

```bash
source .envs/.production/.google-cloud/.just
just gcp mcp setup prod
```

Then upload the MCP secret (OAuth client credentials, etc.) and
deploy the service. You have three levels of granularity:

```bash
# Umbrella — pushes every secret that might have changed
just gcp secrets prod
# Equivalent to: gcp django secrets + gcp mcp secrets

# Surgical — just one service
just gcp django secrets prod   # only .django → django-env
just gcp mcp secrets prod      # only .mcp → mcp-env
```

```bash
# Full deploy — Django web + worker + scheduler + MCP build + MCP deploy
just gcp deploy-all prod

# MCP-only deploy — useful for hotfixing just the MCP image
just gcp mcp build-push
just gcp mcp deploy prod
```

### Routing

To expose MCP on a custom domain via the load balancer you set up
for Django, run:

```bash
just gcp mcp lb-add prod mcp.your-domain.example
```

That provisions a serverless NEG, a backend service, adds the MCP
hostname to the managed SSL certificate, and locks the Cloud Run
service's ingress to load-balancer-only.

## Domain and networking

There are two normal ways to expose a GCP deployment publicly:

- Cloud Run domain mappings for the simpler path in supported regions
- a global HTTP(S) load balancer for the more production-oriented path

If you need a custom domain, SSL, or a single public entrypoint, see the domain section in [Google Cloud Deployment](../google_cloud/deployment.md).

## Good fits for this target

GCP is a good fit when:

- you already use Google Cloud
- you want managed infrastructure rather than running a VM yourself
- you need a cleaner path to multi-environment deployments

## Read next

Use these guides after choosing GCP:

- [Google Cloud Deployment](../google_cloud/deployment.md)
- [Google Cloud Overview](../google_cloud/index.md)
- [Google Cloud Setup Cheatsheet](../google_cloud/setup-cheatsheet.md)
- [Google Cloud Scheduled Jobs](../google_cloud/scheduled-jobs.md)
