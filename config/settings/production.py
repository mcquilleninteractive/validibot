"""
Production settings for Validibot.

This settings file handles all production deployments. The specific
infrastructure is determined by the DEPLOYMENT_TARGET environment
variable:

    - "gcp": Google Cloud Platform (Cloud Run, Cloud Tasks, GCS)
    - "self_hosted": Customer-operated single-VM deployment using
      Docker Compose (typically DigitalOcean, AWS EC2, Hetzner, on-prem).
      See ADR-2026-04-27 (Boring Self-Hosting and Operator Experience).
    - "aws": Reserved for future support and not yet implemented

Required environment variables (all targets):
    DJANGO_SECRET_KEY: Secure secret key for Django
    DATABASE_URL: PostgreSQL connection string
    DJANGO_ALLOWED_HOSTS: Comma-separated list of allowed hosts
    DEPLOYMENT_TARGET: One of "gcp", "self_hosted", "aws"

Target-specific requirements:
    GCP:
        STORAGE_BUCKET: GCS bucket name
        GCP_PROJECT_ID: Google Cloud project ID
        SENTRY_DSN: Sentry error tracking
        DJANGO_ADMIN_URL: Admin URL path

    Self-hosted:
        REDIS_URL: Redis connection string (for Celery)
        (STORAGE_BUCKET optional - uses local filesystem if not set)

    AWS:
        Not yet implemented
"""

import logging

import django.core.exceptions
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import BASE_DIR
from .base import DATABASES
from .base import DEFAULT_FROM_EMAIL
from .base import INSTALLED_APPS
from .base import LOGGING
from .base import SPECTACULAR_SETTINGS
from .base import env

# DEPLOYMENT TARGET
# ------------------------------------------------------------------------------
# Determines which infrastructure backend to use for task queue and validators.
DEPLOYMENT_TARGET = env("DEPLOYMENT_TARGET", default="self_hosted")

VALID_DEPLOYMENT_TARGETS = {"gcp", "self_hosted", "aws"}
if DEPLOYMENT_TARGET not in VALID_DEPLOYMENT_TARGETS:
    raise ImproperlyConfigured(
        f"DEPLOYMENT_TARGET must be one of {VALID_DEPLOYMENT_TARGETS}, "
        f"got: {DEPLOYMENT_TARGET}"
    )

if DEPLOYMENT_TARGET == "aws":
    raise NotImplementedError(
        "DEPLOYMENT_TARGET='aws' is not implemented yet. "
        "Use DEPLOYMENT_TARGET='self_hosted' or DEPLOYMENT_TARGET='gcp'."
    )

# GENERAL
# ------------------------------------------------------------------------------
DEBUG = False
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env("DJANGO_SECRET_KEY")
API_KEY_DIGEST_KEY = env("DJANGO_API_KEY_DIGEST_KEY", default="")
if not API_KEY_DIGEST_KEY:
    raise ImproperlyConfigured(
        "DJANGO_API_KEY_DIGEST_KEY is required in production. Generate one "
        'with: python -c "import secrets; print(secrets.token_urlsafe(32))" '
        "and store it in Secret Manager alongside DJANGO_SECRET_KEY.",
    )
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# DATABASES
# ------------------------------------------------------------------------------
# Django recommends disabling persistent database connections in async mode.
# This also keeps Cloud SQL connection growth bounded under Cloud Run's
# concurrent-request model.
DATABASES["default"]["CONN_MAX_AGE"] = 0

# CACHES
# ------------------------------------------------------------------------------
# Production needs a SHARED cache across Gunicorn workers and scaled Cloud
# Run instances. allauth's rate limiting (login throttling, MFA attempt
# caps) and its short-window "don't let the same TOTP code be reused"
# protection rely on this cross-process visibility. A per-process
# LocMemCache silently weakens those controls — we therefore explicitly
# REFUSE to fall back to LocMem here and pick between two supported
# shared backends:
#
#   1. RedisCache (preferred at scale)   — set REDIS_URL.
#   2. DatabaseCache (default at launch) — zero-marginal-cost option
#      that reuses the existing Cloud SQL / Postgres database. Fine for
#      the low-volume rate-limit workload at our current stage (a few
#      hundred cache ops/day). Run `python manage.py createcachetable`
#      once during first deploy to create the cache table. See
#      docs/dev_docs/how-to/configure-mfa.md for upgrade guidance.
#
# Switch to Redis when traffic grows or when latency on the DB-backed
# cache shows up in auth-path monitoring — it's a one-variable change.
REDIS_URL = env("REDIS_URL", default=None)
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        },
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "django_cache",
        },
    }

