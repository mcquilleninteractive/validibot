# Configuration Reference

This page is the env-file and settings-module reference for self-hosted Validibot. It documents the eight grouped sections of `.envs/.production/.self-hosted/.django`, what each setting controls, and how the deployment profile model selects defaults.

For the install flow, see [Install](install.md).

## File layout

```text
.envs/.production/.self-hosted/
  .django           # Django app settings, security, validators, Pro/signing
  .postgres         # Postgres credentials and tuning
  .build            # Image versions, package index URLs
  .mcp              # MCP server settings (Pro feature)
```

You copy these from `.envs.example/.production/.self-hosted/` once during install. They live outside source control.

## `.django` — the eight grouped sections

The `.django` file is structured into eight comment-headered sections:

### 1. Required

Settings that must be set before the app starts.

| Setting | Purpose |
|---|---|
| `SITE_URL` | The public URL of your Validibot instance (e.g. `https://validibot.example.org`). Used everywhere — emails, OIDC, callback URLs, evidence verification URLs. |
| `DJANGO_SECRET_KEY` | Django's signing key. Generate with `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`. |
| `DJANGO_API_KEY_DIGEST_KEY` | HMAC key for stored API/user bearer-token digests. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and keep it separate from `DJANGO_SECRET_KEY`. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated list of domain names this instance serves. Must include the host portion of `SITE_URL`. |
| `DJANGO_MFA_ENCRYPTION_KEY` | Fernet key for encrypting TOTP secrets. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `WORKER_API_KEY` | Shared secret used by the web and worker containers for internal worker API calls. Generate a high-entropy value with `openssl rand -base64 48`. |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` for community, `config.settings.production_pro` for Pro. |

The doctor command's VB001-VB099 range checks these.

### 2. URLs and security

| Setting | Purpose |
|---|---|
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Origins allowed for CSRF-protected POSTs. Include your `SITE_URL` and any other origins that hit Validibot's API. |
| `DJANGO_SECURE_SSL_REDIRECT` | `true` for production. Redirects HTTP → HTTPS. |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | Set if you have a reverse proxy terminating TLS. Format: `HTTP_X_FORWARDED_PROTO,https`. |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS max-age. Recommended: `31536000` (one year). |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | `true` if all subdomains use HTTPS. |
| `DJANGO_SECURE_HSTS_PRELOAD` | `true` to opt into HSTS preload (irreversible — read the docs first). |

### 3. Database and cache

| Setting | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string. Default: the bundled Compose Postgres. |
| `REDIS_URL` | Redis connection string. Default: the bundled Compose Redis. |
| `CACHE_BACKEND` | Cache backend. Default: Redis. Self-hosted can use `DatabaseCache` if Redis is unavailable. |

### 4. Storage

| Setting | Purpose |
|---|---|
| `DATA_STORAGE_ROOT` | Container path for private validation data. The self-hosted Compose file sets `/app/storage/private` and backs it with the `validibot_storage` Docker named volume. Do not set this to a host path unless you also change the Compose storage layout deliberately. |
| `MEDIA_ROOT` | Django's public media storage. Defaults to `/app/storage/public` in the container and is backed by the same storage volume in the default Compose stack. |
| `DATA_STORAGE_BACKEND` | Storage backend for private validation data. Defaults to local filesystem for self-hosted (leave unset). The `gcs` and `s3` backends exist but are documented for cloud deployments; S3-compatible object storage is not yet operator-supported for self-hosted installs. |

### 5. Email

| Setting | Purpose |
|---|---|
| `DJANGO_EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` for production. `console` for evaluation. |
| `EMAIL_HOST` | SMTP server. |
| `EMAIL_PORT` | SMTP port (587 for STARTTLS, 465 for SMTPS). |
| `EMAIL_USE_TLS` | `true` for STARTTLS. |
| `EMAIL_USE_SSL` | `true` for SMTPS. |
| `EMAIL_HOST_USER` | SMTP username. |
| `EMAIL_HOST_PASSWORD` | SMTP password. |
| `DEFAULT_FROM_EMAIL` | The `From:` address Validibot uses. |

### 6. Validators

| Setting | Purpose |
|---|---|
| `VALIDATOR_RUNNER` | `docker` for self-hosted (default). `google_cloud_run` for GCP. |
| `VALIDATOR_BACKEND_IMAGE_POLICY` | `tag` (default), `digest`, or `signed-digest` (Phase 5). Production: `digest` or higher. Enforced at launch — only enable `digest` once validator images are digest-pinned, or launches fail. |
| `VALIDATOR_RETAIN_HOURS` | How long stopped validator containers are kept before `cleanup` removes them. Default: `24`. |
| `VALIDATOR_TIMEOUT_SECONDS` | Outer per-validator timeout and stuck-run watchdog deadline. Validators can request shorter via their manifest. |
| `CELERY_VISIBILITY_TIMEOUT_SECONDS` | Redis redelivery window. Default: `3600`; it must remain greater than Celery's 30-minute hard task limit so a healthy long task is not delivered twice. |
| `VALIDATION_CALLBACK_PROCESSING_STALE_SECONDS` | Callback storage/verification ownership window. Default: `600`; a duplicate may take over only after this age. |
| `VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS` | Producer-claim repair window for callback-driven workflow resumption. Default: `300`. |
| `VALIDATION_CONTINUATION_EXECUTION_STALE_SECONDS` | Worker-claim repair window. Default: `2100`, intentionally longer than Celery's hard task limit. |

The existing scheduled `cleanup_stuck_runs` command also repairs durable
workflow continuations before evaluating run timeouts. Keep that schedule
enabled: it closes the process-death window between committing callback output
and delivering the next workflow step.

### 7. Pro and signing

Empty for community deployments.

| Setting | Purpose |
|---|---|
| `SIGNING_KEY_PATH` | Path to the local ES256 signing key used for signed credentials. Pro only. |
| `GCP_KMS_SIGNING_KEY` | Full Google Cloud KMS key resource for GCP-hosted Pro installations. |
| `GCP_KMS_SIGNING_KEY_VERSION` | Explicit active KMS version. Required with `GCP_KMS_SIGNING_KEY`; rotate it only after registering the candidate public key. |
| `JWKS_PUBLIC_PATH` | Path to the public JWKS. Pro only. Served at `/.well-known/jwks.json` for credentials issued by this instance. |
| `IDP_OIDC_MCP_RESOURCE_AUDIENCE` | MCP OAuth audience claim. Defaults to `{VALIDIBOT_MCP_BASE_URL}/mcp`. |
| `VALIDIBOT_MCP_BASE_URL` | Public HTTPS origin for the MCP server, such as `https://mcp.example.com`. Self-hosted production has no plaintext public default. |
| `MCP_SERVICE_KEY` | Service-to-service auth key for the MCP server calling the Validibot REST API. Self-hosted only. |
| `MCP_OIDC_AUDIENCE` | Cloud Run OIDC audience (GCP only — self-hosted uses `MCP_SERVICE_KEY`). |
| `ENABLE_MCP_SERVER` | `true` to include the MCP container under the `mcp` Compose profile. Build-time gate; runtime is also gated by the `mcp_server` Pro feature. |

