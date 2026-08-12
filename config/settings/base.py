# ruff: noqa: E402, E501, S105
#
# File-level ignores:
# * E402 — Django settings idiomatically group imports next to the
#   settings they build (OIDC imports sit deep in the file next to
#   the IDP_OIDC_* settings block). Auto-formatters strip per-line
#   suppression markers, so we ignore the rule file-wide.
# * E501 — wide settings lines are readable; line-length noise here
#   obscures the settings structure.
# * S105 — tokens-by-format-name ("jwt") trigger ``possible hardcoded
#   password`` false positives. There are no real secrets in this
#   file; all sensitive values come from ``env(...)``.
"""Base settings to build other settings files upon."""

import logging
from pathlib import Path

import environ
from csp.constants import NONCE
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent
# validibot/
APPS_DIR = BASE_DIR / "validibot"
env = environ.Env()

READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=False)
if READ_DOT_ENV_FILE:
    # OS environment variables take precedence over variables from .env
    env.read_env(str(BASE_DIR / ".env"))

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = env.bool("DJANGO_DEBUG", False)

# DEPLOYMENT TARGET
# ------------------------------------------------------------------------------
# Identifies the deployment environment for selecting appropriate backends.
# This setting controls task dispatching, validator execution, and storage.
#
# Valid values (from validibot.core.constants.DeploymentTarget):
#   - "test": Test environment (synchronous inline execution)
#   - "local_docker_compose": Local Docker Compose dev stack (developer laptop)
#   - "self_hosted": Customer-operated single-VM deployment (Docker Compose)
#   - "gcp": Google Cloud Platform (Cloud Tasks + Cloud Run Jobs)
#   - "aws": Amazon Web Services (SQS + ECS/Batch) - future
#
# See ADR-2026-04-27 (Boring Self-Hosting and Operator Experience) for the
# audience-named taxonomy. ``self_hosted`` was renamed from ``docker_compose``
# in Phase 0 to match the operator-facing module rename.
#
# If not set, auto-detection is used based on other settings.
DEPLOYMENT_TARGET = env("DEPLOYMENT_TARGET", default=None)

# App role (web vs worker). Worker instances expose internal APIs only.
# Default to "web" for local development; production sets APP_ROLE explicitly.
APP_ROLE = env(
    "APP_ROLE",
    default="web",
)
APP_IS_WORKER = APP_ROLE.lower() == "worker"

# Runtime version stamp used by operator backup/restore compatibility checks.
# Validibot app version, surfaced in backup manifests, support bundles, doctor
# output, and the OCI ``org.opencontainers.image.version`` label.
#
# Deploy recipes (``just gcp deploy-all``, ``just self-hosted bootstrap``,
# etc.) compute this by reading ``project.version`` from pyproject.toml at
# build time and pass it as a ``--build-arg VALIDIBOT_VERSION=v0.4.0`` to
# the Docker build, where the Dockerfile stamps it as both an env var and
# an OCI label. Local / test deployments leave this blank; the helper
# ``validibot.core.deployment.get_validibot_runtime_version()`` falls back
# to installed package metadata in that case.
#
# The pyproject.toml-driven path is the agreed source of truth — no git tag
# resolution and no separate version file. See ``just/common.just`` and the
# top of ``just/self-hosted/mod.just`` and ``just/gcp/mod.just`` for the
# resolver. If the source-of-truth choice ever changes, update this comment
# AND the resolver together.
VALIDIBOT_VERSION = env("VALIDIBOT_VERSION", default="")

# Local time zone. Choices are
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# though not all of them may be available with every OS.
# In Windows, this must be set to your system time zone.
TIME_ZONE = "UTC"
# https://docs.djangoproject.com/en/dev/ref/settings/#language-code
LANGUAGE_CODE = "en-us"
# https://docs.djangoproject.com/en/dev/ref/settings/#languages
LANGUAGES = [
    ("en", _("English")),
    ("fr", _("French")),
    ("ja", _("Japanese")),
    ("es", _("Spanish")),
]
# https://docs.djangoproject.com/en/dev/ref/settings/#site-id
SITE_ID = 1
# https://docs.djangoproject.com/en/dev/ref/settings/#use-i18n
USE_I18N = True
# https://docs.djangoproject.com/en/dev/ref/settings/#use-tz
USE_TZ = True
# https://docs.djangoproject.com/en/dev/ref/settings/#locale-paths
LOCALE_PATHS = [str(BASE_DIR / "locale")]

# DATABASES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#databases

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres:///validibot",
    ),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
# https://docs.djangoproject.com/en/stable/ref/settings/#std:setting-DEFAULT_AUTO_FIELD
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# URLS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#root-urlconf
ROOT_URLCONF = "config.urls"
# https://docs.djangoproject.com/en/dev/ref/settings/#wsgi-application
WSGI_APPLICATION = "config.wsgi.application"
# https://docs.djangoproject.com/en/dev/ref/settings/#asgi-application
# ASGI_APPLICATION = "config.asgi.application"

GITHUB_APP_ENABLED = env.bool("GITHUB_APP_ENABLED", False)

# APPS
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",  # Handy template tags
    "django.contrib.admin",
    "django.contrib.sitemaps",
    "django.forms",
    "django.contrib.flatpages",
    "django.contrib.postgres",
]
THIRD_PARTY_APPS = [
    "crispy_forms",
    "crispy_bootstrap5",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "allauth.socialaccount",
    # OIDC authorization server powering MCP OAuth. Listed immediately
    # before "validibot.idp" below — the custom adapter defined in that
    # app is resolved via the ``IDP_OIDC_ADAPTER`` setting at runtime.
    "allauth.idp.oidc",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "drf_spectacular",
    "drf_spectacular_sidecar",  # Serves Swagger UI/ReDoc assets locally
    "django_filters",
    "markdownify",
    "django_recaptcha",
    "django_celery_beat",  # Periodic task scheduling via database
]

LOCAL_APPS = [
    "validibot.core",
    "validibot.users",
    "validibot.validations",
    "validibot.actions",
    "validibot.projects",
    "validibot.events",
    "validibot.tracking",
    "validibot.submissions",
    "validibot.integrations",
    "validibot.workflows",
    "validibot.dashboard",
    "validibot.home",
    "validibot.members",
    "validibot.help",
    "validibot.notifications",
    # Validibot OIDC customizations (custom adapter, discovery views,
    # ensure_oidc_clients management command). Required for MCP OAuth.
    "validibot.idp.apps.ValidibotIDPConfig",
    # Community MCP helper API served under /api/v1/mcp/. The FastMCP
    # server in mcp/ calls these endpoints — without the app registered
    # self-hosted deployments 404 on every tool call.
    "validibot.mcp_api.apps.MCPAPIConfig",
    # Audit log — append-only Pillar-3 observability store. Community-
    # hosted so self-hosted Pro deployments get audit logs too; the
    # Pro-gated UI is added in a later phase.
    "validibot.audit.apps.AuditConfig",
    # Advanced analytics dashboards (Pillar 2). Bare-bones today —
    # every view is Pro-gated by FeatureRequiredMixin(ADVANCED_ANALYTICS)
    # so community deployments 404 on the URLs.
    "validibot.analytics.apps.AnalyticsConfig",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#installed-apps
# Commercial editions are enabled explicitly by adding their Django apps to
# INSTALLED_APPS in an environment-specific settings module. Installing the wheel
# is not enough on its own.
#
# Example:
# INSTALLED_APPS += ["validibot_pro"]
#
# Enterprise currently depends on Pro feature registration, so Enterprise
# installs should include both apps:
# INSTALLED_APPS += ["validibot_pro", "validibot_enterprise"]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MARKDOWNIFY = {
    "default": {
        "MARKDOWN_EXTENSIONS": [
            "markdown.extensions.tables",
            "markdown.extensions.fenced_code",
            "markdown.extensions.codehilite",
        ],
        "MARKDOWN_EXTENSION_CONFIGS": {
            "markdown.extensions.codehilite": {
                "css_class": "highlight",
                "guess_lang": False,
            },
        },
        "WHITELIST_TAGS": [
            "a",
            "abbr",
            "acronym",
            "b",
            "blockquote",
            "br",
            "code",
            "div",
            "em",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "hr",
            "i",
            "li",
            "ol",
            "p",
            "pre",
            "span",
            "strong",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
        ],
        "WHITELIST_ATTRS": {
            "a": ["href", "title", "target", "rel"],
            "code": ["class"],
            "div": ["class"],
            "span": ["class"],
            "td": ["align"],
            "th": ["align"],
        },
    },
}

# MIGRATIONS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#migration-modules
MIGRATION_MODULES = {"sites": "validibot.contrib.sites.migrations"}