# Environment-scoped cache key prefix. Django transparently prepends
# this to every key passed to every backend, so a single setting
# isolates keys across environments without any changes at the
# ``cache.get/set`` callsites. See refactor-step item ``[review-#7]``.
#
# Two environments sharing a Redis instance (staging and prod behind
# the same managed Redis; CI and developers' shared dev Redis) will
# otherwise clobber each other's plan caches, rate-limit counters,
# JWKS ``kid`` caches, and so on. The damage mode is silent — a
# staging org's plan mask bleeding into prod surfaces only as a
# "why did this user suddenly have Pro features?" support ticket
# weeks later.
#
# Recommended values: one of ``prod``, ``staging``, ``ci``,
# ``dev-alice`` (developer-specific when sharing a dev Redis). The
# default is empty, which is correct when Redis is single-tenant
# to this deployment — we emit a loud warning on boot otherwise so
# operators running shared-Redis topologies have to make an explicit
# choice.
_cache_key_prefix = env("DJANGO_CACHE_KEY_PREFIX", default="")
if _cache_key_prefix:
    CACHES["default"]["KEY_PREFIX"] = _cache_key_prefix
elif REDIS_URL:
    import logging as _cache_logging

    _cache_logging.getLogger(__name__).warning(
        "DJANGO_CACHE_KEY_PREFIX is empty while REDIS_URL is set. "
        "Cache keys will not be namespaced. If this Redis instance "
        "is shared with other environments (staging, CI, dev), set "
        "DJANGO_CACHE_KEY_PREFIX to an environment-specific string "
        "to prevent cross-environment cache collisions."
    )

# MFA encryption key. Hard-required in production — we validate the key
# at import time so a misconfigured deploy fails before it can serve
# any traffic, rather than discovering the problem when the first user
# tries to enroll. We both check the env var is present AND try to
# construct a Fernet with it: a malformed key (wrong length, non-base64)
# gets caught now instead of exploding at first MFA use.
_mfa_key = env("DJANGO_MFA_ENCRYPTION_KEY", default=None)
if not _mfa_key:
    from django.core.exceptions import ImproperlyConfigured

    _mfa_key_msg = (
        "DJANGO_MFA_ENCRYPTION_KEY is required in production. Generate "
        'one with: python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())" and store it in GCP '
        "Secret Manager alongside DJANGO_SECRET_KEY."
    )
    raise ImproperlyConfigured(_mfa_key_msg)
try:
    # Import-time validation: proves the key is a well-formed Fernet
    # key. Same check the adapter runs per-call, but running it here
    # surfaces malformed keys before the app starts accepting traffic.
    from cryptography.fernet import Fernet as _Fernet

    _Fernet(_mfa_key if isinstance(_mfa_key, bytes) else _mfa_key.encode())
except ValueError as _exc:
    from django.core.exceptions import ImproperlyConfigured

    _mfa_key_msg = (
        "DJANGO_MFA_ENCRYPTION_KEY is malformed — Fernet expects a "
        "URL-safe base64-encoded 32-byte key. Regenerate with: "
        'python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())".'
    )
    raise ImproperlyConfigured(_mfa_key_msg) from _exc

