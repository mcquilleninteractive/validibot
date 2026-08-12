# Django Settings

This directory contains Django settings modules for different environments.

## Structure

Settings come in two layers: four community modules, plus a thin Pro
activation variant for three of them.

**Community modules** — the baseline, fully functional without any commercial
package installed:

| File | Purpose |
|------|---------|
| `base.py` | Shared settings inherited by all environments |
| `local.py` | Local development (DEBUG=True, console email, etc.) |
| `production.py` | All production deployments (GCP, AWS, self-hosted) |
| `test.py` | Test runner configuration |

**Pro activation variants** — each does exactly one thing: `from .<base> import *`,
then append `validibot_pro` to `INSTALLED_APPS`:

| File | Extends | Selected by |
|------|---------|-------------|
| `local_pro.py` | `local.py` | `just local-pro` (`docker-compose.local-pro.yml`) |
| `production_pro.py` | `production.py` | Self-hosted Pro — operators set `DJANGO_SETTINGS_MODULE=config.settings.production_pro` |
| `test_pro.py` | `test.py` | `just test-pro` (`pytest --ds=config.settings.test_pro`) |

That `INSTALLED_APPS` append is the load-bearing step. Installing the Pro wheel
makes the package *importable*, but Django only runs its `AppConfig.ready()` and
imports its `__init__.py` — where `validibot.core.license.set_license(PRO_LICENSE)`
lives — when the app is listed in `INSTALLED_APPS`. So the settings module, not
the package install, is what actually activates Pro.

There is no `*_enterprise.py` variant yet. The hosted cloud offering doesn't use
these modules either: it has its own `validibot_cloud.settings.*` modules (in the
`validibot-cloud` repo) that import this repo's settings and already include
`validibot_pro` in their app list.

## Local Development

The recommended way to run Validibot locally is with Docker Compose:

```bash
just local up
```

This uses `DJANGO_SETTINGS_MODULE=config.settings.local` with `USE_DOCKER=yes`.

All services (Django, Postgres, Redis, Celery) run in Docker containers.

If you purchased Pro or Enterprise, copy `.envs.example/.local/.build` to
`.envs/.local/.build`, set an exact `VALIDIBOT_COMMERCIAL_PACKAGE` and
`VALIDIBOT_COMMERCIAL_NETRC` path, then run `just local build` before
`just local up`. To activate Pro, point `DJANGO_SETTINGS_MODULE` at
`config.settings.local_pro` (the variant that adds `validibot_pro` to
`INSTALLED_APPS`) rather than hand-editing a settings module — this is the same
module `just local-pro` selects. Enterprise has no dedicated variant yet, so add
its app to a settings module that extends `local.py`. Either way, don't edit
`config/settings/base.py`; keep edition-specific changes in the
environment-specific module.

## Production Settings

The `production.py` file handles **all** production deployment targets. Platform-specific
configuration is controlled via the `DEPLOYMENT_TARGET` environment variable:

| `DEPLOYMENT_TARGET` | Audience | Infrastructure |
|---------------------|----------|----------------|
| `self_hosted` | Customer-operated | Single-VM Docker Compose (Celery, Docker socket, local/S3/GCS storage) |
| `gcp` | Validibot's hosted offering | Google Cloud Platform (Cloud Run, Cloud Tasks, GCS) |
| `aws` | Future | Amazon Web Services (future: ECS/Batch, SQS, S3) |

The settings file reads `DEPLOYMENT_TARGET` and branches accordingly to configure:

- Storage backends (local filesystem, GCS, or S3)
- Validator runner (Docker socket, Cloud Run Jobs, or AWS Batch)
- Task queue (Celery for self_hosted, Cloud Tasks for GCP)
- Platform-specific integrations

Self-hosted Pro deployments use `production_pro.py` instead, which extends
`production.py` (so it inherits all the `DEPLOYMENT_TARGET` branching above) and
adds `validibot_pro` to `INSTALLED_APPS`. Operators select it by setting
`DJANGO_SETTINGS_MODULE=config.settings.production_pro` in their env file; see the
edition note in `.envs.example/.production/.self-hosted/.django`.

## Environment Files

Platform-specific values live in environment files, not in separate settings modules.

**Templates** are in `.envs.example/` (committed to git):

```
.envs.example/
├── .local/
│   ├── .django
│   ├── .build
│   └── .postgres
└── .production/
    ├── .self-hosted/
	    │   ├── .build
	    │   ├── .django
	    │   └── .postgres
    ├── .google-cloud/
	    │   ├── .build
	    │   ├── .django
	    │   └── .just
    └── .aws/
        └── .django
```

**Your actual secrets** go in `.envs/` (gitignored):

```
.envs/
├── .local/
│   ├── .build           # optional build args
│   ├── .django
│   └── .postgres
└── .production/
    ├── .self-hosted/
	    │   ├── .build       # optional build args
	    │   ├── .django          # DEPLOYMENT_TARGET=self_hosted
	    │   └── .postgres
    ├── .google-cloud/
	    │   ├── .build           # deploy knobs and shared non-secret x402 values
	    │   ├── .django          # DEPLOYMENT_TARGET=gcp
	    │   └── .just            # host-side GCP command context
    └── .aws/
        └── .django          # DEPLOYMENT_TARGET=aws
```

Copy templates to `.envs/` and edit with your values. See `.envs.example/README.md` for setup instructions.

Each production env file must set:

```bash
DJANGO_SETTINGS_MODULE=config.settings.production
DEPLOYMENT_TARGET=self_hosted  # or gcp, aws
DJANGO_SECRET_KEY=<generated django signing key>
DJANGO_API_KEY_DIGEST_KEY=<separate generated API-key digest key>
```

## Why This Structure?

1. **Single source of truth** — Production logic is in one file, making it easier to maintain
2. **Clear separation** — Environment differences are in env files, not scattered across settings modules
3. **Explicit configuration** — `DEPLOYMENT_TARGET` makes the deployment type visible and explicit
4. **Reduced duplication** — Common production settings (security, logging, etc.) aren't duplicated
5. **Secrets never committed** — `.envs/` is fully gitignored; templates live in `.envs.example/`

## Adding a New Deployment Target

1. Add the target name to `VALID_DEPLOYMENT_TARGETS` in `production.py`
2. Add conditional blocks for storage, validator runner, and other platform-specific settings
3. Create a template env file in `.envs.example/.production/.new-target/.django`