# AUTHENTICATION
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#authentication-backends
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "validibot.users.permissions.OrgPermissionBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-user-model
AUTH_USER_MODEL = "users.User"
# https://docs.djangoproject.com/en/dev/ref/settings/#login-redirect-url
LOGIN_REDIRECT_URL = "users:redirect"
# https://docs.djangoproject.com/en/dev/ref/settings/#login-url
LOGIN_URL = "account_login"

# PASSWORDS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#password-hashers
PASSWORD_HASHERS = [
    # https://docs.djangoproject.com/en/dev/topics/auth/passwords/#using-argon2-with-django
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# MIDDLEWARE
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must come AFTER AuthenticationMiddleware so ``request.user`` is
    # already resolved, and BEFORE any view-dispatch boundary so
    # signal handlers fired from within a view can read the context.
    "validibot.audit.middleware.AuditContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
    "django_permissions_policy.PermissionsPolicyMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # Production enables this gate so every staff/superuser session must
    # prove a current allauth MFA factor before any admin view is dispatched.
    # It remains installed but is a no-op for local/test settings by default.
    "validibot.users.middleware.AdminMFAMiddleware",
]

# STATIC
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#static-root
STATIC_ROOT = str(BASE_DIR / "staticfiles")
# https://docs.djangoproject.com/en/dev/ref/settings/#static-url
STATIC_URL = "/static/"
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#std:setting-STATICFILES_DIRS
STATICFILES_DIRS = [str(APPS_DIR / "static")]
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#staticfiles-finders
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# MEDIA
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#media-root
MEDIA_ROOT = str(APPS_DIR / "media")
# https://docs.djangoproject.com/en/dev/ref/settings/#media-url
MEDIA_URL = "/media/"

# TEMPLATES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#templates
TEMPLATES = [
    {
        # https://docs.djangoproject.com/en/dev/ref/settings/#std:setting-TEMPLATES-BACKEND
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # https://docs.djangoproject.com/en/dev/ref/settings/#dirs
        # idp/templates is listed explicitly so Validibot-branded OIDC
        # overrides (``idp/oidc/authorization_form.html``) win over
        # allauth.idp.oidc's defaults. DIRS-level matches resolve before
        # the app_directories loader iterates INSTALLED_APPS.
        "DIRS": [
            str(APPS_DIR / "idp" / "templates"),
            str(APPS_DIR / "templates"),
        ],
        # https://docs.djangoproject.com/en/dev/ref/settings/#app-dirs
        "APP_DIRS": True,
        "OPTIONS": {
            # https://docs.djangoproject.com/en/dev/ref/settings/#template-context-processors
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.i18n",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.tz",
                "django.contrib.messages.context_processors.messages",
                "validibot.users.context_processors.allauth_settings",
                "validibot.core.context_processors.site_feature_settings",
                "validibot.core.context_processors.license_context",
                "validibot.core.context_processors.features_context",
                "validibot.users.context_processors.organization_context",
                "validibot.notifications.context_processors.notifications_context",
                # django-csp: makes {{ csp_nonce }} available in all templates.
                # Without this, the nonce is never generated and inline scripts
                # with nonce attributes will trigger CSP violations.
                "csp.context_processors.nonce",
            ],
        },
    },
]

# https://docs.djangoproject.com/en/dev/ref/settings/#form-renderer
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

# http://django-crispy-forms.readthedocs.io/en/latest/install.html#template-packs
CRISPY_TEMPLATE_PACK = "bootstrap5"
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"

# FIXTURES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#fixture-dirs
FIXTURE_DIRS = (str(APPS_DIR / "fixtures"),)

# SECURITY
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-httponly
SESSION_COOKIE_HTTPONLY = True
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-httponly
# Safe: no frontend JS reads the CSRF cookie via document.cookie — HTMx
# sends csrfmiddlewaretoken in form bodies, so HttpOnly does not break AJAX.
# Verified 2026-06-17 (ADR 04-23 §hyg.csrf_cookie_httponly). Keep new fetch/
# axios code on the form-token pattern, not cookie-reading.
CSRF_COOKIE_HTTPONLY = True
# https://docs.djangoproject.com/en/dev/ref/settings/#x-frame-options
X_FRAME_OPTIONS = "DENY"

# EMAIL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#email-backend
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#email-timeout
EMAIL_TIMEOUT = 5

# ADMIN
# ------------------------------------------------------------------------------
# Django Admin URL.
ADMIN_URL = "admin/"
# https://docs.djangoproject.com/en/dev/ref/settings/#admins
# Django 6.1+ prefers a list of email strings (tuples are deprecated).
# Set via DJANGO_ADMINS env var as comma-separated emails, e.g. "admin@example.com,ops@example.com"
ADMINS = env.list("DJANGO_ADMINS", default=[])
# https://docs.djangoproject.com/en/dev/ref/settings/#managers
MANAGERS = ADMINS
# https://cookiecutter-django.readthedocs.io/en/latest/settings.html#other-environment-settings
# Force the `admin` sign in process to go through the `django-allauth` workflow
DJANGO_ADMIN_FORCE_ALLAUTH = env.bool("DJANGO_ADMIN_FORCE_ALLAUTH", default=False)
# Require staff and superusers to enrol a primary MFA factor and prove it in
# the current session before Django dispatches any admin view. Production
# overrides the default to True; local development stays friction-free unless
# a developer explicitly opts in to exercise the hardened path.
DJANGO_ADMIN_REQUIRE_MFA = env.bool("DJANGO_ADMIN_REQUIRE_MFA", default=False)

# LOGGING
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#logging
# See https://docs.djangoproject.com/en/dev/topics/logging for
# more details on how to customize your logging configuration.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "django.request": {
            "level": "ERROR",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
REDIS_SSL = REDIS_URL.startswith("rediss://")

# Redis is retained for potential caching integrations.
# django-allauth
# ------------------------------------------------------------------------------
ACCOUNT_ALLOW_REGISTRATION = env.bool("DJANGO_ACCOUNT_ALLOW_REGISTRATION", True)
# https://docs.allauth.org/en/latest/account/configuration.html
# Allow users to sign in with either their username or their email address.
# Both are unique per user in our schema, so either works as an identifier.
ACCOUNT_LOGIN_METHODS = {"username", "email"}
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_ADAPTER = "validibot.users.adapters.AccountAdapter"
# https://docs.allauth.org/en/latest/account/forms.html
ACCOUNT_FORMS = {
    "signup": "validibot.users.forms.UserSignupForm",
    "login": "validibot.users.forms.UserLoginForm",
}
# https://docs.allauth.org/en/latest/socialaccount/configuration.html
SOCIALACCOUNT_ADAPTER = "validibot.users.adapters.SocialAccountAdapter"
# https://docs.allauth.org/en/latest/socialaccount/configuration.html
SOCIALACCOUNT_FORMS = {"signup": "validibot.users.forms.UserSocialSignupForm"}
# https://docs.allauth.org/en/latest/account/configuration.html
#
# Rate limits for allauth views. Format: "count/period/scope"
# Scopes: ip = per IP address, key = per key (email/user), user = per user
#
# Allauth provides sensible defaults for all keys (signup=20/m/ip,
# login=30/m/ip, confirm_email=1/3m/key, etc.). We override specific
# keys to tighten limits for our use case. Unspecified keys keep their
# allauth defaults.
ACCOUNT_RATE_LIMITS = {
    "login_failed": "5/5m",
    "signup": "5/m/ip",
    "confirm_email": "1/3m/key",
    "reset_password": "5/m/ip,3/m/key",
}

# Signup abuse protections.
# ------------------------------------------------------------------------------
# Both settings default to False so self-hosted community deployments
# (where admins often test with throwaway addresses like test@mailinator.com)
# are unaffected. Cloud turns them on in cloud.py because a hosted free
# trial is an attractive target for credit-farming and spam signups.
#
# REJECT_DISPOSABLE_EMAILS: when True, the UserSignupForm hard-rejects
#   emails whose domain appears in the ``disposable-email-domains`` PyPI
#   blocklist (~2800 throwaway providers). The user sees a clear error
#   asking them to use a non-disposable address. The blocklist is
#   maintained upstream; we refresh by bumping the package pin.
#
# USE_SIGNUP_HONEYPOT: when True, the UserSignupForm adds a hidden
#   ``company`` field. Real users never fill it; naive bots (and
#   solver-service bots that don't inspect the DOM) do. Submissions
#   where the honeypot is filled raise a generic validation error so
#   we don't signal to the bot that the field is the trap.
REJECT_DISPOSABLE_EMAILS = env.bool("REJECT_DISPOSABLE_EMAILS", default=False)
USE_SIGNUP_HONEYPOT = env.bool("USE_SIGNUP_HONEYPOT", default=False)