# SECURITY
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-proxy-ssl-header
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-ssl-redirect
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-secure
SESSION_COOKIE_SECURE = True
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-name
SESSION_COOKIE_NAME = "__Secure-sessionid"
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-secure
CSRF_COOKIE_SECURE = True
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-name
CSRF_COOKIE_NAME = "__Secure-csrftoken"
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-trusted-origins
# Explicitly trust only the origins configured by the operator. This is needed
# when HTTPS terminates at a reverse proxy or cloud load balancer, and keeps the
# setting aligned with the documented DJANGO_CSRF_TRUSTED_ORIGINS contract.
CSRF_TRUSTED_ORIGINS = env.list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default=[],
)
# Language cookie - must be Secure on HTTPS or browsers won't store it reliably
# (especially with HSTS preload enabled)
LANGUAGE_COOKIE_SECURE = True
LANGUAGE_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_AGE = 31536000  # 1 year
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-seconds
# 31536000 = 1 year. The HSTS preload list (hstspreload.org) requires a
# minimum max-age of 1 year. The previous value of 2592000 (30 days) did
# not meet this requirement.
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=31536000)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-include-subdomains
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=True,
)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-preload
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=True)
# https://docs.djangoproject.com/en/dev/ref/settings/#x-content-type-options-nosniff
SECURE_CONTENT_TYPE_NOSNIFF = env.bool(
    "DJANGO_SECURE_CONTENT_TYPE_NOSNIFF",
    default=True,
)

# STORAGE
# ------------------------------------------------------------------------------
# Storage configuration varies by deployment target:
#
# GCP: Uses GCS bucket (required)
# Docker Compose: Uses local filesystem by default, or GCS if configured
# AWS: Not implemented
#
# All targets use a single bucket/directory with prefix-based separation:
#   ├── public/      # Publicly accessible (avatars, workflow images)
#   └── private/     # Private files (submissions, validation data)

STORAGE_BUCKET = env("STORAGE_BUCKET", default=None)
DATA_STORAGE_BACKEND = env("DATA_STORAGE_BACKEND", default=None)

if DEPLOYMENT_TARGET == "gcp":
    # GCP requires a storage bucket
    if not STORAGE_BUCKET:
        raise django.core.exceptions.ImproperlyConfigured(
            "STORAGE_BUCKET is required for GCP deployment."
        )

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
            "OPTIONS": {
                "bucket_name": STORAGE_BUCKET,
                "location": "private",
                "file_overwrite": False,
                "querystring_auth": True,
            },
        },
        "public": {
            "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
            "OPTIONS": {
                "bucket_name": STORAGE_BUCKET,
                "location": "public",
                "file_overwrite": False,
                "querystring_auth": False,
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    MEDIA_URL = f"https://storage.googleapis.com/{STORAGE_BUCKET}/public/"

    # Data storage for validation files
    DATA_STORAGE_BACKEND = "gcs"
    DATA_STORAGE_OPTIONS = {
        "bucket_name": STORAGE_BUCKET,
        "prefix": "private",
    }

elif DEPLOYMENT_TARGET == "aws":
    # AWS requires a storage bucket
    if not STORAGE_BUCKET:
        raise django.core.exceptions.ImproperlyConfigured(
            "STORAGE_BUCKET is required for AWS deployment."
        )

    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default=None)
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default=None)

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "bucket_name": STORAGE_BUCKET,
                "location": "private",
                "file_overwrite": False,
                "querystring_auth": True,
                "region_name": AWS_S3_REGION_NAME,
                "endpoint_url": AWS_S3_ENDPOINT_URL,
            },
        },
        "public": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "bucket_name": STORAGE_BUCKET,
                "location": "public",
                "file_overwrite": False,
                "querystring_auth": False,
                "region_name": AWS_S3_REGION_NAME,
                "endpoint_url": AWS_S3_ENDPOINT_URL,
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    MEDIA_URL = f"https://{STORAGE_BUCKET}.s3.amazonaws.com/public/"

    # Data storage for validation files
    DATA_STORAGE_BACKEND = "s3"
    DATA_STORAGE_OPTIONS = {
        "bucket_name": STORAGE_BUCKET,
        "prefix": "private",
        "region_name": AWS_S3_REGION_NAME,
        "endpoint_url": AWS_S3_ENDPOINT_URL,
    }

