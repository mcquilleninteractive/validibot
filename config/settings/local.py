from .base import *  # noqa: F403
from .base import INSTALLED_APPS
from .base import env

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = True

# DEPLOYMENT TARGET
# ------------------------------------------------------------------------------
# Local Docker Compose uses HTTP calls to worker and Docker for validators.
# Can be overridden via environment variable for testing other targets.
DEPLOYMENT_TARGET = env("DEPLOYMENT_TARGET", default="local_docker_compose")
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default=None,
)
if not SECRET_KEY:
    raise ValueError("DJANGO_SECRET_KEY must be set in local development")

# MFA secret-material encryption key. Separate from SECRET_KEY so the
# two can be rotated independently. See validibot/users/mfa_adapter.py.
# Generate with:
#   python -c "from cryptography.fernet import Fernet; \
#       print(Fernet.generate_key().decode())"
MFA_ENCRYPTION_KEY = env("DJANGO_MFA_ENCRYPTION_KEY", default=None)
if not MFA_ENCRYPTION_KEY:
    raise ValueError(
        "DJANGO_MFA_ENCRYPTION_KEY must be set in local development. "
        "Generate one with: "
        "python -c 'from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())'",
    )

# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
# "web" is the Compose service name. Internal callers reach Django as
# http://web:8000, so their Host header is "web", not localhost.
ALLOWED_HOSTS = ["localhost", "0.0.0.0", "127.0.0.1", "web"]  # noqa: S104

# CACHES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#caches
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "",
    },
}

# STORAGE
# -------------------------------------------------------------------------------
# Validibot uses a single storage location with public/ and private/ prefixes.
#
# For local development, files are stored in:
#   storage/
#   ├── public/      # Public media (avatars, workflow images) - served directly
#   └── private/     # Private files (submissions, artifacts) - signed URL access
#
# To test with GCS locally, set STORAGE_BUCKET and run:
#   gcloud auth application-default login
STORAGE_BUCKET = env("STORAGE_BUCKET", default=None)

if STORAGE_BUCKET:
    # Use GCS for all files (matches production)
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

    # Data storage uses same bucket with private/ prefix
    DATA_STORAGE_BACKEND = "gcs"
    DATA_STORAGE_BUCKET = STORAGE_BUCKET
    DATA_STORAGE_PREFIX = "private"
    DATA_STORAGE_OPTIONS = {
        "bucket_name": DATA_STORAGE_BUCKET,
        "prefix": DATA_STORAGE_PREFIX,
    }
else:
    # Use local filesystem (default for local development)
    # Single storage root with public/ and private/ subdirectories
    STORAGE_ROOT = BASE_DIR / "storage"  # noqa: F405
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

    # Data storage uses private/ subdirectory
    DATA_STORAGE_ROOT = str(PRIVATE_STORAGE_ROOT)
    DATA_STORAGE_OPTIONS = {"root": DATA_STORAGE_ROOT}

# EMAIL
# ------------------------------------------------------------------------------
# Console backend prints emails to terminal (no mail server needed)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
# To use Mailpit instead, comment above and uncomment below, then run `mailpit`
# EMAIL_HOST = "localhost"
# EMAIL_PORT = 1025

# WhiteNoise
# ------------------------------------------------------------------------------
# http://whitenoise.evans.io/en/latest/django.html#using-whitenoise-in-development
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *INSTALLED_APPS]


# django-debug-toolbar
# ------------------------------------------------------------------------------
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#prerequisites
# INSTALLED_APPS += ["debug_toolbar"]
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#middleware
# MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]
# https://django-debug-toolbar.readthedocs.io/en/latest/configuration.html#debug-toolbar-config
DEBUG_TOOLBAR_CONFIG = {
    "DISABLE_PANELS": [
        "debug_toolbar.panels.redirects.RedirectsPanel",
        # Disable profiling panel due to an issue with Python 3.13:
        # https://github.com/jazzband/django-debug-toolbar/issues/1875
        "debug_toolbar.panels.profiling.ProfilingPanel",
    ],
    "SHOW_TEMPLATE_CONTEXT": True,
}
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#internal-ips
INTERNAL_IPS = ["127.0.0.1", "10.0.2.2"]


# django-extensions
# ------------------------------------------------------------------------------
# https://django-extensions.readthedocs.io/en/latest/installation_instructions.html#configuration
INSTALLED_APPS += ["django_extensions"]

# Validibot settings for local development
# ------------------------------------------------------------------------------

# VALIDATOR RUNNER
# ------------------------------------------------------------------------------
# For local development, default to Docker for running validator containers.
# This uses the same backend as Docker Compose deployments.
#
# To test with GCP Cloud Run locally, set:
#   VALIDATOR_RUNNER=google_cloud_run
#   GCP_PROJECT_ID=your-project
#   GCS_VALIDATION_BUCKET=your-bucket
VALIDATOR_RUNNER = env("VALIDATOR_RUNNER", default="docker")
VALIDATOR_RUNNER_OPTIONS = {
    "memory_limit": env("VALIDATOR_MEMORY_LIMIT", default="4g"),
    "cpu_limit": env("VALIDATOR_CPU_LIMIT", default="2.0"),
    "network": env("VALIDATOR_NETWORK", default=None),
    "timeout_seconds": VALIDATOR_TIMEOUT_SECONDS,  # noqa: F405
    # Named volume for storage (validator containers mount this)
    "storage_volume": env("VALIDATOR_STORAGE_VOLUME", default=None),
    # Mount path for storage volume inside validator containers
    "storage_mount_path": env("VALIDATOR_STORAGE_MOUNT_PATH", default="/app/storage"),
}

# Container images for advanced validators
VALIDATOR_IMAGE_TAG = env("VALIDATOR_IMAGE_TAG", default="latest")
VALIDATOR_IMAGE_REGISTRY = env("VALIDATOR_IMAGE_REGISTRY", default="")

# Site URL for callbacks
SITE_URL = env("SITE_URL", default="http://localhost:8000")
WORKER_URL = env("WORKER_URL", default=SITE_URL)

SUPERUSER_USERNAME = env("SUPERUSER_USERNAME", default="admin")
SUPERUSER_PASSWORD = env("SUPERUSER_PASSWORD", default="someadminpwchangeforrealz")
SUPERUSER_EMAIL = env("SUPERUSER_EMAIL", default="")
SUPERUSER_NAME = env("SUPERUSER_NAME", default="Admin User")


TEST_ENERGYPLUS_LIVE_MODAL = env.bool("TEST_ENERGYPLUS_LIVE_MODAL", default=False)


SIMULATE_LONG_TASKS = env.bool("SIMULATE_LONG_TASKS", default=True)
LONG_TASK_DELAY_SECONDS = env.int("LONG_TASK_DELAY_SECONDS", default=5)


# Logging
# ------------------------------------------------------------------------------
# Make local development chatty so timing diagnostics show up immediately.
LOGGING["root"]["level"] = "DEBUG"  # noqa: F405
LOGGING["loggers"]["validibot"] = {  # noqa: F405
    "handlers": ["console"],
    "level": "DEBUG",
    "propagate": False,
}