# MFA (django-allauth)
# ------------------------------------------------------------------------------
# Validibot ships with TOTP (authenticator apps) + recovery codes as the
# two opt-in second factors. Authenticators are a single polymorphic table
# keyed by `type`, so adding "webauthn" here later is a one-line change —
# no schema migration required. MFA is strictly opt-in: users enable it
# from the Security settings page and can disable it at any time.
# https://docs.allauth.org/en/latest/mfa/configuration.html
#
# There is no feature flag to disable MFA globally. If we ever need one
# (e.g. to turn MFA off for a deployment that previously had it on), the
# gap to close covers: (1) gating the `allauth.mfa` URL include in
# config/urls_web.py; (2) hiding the Security page's TOTP + recovery
# sections; (3) skipping allauth's login-time MFA challenge so users
# already enrolled aren't locked into a challenge loop; (4) a management
# command to deactivate all existing Authenticator rows so those users
# can sign in without a second factor. Setting MFA_SUPPORTED_TYPES = []
# alone is NOT a safe kill switch — it hides the UI but leaves the
# login-challenge flow intact, potentially trapping enrolled users.
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
# Name that appears in authenticator apps next to the account email.
# Without this, apps show the bare email, which is confusing when users
# have multiple TOTP entries for different services. Env-configurable so a
# self-hosted deployment can brand it (the MFA_TOTP_ISSUER line in .django).
MFA_TOTP_ISSUER = env("MFA_TOTP_ISSUER", default="Validibot")
# Allauth's default is 10, but we state it explicitly so future maintainers
# don't have to chase the upstream default if we ever need to audit it.
MFA_RECOVERY_CODE_COUNT = 10
# Custom MFA adapter — encrypts TOTP secrets and recovery-code seeds at
# rest. Allauth's default adapter uses no-op encrypt/decrypt, leaving
# secret material in cleartext in the `mfa_authenticator.data` column.
# Our adapter uses Fernet with a dedicated key (not SECRET_KEY, so the
# two can be rotated independently). See validibot/users/mfa_adapter.py.
MFA_ADAPTER = "validibot.users.mfa_adapter.ValidibotMFAAdapter"
# Fernet key used by ValidibotMFAAdapter. Required in every environment
# — the adapter raises ImproperlyConfigured if missing, which is
# deliberately noisy: silent fallback to cleartext storage is worse
# than a failed deploy. Generate a fresh key with:
#   python -c "from cryptography.fernet import Fernet; \
#       print(Fernet.generate_key().decode())"
# Rotate by switching to a cryptography.fernet.MultiFernet in the
# adapter (new key first, old key second) — not wired up yet; add
# when needed.
MFA_ENCRYPTION_KEY = env("DJANGO_MFA_ENCRYPTION_KEY", default=None)

# Hashed personal API keys. Production requires a dedicated digest key in
# ``production.py``; local/test may fall back to ``SECRET_KEY`` in the service
# so developers are not blocked by extra one-off configuration.
API_KEY_DIGEST_KEY = env("DJANGO_API_KEY_DIGEST_KEY", default="")
API_KEY_DIGEST_VERSION = env.int("DJANGO_API_KEY_DIGEST_VERSION", default=1)
API_KEY_DEFAULT_EXPIRY_DAYS = env.int("API_KEY_DEFAULT_EXPIRY_DAYS", default=365)
API_KEY_LAST_USED_UPDATE_INTERVAL_SECONDS = env.int(
    "API_KEY_LAST_USED_UPDATE_INTERVAL_SECONDS",
    default=3600,
)

# django-rest-framework
# -------------------------------------------------------------------------------
# django-rest-framework - https://www.django-rest-framework.org/api-guide/settings/
# DMcQ: Using our our custom AgentAwareNegotiation so every API view gets the agent profile.
REST_FRAMEWORK = {
    "DEFAULT_CONTENT_NEGOTIATION_CLASS": "validibot.core.api.negotiation.AgentAwareNegotiation",
    # Custom handler that wraps DRF's default with a JSON-500 fallback for
    # uncaught Python exceptions. Without this, programming errors raised
    # in API views fall through to Django's HTML 500 page, which API
    # clients can't parse. See validibot/core/api/exception_handler.py for
    # the full rationale. The companion ``handler500`` in config/urls.py
    # covers exceptions raised outside the DRF dispatch cycle (middleware,
    # URL resolution).
    "EXCEPTION_HANDLER": "validibot.core.api.exception_handler.api_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.SessionAuthentication",
        "validibot.core.api.authentication.BearerAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "validibot.core.api.pagination.DefaultCursorPagination",
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    # Rate limiting / throttling
    # See: https://www.django-rest-framework.org/api-guide/throttling/
    # These protect against abuse while allowing legitimate high-volume usage.
    # Exceeding limits returns 429 Too Many Requests with Retry-After header.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.UserRateThrottle",
        # GuestAwareThrottle extends ScopedRateThrottle to apply different rates
        # for workflow guests (users with grants but no org membership)
        "validibot.core.throttles.GuestAwareThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        # Authenticated users: generous limits for normal API usage
        "user": env("DRF_THROTTLE_RATE_USER", default="1000/hour"),
        # Scoped rates for specific high-value endpoints (set throttle_scope on views)
        # Workflow launches are expensive (run validators, store data, etc.)
        "workflow_launch": env("DRF_THROTTLE_RATE_LAUNCH", default="60/minute"),
        # Guest users (workflow guests without org membership) have lower limits
        # to prevent abuse while still allowing legitimate usage
        "guest_workflow_launch": env(
            "DRF_THROTTLE_RATE_GUEST_LAUNCH",
            default="20/minute",
        ),
        # Burst protection: prevent rapid-fire requests in short windows
        "burst": env("DRF_THROTTLE_RATE_BURST", default="30/minute"),
        # Anonymous rate (only used if DRF_ALLOW_ANONYMOUS=True)
        "anon": env("DRF_THROTTLE_RATE_ANON", default="100/hour"),
    },
    # Trusted-proxy depth for client-IP resolution in throttles. DRF's
    # BaseThrottle.get_ident() reads this to pick the real client IP out of
    # X-Forwarded-For: it trusts the Nth address FROM THE RIGHT — the value the
    # Nth trusted proxy appended. With the default (None) DRF trusts the ENTIRE
    # client-supplied XFF, so an attacker could vary the header and mint a fresh
    # throttle bucket per request (defeating IP-keyed throttles like
    # AgentValidateThrottle's pre-payment abuse guard). Our hosted ingress
    # (Cloud Run) appends the real client IP as the last XFF hop, so 1 = "trust
    # only the immediate upstream proxy" is correct and NOT client-spoofable.
    # Operators with extra proxies in front (an external LB ahead of Cloud Run,
    # nginx → gunicorn, etc.) must raise DRF_NUM_PROXIES to match; set 0 to
    # ignore XFF entirely (app exposed directly, no proxy).
    "NUM_PROXIES": env.int("DRF_NUM_PROXIES", default=1),
}

# Toggle for anonymous API access. When False (default), all API endpoints require
# authentication. Set to True to allow unauthenticated access to public endpoints.
# When enabled, AnonRateThrottle is added to protect against abuse.
DRF_ALLOW_ANONYMOUS = env.bool("DRF_ALLOW_ANONYMOUS", default=False)

if DRF_ALLOW_ANONYMOUS:
    REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"].insert(
        0,
        "rest_framework.throttling.AnonRateThrottle",
    )

# django-cors-headers - https://github.com/adamchainz/django-cors-headers#setup
CORS_URLS_REGEX = r"^/api/.*$"
# Explicitly disallow cross-origin requests; only same-origin calls are allowed.
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS: list[str] = []
CORS_ALLOW_CREDENTIALS = False

# By Default swagger ui is available only to admin user(s). You can change permission classes to change that
# See more configuration options at https://drf-spectacular.readthedocs.io/en/latest/settings.html#settings
SPECTACULAR_SETTINGS = {
    "TITLE": "Validibot API",
    "DESCRIPTION": "Documentation of API endpoints of Validibot",
    "VERSION": "1.0.0",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
    "SCHEMA_PATH_PREFIX": "/api/",
    # Workflow definitions call this field ``output_retention`` while run
    # snapshots call it ``output_retention_policy``. Both use the same
    # TextChoices contract, so give the shared values one stable component
    # name instead of letting drf-spectacular generate two aliases.
    "ENUM_NAME_OVERRIDES": {
        "OutputRetentionEnum": "validibot.submissions.constants.OutputRetention",
    },
    # Keep service-to-service routes (the /api/v1/mcp/* helper surface) out
    # of the user-facing reference — they reject user API keys by design.
    # See config/schema.py for the rationale.
    "PREPROCESSING_HOOKS": ["config.schema.exclude_internal_paths"],
    # Serve Swagger UI and ReDoc assets locally via sidecar (fixes encoding issues,
    # works offline, avoids CDN dependencies)
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    # Swagger UI enhancements
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,  # Enable deep linking to operations
        "persistAuthorization": True,  # Remember auth token across page refreshes
        "displayOperationId": False,  # Hide operation IDs for cleaner UI
        "filter": True,  # Enable endpoint filtering/search
        "defaultModelsExpandDepth": 2,  # Expand models by default
        "docExpansion": "list",  # Show endpoints as list (not expanded)
    },
}