# Docker Compose can use local storage or cloud storage
elif STORAGE_BUCKET:
    # Cloud storage configured
    if DATA_STORAGE_BACKEND == "gcs":
        STORAGES = {
            "default": {
                "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
                "OPTIONS": {
                    "bucket_name": STORAGE_BUCKET,
                    "location": "private",
                    "file_overwrite": False,
                    "querystring_auth": True,
                },
            },
            "public": {
                "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
                "OPTIONS": {
                    "bucket_name": STORAGE_BUCKET,
                    "location": "public",
                    "file_overwrite": False,
                    "querystring_auth": False,
                },
            },
            "staticfiles": {
                "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
            },
        }
        MEDIA_URL = f"https://storage.googleapis.com/{STORAGE_BUCKET}/public/"
        DATA_STORAGE_OPTIONS = {
            "bucket_name": STORAGE_BUCKET,
            "prefix": "private",
        }
    elif DATA_STORAGE_BACKEND == "s3":
        AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default=None)
        AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default=None)

        STORAGES = {
            "default": {
                "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
                "OPTIONS": {
                    "bucket_name": STORAGE_BUCKET,
                    "location": "private",
                    "file_overwrite": False,
                    "querystring_auth": True,
                    "region_name": AWS_S3_REGION_NAME,
                    "endpoint_url": AWS_S3_ENDPOINT_URL,
                },
            },
            "public": {
                "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
                "OPTIONS": {
                    "bucket_name": STORAGE_BUCKET,
                    "location": "public",
                    "file_overwrite": False,
                    "querystring_auth": False,
                    "region_name": AWS_S3_REGION_NAME,
                    "endpoint_url": AWS_S3_ENDPOINT_URL,
                },
            },
            "staticfiles": {
                "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
            },
        }
        MEDIA_URL = f"https://{STORAGE_BUCKET}.s3.amazonaws.com/public/"
        DATA_STORAGE_OPTIONS = {
            "bucket_name": STORAGE_BUCKET,
            "prefix": "private",
            "region_name": AWS_S3_REGION_NAME,
            "endpoint_url": AWS_S3_ENDPOINT_URL,
        }
    else:
        raise django.core.exceptions.ImproperlyConfigured(
            f"DATA_STORAGE_BACKEND must be 'gcs' or 's3' when STORAGE_BUCKET is set, "
            f"got: {DATA_STORAGE_BACKEND}"
        )
else:
    # Local filesystem storage (default for simple Docker Compose deployments)
    STORAGE_ROOT = BASE_DIR / "storage"
    PUBLIC_STORAGE_ROOT = STORAGE_ROOT / "public"
    PRIVATE_STORAGE_ROOT = STORAGE_ROOT / "private"
    MEDIA_ROOT = PUBLIC_STORAGE_ROOT
    MEDIA_URL = "/media/"

    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {
                "location": str(PRIVATE_STORAGE_ROOT),
                "base_url": "/private-media/",
            },
        },
        "public": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {
                "location": str(PUBLIC_STORAGE_ROOT),
                "base_url": "/media/",
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }

    DATA_STORAGE_BACKEND = "local"
    DATA_STORAGE_ROOT = str(PRIVATE_STORAGE_ROOT)
    DATA_STORAGE_OPTIONS = {"root": DATA_STORAGE_ROOT}

# VALIDATOR RUNNER
# ------------------------------------------------------------------------------
# The validator runner executes advanced validator containers.
#
# GCP: managed Cloud Run Services and Jobs (async with callbacks)
# Docker Compose: Local Docker socket (synchronous)
# AWS: AWS Batch (future)

if DEPLOYMENT_TARGET == "gcp":
    VALIDATOR_RUNNER = "google_cloud_run"
    VALIDATOR_RUNNER_OPTIONS = {
        "project_id": env("GCP_PROJECT_ID"),
        "region": env("GCP_REGION", default="us-west1"),
    }

    # Cloud Run Job names come from each validator's
    # ``ValidatorConfig.cloud_run_job_name`` (which defaults to the same
    # string as ``image_name``). The launcher resolves them via
    # ``validations/services/cloud_run/launcher._resolve_cloud_run_job_name``
    # at dispatch time. There are no per-validator env vars to set —
    # the legacy ``GCS_ENERGYPLUS_JOB_NAME`` / ``GCS_FMU_JOB_NAME``
    # vars were removed in May 2026 because they had been silently
    # unset in prod Secret Manager for months and were causing runs
    # to fail with empty-job-name errors.
    GCS_VALIDATION_BUCKET = STORAGE_BUCKET
    GCS_TASK_QUEUE_NAME = env("GCS_TASK_QUEUE_NAME", default="validibot-tasks")
    # GCS authority is a fixed Cloud Run contract, not an operator-selectable
    # rollout mode. Every execution receives one short-lived token restricted
    # to its attempt prefix. The validator runtime identity must have no
    # ambient object permissions; stage provisioning removes historical roles
    # and production acceptance proves effective denial with Policy
    # Troubleshooter before traffic is enabled.