### 8. Optional telemetry

Off by default for self-hosted.

| Setting | Purpose |
|---|---|
| `SENTRY_DSN` | If set, error reporting goes to Sentry. Empty by default. |
| `VALIDIBOT_TELEMETRY` | `off` (default) — future: `errors-only`, `anonymous-usage`, `support-session`. |

## `.postgres` — Postgres settings

| Setting | Purpose |
|---|---|
| `POSTGRES_DB` | Database name. Default: `validibot`. |
| `POSTGRES_USER` | Database user. Default: `validibot`. |
| `POSTGRES_PASSWORD` | Database password. Generate a strong one — this is the credential the web/worker containers use. |
| `POSTGRES_HOST` | Hostname. Default: `postgres` (the Compose service). For external Postgres, set to your DB host. |
| `POSTGRES_PORT` | Port. Default: `5432`. |

## `.build` — image versions and package index

| Setting | Purpose |
|---|---|
| `VALIDIBOT_IMAGE_TAG` | Validibot image tag. `latest` for evaluation, exact version (e.g. `0.8.0`) for production. |
| `VALIDIBOT_IMAGE_REGISTRY` | Image registry namespace used by deployment tooling. Published Validibot packages use `ghcr.io/mcquilleninteractive`; no Docker Hub mirror is maintained. |
| `VALIDIBOT_COMMERCIAL_PACKAGE` | For Pro: `validibot-pro==<version>`. Empty for community. |
| `VALIDIBOT_COMMERCIAL_NETRC` | Absolute path to a mode-0600 netrc containing the Pro package credentials. Empty for community. The file is mounted as a BuildKit secret and is not stored in image metadata. |
| `VALIDATOR_CONTAINER_SOCKET` | Host path to the Docker-compatible API socket mounted into the worker. Fresh installs default to `${XDG_RUNTIME_DIR:-/run/user/1000}/docker.sock`; set an exact `/run/user/<numeric-uid>/docker.sock` path when needed. `/var/run/docker.sock` is the explicit rootful compatibility option. |
| `MCP_HOST_PORT` | Host-loopback port used by an external reverse proxy or local diagnostics. Defaults to `8001`; the bind address is fixed to `127.0.0.1` in Compose. |