# Validibot settings
# ------------------------------------------------------------------------------

POSTMARK_SERVER_TOKEN = env("POSTMARK_SERVER_TOKEN", default=None)
POSTMARK_WEBHOOK_ALLOWED_IPS = env.list(
    "POSTMARK_WEBHOOK_ALLOWED_IPS",
    default=["3.134.147.250", "50.31.156.6", "50.31.156.77", "18.217.206.57"],
)
POSTMARK_WEBHOOK_SIGNING_SECRET = env(
    "POSTMARK_WEBHOOK_SIGNING_SECRET",
    default="",
)

if GITHUB_APP_ENABLED:
    GITHUB_APP = {
        "APP_ID": env.int("GITHUB_APP_ID"),
        "CLIENT_ID": env.str("GITHUB_CLIENT_ID"),
        "NAME": env.str("GITHUB_NAME"),
        "PRIVATE_KEY": env.str("GITHUB_PRIVATE_KEY").replace("\\n", "\n"),
        "WEBHOOK_SECRET": env.str("GITHUB_WEBHOOK_SECRET"),
        "WEBHOOK_TYPE": "async",  # Use "async" for ASGI projects or "sync" for WSGI projects
    }

    logger.info("Using GITHUB_APP APP_ID: %s", GITHUB_APP["APP_ID"])

VALIDATION_START_ATTEMPTS = 4  # 4 attempts
VALIDATION_START_ATTEMPT_TIMEOUT = 5  # 5 seconds per attempt
JOB_STATUS_RETRY_AFTER = VALIDATION_START_ATTEMPT_TIMEOUT

# Maximum size (bytes) of a validator's output envelope (output.json) that the
# worker will download from storage when a callback arrives. Advanced validator
# containers process attacker-influenced models, so a compromised or buggy
# container could write an arbitrarily large result object; the worker checks
# the object's size against this cap and refuses to download anything larger,
# rather than buffering an unbounded payload into memory. The default is
# generous — real envelopes are findings + metrics, far smaller.
VALIDATION_RESULT_MAX_BYTES = env.int(
    "VALIDATION_RESULT_MAX_BYTES",
    default=25 * 1024 * 1024,  # 25 MB
)

# A callback processor owns storage download and verification using a durable
# token instead of retaining database locks. Another delivery may take over
# only after this window; it should comfortably exceed a normal output fetch.
VALIDATION_CALLBACK_PROCESSING_STALE_SECONDS = env.int(
    "VALIDATION_CALLBACK_PROCESSING_STALE_SECONDS",
    default=10 * 60,
)
if VALIDATION_CALLBACK_PROCESSING_STALE_SECONDS <= 0:
    raise ImproperlyConfigured(
        "VALIDATION_CALLBACK_PROCESSING_STALE_SECONDS must be greater than zero."
    )

# Submission settings
SUBMISSION_INLINE_MAX_BYTES = 10_000_000  # 10MB
SUBMISSION_FILE_MAX_BYTES = 1_000_000_000  # 1GB

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
SECURE_CONTENT_TYPE_NOSNIFF = True

# REFERRER-POLICY
# ------------------------------------------------------------------------------
# Prevents leaking URL parameters (tokens, IDs) to external sites via the
# Referer header. "strict-origin-when-cross-origin" sends the full URL for
# same-origin requests but only the origin for cross-origin navigation.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# PERMISSIONS-POLICY
# ------------------------------------------------------------------------------
# Disables browser APIs that Validibot doesn't use. Defense-in-depth against
# potential XSS or iframe-based attacks accessing device capabilities.
# https://github.com/adamchainz/django-permissions-policy
PERMISSIONS_POLICY: dict[str, list[str]] = {
    "camera": [],
    "geolocation": [],
    "microphone": [],
    "payment": [],
    "usb": [],
}

# CONTENT SECURITY POLICY (CSP)
# ------------------------------------------------------------------------------
# Enforcing mode: the browser blocks any script, style, font, or connection
# that doesn't match the policy. Previously in report-only mode for testing.
# Switched to enforcing 2026-03-17 after verifying no violations on all pages.
# https://django-csp.readthedocs.io/
#
# The NONCE sentinel tells django-csp to generate a unique cryptographic nonce
# per request and include it in the CSP header. Templates use {{ CSP_NONCE }}
# to attach the same nonce to inline <script> tags.


CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ("'self'",),
        "script-src": (
            "'self'",
            NONCE,
            # PostHog analytics — the tracker stub (in web_tracker.html) has a
            # nonce, but it dynamically creates a <script> tag to load the full
            # SDK from PostHog's CDN. That injected script can't carry a nonce,
            # so we must whitelist the PostHog asset host.
            "https://*.i.posthog.com",
            # Google reCAPTCHA v3 — the invisible widget loads scripts from
            # Google's reCAPTCHA CDN and gstatic.
            "https://www.google.com/recaptcha/",
            "https://www.gstatic.com/recaptcha/",
        ),
        "style-src": (
            "'self'",
            NONCE,
            "https://fonts.googleapis.com",
        ),
        # Allow inline style= attributes on HTML elements. These are layout
        # values (heights, widths) not user-controlled content, so the security
        # risk is negligible and migrating 46 templates to CSS classes isn't
        # worth the effort.
        #
        # Note: HTMx swap transitions apply styles via JavaScript
        # (element.style), which triggers a CSP report-only violation
        # against style-src. This is a known cosmetic issue — the styles
        # are framework-internal, not user-controlled.
        "style-src-attr": ("'unsafe-inline'",),
        "font-src": (
            "'self'",
            "https://fonts.gstatic.com",
        ),
        "img-src": ("'self'", "data:"),
        "connect-src": (
            "'self'",
            "https://us.i.posthog.com",
            # Google reCAPTCHA v3 — the widget makes XHR/fetch requests
            # back to Google to verify the challenge token. Without these
            # entries, the token exchange silently fails and the form
            # submission loops without any user-visible error.
            "https://www.google.com/recaptcha/",
            "https://www.gstatic.com/recaptcha/",
        ),
        # Google reCAPTCHA v3 uses an invisible iframe for challenge rendering.
        "frame-src": (
            "'self'",
            "https://www.google.com/recaptcha/",
            "https://recaptcha.google.com/recaptcha/",
        ),
        # Restrict iframe embedding. If we need badge/widget embedding
        # in the future, override frame-ancestors per-view using CSP
        # decorators rather than allowing all origins globally.
        "frame-ancestors": ("'none'",),
    },
}


# Workflow validation run settings
WORKFLOW_RUN_POLL_INTERVAL_SECONDS = env(
    "WORKFLOW_RUN_POLL_INTERVAL_SECONDS",
    default=3,
)

# Site features
ACCOUNT_ALLOW_LOGIN = env.bool("DJANGO_ACCOUNT_ALLOW_LOGIN", True)

ENABLE_APP = env.bool("ENABLE_APP", True)
# API flag is a global admin switch; defaults to True. Route exposure is
# controlled at the URLConf (web vs worker) rather than here.
ENABLE_API = env.bool("ENABLE_API", True)

# django-recaptcha (Google reCAPTCHA)
# https://github.com/django-recaptcha/django-recaptcha
# Get keys from: https://www.google.com/recaptcha/admin
RECAPTCHA_PUBLIC_KEY = env("RECAPTCHA_PUBLIC_KEY", default="")
RECAPTCHA_PRIVATE_KEY = env("RECAPTCHA_PRIVATE_KEY", default="")
# reCAPTCHA v3 is configured via ReCaptchaV3Field in forms
# Optional: Set minimum score threshold (default 0.5, range 0.0-1.0)
# RECAPTCHA_REQUIRED_SCORE = 0.5
# Disable reCAPTCHA if keys not configured (for local development)
SILENCED_SYSTEM_CHECKS = ["django_recaptcha.recaptcha_test_key_error"]

TEST_ENERGYPLUS_WEATHER_FILE = env(
    "TEST_ENERGYPLUS_WEATHER_FILE",
    default="USA_CA_SF.epw",
)
# Validator assets are stored under gs://{bucket}/validator_assets/{asset_type}/
# Weather data files (EPW) go in the weather_data subdirectory
GCS_VALIDATOR_ASSETS_PREFIX = "validator_assets"
GCS_WEATHER_DATA_DIR = env(
    "GCS_WEATHER_DATA_DIR",
    default="weather_data",
)

# PostHog (or other) tracker settings
TRACKER_INCLUDE_SUPERUSER = env.bool("TRACKER_INCLUDE_SUPERUSER", False)
POSTHOG_PROJECT_KEY = env("POSTHOG_PROJECT_KEY", default="")
POSTHOG_API_HOST = env("POSTHOG_API_HOST", default="https://us.i.posthog.com")