elif DEPLOYMENT_TARGET == "aws":
    # AWS Batch runner (future implementation)
    VALIDATOR_RUNNER = env("VALIDATOR_RUNNER", default="aws_batch")
    VALIDATOR_RUNNER_OPTIONS = {
        "region": env("AWS_REGION", default="us-east-1"),
        # Additional AWS Batch configuration will go here
    }

else:  # self_hosted (DeploymentTarget.SELF_HOSTED — single-VM Docker Compose)
    VALIDATOR_RUNNER = env("VALIDATOR_RUNNER", default="docker")
    VALIDATOR_RUNNER_OPTIONS = {
        "memory_limit": env("VALIDATOR_MEMORY_LIMIT", default="4g"),
        "cpu_limit": env("VALIDATOR_CPU_LIMIT", default="2.0"),
        "network": env("VALIDATOR_NETWORK", default=None),
        "timeout_seconds": VALIDATOR_TIMEOUT_SECONDS,  # noqa: F405
    }

    # Container image configuration for advanced validators
    VALIDATOR_IMAGE_TAG = env("VALIDATOR_IMAGE_TAG", default="latest")
    VALIDATOR_IMAGE_REGISTRY = env("VALIDATOR_IMAGE_REGISTRY", default="")

    # Advanced validator images to enable (for sync_validators command)
    ADVANCED_VALIDATOR_IMAGES = env.list("ADVANCED_VALIDATOR_IMAGES", default=[])

# Site URL for callbacks
SITE_URL = env("SITE_URL", default="http://localhost:8000")
# WORKER_URL is the worker service's URL origin. In multi-service deployments
# (GCP) the worker is a SEPARATE, internal Cloud Run service with its own
# *.run.app URL, distinct from the public web service (SITE_URL). We keep the
# SITE_URL fallback for single-service / dev convenience, but capture whether
# WORKER_URL was set explicitly so the GCP boot check below can reject the
# silent fallback (which would mis-route callbacks/tasks to the public service).
_worker_url_explicit = env("WORKER_URL", default="").strip()
WORKER_URL = _worker_url_explicit or SITE_URL