For Pro, credentials live only in the netrc referenced by `.build`. Keep that
netrc at mode 0600 and out of version control; `.build` contains its path and
the credential-free package reference and remains gitignored.

## `.mcp` — MCP server settings (Pro feature)

| Setting | Purpose |
|---|---|
| `VALIDIBOT_LOG_LEVEL` | `INFO` for production. `DEBUG` for troubleshooting. |
| `VALIDIBOT_API_BASE_URL` | Internal or public URL of the Django API that MCP forwards tool calls to. |
| `VALIDIBOT_MCP_BASE_URL` | Public MCP URL used for metadata, redirects, and the default token audience. Keep it aligned with Django's value. |
| `VALIDIBOT_OAUTH_AUTHORIZATION_SERVER_URL` | Public base URL of the Django OIDC issuer. |
| `VALIDIBOT_OAUTH_CLIENT_ID`, `VALIDIBOT_OAUTH_CLIENT_SECRET` | Confidential client registered with Django. The secret is paired with `IDP_OIDC_MCP_SERVER_CLIENT_SECRET` in `.django`. |
| `VALIDIBOT_MCP_SERVICE_KEY` | Shared MCP-to-Django key for self-hosting. Use the same generated value as `MCP_SERVICE_KEY` in `.django`. |
| `VALIDIBOT_MCP_ENABLED` | Runtime kill switch. `false` makes every tool call return 503. |
| `VALIDIBOT_OAUTH_AUTHORIZATION_ENDPOINT`, `VALIDIBOT_OAUTH_TOKEN_ENDPOINT`, `VALIDIBOT_OAUTH_REVOCATION_ENDPOINT`, `VALIDIBOT_OAUTH_JWKS_URL` | Optional complete-URL overrides if a compatible provider is routed differently. Validibot's standard paths are derived locally, without a startup discovery request. |

The MCP container always listens on port `8080` inside the private Compose
network. `MCP_HOST_PORT` controls only the loopback publication used by a host
reverse proxy; public clients must connect through an HTTPS origin.

## Deployment targets

The current doctor command supports these deployment targets. Hardening is
configured through the explicit settings above; there is no automatic
`self-hosted-hardened` profile yet.

| Target | Purpose | Defaults |
|---|---|---|
| `local_docker_compose` | local contributor or evaluation Compose stack | host-only checks skipped |
| `self_hosted` | production single VM | production compatibility findings enforced |
| `gcp` | hosted Validibot | GCP/Stripe/metering/x402 |
| `test` | automated Django test environment | external-service checks skipped |

The active settings module defines the deployment target. Pass
`--target self_hosted`, `gcp`, `local_docker_compose`, or `test` to override
doctor's inference for a diagnostic run.

## Settings module switching

Validibot uses Django settings modules to control which apps and features are loaded:

| Module | Use |
|---|---|
| `config.settings.local` | Community-only local dev. Used by `just local`. |
| `config.settings.local_pro` | Community + `validibot_pro` mounted as a volume. Used by `just local-pro`. |
| `config.settings.production` | Self-hosted community production. |
| `config.settings.production_pro` | Self-hosted Pro production — adds `validibot_pro` to `INSTALLED_APPS`. |

Switching from community to Pro is a settings module change in `.django` plus a package install in `.build`. See [Install](install.md) for the activation flow.

## Reverse proxy: bring your own, or use bundled Caddy

Caddy ships as an opt-in Compose profile, off by default. Most production operators already have a reverse proxy.

To enable Caddy:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

The Caddyfile lives at `deploy/self-hosted/caddy/Caddyfile` and uses `SITE_URL` to provision Let's Encrypt certificates.

To bring your own proxy: leave the `caddy` profile off, configure your proxy to forward to the `web` container on port 8000, and set `DJANGO_SECURE_PROXY_SSL_HEADER` plus `DJANGO_CSRF_TRUSTED_ORIGINS` appropriately.

## Verifying configuration

```bash
just self-hosted check-env       # parse env files and warn about missing settings
just self-hosted check-dns       # verify SITE_URL resolves to this VM
just self-hosted doctor          # full health check
just self-hosted doctor --json   # machine-readable output for CI
just self-hosted doctor --strict # fail on warnings (suitable for CI gates)
```

Doctor's check IDs are documented in [doctor-check-ids.md](doctor-check-ids.md).

## See also

- [Install](install.md) — initial setup
- [Doctor Check IDs](doctor-check-ids.md) — what each check ID means
- [Security Hardening](security-hardening.md) — recommended hardening
- [Operator Recipes](operator-recipes.md) — full recipe reference