# EMAIL
DEFAULT_FROM_EMAIL = env(
    "DJANGO_DEFAULT_FROM_EMAIL",
    default="Validibot <noreply@example.com>",
)

# STORAGE ARCHITECTURE
# ------------------------------------------------------------------------------
# Validibot uses a single storage location (bucket or directory) with two prefixes:
#
#   storage/
#   ├── public/                     # Publicly accessible (avatars, workflow images)
#   └── private/                    # Private files
#       └── runs/{run_id}/          # Each validation run gets its own directory
#           ├── input/              # Written by web app (envelope, submission files)
#           └── output/             # Written by validator container (results, artifacts)
#
# This structure is standardized across all platforms (Docker, K8s, GCS, etc.).
# Validator containers receive STORAGE_ROOT and RUN_PATH environment variables
# to read from input/ and write to output/.
#
# For GCS/S3: The bucket is private by default. The `public/` prefix is made
# publicly readable via IAM policy (allUsers → objectViewer on public/* prefix).
#
# Django STORAGES:
#   - "default": Private media files (uses private/ prefix)
#   - "public": Public media files (uses public/ prefix)
#
# Data Storage (validibot.core.storage):
#   - Validation pipeline files under private/runs/
#   - Accessed via signed URLs for user downloads
#
# See docs/dev_docs/how-to/configure-storage.md for complete details.

# Storage bucket/root for all files (single bucket architecture)
STORAGE_BUCKET = env("STORAGE_BUCKET", default="")
STORAGE_ROOT = env("STORAGE_ROOT", default=str(BASE_DIR / "storage"))

# DATA STORAGE (Validation Pipeline Files)
# ------------------------------------------------------------------------------
# Data storage handles validation pipeline files (submissions, envelopes, outputs).
# These files are private and accessed via signed URLs when users download them.
#
# Backend options:
#   - "local": Local filesystem (default, good for development and Docker Compose)
#   - "gcs": Google Cloud Storage (production GCP)
#   - "s3": Amazon S3 (future)
#   - Full class path for custom backends
#
# See validibot/core/storage/ for implementation details.
DATA_STORAGE_BACKEND = env("DATA_STORAGE_BACKEND", default="local")

# For local backend, use private/ subdirectory under STORAGE_ROOT
DATA_STORAGE_ROOT = env(
    "DATA_STORAGE_ROOT", default=str(Path(STORAGE_ROOT) / "private")
)

# For cloud backends, use the same bucket with private/ prefix
DATA_STORAGE_BUCKET = env("DATA_STORAGE_BUCKET", default=STORAGE_BUCKET)
DATA_STORAGE_PREFIX = env("DATA_STORAGE_PREFIX", default="private")

# Build DATA_STORAGE_OPTIONS based on backend type
if DATA_STORAGE_BACKEND == "local":
    DATA_STORAGE_OPTIONS = {"root": DATA_STORAGE_ROOT}
elif DATA_STORAGE_BACKEND == "gcs":
    DATA_STORAGE_OPTIONS = {
        "bucket_name": DATA_STORAGE_BUCKET,
        "prefix": DATA_STORAGE_PREFIX,
    }
else:
    DATA_STORAGE_OPTIONS = {}

# VALIDATOR RUNNER
# ------------------------------------------------------------------------------
# Configuration for running container-based validators (EnergyPlus, FMU, etc.).
#
# Available runners:
#   - "docker": Local Docker socket (default, for Docker Compose deployments)
#   - "google_cloud_run": Managed Cloud Run Services or Jobs (GCP production)
#   - "aws_batch": AWS Batch (future)
#   - Full class path for custom runners
#
# See validibot/validations/services/runners/ for implementation details.
VALIDATOR_RUNNER = env("VALIDATOR_RUNNER", default="docker")
# One outer wall-clock budget shared by the runner and the stuck-run watchdog.
# Provider deployment recipes use the same environment variable and default,
# so the watchdog never declares timeout before the configured runtime does.
VALIDATOR_TIMEOUT_SECONDS = env.int("VALIDATOR_TIMEOUT_SECONDS", default=3600)
MAX_PROVIDER_STATUS_LOOKUP_GRACE_SECONDS = 1800
VALIDATOR_STATUS_LOOKUP_GRACE_SECONDS = env.int(
    "VALIDATOR_STATUS_LOOKUP_GRACE_SECONDS",
    default=300,
)
if not (
    0
    <= VALIDATOR_STATUS_LOOKUP_GRACE_SECONDS
    <= MAX_PROVIDER_STATUS_LOOKUP_GRACE_SECONDS
):
    raise ImproperlyConfigured(
        "VALIDATOR_STATUS_LOOKUP_GRACE_SECONDS must be between 0 and 1800."
    )
# Domain budget used by the Fast-response workflow profile when the deployment
# declares author-selectable execution profiles. 1500 seconds fits the GCP
# request-driven Service contract. Long-running steps and single-route
# self-hosted/local deployments use the site-wide VALIDATOR_TIMEOUT_SECONDS
# ceiling.
VALIDATOR_DEFAULT_EXECUTION_SECONDS = env.int(
    "VALIDATOR_DEFAULT_EXECUTION_SECONDS",
    default=min(1500, VALIDATOR_TIMEOUT_SECONDS),
)
if not 1 <= VALIDATOR_DEFAULT_EXECUTION_SECONDS <= VALIDATOR_TIMEOUT_SECONDS:
    raise ImproperlyConfigured(
        "VALIDATOR_DEFAULT_EXECUTION_SECONDS must be between 1 and "
        "VALIDATOR_TIMEOUT_SECONDS."
    )
VALIDATOR_RUNNER_OPTIONS = {
    "memory_limit": env("VALIDATOR_MEMORY_LIMIT", default="4g"),
    "cpu_limit": env("VALIDATOR_CPU_LIMIT", default="2.0"),
    # Docker network for validator containers (set when running in Docker Compose)
    "network": env("VALIDATOR_NETWORK", default=None),
    # Named volume for storage (for Docker-in-Docker scenarios)
    "storage_volume": env("VALIDATOR_STORAGE_VOLUME", default=None),
    # Mount path for storage volume inside validator containers
    "storage_mount_path": env("VALIDATOR_STORAGE_MOUNT_PATH", default="/app/storage"),
    "timeout_seconds": VALIDATOR_TIMEOUT_SECONDS,
}

# Validator backend trust-tier hardening overrides (Trust ADR
# Phase 5 Session C)
# ------------------------------------------------------------------------------
# These settings tune the Tier-2 sandbox profile the runner applies
# when a Validator row's ``trust_tier`` column is ``TIER_2`` (i.e.
# user-added or partner-authored backend, future feature). Tier 1
# (the default for everything we ship today) keeps the existing
# Phase 1 hardening — these settings have no effect on Tier 1 runs.
#
# ``VALIDATOR_TIER_2_CONTAINER_RUNTIME``: Docker runtime name for
# tier-2 containers. Set to ``"runsc"`` if gVisor is installed on
# the worker, ``"kata"`` for Kata Containers, or leave empty to use
# the host's default runtime. The runner does *not* check
# availability — misconfigured deployments produce launch errors,
# which the doctor command flags.
VALIDATOR_TIER_2_CONTAINER_RUNTIME = env(
    "VALIDATOR_TIER_2_CONTAINER_RUNTIME",
    default="",
)
# Tighter resource caps for tier-2 containers. Defaults are roughly
# half the tier-1 limits — partner code shouldn't get more than the
# minimum it needs.
VALIDATOR_TIER_2_MEMORY_LIMIT = env(
    "VALIDATOR_TIER_2_MEMORY_LIMIT",
    default="2g",
)
VALIDATOR_TIER_2_CPU_LIMIT = env(
    "VALIDATOR_TIER_2_CPU_LIMIT",
    default="1.0",
)

# Native PDF parsing is always network-isolated by the Docker runner. Operators
# serving public hostile uploads should install gVisor or Kata, select it here,
# and enable the fail-closed requirement. The empty/default pair preserves a
# usable runc deployment for self-hosters who have not installed another
# runtime while retaining capabilities, mount, PID, IPC, and egress controls.
VALIDATOR_PDF_CONTAINER_RUNTIME = env(
    "VALIDATOR_PDF_CONTAINER_RUNTIME",
    default="",
)
VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX = env.bool(
    "VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX",
    default=False,
)

# Validator backend image policy (Trust ADR Phase 5 Session B)
# ------------------------------------------------------------------------------
# Every deployment target may choose ``tag``, ``digest``, or
# ``signed-digest``. GCP production templates select ``digest`` while local
# development keeps the community-friendly ``tag`` default. Managed release
# tooling may impose a stricter minimum without removing this core choice.
VALIDATOR_BACKEND_IMAGE_POLICY = env(
    "VALIDATOR_BACKEND_IMAGE_POLICY",
    default="tag",
)