# WORKER-ENDPOINT OIDC VERIFICATION (GCP only)
# ------------------------------------------------------------------------------
# Fail-fast boot check. When DEPLOYMENT_TARGET=gcp the worker service uses
# ``CloudTasksOIDCAuthentication`` (see validibot/core/api/task_auth.py) to
# verify inbound OIDC tokens on every callback and scheduled-task request.
# Two settings must resolve to non-empty values, either directly or via their
# documented fallbacks:
#
#   * TASK_OIDC_AUDIENCE               → falls back to WORKER_URL
#   * TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS → falls back to [CLOUD_TASKS_SERVICE_ACCOUNT]
#
# If we let Django boot with either resolved to empty, EVERY worker endpoint
# call would 401 — validator callbacks, Cloud Tasks dispatches, and
# scheduled tasks would all silently fail. ImproperlyConfigured at boot
# time surfaces the misconfig in the Cloud Run deploy log instead of in
# production traffic.
if DEPLOYMENT_TARGET == "gcp":
    # On GCP the worker is a SEPARATE, internal Cloud Run service with its own
    # *.run.app URL — distinct from the public web service (SITE_URL, often a
    # custom domain like app.validibot.com). WORKER_URL is BOTH the callback
    # target the validator jobs POST to AND the OIDC audience the dispatcher
    # mints for worker-bound tasks. If it silently fell back to SITE_URL,
    # callbacks and Cloud Tasks would route to the public web service instead
    # of the worker, with only a log warning. Requiring it to be set explicitly
    # turns that latent mis-route into a loud, deploy-time error.
    if not _worker_url_explicit:
        raise ImproperlyConfigured(
            "DEPLOYMENT_TARGET=gcp requires WORKER_URL to be set explicitly to "
            "the worker service's URL origin (scheme + host, no path), e.g. "
            "https://validibot-worker-xxxx.a.run.app. Without it, validator "
            "callbacks and Cloud Tasks would silently route to the public web "
            "service (SITE_URL). See "
            "docs/dev_docs/deployment/environment-configuration.md."
        )

    _oidc_audience = (TASK_OIDC_AUDIENCE or WORKER_URL or "").strip()  # noqa: F405
    if not _oidc_audience:
        raise ImproperlyConfigured(
            "DEPLOYMENT_TARGET=gcp requires an OIDC audience. Set "
            "TASK_OIDC_AUDIENCE (or WORKER_URL) to the worker service URL "
            "origin (scheme + host, no path). See "
            "docs/dev_docs/deployment/environment-configuration.md."
        )

    _oidc_allowlist = [
        sa.strip().lower()
        for sa in (TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS or [])  # noqa: F405
        if sa.strip()
    ]
    if not _oidc_allowlist:
        _fallback_sa = (CLOUD_TASKS_SERVICE_ACCOUNT or "").strip()  # noqa: F405
        if not _fallback_sa:
            raise ImproperlyConfigured(
                "DEPLOYMENT_TARGET=gcp requires at least one allowlisted "
                "service account for worker-endpoint OIDC verification. "
                "Set TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS (comma-separated "
                "emails) or CLOUD_TASKS_SERVICE_ACCOUNT. Empty allowlist "
                "would reject every validator callback and Cloud Tasks "
                "dispatch."
            )

    # The legacy ``/api/v1/mcp/*`` compatibility adapter retains optional
    # service-to-service OIDC while Cloud x402 dependencies are unwound. This
    # is unrelated to the embedded `/mcp` endpoint. If the compatibility
    # audience is configured, its allowlist must fail closed.
    if MCP_OIDC_AUDIENCE:  # noqa: F405
        _mcp_allowlist = [
            sa.strip().lower()
            for sa in (MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS or [])  # noqa: F405
            if sa.strip()
        ]
        if not _mcp_allowlist:
            raise ImproperlyConfigured(
                "DEPLOYMENT_TARGET=gcp with MCP_OIDC_AUDIENCE set "
                "requires MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS (comma-"
                "separated Cloud Run service-account emails). Without "
                "an allowlist, any Google SA that can mint a token "
                "with our audience would be authorised — effectively "
                "broken service-to-service auth."
            )

# Fail-fast boot check for non-GCP targets. On DEPLOYMENT_TARGET in
# {self_hosted, aws} the worker-only endpoints (execute-validation-run,
# validation-callbacks, scheduled-task triggers — see
# config/urls_worker.py) are protected at the application layer ONLY by
# ``WorkerKeyAuthentication`` (a shared secret), because there is no
# infrastructure IAM to fall back on as there is on GCP. That class
# *abstains* (returns None, which DRF treats as "no opinion") when
# WORKER_API_KEY is empty, and ``WorkerOnlyAPIView`` carries no permission
# gate, so an empty key leaves those endpoints unauthenticated — anyone who
# can reach the worker service could forge run completions (mark failing
# validations PASSED, defeating the attestation guarantee) or trigger runs.
# Requiring the key at boot turns that latent fail-open into a loud,
# deploy-time error instead of a silent production exposure.
if DEPLOYMENT_TARGET in {"self_hosted", "aws"} and not (WORKER_API_KEY or "").strip():  # noqa: F405
    raise ImproperlyConfigured(
        f"DEPLOYMENT_TARGET={DEPLOYMENT_TARGET} requires WORKER_API_KEY to "
        "be set to a strong shared secret. It is the only authentication on "
        "the worker-only API endpoints (execute-validation-run, "
        "validation-callbacks, scheduled tasks); leaving it empty would "
        "expose them unauthenticated. Generate one with: "
        'python -c "import secrets; print(secrets.token_urlsafe(48))".',
    )

# EMAIL
# ------------------------------------------------------------------------------
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = env("DJANGO_EMAIL_SUBJECT_PREFIX", default="[Validibot] ")
ACCOUNT_EMAIL_SUBJECT_PREFIX = EMAIL_SUBJECT_PREFIX

# Check for various email backends
POSTMARK_SERVER_TOKEN = env("POSTMARK_SERVER_TOKEN", default=None)
EMAIL_HOST = env("EMAIL_HOST", default=None)

