# Configuration Reference

This page is the env-file and settings-module reference for self-hosted Validibot. It documents the eight grouped sections of `.envs/.production/.self-hosted/.django`, what each setting controls, and how the deployment profile model selects defaults.

For the install flow, see [Install](install.md).

## File layout

```text
.envs/.production/.self-hosted/
  .django           # Django app settings, security, validators, Pro/signing
  .postgres         # Postgres credentials and tuning
  .build            # Image versions, package index URLs
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
| `VALIDATOR_PDF_CONTAINER_RUNTIME` | Optional registered Docker runtime for PDF parsing, such as `runsc` (gVisor) or a Kata runtime name. The default is the daemon runtime. |
| `VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX` | `false` by default. Set `true` for public hostile PDF ingress after configuring the runtime above; PDF launches then fail closed if no stronger runtime is selected. |
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
| `IDP_OIDC_MCP_RESOURCE_AUDIENCE` | Exact MCP OAuth resource and JWT audience. Defaults to `<SITE_URL>/mcp`. |
| `IDP_OIDC_CHATGPT_REDIRECT_URIS` | Complete app-specific callback generated in ChatGPT app management: `https://chatgpt.com/connector/oauth/{callback_id}`. The client is public, uses PKCE, and has no secret. Omit it to skip ChatGPT provisioning. |
| `IDP_OIDC_ACCESS_TOKEN_EXPIRES_IN` | MCP access-token lifetime. Default: `900` (15 minutes). Keep access tokens short-lived because bearer tokens remain usable until expiry. |
| `IDP_OIDC_REFRESH_TOKEN_EXPIRES_IN` | MCP refresh-token lifetime. Default: `2592000` (30 days). Refresh tokens are stored as revocable database records. |
| `MCP_FILE_ALLOWED_HOSTS` | Exact comma-separated attachment hostnames. Empty denies every attachment download; production rejects wildcards. Add only hosts observed for the client you support. |
| `MCP_FILE_MAX_BYTES` | Maximum downloaded ChatGPT validation file size. Default: `2500000`. |
| `MCP_FILE_DOWNLOAD_TOTAL_TIMEOUT_SECONDS` | One deadline covering DNS, redirects, connections, and streamed reads. Default: `30`. |
| `MCP_FILE_DOWNLOAD_MAX_ADDRESSES` | Maximum validated public DNS addresses attempted at each redirect hop. Default: `4`. |
| `MCP_MAX_REQUEST_BODY_BYTES` | Maximum Streamable HTTP request body. Default: `4194304`. |
| `MCP_MAX_RESPONSE_BYTES` | Maximum serialized result from any MCP tool. Default: `524288`. |
| `MCP_READS_PER_MINUTE` | Shared per-principal quota across all read tools. Default: `120`. |
| `MCP_STARTS_PER_MINUTE` | Per-principal validation-start quota. Default: `20`. |
| `MCP_REQUESTS_PER_IP_PER_MINUTE` | Shared-cache MCP transport budget per trusted client address. Default: `240`. |
| `MCP_FAILED_AUTH_PER_IP_PER_MINUTE` | Failed MCP bearer attempts per trusted client address. Default: `20`. |
| `MCP_GLOBAL_REQUESTS_PER_MINUTE` | Deployment-wide MCP transport ceiling. Default: `3000`. |
| `IDP_OIDC_TOKEN_REQUESTS_PER_IP_PER_MINUTE` | Per-client-IP limit for token endpoint POSTs. Default: `60`. |
| `IDP_OIDC_REVOKE_REQUESTS_PER_IP_PER_MINUTE` | Per-client-IP limit for revocation endpoint POSTs. Default: `30`. |
| `IDP_OIDC_ENDPOINT_GLOBAL_REQUESTS_PER_MINUTE` | Shared global limit across token and revocation POSTs. Default: `1000`. Use a shared cache on multi-instance deployments. |
| `MCP_ALLOWED_ORIGINS` | Optional comma-separated additional exact browser origins. Leave empty unless required. |

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

For Pro, credentials live only in the netrc referenced by `.build`. Keep that
netrc at mode 0600 and out of version control; `.build` contains its path and
the credential-free package reference and remains gitignored.

The MCP implementation is embedded in the Pro Django ASGI process. There is no
`.mcp` env file, second container, service key, internal MCP port, or
confidential proxy client. The normal web image and `.django` environment own
the endpoint at `<SITE_URL>/mcp`.

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