# Validator backend image cosign verification (Trust ADR Phase 5
# Session A.2)
# ------------------------------------------------------------------------------
# When ``COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True``, the runner
# shells out to the ``cosign`` CLI before launching each validator
# backend container and aborts the run if the image isn't signed by
# the configured key. Disabled by default for community quick-start
# parity. Production deployments that require signed images should
# enable this AND pin to ``signed-digest`` policy in Phase 5
# Session B (when that ships).
COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES = env.bool(
    "COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES",
    default=False,
)
# Path to the cosign public key used to verify validator backend
# image signatures. Required when verification is enabled.
COSIGN_VERIFY_PUBLIC_KEY_PATH = env(
    "COSIGN_VERIFY_PUBLIC_KEY_PATH",
    default="",
)
# Path to the cosign binary. Empty string means "look up on PATH"
# which is the right default for most deployments. Override when
# the binary lives in a non-standard location.
COSIGN_BINARY_PATH = env("COSIGN_BINARY_PATH", default="cosign")

# SHACL validator resource limits (ADR-2026-05-18 "Security")
# ------------------------------------------------------------------------------
# Every limit is enforced inside the SHACL engine and produces a
# ``ValidationIssue`` with severity ERROR when hit — the worker does not
# crash. Operators can lower defaults for stricter deployments but
# cannot exceed the hard caps (enforced by ``min(configured, cap)`` in
# engine code). See validibot/validations/validators/shacl/engine.py.
#
# Core SHACL is enabled by default. Advanced SHACL-AF constructs
# (embedded SPARQL constraints and SHACL Rules) are disabled at the
# deployment level until the operator explicitly opts in for trusted,
# isolated worker environments. SHACL-JS is always rejected.
SHACL_ENABLE_ADVANCED_FEATURES = env.bool(
    "SHACL_ENABLE_ADVANCED_FEATURES",
    default=False,
)

# Submission triple-count caps. Defaults sized for a typical
# building-scale 223P model (largest published example is ~50K triples);
# hard caps allow campus-scale graphs while keeping a finite ceiling.
SHACL_MAX_DATA_TRIPLES = env.int("SHACL_MAX_DATA_TRIPLES", default=100_000)
SHACL_MAX_SHAPE_TRIPLES = env.int("SHACL_MAX_SHAPE_TRIPLES", default=50_000)
SHACL_MAX_ONTOLOGY_TRIPLES = env.int("SHACL_MAX_ONTOLOGY_TRIPLES", default=100_000)

# pyshacl validation-depth cap. Bounds the recursion of constraint
# evaluation; deeply nested constraint chains in malicious shapes are
# capped here to prevent stack / time exhaustion.
SHACL_MAX_VALIDATION_DEPTH = env.int("SHACL_MAX_VALIDATION_DEPTH", default=25)

# pySHACL process timeout. The engine launches pySHACL in a child
# process and terminates it if validation exceeds this wall-clock
# budget. This is a backstop against pathological shapes, NOT the size
# guard — input size is bounded by SHACL_MAX_DATA_TRIPLES (up to 1M). The
# default is therefore generous (5 min) so real building models near the
# triple cap can validate; the code hard-caps this at 1800s (30 min),
# which sits below the 3600s container timeout. Cloud overrides this env
# var down for cost / anonymous-agent tiers; self-hosted operators own
# their compute and keep the generous default.
SHACL_VALIDATION_TIMEOUT_SECONDS = env.int(
    "SHACL_VALIDATION_TIMEOUT_SECONDS",
    default=300,
)

# SPARQL ASK assertion budgets. Authors write SPARQL ASK queries as
# project-specific gates layered on top of SHACL. Each ASK gets its
# own wall-clock budget enforced by a daemon-thread wrapper; the cap
# protects against accidental or malicious cartesian-product queries.
SHACL_SPARQL_QUERY_TIMEOUT_SECONDS = env.int(
    "SHACL_SPARQL_QUERY_TIMEOUT_SECONDS",
    default=10,
)
SHACL_SPARQL_QUERY_TIMEOUT_MAX_SECONDS = env.int(
    "SHACL_SPARQL_QUERY_TIMEOUT_MAX_SECONDS",
    default=60,
)

# SPARQL ASK AST-scrub caps. The scrubber runs at form save time and
# rejects queries that exceed either of these limits, naming the
# specific construct that triggered the refusal so the author can fix
# it without consulting the source.
SHACL_SPARQL_QUERY_LENGTH_MAX = env.int(
    "SHACL_SPARQL_QUERY_LENGTH_MAX",
    default=10_000,
)
SHACL_SPARQL_PROPERTY_PATH_DEPTH_MAX = env.int(
    "SHACL_SPARQL_PROPERTY_PATH_DEPTH_MAX",
    default=8,
)

# Form-level cap on the number of SPARQL ASK assertions a single step
# may carry. Layered as a coarse rate-limit against authors who paste
# in dozens of expensive assertions.
SHACL_SPARQL_ASKS_PER_STEP_MAX = env.int(
    "SHACL_SPARQL_ASKS_PER_STEP_MAX",
    default=25,
)

# Managed Cloud Run Validator Settings (overridden in production.py)
# ------------------------------------------------------------------------------
# These defaults allow local development without Cloud Run Services or Jobs.
GCP_PROJECT_ID = env("GCP_PROJECT_ID", default="")
GCP_REGION = env("GCP_REGION", default="us-west1")
GCS_VALIDATION_BUCKET = env("GCS_VALIDATION_BUCKET", default="")
GCS_TASK_QUEUE_NAME = env("GCS_TASK_QUEUE_NAME", default="")
SITE_URL = env("SITE_URL", default="http://localhost:8000")
WORKER_URL = env("WORKER_URL", default="")
CREDENTIAL_ISSUER_URL = env("CREDENTIAL_ISSUER_URL", default=SITE_URL)
SIGNING_KEY_PATH = env("SIGNING_KEY_PATH", default="")
GCP_KMS_SIGNING_KEY = env("GCP_KMS_SIGNING_KEY", default="")
GCP_KMS_SIGNING_KEY_VERSION = env("GCP_KMS_SIGNING_KEY_VERSION", default="")
SIGNING_ALGORITHM = env("SIGNING_ALGORITHM", default="ES256")
CLOUD_TASKS_SERVICE_ACCOUNT = env("CLOUD_TASKS_SERVICE_ACCOUNT", default="")
# HTTP delivery is only the short Django orchestration request; validator
# compute runs separately. Keep this comfortably below Cloud Tasks' 30-minute
# maximum so a wedged worker is retried promptly.
CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS = env.int(
    "CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS",
    default=10 * 60,
)
_CLOUD_TASKS_MIN_DEADLINE_SECONDS = 15
_CLOUD_TASKS_MAX_DEADLINE_SECONDS = 30 * 60
if not (
    _CLOUD_TASKS_MIN_DEADLINE_SECONDS
    <= CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS
    <= _CLOUD_TASKS_MAX_DEADLINE_SECONDS
):
    raise ImproperlyConfigured(
        "CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS must be between 15 and 1800."
    )

# Callback-driven workflow resumption is durable in PostgreSQL. These leases
# bound takeover of a crashed queue producer or worker; neither is a business
# timeout, and both operations remain safe under at-least-once redelivery.
VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS = env.int(
    "VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS",
    default=5 * 60,
)
VALIDATION_CONTINUATION_EXECUTION_STALE_SECONDS = env.int(
    "VALIDATION_CONTINUATION_EXECUTION_STALE_SECONDS",
    # Exceeds the self-hosted Celery hard limit (30 minutes) as well as the
    # default GCP orchestration request. A healthy synchronous Docker step must
    # not be mistaken for an abandoned continuation.
    default=35 * 60,
)
if VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS <= 0:
    raise ImproperlyConfigured(
        "VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS must be greater than zero."
    )
if (
    VALIDATION_CONTINUATION_EXECUTION_STALE_SECONDS
    <= CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS
):
    raise ImproperlyConfigured(
        "VALIDATION_CONTINUATION_EXECUTION_STALE_SECONDS must exceed "
        "CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS."
    )

# Provider queue for request-driven validator Services. This is deliberately
# separate from the short application-orchestration queue above: it has its own
# invoker identity, capacity/retry policy, and the full Cloud Tasks HTTP limit.
GCP_VALIDATOR_TASK_QUEUE_NAME = env(
    "GCP_VALIDATOR_TASK_QUEUE_NAME",
    default="",
)
GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT = env(
    "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT",
    default="",
)
GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS = env.int(
    "GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS",
    default=30 * 60,
)
if GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS != _CLOUD_TASKS_MAX_DEADLINE_SECONDS:
    raise ImproperlyConfigured(
        "GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS must be exactly 1800."
    )