if POSTMARK_SERVER_TOKEN:
    # Postmark (via Anymail)
    INSTALLED_APPS += ["anymail"]
    EMAIL_BACKEND = "anymail.backends.postmark.EmailBackend"
    ANYMAIL = {
        "POSTMARK_SERVER_TOKEN": POSTMARK_SERVER_TOKEN,
    }
elif EMAIL_HOST:
    # Generic SMTP
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = env.int("EMAIL_PORT", default=587)
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
else:
    raise ImproperlyConfigured(
        "Production requires an email backend. Set POSTMARK_SERVER_TOKEN "
        "or EMAIL_HOST in the environment."
    )

# ADMIN
# ------------------------------------------------------------------------------
ADMIN_URL = env("DJANGO_ADMIN_URL", default="admin/")
# Production fails closed for privileged browser access. Routing the initial
# sign-in through allauth prevents the plain Django password form from being a
# bypass; the separate middleware gate also requires an enrolled factor and an
# MFA authentication record tied to the current session.
DJANGO_ADMIN_FORCE_ALLAUTH = env.bool("DJANGO_ADMIN_FORCE_ALLAUTH", default=True)
DJANGO_ADMIN_REQUIRE_MFA = env.bool("DJANGO_ADMIN_REQUIRE_MFA", default=True)

if DJANGO_ADMIN_REQUIRE_MFA and not ACCOUNT_ALLOW_LOGIN:  # noqa: F405
    raise ImproperlyConfigured(
        "DJANGO_ADMIN_REQUIRE_MFA requires DJANGO_ACCOUNT_ALLOW_LOGIN=True "
        "so staff can enrol and complete the allauth MFA challenge.",
    )

# LOGGING
# ------------------------------------------------------------------------------
# Use JSON logging for production (compatible with most log aggregators)
LOGGING["formatters"]["json"] = {
    "()": "pythonjsonlogger.json.JsonFormatter",
    "format": "%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s %(message)s",
    "rename_fields": {"levelname": "severity"},
}
LOGGING["handlers"]["console"]["formatter"] = "json"
LOGGING["root"]["level"] = "INFO"

# Add production-specific logger configuration
LOGGING["loggers"] = {
    "django.db.backends": {
        "level": "ERROR",
        "handlers": ["console"],
        "propagate": False,
    },
    "django.request": {
        "level": "ERROR",
        "handlers": ["console"],
        "propagate": False,
    },
    "sentry_sdk": {
        "level": "ERROR",
        "handlers": ["console"],
        "propagate": False,
    },
    "django.security.DisallowedHost": {
        "level": "ERROR",
        "handlers": ["console"],
        "propagate": False,
    },
    "validibot.users": {
        "level": "INFO",
        "handlers": ["console"],
        "propagate": False,
    },
    "validibot.validations": {
        "level": "INFO",
        "handlers": ["console"],
        "propagate": False,
    },
}

# SENTRY (Optional)
# ------------------------------------------------------------------------------
SENTRY_DSN = env("SENTRY_DSN", default=None)
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_logging = LoggingIntegration(
        level=logging.INFO,
        event_level=logging.ERROR,
    )
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[sentry_logging, DjangoIntegration()],
        environment=env("SENTRY_ENVIRONMENT", default=DEPLOYMENT_TARGET),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        ignore_errors=[django.core.exceptions.DisallowedHost],
    )

# SUPERUSER BOOTSTRAP
# ------------------------------------------------------------------------------
# Used by setup_validibot management command to create initial superuser
SUPERUSER_USERNAME = env("SUPERUSER_USERNAME", default=None)
SUPERUSER_PASSWORD = env("SUPERUSER_PASSWORD", default=None)
SUPERUSER_EMAIL = env("SUPERUSER_EMAIL", default=None)
SUPERUSER_NAME = env("SUPERUSER_NAME", default=None)

# django-rest-framework
# -------------------------------------------------------------------------------
if SITE_URL and SITE_URL != "http://localhost:8000":
    SPECTACULAR_SETTINGS["SERVERS"] = [
        {
            "url": SITE_URL,
            "description": "Production server",
        },
    ]