# Shared secret for authenticating requests to worker-only API endpoints.
# Required for Docker Compose deployments (all services share the same key).
# Leave empty for GCP deployments (Cloud Run IAM + OIDC handle authentication).
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
WORKER_API_KEY = env("WORKER_API_KEY", default="")

# Worker-endpoint OIDC verification (GCP deployments only).
# ------------------------------------------------------------------------------
# ``CloudTasksOIDCAuthentication`` re-verifies Cloud Tasks / Cloud Scheduler
# OIDC identity tokens at the application layer, as defence in depth against
# Cloud Run IAM misconfiguration. See validibot/core/api/task_auth.py.
#
#   TASK_OIDC_AUDIENCE
#       Expected ``aud`` claim on inbound tokens. Cloud Tasks signs tokens
#       with audience = WORKER_URL (see GoogleCloudTasksDispatcher). Leave
#       empty to inherit ``WORKER_URL``, which is the correct default for
#       single-service GCP deployments.
#
#   TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS
#       Comma-separated list of service-account emails permitted to invoke
#       worker endpoints (``execute-validation-run``, validator callbacks,
#       scheduled tasks). Leave empty to inherit
#       ``[CLOUD_TASKS_SERVICE_ACCOUNT]`` — correct for the default
#       single-SA deployment (Cloud Tasks, Cloud Scheduler, and Cloud Run
#       Jobs all reuse ``${APP_NAME}-cloudrun-<stage>@...``). Override when
#       splitting SAs per caller. The current GCP recipes use a separate
#       validator runtime identity for both Jobs and Services, so production
#       must explicitly include both the application and validator runtime SAs.
#       The provider-task invoker calls validator Services, not this worker,
#       and does not belong in this allowlist.
TASK_OIDC_AUDIENCE = env("TASK_OIDC_AUDIENCE", default="")
TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS = env.list(
    "TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS",
    default=[],
)


# OIDC PROVIDER (MCP OAuth)
# ------------------------------------------------------------------------------
# Validibot runs its own OIDC authorization server (via django-allauth) so the
# standalone FastMCP server can accept JWT access tokens issued here. The
# configuration below applies to every tier — community, Pro, Enterprise, and
# the hosted cloud offering — with cloud overriding ``IDP_OIDC_MCP_RESOURCE_AUDIENCE``
# and supplying the confidential MCP client's secret from Secret Manager.
#
# The `validibot.idp` app adds:
#   * ValidibotOIDCAdapter       — stamps validibot:mcp scope + audience on JWTs
#   * OIDC discovery views        — /.well-known/openid-configuration etc.
#   * ensure_oidc_clients command — idempotent bootstrap of Claude + MCP clients
#
# Store the signing key PEM base64-encoded (``base64 < key.pem | tr -d '\n'``)
# to avoid multiline escaping pain in env files and secret stores.
# NOTE: these imports are deliberately placed deep in the file next
# to their IDP-OIDC usage. E402 is waived file-wide — see the top-of-
# file suppression directive for rationale.
import base64 as _idp_base64

from validibot.idp.constants import CLAUDE_OIDC_CLIENT_ID as _IDP_CLAUDE_CLIENT_ID
from validibot.idp.constants import CLAUDE_OIDC_CLIENT_NAME as _IDP_CLAUDE_CLIENT_NAME
from validibot.idp.constants import CLAUDE_OIDC_GRANT_TYPES as _IDP_CLAUDE_GRANT_TYPES
from validibot.idp.constants import (
    CLAUDE_OIDC_REDIRECT_URIS as _IDP_CLAUDE_REDIRECT_URIS,
)
from validibot.idp.constants import (
    CLAUDE_OIDC_RESPONSE_TYPES as _IDP_CLAUDE_RESPONSE_TYPES,
)
from validibot.idp.constants import CLAUDE_OIDC_SCOPES as _IDP_CLAUDE_SCOPES
from validibot.idp.constants import MCP_SERVER_OIDC_CLIENT_ID as _IDP_MCP_CLIENT_ID
from validibot.idp.constants import MCP_SERVER_OIDC_CLIENT_NAME as _IDP_MCP_CLIENT_NAME
from validibot.idp.constants import MCP_SERVER_OIDC_GRANT_TYPES as _IDP_MCP_GRANT_TYPES
from validibot.idp.constants import (
    MCP_SERVER_OIDC_RESPONSE_TYPES as _IDP_MCP_RESPONSE_TYPES,
)
from validibot.idp.constants import MCP_SERVER_OIDC_SCOPES as _IDP_MCP_SCOPES
from validibot.idp.constants import normalize_oidc_values as _idp_normalize


def _decode_idp_pem_from_env(b64_value: str) -> str:
    """Decode a base64-encoded PEM private key from an env var value."""

    if not b64_value:
        return ""
    try:
        return _idp_base64.b64decode(b64_value).decode("utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        msg = (
            "Invalid IDP_OIDC_PRIVATE_KEY_B64. Generate with: "
            "base64 < key.pem | tr -d '\\n'"
        )
        raise ValueError(msg) from exc


IDP_OIDC_ADAPTER = "validibot.idp.adapter.ValidibotOIDCAdapter"
IDP_OIDC_ACCESS_TOKEN_FORMAT = "jwt"
IDP_OIDC_ROTATE_REFRESH_TOKEN = True
IDP_OIDC_AUTHORIZATION_CODE_EXPIRES_IN = env.int(
    "IDP_OIDC_AUTHORIZATION_CODE_EXPIRES_IN",
    default=60,
)
IDP_OIDC_ACCESS_TOKEN_EXPIRES_IN = env.int(
    "IDP_OIDC_ACCESS_TOKEN_EXPIRES_IN",
    default=3600,
)
IDP_OIDC_PRIVATE_KEY = _decode_idp_pem_from_env(
    env.str("IDP_OIDC_PRIVATE_KEY_B64", default=""),
)

# Public base URL of this deployment's MCP server. Self-hosted Pro operators
# set this to whatever hostname their reverse proxy terminates TLS at; the
# local compose stack defaults to the port-mapped ``http://localhost:8001``.
# The same value is passed to the FastMCP server as ``VALIDIBOT_MCP_BASE_URL``.
# Reading it from a single env var keeps the audience claim on OIDC tokens
# and the audience check inside the MCP server in lockstep without asking
# the operator to remember two settings.
VALIDIBOT_MCP_BASE_URL = env.str(
    "VALIDIBOT_MCP_BASE_URL",
    default="http://localhost:8001",
)

# MCP resource audience stamped onto JWT access tokens by the OIDC adapter.
# Derived from VALIDIBOT_MCP_BASE_URL so a single env override lines up
# OAuth issuance on the Django side and audience validation on the MCP
# side. Cloud overrides this in validibot_cloud/settings/_cloud_common.py
# to use the hosted ``https://mcp.validibot.com/mcp`` value.
IDP_OIDC_MCP_RESOURCE_AUDIENCE = env.str(
    "IDP_OIDC_MCP_RESOURCE_AUDIENCE",
    default=f"{VALIDIBOT_MCP_BASE_URL.rstrip('/')}/mcp",
)

# ── Claude Desktop public client ─────────────────────────────────────────
IDP_OIDC_CLAUDE_CLIENT_ID = env.str(
    "IDP_OIDC_CLAUDE_CLIENT_ID",
    default=_IDP_CLAUDE_CLIENT_ID,
)
IDP_OIDC_CLAUDE_CLIENT_NAME = env.str(
    "IDP_OIDC_CLAUDE_CLIENT_NAME",
    default=_IDP_CLAUDE_CLIENT_NAME,
)
IDP_OIDC_CLAUDE_REDIRECT_URIS = _idp_normalize(
    tuple(
        env.list(
            "IDP_OIDC_CLAUDE_REDIRECT_URIS",
            default=list(_IDP_CLAUDE_REDIRECT_URIS),
        ),
    ),
)
IDP_OIDC_CLAUDE_SCOPES = _idp_normalize(_IDP_CLAUDE_SCOPES)
IDP_OIDC_CLAUDE_GRANT_TYPES = _IDP_CLAUDE_GRANT_TYPES
IDP_OIDC_CLAUDE_RESPONSE_TYPES = _IDP_CLAUDE_RESPONSE_TYPES
IDP_OIDC_CLAUDE_SKIP_CONSENT = env.bool(
    "IDP_OIDC_CLAUDE_SKIP_CONSENT",
    default=False,
)

# ── MCP server confidential client ───────────────────────────────────────
# ensure_oidc_clients only creates this client when a secret is configured;
# absent a secret the MCP server falls back to the legacy API-token flow.
IDP_OIDC_MCP_SERVER_CLIENT_ID = env.str(
    "IDP_OIDC_MCP_SERVER_CLIENT_ID",
    default=_IDP_MCP_CLIENT_ID,
)
IDP_OIDC_MCP_SERVER_CLIENT_NAME = env.str(
    "IDP_OIDC_MCP_SERVER_CLIENT_NAME",
    default=_IDP_MCP_CLIENT_NAME,
)
IDP_OIDC_MCP_SERVER_CLIENT_SECRET = env.str(
    "IDP_OIDC_MCP_SERVER_CLIENT_SECRET",
    default="",
)
IDP_OIDC_MCP_SERVER_REDIRECT_URIS = _idp_normalize(
    tuple(
        env.list(
            "IDP_OIDC_MCP_SERVER_REDIRECT_URIS",
            # Derive the default from VALIDIBOT_MCP_BASE_URL so self-hosted
            # Pro operators don't accidentally advertise the hosted Validibot
            # MCP callback URL (``https://mcp.validibot.com/auth/callback``).
            # Cloud overrides via the explicit env var.
            default=[f"{VALIDIBOT_MCP_BASE_URL.rstrip('/')}/auth/callback"],
        ),
    ),
)
IDP_OIDC_MCP_SERVER_SCOPES = _idp_normalize(_IDP_MCP_SCOPES)
IDP_OIDC_MCP_SERVER_GRANT_TYPES = _IDP_MCP_GRANT_TYPES
IDP_OIDC_MCP_SERVER_RESPONSE_TYPES = _IDP_MCP_RESPONSE_TYPES

# ── Audit log retention + archival ───────────────────────────────────
# Read by the ``enforce_audit_retention`` management command, which
# runs on a schedule defined in ``validibot/core/tasks/registry.py``.
# The command deletes AuditLogEntry rows older than
# AUDIT_HOT_RETENTION_DAYS after calling the configured backend's
# ``archive()`` so rows are preserved durably before deletion. See
# ``validibot/audit/archive.py`` for the backend contract.
AUDIT_HOT_RETENTION_DAYS = env.int("AUDIT_HOT_RETENTION_DAYS", default=90)
# Kill-switch. Set False to freeze the table during incident
# investigation — the scheduled task still runs but exits early
# without touching any rows.
AUDIT_RETENTION_ENABLED = env.bool("AUDIT_RETENTION_ENABLED", default=True)
# Dotted path to the backend class. Community default discards rows
# after the retention window so the table stops growing unbounded.
# Pro / Enterprise / cloud deployments override this to preserve
# rows long-term (validibot-cloud ships a GCS-backed backend).
AUDIT_ARCHIVE_BACKEND = env.str(
    "AUDIT_ARCHIVE_BACKEND",
    default="validibot.audit.archive.NullArchiveBackend",
)
# Base directory for the reference ``FilesystemArchiveBackend``.
# Ignored by other backends. Must be a path on durable storage
# (persistent disk / mounted volume) for archives to survive
# container restarts.
AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH = env.str(
    "AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH",
    default="",
)
# GCS-backend settings. Read by
# ``validibot.audit.backends.gcs.GCSArchiveBackend`` when
# ``AUDIT_ARCHIVE_BACKEND`` points at it. Community-hosted for
# self-hosted Pro deployments on GCP that want the same CMEK-
# encrypted archive story the hosted cloud offering has.
#
# The Django startup system check at ``validibot.audit.checks``
# fires an E001 error when the GCS backend is selected but
# ``AUDIT_ARCHIVE_GCS_BUCKET`` is empty — misconfiguration
# surfaces at deploy time rather than at 02:30 the next morning.
AUDIT_ARCHIVE_GCS_BUCKET = env.str("AUDIT_ARCHIVE_GCS_BUCKET", default="")
AUDIT_ARCHIVE_GCS_PREFIX = env.str("AUDIT_ARCHIVE_GCS_PREFIX", default="audit/")
# Fully-qualified KMS resource name
# (``projects/.../cryptoKeys/...``). Empty means new objects
# inherit the bucket's default CMEK. Google's recommendation for
# high-sensitivity data is a dedicated per-app key.
AUDIT_ARCHIVE_GCS_KMS_KEY = env.str("AUDIT_ARCHIVE_GCS_KMS_KEY", default="")
# GCP project id for the storage client. Empty → ADC / env-based
# resolution. Rarely needed in production.
AUDIT_ARCHIVE_GCS_PROJECT_ID = env.str("AUDIT_ARCHIVE_GCS_PROJECT_ID", default="")

# ── MCP helper API service-to-service auth ───────────────────────────
# Read by ``validibot.mcp_api.authentication.MCPServiceAuthentication``.
# Locally, the FastMCP server and Django share a long random string via
# ``VALIDIBOT_MCP_SERVICE_KEY``. In production (hosted cloud), this env
# var is empty and the auth class falls back to verifying Cloud Run
# OIDC identity tokens against ``MCP_OIDC_AUDIENCE`` instead.
MCP_SERVICE_KEY = env("VALIDIBOT_MCP_SERVICE_KEY", default="")

# Expected audience on Cloud Run OIDC identity tokens. Defaults empty
# so the OIDC verification path is off by default — the shared-secret
# path covers local dev and self-hosted Pro. Cloud settings override
# this with the deployment's Django API URL.
MCP_OIDC_AUDIENCE = env.str("MCP_OIDC_AUDIENCE", default="")

# Service-account allowlist for Cloud Run OIDC identity tokens. A
# valid Google-signed token with the right audience is necessary but
# not sufficient — any Google SA that can mint a token with our
# audience would pass ``verify_oauth2_token``. The allowlist narrows
# that to our own MCP-invoker SA(s).
#
# Accepts a comma-separated env var OR a Python list (e.g. from a
# cloud settings module): both flow through the same set-of-strings
# normaliser in the auth class. Community leaves this empty; a
# production GCP deployment populates it with the SAs that run the
# MCP container. An empty allowlist with the OIDC path active is a
# deployment error — the auth class logs and fails closed.
MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS = env.list(
    "MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS",
    default=[],
)

# CELERY TASK QUEUE
# ------------------------------------------------------------------------------
# Celery is used for background task processing in Docker Compose deployments.
# Works with Redis as the message broker.
#
# Components:
#   - Worker: Processes background tasks (`celery -A config worker`)
#   - Beat: Triggers periodic tasks (`celery -A config beat`)
#
# For GCP deployments, tasks are dispatched via Google Cloud Tasks instead.
# See validibot/core/tasks/dispatch/ for the dispatcher abstraction.
#
# Design decisions:
#   - Fire-and-forget: No result backend; all state lives in Django models
#   - Single worker process with prefork pool (concurrency=1 default)
#   - No Celery canvas features (chains, groups, chords) - keep it simple
#   - django-celery-beat for periodic task scheduling via admin UI
#
# See docs/dev_docs/how-to/configure-scheduled-tasks.md for details.

# Broker URL - Redis
CELERY_BROKER_URL = REDIS_URL

# No result backend - fire-and-forget pattern
# All task state is stored in Django models (ValidationRun, etc.)
CELERY_RESULT_BACKEND = None

# Task serialization
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]

# Time zone (use Django's TIME_ZONE)
CELERY_TIMEZONE = TIME_ZONE

# Task execution settings
CELERY_TASK_TRACK_STARTED = True  # Track when tasks start (useful for monitoring)
CELERY_TASK_TIME_LIMIT = 30 * 60  # Hard time limit: 30 minutes
CELERY_TASK_SOFT_TIME_LIMIT = (
    25 * 60
)  # Soft time limit: 25 minutes (raises SoftTimeLimitExceeded)

# Worker settings
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # Don't prefetch; process one task at a time
CELERY_WORKER_CONCURRENCY = env.int("CELERY_WORKER_CONCURRENCY", default=1)

# Beat scheduler - use django-celery-beat's database scheduler
# This allows managing periodic tasks via Django admin
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Task routing (optional - all tasks go to default queue for simplicity)
CELERY_TASK_DEFAULT_QUEUE = "celery"

# Late ack - acknowledge tasks after completion (prevents data loss on worker crash)
CELERY_TASK_ACKS_LATE = True

# Reject on worker lost - requeue task if worker dies unexpectedly
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Redis restores an unacknowledged Celery message after this visibility
# window. It must exceed the hard task limit or a healthy long validation can
# be delivered to a second worker while the first is still running.
CELERY_VISIBILITY_TIMEOUT_SECONDS = env.int(
    "CELERY_VISIBILITY_TIMEOUT_SECONDS",
    default=60 * 60,
)
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": CELERY_VISIBILITY_TIMEOUT_SECONDS,
}
if CELERY_VISIBILITY_TIMEOUT_SECONDS <= CELERY_TASK_TIME_LIMIT:
    raise ImproperlyConfigured(
        "CELERY_VISIBILITY_TIMEOUT_SECONDS must exceed CELERY_TASK_TIME_LIMIT."
    )
