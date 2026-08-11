# =============================================================================
# Validibot - Google Cloud Platform Deployment
# =============================================================================
#
# Commands for deploying and managing Validibot on Google Cloud Platform.
#
# Architecture:
#   - Cloud Run: Web and Worker services (containerized Django)
#   - Cloud SQL: PostgreSQL database (managed)
#   - Cloud Storage: Media and file storage + CMEK-encrypted audit log
#                    archive (GCS buckets)
#   - Cloud Tasks: Background job queue
#   - Cloud Scheduler: Cron-like scheduled tasks
#   - Secret Manager: Environment variables and secrets
#   - Cloud KMS: Cryptographic key management for credential signing
#   - Global Load Balancer: HTTPS termination and custom domains
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Docker configured for Artifact Registry: just gcp auth
#   - GCP config sourced: source .envs/.production/.google-cloud/.just
#
# Quick start (new environment):
#   source .envs/.production/.google-cloud/.just
#   just gcp init-stage dev           # Create infrastructure
#   # Create .envs/.dev/.google-cloud/.django manually (see .envs.example/)
#   just gcp secrets dev              # Upload secrets to Secret Manager
#   just gcp deploy-all dev           # Build, migrate, initialize, and deploy
#
# Daily operations:
#   source .envs/.production/.google-cloud/.just
#   just gcp deploy prod              # Deploy web service to prod
#   just gcp logs prod                # View logs
#   just gcp status prod              # Check service status
#
# =============================================================================

# Use bash for shell commands (more predictable than sh)
set shell := ["bash", "-cu"]

# Run recipes from the project root (not this submodule directory)
set working-directory := '../..'

# Ensure gcloud SDK is in PATH (needed for docker-credential-gcloud)
# This handles the case where gcloud is installed but not in PATH
export PATH := env_var("HOME") + "/google-cloud-sdk/bin:" + env_var("PATH")

# =============================================================================
# GCP Project Configuration
# =============================================================================
#
# Configuration is loaded from environment variables. Set these before running:
#
#   export GCP_PROJECT_ID="your-project-id"
#   export GCP_REGION="us-central1"
#
# Or source the config file:
#
#   source .envs/.production/.google-cloud/.just
#
# See .envs.example/.production/.google-cloud/.just for a template.
#
# =============================================================================

# Load from environment variables
# Use env_var_or_default with empty string to allow non-GCP commands to run.
# Actual GCP commands will fail with a clear error if not set.
gcp_project := env_var_or_default("GCP_PROJECT_ID", "")
gcp_region := env_var_or_default("GCP_REGION", "us-central1")

# Application name prefix for all GCP resource names.
# All resource names follow the pattern: {app_name}-{resource}[-{stage}]
# Set GCP_APP_NAME in .envs/.production/.google-cloud/.just or your environment.
app_name := env_var_or_default("GCP_APP_NAME", "validibot")

# Provider retention policy. The current hosted installation is empty, so its
# project wrapper uses ``latest-only``. A live installation with users or data
# must set ``drain-and-retain`` before its first release update.
validator_provider_retention_mode := env_var_or_default("VALIDATOR_PROVIDER_RETENTION_MODE", "latest-only")

# Docker image path in Artifact Registry
# Format: {region}-docker.pkg.dev/{project}/{repository}/{image}
gcp_image := gcp_region + "-docker.pkg.dev/" + gcp_project + "/" + app_name + "/" + app_name + "-web"

# Cloud Run request timeout (1 hour)
# Long timeout needed for validator jobs that process large files
gcp_cloud_run_request_timeout := env_var_or_default("GCP_CLOUD_RUN_TIMEOUT", "3600s")

# Migration Cloud Run Job task timeout. Default 5 min; override for a long
# prod data migration, e.g. GCP_MIGRATE_TIMEOUT=1800s just gcp deploy-all prod
gcp_migrate_task_timeout := env_var_or_default("GCP_MIGRATE_TIMEOUT", "300s")

# Outer validator Job timeout. The Django watchdog reads the same setting and
# only fences a run once this provider budget has elapsed.
gcp_validator_task_timeout := env_var_or_default("VALIDATOR_TIMEOUT_SECONDS", "3600")

# Cloud Scheduler timezone for cron jobs
gcp_scheduler_timezone := env_var_or_default("GCP_SCHEDULER_TIMEZONE", "UTC")

# Validator container registry path
validator_repo := gcp_region + "-docker.pkg.dev/" + gcp_project + "/" + app_name

# Helper to check GCP_PROJECT_ID is set before running commands
[private]
_require-gcp-config:
    #!/usr/bin/env bash
    if [ -z "${GCP_PROJECT_ID:-}" ]; then
        echo "Error: GCP_PROJECT_ID is not set."
        echo ""
        echo "GCP deployment is not yet automated in CI. For now, deploy"
        echo "manually by sourcing your GCP config first:"
        echo ""
        echo "  source .envs/.production/.google-cloud/.just"
        exit 1
    fi

# Require the complete LIVE lifecycle state before an ordinary deploy or other
# online-only operation. Database availability alone is insufficient because
# MAINTENANCE deliberately keeps Cloud SQL running while runtime ingress,
# queues, and schedulers remain isolated.
[private]
_mode-require-live stage:
    #!/usr/bin/env bash
    set -euo pipefail
    MODE=$(just gcp _mode-current {{stage}})
    if [ "$MODE" != "LIVE" ]; then
        echo "Error: {{stage}} is not LIVE (current mode: $MODE)."
        echo ""
        echo "This command is blocked unless every lifecycle signal is healthy:"
        echo "database ready, public runtime restored, queues running, and managed"
        echo "scheduler jobs active. Inspect and reconcile the stage first:"
        echo ""
        echo "  just gcp mode-status {{stage}}"
        echo "  just gcp mode-live {{stage}}"
        exit 1
    fi

# Print the reconciled lifecycle mode for a stage. The public status recipe
# remains intentionally verbose for operators; callers use this small helper
# when they need a machine-readable decision.
[private]
_mode-current stage:
    #!/usr/bin/env bash
    set -euo pipefail
    STATUS=$(just gcp mode-status {{stage}})
    MODE=$(awk '/^[[:space:]]*Overall:/ {print $2; exit}' <<< "$STATUS")
    case "$MODE" in
        LIVE|MAINTENANCE|PARKED)
            printf '%s\n' "$MODE"
            ;;
        *)
            printf '%s\n' "PARTIALLY_TRANSITIONED"
            ;;
    esac

# Return a stage to the lifecycle state captured by a higher-level operation.
# Validator workflows use this after their isolated work completes so they do
# not accidentally convert a PARKED or MAINTENANCE stage into LIVE.
[private]
_mode-restore stage mode:
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{mode}}" in
        LIVE)
            GCP_YES=1 just gcp mode-live {{stage}}
            ;;
        MAINTENANCE)
            GCP_YES=1 just gcp mode-maintenance {{stage}}
            ;;
        PARKED)
            GCP_YES=1 just gcp mode-parked {{stage}}
            ;;
        *)
            echo "Error: cannot restore unknown lifecycle mode '{{mode}}'." >&2
            exit 1
            ;;
    esac

# Git SHA for image tagging (inherited from common.just via root justfile)
git_sha := `git rev-parse --short HEAD`

# Validibot app version stamped into the Django image and Cloud Run env.
#
# Version-stamping policy: pyproject.toml is the source of truth on
# every deploy path (daily ``just gcp deploy <stage>``,
# ``deploy-all <stage>``, etc.). The ``upgrade`` recipe overrides
# this via env var (``VALIDIBOT_VERSION="${TARGET}"`` after
# ``git checkout``); daily deploys use whatever pyproject says at the
# current commit. Detailed rationale lives in ``just/common.just``
# (deliberately repeated here because just modules have isolated
# scope and the constant assignment must live in the module).
validibot_version := `awk -F'"' '/^[[:space:]]*version[[:space:]]*=/ {print "v" $2; exit}' "$(git rev-parse --show-toplevel)/pyproject.toml"`

# Validator backend source metadata for images built by the developer-facing
# validator-build recipes below. Runtime trust does not consume this; validator
# runs are still identified by image digest.
validator_backend_git_sha := `git -C ../validibot-validator-backends rev-parse --short HEAD 2>/dev/null || echo "dev"`

# =============================================================================
# Submodules
# =============================================================================
#
# Service-specific submodules under ``just gcp``. The umbrella recipes
# at the top level of this module (``secrets``, ``deploy-all``) delegate
# to these for the per-service work, so there's one source of truth for
# each service's GCP operations.
#
# Users pick the granularity they need:
#
#   just gcp secrets prod           # umbrella — pushes .django AND .mcp
#   just gcp django secrets prod    # surgical — only .django
#   just gcp mcp secrets prod       # surgical — only .mcp
#
# The same triangle exists for other MCP operations (build, deploy,
# logs, status, ...) via the ``gcp mcp`` submodule; there's no
# corresponding Django submodule yet because Django has no additional
# per-service recipes distinct from the main ``gcp`` commands.

# Django-specific operations on GCP (currently just the surgical
# ``secrets`` recipe).
mod django 'django'

# MCP server operations on GCP — re-exposes the existing ``just/mcp``
# module at ``just gcp mcp`` so operators can use either grammar:
#
#   just mcp deploy prod         # historical path (still works)
#   just gcp mcp deploy prod     # new grammar, matches django + secrets pattern
#
# Both invocations hit the same module; there is no code duplication.
# The test recipes (``just mcp test``, ``just mcp test-e2e``) also
# surface at ``just gcp mcp test`` but are genuinely local commands —
# the more natural home remains ``just mcp ...`` for those.
mod mcp '../mcp'

# =============================================================================
# Service Account Configuration
# =============================================================================
#
# Each stage has its own service account for security isolation:
#   - {app_name}-cloudrun-prod@{project}.iam.gserviceaccount.com
#   - {app_name}-cloudrun-dev@{project}.iam.gserviceaccount.com
#   - {app_name}-cloudrun-staging@{project}.iam.gserviceaccount.com
#
# The app_name defaults to "validibot" and can be changed via GCP_APP_NAME.
#
# Service accounts have minimal permissions:
#   - cloudsql.client: Connect to Cloud SQL
#   - secretmanager.secretAccessor: Read secrets
#   - run.invoker: Call other Cloud Run services
#   - cloudtasks.enqueuer/viewer: Manage task queue
#   - storage.objectUser: Read/write to GCS buckets
#   - cloudkms.viewer/signerVerifier: Sign credentials with KMS
#
# =============================================================================

# Helper to compute service account email
[private]
_service-account stage:
    @if [ "{{stage}}" = "prod" ]; then \
        echo "{{app_name}}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"; \
    else \
        echo "{{app_name}}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"; \
    fi

# =============================================================================
# Build & Push
# =============================================================================

# Build Docker image for Cloud Run (linux/amd64 for Cloud Run on x86_64).
# Set GCP_BUILD_VALIDIBOT_CLOUD=1 to include the commercial layers (Pro + Cloud).
# Community users can ignore this flag.
#
# Safety: Before building, checks all repos for uncommitted changes. Building
# from a dirty working tree is dangerous because rsync copies whatever is on
# disk — you could accidentally deploy unreleased or broken code. The check
# covers the community repo plus Pro and Cloud (when included). Set
# GCP_ALLOW_DIRTY=1 to bypass in emergencies.
[private]
_build-preflight: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "${GCP_ALLOW_DIRTY:-0}" != "1" ]; then
        DIRTY_REPOS=()

        # Check community repo (always — it's the base image)
        if [ -n "$(git status --porcelain)" ]; then
            DIRTY_REPOS+=("validibot (community)")
        fi

        # Check commercial repos when they'll be included in the build
        if [ "${GCP_BUILD_VALIDIBOT_CLOUD:-0}" = "1" ]; then
            for REPO in validibot-pro validibot-cloud; do
                if [ -d "../$REPO" ] && [ -n "$(git -C "../$REPO" status --porcelain)" ]; then
                    DIRTY_REPOS+=("$REPO")
                fi
            done
        fi

        if [ ${#DIRTY_REPOS[@]} -gt 0 ]; then
            echo "Error: Uncommitted changes detected in:"
            for REPO in "${DIRTY_REPOS[@]}"; do
                echo "  - $REPO"
            done
            echo ""
            echo "Building from a dirty working tree means the image contents"
            echo "won't match the git SHA tag ({{git_sha}}). This makes rollbacks"
            echo "unreliable and debugging difficult."
            echo ""
            echo "Commit or stash your changes first, or set GCP_ALLOW_DIRTY=1"
            echo "to bypass this check in an emergency."
            exit 1
        fi
    else
        echo "Warning: GCP_ALLOW_DIRTY=1 — skipping dirty-repo check."
        echo "The image may contain uncommitted changes not tracked by git."
    fi

# Docker images are tagged with the HEAD commit SHA, so the preflight must
# prove every contributing repository is clean before a build can begin.
build: _require-gcp-config _build-preflight
    #!/usr/bin/env bash
    set -euo pipefail

    # Resolve the image tag once. Daily deploys use the parse-time
    # ``{{git_sha}}`` constant (whatever HEAD was when ``just`` parsed
    # the file). The ``upgrade`` recipe exports
    # ``VALIDIBOT_REVISION=$(git rev-parse --short HEAD)`` AFTER its
    # ``git checkout``, so the override gives us the post-checkout
    # commit. Threading this through ``build → push → deploy → migrate``
    # consistently means the registry tag matches the actual code in
    # the image — preventing the "OLD tag silently re-points to NEW
    # code" hazard during upgrades.
    IMAGE_TAG="${VALIDIBOT_REVISION:-{{git_sha}}}"

    echo "Building image: {{gcp_image}}:${IMAGE_TAG} (Validibot ${VALIDIBOT_VERSION:-{{validibot_version}}})"

    DOCKERFILE="compose/production/django/Dockerfile"

    # Default to community build: no extras, empty overlay directory.
    EXTRA_UV_EXTRAS=""
    EXTRA_PACKAGES_CONTEXT=""

    if [ "${GCP_BUILD_VALIDIBOT_CLOUD:-0}" = "1" ]; then
        # Copy sibling commercial package sources into an out-of-tree
        # overlay that the Dockerfile reads via --build-context.
        # Out-of-tree placement means the runtime stage's COPY . /app
        # can't pick them up even if .dockerignore regresses.
        OVERLAY_DIR="$(mktemp -d -t validibot-extras-XXXXXXXX)"
        cleanup() { rm -rf "$OVERLAY_DIR"; }
        trap cleanup EXIT

        # Denylist of paths a commercial package source tree might
        # contain but MUST NOT enter the image. Anything not strictly
        # needed for ``uv pip install --no-deps .`` is excluded.
        # Operator-local files (.envs/, .envrc) are the real risk;
        # the rest just keeps the overlay small.
        RSYNC_EXCLUDES=(
            # VCS and developer state
            --exclude='.git' --exclude='.git/' --exclude='.gitignore'
            --exclude='.venv' --exclude='venv' --exclude='env'
            --exclude='.direnv' --exclude='.envrc'
            # Operator-local env files (NEVER copy — these contain secrets)
            --exclude='.envs' --exclude='.envs/' --exclude='.envs.example'
            --exclude='.env' --exclude='.env.*' --exclude='*.env'
            # Python caches and build artifacts
            --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo'
            --exclude='*.egg-info' --exclude='build' --exclude='dist'
            --exclude='.pytest_cache' --exclude='.ruff_cache'
            --exclude='.mypy_cache' --exclude='.tox' --exclude='htmlcov'
            --exclude='.coverage' --exclude='.coverage.*' --exclude='coverage.xml'
            # IDE and agent config
            --exclude='.vscode' --exclude='.idea' --exclude='.claude'
            # Infra definitions that belong to the source repo, not the image
            --exclude='Dockerfile' --exclude='Dockerfile.*'
            --exclude='docker-compose.yml' --exclude='docker-compose.*.yml'
            --exclude='compose' --exclude='deploy'
            --exclude='justfile' --exclude='just' --exclude='.pre-commit-config.yaml'
            # Docs, tests, and other runtime-irrelevant trees
            --exclude='docs' --exclude='tests' --exclude='test'
            --exclude='node_modules' --exclude='static/dist' --exclude='media'
            --exclude='tmp' --exclude='.cache' --exclude='knowledge_base'
            # Stray data files a developer might leave lying around
            --exclude='*.log' --exclude='*.sqlite3' --exclude='*.db'
        )

        for REPO in validibot-pro validibot-cloud; do
            SRC="../$REPO"
            DST="${OVERLAY_DIR}/$REPO"

            if [ ! -d "$SRC" ]; then
                echo "Error: $SRC not found."
                echo "GCP_BUILD_VALIDIBOT_CLOUD=1 requires the $REPO repo as a sibling directory."
                exit 1
            fi

            echo "Copying $REPO into build overlay at ${OVERLAY_DIR}/$REPO..."
            rsync -a "${RSYNC_EXCLUDES[@]}" "$SRC/" "$DST/"
        done

        # Paranoia check — confirm no secret-bearing paths sneaked through
        # (belt-and-suspenders against a future rsync-excludes regression).
        if find "$OVERLAY_DIR" \( -name '.env' -o -name '.env.*' -o -name '*.env' -o -path '*/.envs*' -o -path '*/.envrc' \) -print -quit 2>/dev/null | grep -q .; then
            echo "ERROR: secret-bearing path found in overlay after rsync:"
            find "$OVERLAY_DIR" \( -name '.env' -o -name '.env.*' -o -name '*.env' -o -path '*/.envs*' -o -path '*/.envrc' \)
            echo "The rsync excludes list in just/gcp/mod.just is missing a pattern."
            exit 1
        fi

        # Enable the cloud extra and point --build-context at the
        # overlay. "extras" must match the ``FROM scratch AS extras``
        # label in the Dockerfile.
        EXTRA_UV_EXTRAS="cloud"
        EXTRA_PACKAGES_CONTEXT="--build-context extras=${OVERLAY_DIR}"
    fi

    # --provenance=false: Docker 28+ defaults to provenance attestations,
    # which creates a multi-platform manifest index. When pushing a
    # cross-platform image (amd64 built on arm64 Mac), Artifact Registry
    # sees the manifest but can't resolve the platform, causing:
    #   "image was found but does not provide any platform"
    # Disabling provenance produces a simple single-platform image that
    # pushes and deploys correctly.
    #
    # Use --no-cache only when GCP_NO_CACHE=1 is set (e.g. after
    # Dockerfile RUN-line changes that Docker's layer cache would mask).
    # Default builds use the cache for speed — dependency installation
    # layers take 10+ minutes to rebuild from scratch.
    CACHE_FLAG=""
    if [ "${GCP_NO_CACHE:-0}" = "1" ]; then
        CACHE_FLAG="--no-cache"
        echo "Building with --no-cache (GCP_NO_CACHE=1)"
    fi
    # Version + revision stamping policy:
    #
    # Default: ``{{validibot_version}}`` (from pyproject.toml) and
    # ``{{git_sha}}`` (from ``git rev-parse --short HEAD``) are
    # captured at justfile-load time. That covers the everyday
    # "deploy whatever's checked out" flow.
    #
    # Override: when an operator runs ``just gcp upgrade <stage> <ver>``,
    # the upgrade recipe exports BOTH ``VALIDIBOT_VERSION`` and
    # ``VALIDIBOT_REVISION`` after ``git checkout``. The
    # ``${VAR:-default}`` forms pick the explicit overrides if set,
    # otherwise fall back to the parse-time defaults.
    #
    # The override threads through THREE places that must all agree
    # for upgrade provenance to be honest:
    #
    #   1. ``--build-arg VALIDIBOT_REVISION`` → in-image LABEL.
    #      Surfaced by doctor / support-bundle.
    #   2. ``-t {{gcp_image}}:${IMAGE_TAG}`` (computed above) →
    #      registry tag. The tag must move with the actual code, or
    #      an upgrade silently re-points the OLD tag to NEW image
    #      contents — breaking rollback by tag.
    #   3. ``--build-arg VALIDIBOT_VERSION`` → release-tag label.
    #      Drives backup-manifest provenance and doctor metadata.
    #
    # All three reach the daily-deploy path via parse-time defaults,
    # and all three reach the upgrade path via the same env-var
    # override mechanism — symmetry that's easy to verify.
    docker build --platform linux/amd64 --provenance=false $CACHE_FLAG \
        -f "$DOCKERFILE" \
        --build-arg EXTRA_UV_EXTRAS="${EXTRA_UV_EXTRAS}" \
        --build-arg VALIDIBOT_VERSION="${VALIDIBOT_VERSION:-{{validibot_version}}}" \
        --build-arg VALIDIBOT_REVISION="${VALIDIBOT_REVISION:-{{git_sha}}}" \
        ${EXTRA_PACKAGES_CONTEXT} \
        -t {{gcp_image}}:${IMAGE_TAG} \
        -t {{gcp_image}}:latest .

    if [ "${GCP_BUILD_VALIDIBOT_CLOUD:-0}" = "1" ]; then
        echo "Build complete. Commercial layers included (Pro + Cloud)."
    else
        echo "Build complete (community only)."
    fi

# Push Docker image to Google Artifact Registry
# Requires: gcloud auth configure-docker (run 'just gcp auth' first)
#
# Tag selection mirrors ``build``: ``${VALIDIBOT_REVISION:-{{git_sha}}}``
# resolves to the parse-time SHA on daily deploys and the post-checkout
# SHA when called from ``upgrade``. Inlined per call site rather than
# stored in a local because ``push`` is a non-bash recipe and each
# line runs as its own shell command (no shared variables).
push:
    @echo "Pushing image: {{gcp_image}}:${VALIDIBOT_REVISION:-{{git_sha}}}"
    docker push {{gcp_image}}:${VALIDIBOT_REVISION:-{{git_sha}}}
    docker push {{gcp_image}}:latest

# =============================================================================
# Deploy Services
# =============================================================================

# Deploy web service to a specific stage.
# This is the main user-facing service that handles HTTP requests.
#
# The deploy chain runs in order:
#   1. build   — Build Docker image (with dirty-repo guard)
#   2. push    — Push to Artifact Registry
#   3. _migrate — Run database migrations via Cloud Run Job (new image)
#   4. _deploy-web — Update the Cloud Run service to the new image
#
# Migrations run BEFORE new instances start, so the database schema is
# always compatible with the code that's about to serve traffic. If
# migrations fail, the deploy is aborted and the current version keeps
# running.
#
# To skip migrations (e.g., hotfix with no schema changes):
#   GCP_SKIP_MIGRATE=1 just gcp deploy prod
#
# Key configuration:
#   - min-instances: prod=1 (avoid cold starts), dev/staging=0 (cost saving)
#   - max-instances: prod=4, dev/staging=2
#   - memory: 1Gi (sufficient for Django + gunicorn)
#   - allow-unauthenticated: public web access

# _confirm-deploy: interactive guard before a PROD traffic cutover.
# Every ``gcloud run deploy`` shifts 100% of traffic to the new revision
# immediately, so for prod we require the operator to confirm by typing the
# stage name. Non-prod stages skip it; set GCP_YES=1 to skip for automation.
[private]
[arg('stage', pattern='dev|staging|prod')]
_confirm-deploy stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" != "prod" ]; then
        exit 0
    fi
    if [ "${GCP_YES:-}" = "1" ] || [ "${GCP_YES:-}" = "true" ]; then
        echo "GCP_YES set — skipping prod deploy confirmation."
        exit 0
    fi
    echo ""
    echo "⚠  About to deploy to PROD and shift 100% of traffic to the new revision."
    echo "   Roll back afterwards with:  just gcp rollback prod"
    echo "   Type the stage name 'prod' to confirm, or anything else to abort:"
    read -r CONFIRM
    if [ "$CONFIRM" != "prod" ]; then
        echo "Aborted."
        exit 1
    fi
    printf 'Starting production deployment...\n'

[arg('stage', pattern='dev|staging|prod')]
deploy stage: _require-gcp-config (_confirm-deploy stage) (_mode-require-live stage) build push (_maybe-migrate stage) (_deploy-web stage)

# Deploy worker service to a specific stage.
# The worker handles background tasks triggered by Cloud Tasks.
#
# Key differences from web service:
#   - no-allow-unauthenticated: Only Cloud Tasks/Scheduler can invoke
#   - Always scales to zero (no warm instances needed)
#   - Same image as web, different APP_ROLE env var
#
# Migrations run before the worker deploy too, because the worker
# processes may rely on new database schema (new model fields, etc.).
#
# Usage: just gcp deploy-worker dev | just gcp deploy-worker prod
[arg('stage', pattern='dev|staging|prod')]
deploy-worker stage: _require-gcp-config (_confirm-deploy stage) (_mode-require-live stage) build push (_maybe-migrate stage) (_deploy-worker stage)

# Deploy every service to a stage (single build+push for Django, plus
# an MCP build+deploy when ENABLE_MCP_SERVER is set).
#
# Composition:
#   1. build + push Django image
#   2. migrate (unless GCP_SKIP_MIGRATE=1)
#   3. deploy web + worker Cloud Run services
#   4. sync Cloud Scheduler jobs
#   5. build + deploy MCP — no-op when ENABLE_MCP_SERVER is unset
#
# Step 5 is gated by the ``just mcp`` recipes' built-in
# ``_mcp_enabled_gate``: when ENABLE_MCP_SERVER is unset, each MCP
# recipe prints a "skipped" message and exits 0. Community-only
# operators get the same deploy-all UX as cloud operators — just
# without the MCP step running.
#
# ENABLE_MCP_SERVER can be set three ways (in precedence order):
#   1. Inline on the command line
#   2. Your shell environment
#   3. The STAGE-SPECIFIC ``.envs/<stage>/.google-cloud/.build`` file
#      (prod reads .envs/.production/...; dev reads .envs/.dev/...).
#      See ``.envs.example/.production/.google-cloud/.build`` for the
#      template. This is the "set and forget" path — each stage has
#      its own knob file so a dev deploy cannot accidentally inherit
#      prod's settings.
#
# Usage: just gcp deploy-all dev | just gcp deploy-all prod
# Usage (with MCP, ad-hoc): ENABLE_MCP_SERVER=true just gcp deploy-all prod
# Usage (with MCP, persisted via .build file): just gcp deploy-all prod
[arg('stage', pattern='dev|staging|prod')]
deploy-all stage: _require-gcp-config (_confirm-deploy stage) (_mode-require-live stage) build push (_maybe-migrate stage) (_deploy-web stage) (_deploy-worker stage) (scheduler-setup stage) (_maybe-deploy-mcp stage)
    @echo ""
    @echo "All services deployed to {{stage}}"
    @echo ""
    @echo "Note: This deploy does NOT upload secrets. If you changed"
    @echo "environment variables in .envs/, run:"
    @echo "  just gcp secrets {{stage}}"
    @echo "  just gcp deploy-all {{stage}}"
    @echo ""
    @echo "``just gcp secrets`` uploads .django always; it also uploads"
    @echo ".mcp when ENABLE_MCP_SERVER is set (in .build or inline)."

# Roll the web service back to a previously-deployed revision by shifting
# 100% of traffic to it — the recovery path for a bad deploy. With no
# ``revision`` argument it targets the previous revision (the one created
# just before the newest). Pass an explicit revision name to pin a known-good
# one. This only moves traffic: it does NOT rebuild or re-migrate, so it is
# fast and safe to run. Skip the confirmation with GCP_YES=1.
#
# CAVEAT: rolling code back does not roll a database migration back. Keep
# migrations expand-only (additive) — see ADR-2026-06-10c — so the older
# revision is always compatible with the newer schema.
#
# Usage: just gcp rollback prod  |  just gcp rollback prod <revision-name>
[arg('stage', pattern='dev|staging|prod')]
rollback stage revision="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
    fi

    echo "Recent revisions for $SERVICE (newest first):"
    gcloud run revisions list \
        --service="$SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --sort-by="~metadata.creationTimestamp" \
        --format="table(metadata.name, metadata.creationTimestamp, status.conditions[0].type)" \
        --limit=5 2>/dev/null || true
    echo ""

    TARGET="{{revision}}"
    if [ -z "$TARGET" ]; then
        # No revision given: pick the previous revision (2nd newest by creation).
        TARGET=$(gcloud run revisions list \
            --service="$SERVICE" \
            --region={{gcp_region}} \
            --project={{gcp_project}} \
            --sort-by="~metadata.creationTimestamp" \
            --format="value(metadata.name)" 2>/dev/null | sed -n '2p')
        if [ -z "$TARGET" ]; then
            echo "Error: could not determine a previous revision for $SERVICE." >&2
            echo "  Pass one explicitly:  just gcp rollback {{stage}} <revision-name>" >&2
            exit 1
        fi
    fi

    echo "⚠  Roll back $SERVICE ({{stage}}) — shift 100% of traffic to:"
    echo "     $TARGET"
    if [ "${GCP_YES:-}" != "1" ] && [ "${GCP_YES:-}" != "true" ]; then
        echo "   Type 'yes' to confirm, or anything else to abort:"
        read -r CONFIRM
        if [ "$CONFIRM" != "yes" ]; then
            echo "Aborted."
            exit 1
        fi
    fi

    gcloud run services update-traffic "$SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --to-revisions="$TARGET=100"

    echo ""
    echo "✓ Rolled $SERVICE back to $TARGET (100% traffic)."

# =============================================================================
# Versioned Upgrade (Phase 4 of ADR-2026-04-27)
# =============================================================================
#
# ``just gcp upgrade <stage> <version>`` wraps ``deploy`` with the same
# discipline self-hosted gets from ``just self-hosted upgrade``:
#
#   1. Pre-flight: doctor --strict       → refuse on unhealthy state (AC #15)
#   2. Pre-flight: clean working tree    → refuse to checkout over uncommitted edits
#   3. Pre-flight: target tag exists     → refuse to upgrade to a non-existent version
#   4. Pre-flight: validate upgrade path → refuse cross-major-version jumps (AC #16)
#   5. Backup                            → manifested backup unless --no-backup
#   6. Git checkout target version       → switch the working tree to <target>
#   7. ``just gcp deploy-all <stage>``   → existing build + push + migrate + deploy
#   8. Post-flight: doctor               → verify deploy landed cleanly
#   9. Post-flight: smoke-test           → end-to-end pipeline check
#  10. Write upgrade report              → backups/upgrades/<target>-<stage>/report.json
#
# We DON'T overload the existing ``deploy`` recipe with a ``--to`` flag
# because the team uses ``deploy`` for daily continuous-deploy work
# from main; the gated ``upgrade`` recipe is for explicit version
# transitions where the operator wants every safety rail engaged.
# Same lifecycle, different verbs per audience.

# Versioned upgrade with backup + doctor + smoke-test gates (Phase 4).
# Usage: just gcp upgrade prod v0.9.0 [--no-backup]
[arg('stage', pattern='dev|staging|prod')]
upgrade stage version *flags: _require-gcp-config
    @just gcp _run-upgrade {{stage}} {{version}} "{{flags}}"

# Run the manifested upgrade orchestration for GCP.
#
# Mirrors the self-hosted _run-upgrade recipe step-for-step. The only
# differences are the substrate-specific calls — ``just gcp backup``
# instead of ``just self-hosted backup``, ``just gcp deploy-all``
# instead of local ``docker compose build`` + migrate + restart, and
# ``just gcp doctor`` / ``just gcp smoke-test`` for the post-flight.
[private]
_run-upgrade stage version flags:
    #!/usr/bin/env bash
    set -euo pipefail

    TARGET="{{version}}"
    if [ -z "${TARGET}" ]; then
        echo "ERROR: version is required." >&2
        echo "Usage: just gcp upgrade <stage> <version> [--no-backup]" >&2
        exit 1
    fi

    NO_BACKUP=0
    # shellcheck disable=SC2086
    set -- {{flags}}
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-backup) NO_BACKUP=1 ;;
            "") ;;
            *)
                echo "ERROR: unknown flag: $1" >&2
                echo "Usage: just gcp upgrade <stage> <version> [--no-backup]" >&2
                exit 1
                ;;
        esac
        shift
    done

    if ! [[ "${TARGET}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]]; then
        echo "ERROR: '${TARGET}' is not a vX.Y.Z release tag." >&2
        exit 1
    fi

    UPGRADE_DIR="backups/upgrades/${TARGET}-{{stage}}"
    REPORT_FILE="${UPGRADE_DIR}/report.json"
    # ``completed.ok`` is the success sentinel — written only when both
    # post-flight checks (doctor + smoke-test) pass. ``report.json``
    # is unconditional status, including failed runs. Splitting the
    # two means a degraded upgrade (report present, marker absent) is
    # retryable — see the self-hosted equivalent for the rationale.
    COMPLETED_MARKER="${UPGRADE_DIR}/completed.ok"
    mkdir -p "${UPGRADE_DIR}"
    STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    if [ -f "${COMPLETED_MARKER}" ]; then
        echo "Upgrade to ${TARGET} on {{stage}} already complete — marker at ${COMPLETED_MARKER}."
        exit 0
    fi
    if [ -f "${REPORT_FILE}" ]; then
        echo "Previous attempt to ${TARGET} on {{stage}} did not complete cleanly — retrying."
        echo "(See ${REPORT_FILE} for the prior verdict.)"
        echo ""
    fi

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Upgrade — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Target:  ${TARGET}"
    echo "  Backup:  $([ ${NO_BACKUP} -eq 1 ] && echo 'SKIPPED (--no-backup)' || echo 'will run')"
    echo "  Report:  ${REPORT_FILE}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Pre-flight 1: doctor --strict ─────────────────────────────────
    echo "Pre-flight 1/4: running doctor --strict..."
    if ! just gcp doctor {{stage}} --strict; then
        echo "Error: doctor --strict failed. Refusing to upgrade." >&2
        exit 1
    fi
    echo "  ✓ doctor --strict passed."
    echo ""

    # ── Pre-flight 2: clean working tree ──────────────────────────────
    echo "Pre-flight 2/4: checking working tree..."
    if ! git diff --quiet HEAD 2>/dev/null; then
        echo "Error: working tree has uncommitted changes." >&2
        exit 1
    fi
    echo "  ✓ Working tree is clean."
    echo ""

    # ── Pre-flight 3: target tag exists ───────────────────────────────
    echo "Pre-flight 3/4: verifying target tag exists..."
    git fetch --tags --quiet origin || true
    if ! git rev-parse --verify --quiet "${TARGET}^{commit}" >/dev/null; then
        echo "Error: tag '${TARGET}' not found in this repository." >&2
        echo "       Pull latest tags with: git fetch --tags origin" >&2
        exit 1
    fi
    CURRENT_TAG=$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || echo "v0.0.0")
    echo "  ✓ Target ${TARGET} reachable (current: ${CURRENT_TAG})."
    echo ""

    # ── Pre-flight 4: validate upgrade path ───────────────────────────
    echo "Pre-flight 4/4: validating upgrade path..."
    CURRENT_MAJOR=$(echo "${CURRENT_TAG}" | sed -E 's/^v([0-9]+)\..*/\1/')
    TARGET_MAJOR=$(echo "${TARGET}" | sed -E 's/^v([0-9]+)\..*/\1/')
    if [ "${CURRENT_MAJOR}" -ne "${TARGET_MAJOR}" ]; then
        NEXT_STOP=$(git tag --list "v${CURRENT_MAJOR}.*" --sort=-v:refname | head -1)
        echo "Error: cannot upgrade across major versions in one step." >&2
        if [ -n "${NEXT_STOP}" ]; then
            echo "  Required intermediate stop: ${NEXT_STOP}." >&2
            echo "  Run:  just gcp upgrade {{stage}} ${NEXT_STOP}  first." >&2
        fi
        exit 1
    fi
    echo "  ✓ Path ${CURRENT_TAG} → ${TARGET} is within major v${CURRENT_MAJOR}."
    echo ""

    # ── Step 1/4: Backup ──────────────────────────────────────────────
    BACKUP_REF=""
    if [ ${NO_BACKUP} -eq 0 ]; then
        echo "Step 1/4: Creating pre-upgrade backup..."
        TMP_BACKUP_LOG=$(mktemp)
        trap 'rm -f "$TMP_BACKUP_LOG"' EXIT
        just gcp backup {{stage}} | tee "${TMP_BACKUP_LOG}"
        # GCP backup prints "Backup root: gs://..." near the end.
        BACKUP_REF=$(awk '/Backup root:/ {print $NF; exit}' "${TMP_BACKUP_LOG}")
        echo "  ✓ Backup at ${BACKUP_REF}"
        echo ""
    else
        echo "Step 1/4: Backup SKIPPED (--no-backup)."
        echo "          ⚠ Restore from backup is not available if this upgrade fails."
        echo ""
    fi

    # ── Step 2/4: Git checkout target ─────────────────────────────────
    echo "Step 2/4: Checking out ${TARGET}..."
    git checkout --quiet "${TARGET}"
    echo "  ✓ Working tree at ${TARGET}."
    echo ""

    # ── Pin VALIDIBOT_VERSION + VALIDIBOT_REVISION post-checkout ──────
    # The default stamping path reads pyproject.toml + ``git rev-parse
    # --short HEAD`` at justfile-load time and bakes them into
    # ``{{validibot_version}}`` / ``{{git_sha}}``. That works for
    # everyday "deploy whatever's checked out" flows, but the upgrade
    # recipe is a different contract: the operator named an exact
    # release tag (``v0.9.0``) and the resulting Cloud Run revision /
    # OCI image labels / support-bundle output should reflect the
    # post-checkout state, not the pre-checkout state.
    #
    # Two values must be overridden together for the labels to be
    # self-consistent:
    #
    #   - VALIDIBOT_VERSION → the operator-supplied tag (``${TARGET}``).
    #     Without this, ``{{validibot_version}}`` from parse time
    #     would still reflect the OLD pyproject version.
    #
    #   - VALIDIBOT_REVISION → ``git rev-parse --short HEAD`` at this
    #     point (now pointing at ``${TARGET}``'s commit). Without
    #     this, ``{{git_sha}}`` from parse time would still reflect
    #     the OLD HEAD, so the image would have the right release
    #     version label but the wrong commit revision label — what
    #     the second-pass review flagged.
    #
    # The build, ``_deploy-web``, and ``_deploy-worker`` recipes all
    # read ``${VALIDIBOT_VERSION:-{{validibot_version}}}`` and
    # ``${VALIDIBOT_REVISION:-{{git_sha}}}``, so exporting here
    # propagates through the entire ``deploy-all`` call chain.
    export VALIDIBOT_VERSION="${TARGET}"
    export VALIDIBOT_REVISION="$(git rev-parse --short HEAD)"

    # ── Step 3/4: deploy-all ──────────────────────────────────────────
    # Reuse the existing recipe so all the stage-specific naming,
    # scheduler setup, and MCP gating stay in one place. ``deploy-all``
    # builds + pushes the image, runs migrations as a Cloud Run Job,
    # and deploys web + worker services. Because we exported
    # VALIDIBOT_VERSION above, every layer of that chain stamps the
    # operator-supplied target rather than the pre-checkout default.
    echo "Step 3/4: Running deploy-all {{stage}} (pinned to ${VALIDIBOT_VERSION} @ ${VALIDIBOT_REVISION})..."
    just gcp deploy-all {{stage}}
    echo ""

    # ── Step 4/4: Post-flight verification ────────────────────────────
    echo "Step 4/4: Running post-upgrade doctor + smoke-test..."
    DOCTOR_PASS=1
    if ! just gcp doctor {{stage}}; then
        DOCTOR_PASS=0
        echo "  ⚠ Doctor reported issues on {{stage}}." >&2
    fi
    SMOKE_PASS=1
    if ! just gcp smoke-test {{stage}}; then
        SMOKE_PASS=0
        echo "  ⚠ Smoke-test failed on {{stage}}." >&2
    fi
    echo ""

    # ── Write upgrade report ──────────────────────────────────────────
    if ! command -v jq >/dev/null 2>&1; then
        echo "Error: 'jq' not found on host — install it (apt install jq)." >&2
        exit 1
    fi
    COMPLETED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    SKIPPED_JSON=$(if [ ${NO_BACKUP} -eq 1 ]; then echo true; else echo false; fi)
    DOCTOR_JSON=$(if [ ${DOCTOR_PASS} -eq 1 ]; then echo true; else echo false; fi)
    SMOKE_JSON=$(if [ ${SMOKE_PASS} -eq 1 ]; then echo true; else echo false; fi)
    jq -n \
        --arg stage "{{stage}}" \
        --arg from "${CURRENT_TAG}" \
        --arg to "${TARGET}" \
        --arg started "${STARTED_AT}" \
        --arg completed "${COMPLETED_AT}" \
        --arg backup "${BACKUP_REF}" \
        --argjson skipped "${SKIPPED_JSON}" \
        --argjson doctor "${DOCTOR_JSON}" \
        --argjson smoke "${SMOKE_JSON}" \
        '{
            schema_version: "validibot.upgrade.v1",
            target_substrate: "gcp",
            stage: $stage,
            from_version: $from,
            to_version: $to,
            started_at: $started,
            completed_at: $completed,
            backup_uri: $backup,
            backup_skipped: $skipped,
            doctor_pass: $doctor,
            smoke_test_pass: $smoke
        }' > "${REPORT_FILE}"

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    if [ ${DOCTOR_PASS} -eq 1 ] && [ ${SMOKE_PASS} -eq 1 ]; then
        echo "  ✓ Upgrade to ${TARGET} complete on {{stage}}"
    else
        echo "  ⚠ Upgrade to ${TARGET} landed with post-flight findings"
    fi
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Report:  ${REPORT_FILE}"
    if [ -n "${BACKUP_REF}" ]; then
        echo "  Backup:  ${BACKUP_REF}"
    fi
    if [ ${DOCTOR_PASS} -eq 0 ] || [ ${SMOKE_PASS} -eq 0 ]; then
        echo ""
        echo "  This run is RETRYABLE — re-run ``just gcp upgrade {{stage}} ${TARGET}``"
        echo "  after fixing the issues above. The completed.ok marker is written"
        echo "  only when both post-flight checks pass."
        exit 1
    fi
    # Both doctor and smoke-test passed — write the completion sentinel
    # so future invocations short-circuit cleanly.
    : > "${COMPLETED_MARKER}"
    exit 0

# Internal: build and deploy the MCP server when the deploy is MCP-aware.
#
# Delegates straight to the ``just gcp mcp`` submodule recipes so there
# is one source of truth for how MCP gets built and deployed. Those
# recipes have their own ENABLE_MCP_SERVER gate — when the flag is
# unset they print a short "skipped" message and exit 0, so calling
# them unconditionally from deploy-all is safe on community-only
# stacks.
#
# The flag comes from (in precedence order):
#   1. Inline on the command line
#   2. The user's shell environment
#   3. ``.envs/<stage>/.google-cloud/.build`` if present — the file is
#      STAGE-SPECIFIC so ``just gcp deploy-all dev`` does not read the
#      prod build file. ``set -a`` exports the values so the child
#      ``just`` processes inherit them.
[private]
_maybe-deploy-mcp stage:
    #!/usr/bin/env bash
    set -euo pipefail

    if [ "{{stage}}" = "prod" ]; then
        BUILD_FILE=".envs/.production/.google-cloud/.build"
    else
        BUILD_FILE=".envs/.{{stage}}/.google-cloud/.build"
    fi
    if [ -f "$BUILD_FILE" ]; then
        set -a
        # shellcheck disable=SC1091
        source "$BUILD_FILE"
        set +a
    fi

    just gcp mcp build-push
    just gcp mcp deploy "{{stage}}"

# Internal: conditionally run migrations before deploy.
# Skipped when GCP_SKIP_MIGRATE=1 (for hotfixes with no schema changes).
[private]
_maybe-migrate stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "${GCP_SKIP_MIGRATE:-0}" = "1" ]; then
        echo ""
        echo "Skipping migrations (GCP_SKIP_MIGRATE=1)"
        echo "Warning: Ensure the new code is compatible with the current database schema."
        echo ""
    else
        just gcp _migrate "{{stage}}"
    fi

# Internal: deploy web service (assumes image already built and pushed)
_deploy-web stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Compute environment-specific names
    # Prod keeps 1 instance warm to avoid cold starts; dev/staging scale to zero
    APP_NAME="{{app_name}}"

    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
        MIN_INSTANCES=1
        MAX_INSTANCES=4
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        MIN_INSTANCES=0
        MAX_INSTANCES=2
    fi

    # Determine ingress mode based on whether a load balancer is configured.
    #
    # If a serverless NEG exists for this stage, the load balancer is set up
    # and we restrict ingress to internal + LB traffic only. This blocks
    # direct access to the *.run.app URL, forcing all public traffic through
    # the load balancer where Cloud Armor (WAF) rules can be applied.
    #
    # If no NEG exists (e.g., dev stage without a custom domain), fall back
    # to --ingress all so the service is still reachable via its *.run.app URL.
    if [ "{{stage}}" = "prod" ]; then NEG_NAME="${APP_NAME}-neg"; else NEG_NAME="${APP_NAME}-neg-{{stage}}"; fi
    if gcloud compute network-endpoint-groups describe "$NEG_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null 2>&1; then
        INGRESS="internal-and-cloud-load-balancing"
    else
        INGRESS="all"
    fi
    if [ "${GCP_DEPLOY_MAINTENANCE:-0}" = "1" ]; then
        INGRESS="internal"
        MIN_INSTANCES=0
        echo "   Maintenance-safe deploy: forcing ingress=internal and min-instances=0"
    fi

    # Determine VPC egress mode. If the Cloud SQL instance has a private IP,
    # we enable Direct VPC egress so Cloud Run connects to the database over
    # the private network (never traversing the public internet). This uses
    # the default VPC and its default subnet — no VPC connector VMs needed.
    #
    # --vpc-egress=private-ranges-only means only traffic to private IP
    # ranges (10.x.x.x, etc.) routes through the VPC. Outbound traffic to
    # public IPs (Sentry, Stripe, PostHog) goes through the normal path.
    if [ "{{stage}}" = "prod" ]; then DB_INSTANCE="${APP_NAME}-db"; else DB_INSTANCE="${APP_NAME}-db-{{stage}}"; fi
    PRIVATE_IP=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project={{gcp_project}} \
        --format=json 2>/dev/null \
        | python3 -c "import json,sys; [print(a['ipAddress']) for a in json.load(sys.stdin).get('ipAddresses',[]) if a.get('type')=='PRIVATE']" 2>/dev/null || echo "")
    if [ -n "$PRIVATE_IP" ]; then
        VPC_FLAGS="--network=default --subnet=default --vpc-egress=private-ranges-only"
        echo "   Private IP detected ($PRIVATE_IP) — enabling Direct VPC egress"
    else
        VPC_FLAGS=""
        echo "   No private IP — skipping VPC egress (using Cloud SQL Auth Proxy)"
    fi

    # Resolve image tag from the same env-var override + parse-time
    # default pair used by ``build`` / ``push``. During an upgrade,
    # the upgrade recipe has already exported VALIDIBOT_REVISION to
    # the post-checkout SHA, so the tag picked here matches the tag
    # that ``build``/``push`` produced.
    IMAGE_TAG="${VALIDIBOT_REVISION:-{{git_sha}}}"

    # Source the stage's .build file so shared deploy-time public values are
    # authored once and stamped onto the Cloud Run services. If the file is
    # absent, inline/shell env values still work.
    if [ "{{stage}}" = "prod" ]; then
        BUILD_FILE=".envs/.production/.google-cloud/.build"
    else
        BUILD_FILE=".envs/.{{stage}}/.google-cloud/.build"
    fi
    if [ -f "$BUILD_FILE" ]; then
        set -a
        # shellcheck disable=SC1091
        source "$BUILD_FILE"
        set +a
    fi

    # Shared public values stamped onto the web revision. x402 is NOT here
    # anymore: it is cloud-only config and lives entirely in the .django secret
    # (loaded by the entrypoint's `set -a; source /secrets/.env`), so it must
    # not be stamped via --set-env-vars. VALIDIBOT_MCP_BASE_URL is stamped so
    # Django derives its OAuth audience and redirect URI from the same MCP
    # public URL the MCP service uses.
    SET_ENV_VARS="APP_ROLE=web,GCP_APP_NAME={{app_name}},VALIDIBOT_STAGE={{stage}},VALIDIBOT_VERSION=${VALIDIBOT_VERSION:-{{validibot_version}}}"
    if [ -n "${VALIDIBOT_MCP_API_BASE_URL:-}" ]; then SET_ENV_VARS="${SET_ENV_VARS},MCP_OIDC_AUDIENCE=${VALIDIBOT_MCP_API_BASE_URL}"; fi
    if [ -n "${VALIDIBOT_MCP_BASE_URL:-}" ]; then SET_ENV_VARS="${SET_ENV_VARS},VALIDIBOT_MCP_BASE_URL=${VALIDIBOT_MCP_BASE_URL}"; fi

    echo "Deploying $SERVICE to Cloud Run ({{stage}}, ingress: $INGRESS)..."
    # Scaling flags: --min/--max are Cloud Run SERVICE-level scaling limits,
    # a different property from the older REVISION-level --min-instances /
    # --max-instances — the four flags are not duplicates and gcloud accepts
    # them together. Service-level limits apply across all revisions and can
    # be changed without deploying a new revision, which is what maintenance
    # mode depends on (it flips --min=0 and back via `services update`).
    # Passing --min-instances/--max-instances 'default' REMOVES any
    # revision-level pin left by pre-2026-07 deploys; a leftover revision
    # min would keep instances running during maintenance, and a leftover
    # revision max would fight the service-level cap. Do not "simplify"
    # this back to revision-level flags.
    gcloud run deploy "$SERVICE" \
        --image {{gcp_image}}:${IMAGE_TAG} \
        --region {{gcp_region}} \
        --port 8000 \
        --service-account "$SA" \
        --add-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --set-env-vars "$SET_ENV_VARS" \
        --min "$MIN_INSTANCES" \
        --max "$MAX_INSTANCES" \
        --min-instances default \
        --max-instances default \
        --memory 1Gi \
        --timeout {{gcp_cloud_run_request_timeout}} \
        --execution-environment gen2 \
        --allow-unauthenticated \
        --ingress "$INGRESS" \
        --project {{gcp_project}} \
        $VPC_FLAGS

    echo ""
    echo "Web service deployed to {{stage}} (service-level min=$MIN_INSTANCES max=$MAX_INSTANCES, ingress=$INGRESS)"

# Internal: deploy worker service (assumes image already built and pushed)
_deploy-worker stage:
    #!/usr/bin/env bash
    set -euo pipefail

    APP_NAME="{{app_name}}"

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-worker"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        SERVICE="${APP_NAME}-worker-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    # Same VPC egress detection as _deploy-web (see comments there).
    if [ "{{stage}}" = "prod" ]; then DB_INSTANCE="${APP_NAME}-db"; else DB_INSTANCE="${APP_NAME}-db-{{stage}}"; fi
    PRIVATE_IP=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project={{gcp_project}} \
        --format=json 2>/dev/null \
        | python3 -c "import json,sys; [print(a['ipAddress']) for a in json.load(sys.stdin).get('ipAddresses',[]) if a.get('type')=='PRIVATE']" 2>/dev/null || echo "")
    if [ -n "$PRIVATE_IP" ]; then
        VPC_FLAGS="--network=default --subnet=default --vpc-egress=private-ranges-only"
        echo "   Private IP detected ($PRIVATE_IP) — enabling Direct VPC egress"
    else
        VPC_FLAGS=""
    fi

    # Resolve image tag (see comment in _deploy-web for why).
    IMAGE_TAG="${VALIDIBOT_REVISION:-{{git_sha}}}"

    echo "Deploying $SERVICE to Cloud Run ({{stage}}, private)..."
    gcloud run deploy "$SERVICE" \
        --image {{gcp_image}}:${IMAGE_TAG} \
        --region {{gcp_region}} \
        --port 8000 \
        --service-account "$SA" \
        --add-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --set-env-vars APP_ROLE=worker,GCP_APP_NAME={{app_name}},VALIDIBOT_STAGE={{stage}},VALIDIBOT_VERSION="${VALIDIBOT_VERSION:-{{validibot_version}}}" \
        --no-allow-unauthenticated \
        --ingress internal \
        --min-instances 0 \
        --max-instances 2 \
        --memory 1Gi \
        --timeout {{gcp_cloud_run_request_timeout}} \
        --execution-environment gen2 \
        --project {{gcp_project}} \
        $VPC_FLAGS

    # Ensure Cloud Tasks queue is active so tasks are dispatched to the worker
    if [ "{{stage}}" = "prod" ]; then
        QUEUE_NAME="${APP_NAME}-tasks"
    else
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
    fi
    QUEUE_STATE=$(gcloud tasks queues describe "$QUEUE_NAME" \
        --location={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
    if [ "$QUEUE_STATE" = "PAUSED" ] && [ "${GCP_DEPLOY_MAINTENANCE:-0}" != "1" ]; then
        echo "Resuming Cloud Tasks queue '$QUEUE_NAME'..."
        gcloud tasks queues resume "$QUEUE_NAME" \
            --location={{gcp_region}} \
            --project={{gcp_project}}
    elif [ "$QUEUE_STATE" = "PAUSED" ]; then
        echo "Leaving Cloud Tasks queue '$QUEUE_NAME' paused for maintenance-safe deploy."
    fi

    echo ""
    echo "Worker service deployed to {{stage}} (ingress: internal)"

# =============================================================================
# Secrets Management
# =============================================================================

# Umbrella secrets command — pushes every env file that might need
# pushing before a deploy.
#
# Delegates to two submodule recipes:
#   - ``just gcp django secrets <stage>``  — always runs
#   - ``just gcp mcp secrets <stage>``     — runs but short-circuits
#     via its ``_mcp_enabled_gate`` when ENABLE_MCP_SERVER is unset
#
# The stage-specific ``.envs/<stage>/.google-cloud/.build`` file
# provides the default for ENABLE_MCP_SERVER so operators don't have
# to prefix the flag on every invocation. See
# ``.envs.example/.production/.google-cloud/.build`` for the template.
#
# For surgical pushes (rotating only one service's secrets), use:
#   - ``just gcp django secrets <stage>``  — only .django
#   - ``just gcp mcp secrets <stage>``     — only .mcp
#
# Avoiding the umbrella when you only changed one file keeps the audit history
# meaningful. Each surgical uploader also bounds only its own version history:
# newest two non-destroyed versions plus every live Cloud Run pin.
#
# Usage:
#   just gcp secrets dev
#   just gcp secrets staging
#   just gcp secrets prod
#
# After uploading, redeploy services to pick up new secrets (Cloud Run
# mounts Secret Manager values at container start time, so a running
# revision keeps the old values until a new revision starts).
[arg('stage', pattern='dev|staging|prod')]
secrets stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail

    # Source the STAGE-SPECIFIC .build knob file so ``just gcp mcp
    # secrets`` sees the current value of ENABLE_MCP_SERVER for THIS
    # stage. ``set -a`` exports every sourced variable so the child
    # ``just`` processes inherit it.
    if [ "{{stage}}" = "prod" ]; then
        BUILD_FILE=".envs/.production/.google-cloud/.build"
    else
        BUILD_FILE=".envs/.{{stage}}/.google-cloud/.build"
    fi
    if [ -f "$BUILD_FILE" ]; then
        set -a
        # shellcheck disable=SC1091
        source "$BUILD_FILE"
        set +a
    fi

    just gcp django secrets "{{stage}}"
    just gcp mcp secrets "{{stage}}"

# =============================================================================
# Operations
# =============================================================================

# View recent Cloud Run logs (last 50 entries)
# Usage: just gcp logs dev | just gcp logs prod
[arg('stage', pattern='dev|staging|prod')]
logs stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,severity,textPayload)"

# View logs and follow (stream new logs as they arrive)
# Usage: just gcp logs-follow dev | just gcp logs-follow prod
[arg('stage', pattern='dev|staging|prod')]
logs-follow stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE" \
        --project {{gcp_project}} \
        --format="table(timestamp,severity,textPayload)"

# Pause the service (block public access, but keep it deployed)
# Useful for maintenance or cost control without full teardown.
# Usage: just gcp pause dev | just gcp pause prod
[arg('stage', pattern='dev|staging|prod')]
pause stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    gcloud run services update "$SERVICE" \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}}
    echo "$SERVICE paused. Public access blocked."

# Resume the service (restore public access)
# Uses internal-and-cloud-load-balancing if a load balancer is configured,
# otherwise falls back to --ingress all.
# Usage: just gcp resume dev | just gcp resume prod
[arg('stage', pattern='dev|staging|prod')]
resume stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi

    # Check if a load balancer NEG exists for this stage (indicates LB is set up)
    if [ "{{stage}}" = "prod" ]; then NEG_NAME="${APP_NAME}-neg"; else NEG_NAME="${APP_NAME}-neg-{{stage}}"; fi
    if gcloud compute network-endpoint-groups describe "$NEG_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null 2>&1; then
        INGRESS="internal-and-cloud-load-balancing"
    else
        INGRESS="all"
    fi

    gcloud run services update "$SERVICE" \
        --region {{gcp_region}} \
        --ingress "$INGRESS" \
        --project {{gcp_project}}
    echo "$SERVICE resumed (ingress: $INGRESS)"

# Show current service status and URL
# Usage: just gcp status dev | just gcp status prod
[arg('stage', pattern='dev|staging|prod')]
status stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
    fi

    echo "Web service:    $WEB_SERVICE"
    gcloud run services describe "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="table(status.url,spec.template.spec.timeoutSeconds,spec.template.spec.containerConcurrency)" 2>/dev/null || echo "  (not deployed)"
    echo ""
    echo "Worker service: $WORKER_SERVICE"
    gcloud run services describe "$WORKER_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="table(status.url,spec.template.spec.timeoutSeconds,spec.template.spec.containerConcurrency)" 2>/dev/null || echo "  (not deployed)"

    echo ""
    echo "Tip:"
    echo "  - SITE_URL should be your public base URL (prod: custom domain; dev/staging: web *.run.app URL is fine)."
    echo "  - WORKER_URL should be the worker *.run.app URL (used for callbacks and scheduled tasks)."

# Show status of all stages
status-all:
    @echo "=== DEV ===" && just gcp status dev 2>/dev/null || echo "(not deployed)"
    @echo ""
    @echo "=== STAGING ===" && just gcp status staging 2>/dev/null || echo "(not deployed)"
    @echo ""
    @echo "=== PROD ===" && just gcp status prod 2>/dev/null || echo "(not deployed)"

# Open the web service URL in browser
# Usage: just gcp open dev | just gcp open prod
[arg('stage', pattern='dev|staging|prod')]
open stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    URL=$(gcloud run services describe "$SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(status.url)" 2>/dev/null)
    INGRESS=$(gcloud run services describe "$SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(metadata.annotations.'run.googleapis.com/ingress')" 2>/dev/null)
    if [ "$INGRESS" = "internal-and-cloud-load-balancing" ]; then
        if [ "{{stage}}" = "prod" ]; then
            DJANGO_ENV_FILE=".envs/.production/.google-cloud/.django"
        else
            DJANGO_ENV_FILE=".envs/.{{stage}}/.google-cloud/.django"
        fi
        PUBLIC_URL=$(sed -n 's/^SITE_URL=//p' "$DJANGO_ENV_FILE" | head -n 1 | tr -d "\"'")
        if [ -z "$PUBLIC_URL" ]; then
            echo "Error: SITE_URL is missing from $DJANGO_ENV_FILE." >&2
            exit 1
        fi
        URL="${PUBLIC_URL%/}"
    fi
    if [ -z "$URL" ]; then
        echo "Error: Could not get URL for $SERVICE"
        exit 1
    fi
    echo "Opening $URL"
    open "$URL"

# Quick health check - just test if service is responding
# Usage: just gcp health-check dev | just gcp health-check prod
[arg('stage', pattern='dev|staging|prod')]
health-check stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    URL=$(gcloud run services describe "$SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(status.url)")
    INGRESS=$(gcloud run services describe "$SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(metadata.annotations.'run.googleapis.com/ingress')")
    if [ "$INGRESS" = "internal-and-cloud-load-balancing" ]; then
        if [ "{{stage}}" = "prod" ]; then
            DJANGO_ENV_FILE=".envs/.production/.google-cloud/.django"
        else
            DJANGO_ENV_FILE=".envs/.{{stage}}/.google-cloud/.django"
        fi
        PUBLIC_URL=$(sed -n 's/^SITE_URL=//p' "$DJANGO_ENV_FILE" | head -n 1 | tr -d "\"'")
        if [ -z "$PUBLIC_URL" ]; then
            echo "Error: SITE_URL is missing from $DJANGO_ENV_FILE." >&2
            exit 1
        fi
        URL="${PUBLIC_URL%/}"
    fi
    HEALTH_URL="${URL%/}/health/"
    echo "Checking: $HEALTH_URL"
    curl --fail --silent --show-error --output /dev/null \
        --write-out "HTTP Status: %{http_code}\nTime: %{time_total}s\n" \
        "$HEALTH_URL"

# =============================================================================
# Management Commands
# =============================================================================

# Run database migrations for a stage.
#
# This creates (or updates) a Cloud Run Job and executes it. The job uses
# the same image, service account, Cloud SQL instance, and secrets as the
# web service — so migrations run in an identical environment.
#
# When called standalone (`just gcp migrate prod`), the recipe looks up
# the image currently deployed on the web service. When called as part of
# a deploy (`just gcp deploy prod`), the internal `_migrate` recipe passes
# the newly-built image SHA so migrations run against the NEW code before
# any instances start serving traffic.
#
# The job is NOT deleted after execution — it stays around so you can
# inspect logs via `gcloud run jobs executions list` or the GCP console.
# It's updated in-place on subsequent runs.
#
# Usage:
#   just gcp migrate dev        # Run migrations using currently-deployed image
#   just gcp migrate prod       # Run migrations using currently-deployed image
[arg('stage', pattern='dev|staging|prod')]
migrate stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Determine which image to use: the one currently deployed on the web service.
    # (When called from the deploy chain, _migrate passes the newly-built image instead.)
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
    fi

    IMAGE=$(gcloud run services describe "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(spec.template.spec.containers[0].image)" 2>/dev/null) || {
        echo "Error: Could not determine deployed image for $WEB_SERVICE."
        echo "Is the web service deployed to {{stage}}?"
        exit 1
    }

    echo "Running migrations on {{stage}} with currently-deployed image..."
    echo "Image: $IMAGE"

    just gcp _run-migrate-job "{{stage}}" "$IMAGE"

# Internal: Run migrations as part of a deploy using the NEWLY-BUILT image.
#
# This is the key difference from the public `migrate` recipe: during a
# deploy, we want to run migrations with the NEW code (which may contain
# new migration files) BEFORE the new instances start serving traffic.
# Using the old image would miss any migrations added in this release.
#
# Called by: deploy, deploy-all, deploy-worker dependency chains
[private]
_migrate stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Resolve image tag (see comment in build for the override
    # mechanism). Migrations run BEFORE the web/worker switch, so
    # they must run against the same image those services will run
    # — ``build`` and ``push`` produced ``{{gcp_image}}:${IMAGE_TAG}``,
    # so we point the migrate job at the same tag.
    IMAGE_TAG="${VALIDIBOT_REVISION:-{{git_sha}}}"

    echo "Running pre-deploy migrations on {{stage}} with new image..."
    echo "Image: {{gcp_image}}:${IMAGE_TAG}"

    just gcp _run-migrate-job "{{stage}}" "{{gcp_image}}:${IMAGE_TAG}"

# Internal: The actual Cloud Run Job creation and execution logic.
#
# Shared by both `migrate` (standalone) and `_migrate` (deploy chain).
# Separated to avoid duplicating the job orchestration code.
#
# How it works:
#   1. Compute stage-specific names (job, service account, DB, secret)
#   2. Create or update the Cloud Run Job with the given image
#   3. Execute the job and wait for it to complete
#   4. Check the exit status and fail the recipe if migrations failed
#   5. Show the execution logs for visibility
#
# The job runs: /entrypoint → gosu django → check_migration_history → migrate.
# The read-only history guard refuses deleted pre-reset migration tails before
# any schema write. Cache/OIDC setup and the guarded application-data
# initializer follow. A fresh database is therefore complete before any new
# service revision receives traffic.
# This means it goes through the same entrypoint as the web service
# (loading /secrets/.env, waiting for the DB, etc.) but runs migrate
# instead of gunicorn.
[private]
_run-migrate-job stage image:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # ── Stage-specific configuration ────────────────────────────────────
    # Each stage has its own:
    #   - Job name: so concurrent deploys to different stages don't collide
    #   - Service account: for security isolation between environments
    #   - Cloud SQL instance: each stage has its own database
    #   - Secret: each stage has its own env vars in Secret Manager
    # ────────────────────────────────────────────────────────────────────
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="${APP_NAME}-migrate"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        JOB_NAME="${APP_NAME}-migrate-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Database Preparation — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Job:     $JOB_NAME"
    echo "  Image:   {{image}}"
    echo "  DB:      $DB"
    echo "  Account: $SA"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Create or update the job ────────────────────────────────────────
    # Check if the job already exists. If so, update it with the new image.
    # If not, create it fresh. We use `update` rather than delete+create
    # so that previous execution history is preserved for debugging.
    # ────────────────────────────────────────────────────────────────────
    if gcloud run jobs describe "$JOB_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        echo "Updating Cloud Run Job '$JOB_NAME'..."
        gcloud run jobs update "$JOB_NAME" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --set-secrets=/secrets/.env="$SECRET":latest \
            --memory 1Gi \
            --task-timeout {{gcp_migrate_task_timeout}} \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "-c,set -a && source /secrets/.env && set +a && python manage.py check_migration_history && python manage.py migrate --noinput && python manage.py createcachetable && python manage.py ensure_oidc_clients && python manage.py initialize_validibot --if-needed && echo Migrations and application initialization complete" \
            --project {{gcp_project}} \
            --quiet
    else
        echo "Creating Cloud Run Job '$JOB_NAME'..."
        gcloud run jobs create "$JOB_NAME" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "$SA" \
            --set-cloudsql-instances "$DB" \
            --set-secrets=/secrets/.env="$SECRET":latest \
            --memory 1Gi \
            --task-timeout {{gcp_migrate_task_timeout}} \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "-c,set -a && source /secrets/.env && set +a && python manage.py check_migration_history && python manage.py migrate --noinput && python manage.py createcachetable && python manage.py ensure_oidc_clients && python manage.py initialize_validibot --if-needed && echo Migrations and application initialization complete" \
            --project {{gcp_project}} \
            --quiet
    fi

    # ── Execute and wait ────────────────────────────────────────────────
    # --wait blocks until the job finishes (success or failure).
    # We capture the execution name for log retrieval.
    # ────────────────────────────────────────────────────────────────────
    echo "Executing migrations and guarded initialization (waiting for completion)..."
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --wait \
        --format="value(metadata.name)" 2>&1) || {
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  ✗ DATABASE PREPARATION FAILED on {{stage}}"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "  The deploy has been ABORTED. No new instances will be started."
        echo "  The currently-deployed version continues serving traffic."
        echo ""
        echo "  To investigate:"
        echo "    gcloud run jobs executions list --job=$JOB_NAME --region={{gcp_region}} --project={{gcp_project}}"
        echo "    gcloud beta run jobs executions logs read <execution-name> --region={{gcp_region}} --project={{gcp_project}}"
        echo ""
        echo "  Common causes:"
        echo "    - Migration file conflicts (two developers created migrations on the same app)"
        echo "    - Database schema already modified manually"
        echo "    - Missing data migration dependency"
        echo "    - Missing required runtime configuration or initialization resource"
        echo ""
        exit 1
    }

    # ── Show execution logs ─────────────────────────────────────────────
    # Non-fatal: the gcloud beta logs command can occasionally crash
    # even when the job succeeded. Don't fail the deploy for a log
    # retrieval issue.
    # ────────────────────────────────────────────────────────────────────
    echo ""
    echo "Execution: $EXECUTION"
    gcloud beta run jobs executions logs read "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} 2>/dev/null || echo "(Could not retrieve execution logs — check GCP console)"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✓ Migrations and application initialization complete on {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─────────────────────────────────────────────────────────────────────────
# _resolve-deployed-image: query the live image of the web service
# ─────────────────────────────────────────────────────────────────────────
#
# Returns (on stdout) the full image reference currently serving
# traffic for the stage's web service, e.g.
# ``us-central1-docker.pkg.dev/proj/validibot/validibot-web@sha256:...``.
#
# Used by ``doctor``, ``backup``, and ``restore`` so each runs
# against the actually-deployed image rather than the operator's
# local checkout.  An operator with unpushed local changes, a dirty
# tree, or a branch that diverges from prod must not be able to run
# diagnostics or write backup manifests using code that isn't
# serving traffic.
#
# Note this is INTENTIONALLY different from ``_run-migrate-job``,
# which takes ``{{gcp_image}}:{{git_sha}}`` because migrate runs
# inside ``deploy`` (after build+push) — at that point the local
# SHA has just become the deployed image.  Doctor / backup /
# restore all run independently of deploy and need to look up what's
# live.
[private]
[arg('stage', pattern='dev|staging|prod')]
_resolve-deployed-image stage:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
    fi

    IMAGE=$(gcloud run services describe "${SERVICE}" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format='value(spec.template.spec.containers[0].image)' 2>&1) || {
        echo "Error: could not resolve deployed image for ${SERVICE}." >&2
        echo "" >&2
        echo "  Has the web service been deployed for stage '{{stage}}'?" >&2
        echo "    just gcp deploy {{stage}}" >&2
        exit 1
    }

    if [ -z "${IMAGE}" ]; then
        echo "Error: ${SERVICE} reports an empty image (newly created service?)." >&2
        echo "  Deploy first:  just gcp deploy {{stage}}" >&2
        exit 1
    fi

    echo "${IMAGE}"

# ─────────────────────────────────────────────────────────────────────────
# _run-doctor-job: stage doctor execution as a Cloud Run Job
# ─────────────────────────────────────────────────────────────────────────
#
# Mirrors ``_run-migrate-job`` but for the doctor command.  The Job
# runs ``python manage.py check_validibot --target gcp --stage <stage>``
# in the deployed image, with the same SA / Cloud SQL / secret as the
# web service so the doctor sees what the runtime sees.
#
# Why this is a Job rather than a one-shot ``gcloud run services exec``:
#
#   - Jobs have a stable name we can describe + update across
#     invocations, so an operator running ``just gcp doctor prod`` ten
#     times in a row doesn't pay container-cold-start ten times.
#   - Job execution history is preserved in Cloud Run, which is
#     useful for "doctor was green at 09:00, when did it start
#     failing?" investigations.
#   - The migrate Job already follows this pattern, so contributors
#     who understand one understand both.
#
# Operator-supplied args (``--strict``, ``--json``, etc.) are
# forwarded to the underlying command via the ``args`` parameter.
# We always prepend ``--target gcp --stage <stage>`` so operators
# don't have to repeat them.
#
# The ``image`` parameter is the *deployed* image ref resolved by
# ``_resolve-deployed-image`` — never the operator's local SHA.
[private]
_run-doctor-job stage image args:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Stage-specific configuration — same convention as
    # ``_run-migrate-job``: prod gets the unsuffixed name; other
    # stages append ``-{stage}`` to avoid collisions.
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="${APP_NAME}-doctor"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        JOB_NAME="${APP_NAME}-doctor-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    # The container command.  ``set -a && source /secrets/.env``
    # mirrors the entrypoint pattern other Jobs use so the runtime
    # picks up the same env vars Django expects.  ``--target gcp
    # --stage`` is always set; operator-supplied args follow.
    DOCTOR_CMD="python manage.py check_validibot --target gcp --stage {{stage}} {{args}}"
    BASH_ARGS="-c,set -a && source /secrets/.env && set +a && ${DOCTOR_CMD}"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Doctor — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Job:     $JOB_NAME"
    echo "  Image:   {{image}}"
    echo "  Args:    {{args}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Create or update the Job ────────────────────────────────────────
    # Same describe-or-create pattern ``_run-migrate-job`` uses.  The
    # update path is taken on every invocation because the args may
    # have changed; the create path runs on the first ever doctor
    # call against a stage.  Memory / timeout are smaller than the
    # migrate Job because the doctor is read-only and fast.
    # ────────────────────────────────────────────────────────────────────
    if gcloud run jobs describe "$JOB_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        gcloud run jobs update "$JOB_NAME" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "$SA" \
            --set-cloudsql-instances "$DB" \
            --set-secrets=/secrets/.env="$SECRET":latest \
            --memory 512Mi \
            --task-timeout 120s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "$BASH_ARGS" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    else
        gcloud run jobs create "$JOB_NAME" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "$SA" \
            --set-cloudsql-instances "$DB" \
            --set-secrets=/secrets/.env="$SECRET":latest \
            --memory 512Mi \
            --task-timeout 120s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "$BASH_ARGS" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    fi

    # ── Execute and wait ────────────────────────────────────────────────
    # ``--wait`` blocks until the Job finishes.  We DON'T fail the
    # recipe on a non-zero exit code from gcloud — ``check_validibot
    # --strict`` exits 1 when warnings are present, which is a real
    # signal we want to propagate to the caller, not swallow.  The
    # final ``exit $RC`` below preserves that.
    # ────────────────────────────────────────────────────────────────────
    set +e
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --wait \
        --format="value(metadata.name)" 2>&1)
    RC=$?
    set -e

    # Best-effort log retrieval.  Even if the doctor returned non-zero
    # we want to show its output — that's where the operator finds out
    # WHICH check failed.  ``2>/dev/null || true`` because the gcloud
    # beta logs subcommand can flake on cold project state.
    if [ -n "$EXECUTION" ]; then
        echo ""
        gcloud beta run jobs executions logs read "$EXECUTION" \
            --region {{gcp_region}} \
            --project {{gcp_project}} 2>/dev/null \
            || echo "(Could not retrieve execution logs — check GCP console)"
    fi

    echo ""
    if [ $RC -eq 0 ]; then
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  ✓ Doctor passed on {{stage}}"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    else
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  ✗ Doctor reported issues on {{stage}} (exit $RC)"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "  Re-run with --json to get machine-readable output:"
        echo "    just gcp doctor {{stage}} --json"
    fi
    exit $RC

# ─────────────────────────────────────────────────────────────────────────
# _run-backup: stage backup orchestration
# ─────────────────────────────────────────────────────────────────────────
#
# Three-step flow: Cloud SQL export → media rsync → manifest write.
# Each step is independent so a partial failure leaves a partial
# backup (with no manifest) that an operator can either delete or
# resume from; the manifest is only written when every artifact is
# in place and checksummed.
#
# The backup bucket name follows the convention
# ``<project>-validibot-backups-<stage>``.  Created on first use
# with object versioning enabled (so concurrent backups don't
# overwrite each other) and uniform bucket-level access (no
# per-object ACLs, simpler IAM).
[private]
_run-backup stage image:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # ── Stage configuration ─────────────────────────────────────────────
    if [ "{{stage}}" = "prod" ]; then
        DB_INSTANCE="${APP_NAME}-db"
        SECRET="django-env"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        MEDIA_BUCKET="${APP_NAME}-media"
    else
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        MEDIA_BUCKET="${APP_NAME}-media-{{stage}}"
    fi

    # GCS bucket names are globally unique across all GCP projects, so
    # ``validibot-backups-prod`` would collide on first use in any
    # environment that isn't ours.  Including the project ID keeps
    # the name unambiguous.  Operators who want a specific bucket
    # can override via ``GCP_BACKUP_BUCKET`` in ``.envs/...``.
    BACKUP_BUCKET="${GCP_BACKUP_BUCKET:-{{gcp_project}}-${APP_NAME}-backups-{{stage}}}"
    BACKUP_ID=$(date -u +%Y%m%dT%H%M%SZ)
    BACKUP_URI="gs://${BACKUP_BUCKET}/${BACKUP_ID}"
    DB_OBJECT="${BACKUP_URI}/db.sql.gz"
    MEDIA_PREFIX="${BACKUP_URI}/media/"
    MEDIA_INVENTORY="${BACKUP_URI}/media-inventory.jsonl"
    MANIFEST_OBJECT="${BACKUP_URI}/manifest.json"

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Backup — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Backup ID:    ${BACKUP_ID}"
    echo "  Destination:  ${BACKUP_URI}"
    echo "  DB instance:  ${DB_INSTANCE}"
    echo "  Media bucket: gs://${MEDIA_BUCKET}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Ensure the backup bucket exists ────────────────────────────────
    # First-run operators don't pre-create the bucket; we provision
    # it with versioning + uniform bucket-level access on demand.
    # ``gcloud storage buckets describe`` exits non-zero if missing,
    # so we use it as the existence check.
    if ! gcloud storage buckets describe "gs://${BACKUP_BUCKET}" \
        --project={{gcp_project}} &>/dev/null; then
        echo "Creating backup bucket gs://${BACKUP_BUCKET}..."
        gcloud storage buckets create "gs://${BACKUP_BUCKET}" \
            --location={{gcp_region}} \
            --uniform-bucket-level-access \
            --enable-versioning \
            --project={{gcp_project}}
        # Grant the Cloud SQL service agent permission to write
        # exports here.  Cloud SQL refuses the export operation
        # otherwise, with a notoriously opaque error.
        SQL_AGENT=$(gcloud sql instances describe "${DB_INSTANCE}" \
            --project={{gcp_project}} \
            --format='value(serviceAccountEmailAddress)')
        gcloud storage buckets add-iam-policy-binding "gs://${BACKUP_BUCKET}" \
            --member="serviceAccount:${SQL_AGENT}" \
            --role=roles/storage.objectAdmin \
            --project={{gcp_project}} >/dev/null
    fi

    # ── Step 1: Cloud SQL export ───────────────────────────────────────
    # ``--offload`` performs the export from a serverless export
    # operation rather than against the primary; available on all
    # Postgres instances and avoids primary load.  ``--async`` would
    # let us run multiple exports in parallel, but for a single
    # backup we want to wait inline so the manifest writer has
    # something to checksum.
    echo "Step 1/4: Exporting Cloud SQL database to ${DB_OBJECT}..."
    gcloud sql export sql "${DB_INSTANCE}" "${DB_OBJECT}" \
        --offload \
        --project={{gcp_project}}
    echo "  ✓ DB export complete."
    echo ""

    # ── Step 2: Media rsync ────────────────────────────────────────────
    # First verify the source bucket EXISTS — a missing bucket / IAM
    # denial / API blip would otherwise silently degrade to "no
    # media in this backup," which is a serious data-loss footgun.
    # Only after the bucket-exists check do we attempt to list +
    # rsync; an empty listing AT THAT POINT is genuinely "bucket is
    # empty," not "we couldn't tell."
    echo "Step 2/4: Syncing media bucket to ${MEDIA_PREFIX}..."
    if ! gcloud storage buckets describe "gs://${MEDIA_BUCKET}" \
        --project={{gcp_project}} &>/dev/null; then
        echo "Error: media bucket gs://${MEDIA_BUCKET} not found or not accessible." >&2
        echo "" >&2
        echo "  Verify the bucket exists and the running gcloud principal has" >&2
        echo "  storage.buckets.get permission on it." >&2
        exit 1
    fi
    # ``gcloud storage rsync -r`` copies the bucket contents and sets
    # the destination's metadata to match.  We DON'T use ``-d``
    # (delete extras) because the destination prefix is timestamp-
    # fresh — there are no extras to delete.  An empty source
    # bucket is fine; rsync no-ops cleanly.
    gcloud storage rsync \
        "gs://${MEDIA_BUCKET}" "${MEDIA_PREFIX}" \
        --recursive \
        --project={{gcp_project}}
    echo "  ✓ Media sync complete."
    echo ""

    # ── Step 3: Inventory + checksums ──────────────────────────────────
    # We need three things for the manifest writer:
    #   - DB dump size + sha256
    #   - List of media files with GCS-native metadata
    #
    # The DB dump checksum is computed by streaming the GCS object
    # through sha256sum — we never download it to local disk, just
    # pipe it through.  Cloud Run Job environments don't have local
    # disk space for multi-GB dumps, but ``gsutil cat`` is streaming
    # so memory usage stays bounded.
    echo "Step 3/4: Computing checksums + inventory..."
    DB_SIZE=$(gcloud storage objects describe "${DB_OBJECT}" \
        --project={{gcp_project}} \
        --format='value(size)')
    DB_SHA256=$(gcloud storage cat "${DB_OBJECT}" \
        --project={{gcp_project}} | sha256sum | awk '{print $1}')
    echo "  DB size: ${DB_SIZE} bytes"
    echo "  DB sha256: ${DB_SHA256}"

    # Build the media inventory as JSONL.  Each line is a complete
    # ``BackupFileEntry`` JSON object that the manifest writer
    # parses.  We stream this directly to GCS rather than buffering
    # it locally because it can be large for media-heavy
    # deployments.
    INVENTORY_FILE=$(mktemp)
    trap 'rm -f "$INVENTORY_FILE"' EXIT

    # ``gcloud storage objects list`` with ``--format=json`` gives us
    # the raw object metadata; jq reshapes it into BackupFileEntry
    # shape.  ``content_type``, ``md5_hash`` (base64 → hex), and
    # ``crc32c`` are all on the response.
    #
    # ``--format=json`` always emits valid JSON (an empty array
    # ``[]`` when no objects match), so we can run jq
    # unconditionally and let it produce zero lines for an empty
    # bucket.  This avoids the "ls failed → assume empty" footgun
    # that masked errors in the previous version.
    gcloud storage objects list "${MEDIA_PREFIX}**" \
        --project={{gcp_project}} \
        --format=json | \
        jq -r --arg prefix "${MEDIA_PREFIX}" '
            .[] | {
                path: (.name | sub($prefix; "media/")),
                size_bytes: (.size | tonumber),
                content_type: .contentType,
                checksum_sha256: null,
                checksum_md5: .md5Hash,
                checksum_crc32c: .crc32c
            } | tostring
        ' > "${INVENTORY_FILE}"
    gcloud storage cp "${INVENTORY_FILE}" "${MEDIA_INVENTORY}" \
        --project={{gcp_project}} >/dev/null
    INVENTORY_COUNT=$(wc -l < "${INVENTORY_FILE}" | tr -d ' ')
    echo "  Inventory: ${INVENTORY_COUNT} media files"
    echo ""

    # ── Step 4: Write the manifest via Cloud Run Job ───────────────────
    # The Job runs ``manage.py write_backup_manifest`` against the
    # deployed image, which means it sees the live migration head +
    # Postgres version.  The Job needs:
    #   - access to the deployed Cloud SQL instance (for migration state)
    #   - GCS read on the inventory file + write on the manifest
    #     object (covered by the existing service account, which
    #     already has these permissions for evidence storage)
    echo "Step 4/4: Writing manifest via Cloud Run Job..."

    JOB_NAME="${APP_NAME}-backup-manifest-{{stage}}"
    [ "{{stage}}" = "prod" ] && JOB_NAME="${APP_NAME}-backup-manifest"

    DB="{{gcp_project}}:{{gcp_region}}:${DB_INSTANCE}"
    RESTORE_HINT="just gcp restore {{stage}} ${BACKUP_URI}"

    # Capture currently-active Secret Manager versions for the
    # config component.  ``gcloud secrets versions list --filter=
    # state=ENABLED --limit=1`` returns the most recent enabled
    # version; we record that so a future restore knows which
    # version was active at backup time.  Missing secrets (e.g.
    # mcp-env in a deployment without MCP) are silently skipped —
    # we record what's there, not what should be there.
    SECRET_VERSION_ARGS=()
    for SECRET_NAME in "${SECRET}" "mcp-env" "mcp-env-{{stage}}"; do
        # Only the prod-named MCP secret applies to prod, etc.
        if [ "{{stage}}" = "prod" ] && [ "${SECRET_NAME}" = "mcp-env-{{stage}}" ]; then
            continue
        fi
        if [ "{{stage}}" != "prod" ] && [ "${SECRET_NAME}" = "mcp-env" ]; then
            continue
        fi
        SECRET_VERSION=$(gcloud secrets versions list "${SECRET_NAME}" \
            --filter="state=ENABLED" \
            --limit=1 \
            --format='value(name)' \
            --project={{gcp_project}} 2>/dev/null || echo "")
        if [ -n "${SECRET_VERSION}" ]; then
            SECRET_VERSION_ARGS+=(
                "--secret-manager-version"
                "${SECRET_NAME}=${SECRET_VERSION}"
            )
        fi
    done

    # Build the writer's argv.  Note ``--media-inventory`` references
    # the GCS path; the Job downloads it on entry into a temp file.
    WRITER_ARGS=(
        "--backup-id" "${BACKUP_ID}"
        "--target" "gcp"
        "--stage" "{{stage}}"
        "--backup-uri" "${BACKUP_URI}"
        "--db-file" "db.sql.gz"
        "--db-sha256" "${DB_SHA256}"
        "--db-size-bytes" "${DB_SIZE}"
        "--media-inventory" "/tmp/inventory.jsonl"
        "--restore-command-hint" "${RESTORE_HINT}"
        "--output" "${MANIFEST_OBJECT}"
        "${SECRET_VERSION_ARGS[@]}"
    )
    # Quote each arg for the bash -c command line.
    WRITER_ARGS_QUOTED=$(printf '%q ' "${WRITER_ARGS[@]}")
    BASH_ARGS="-c,set -a && source /secrets/.env && set +a && \
        gcloud storage cp '${MEDIA_INVENTORY}' /tmp/inventory.jsonl --project={{gcp_project}} && \
        python manage.py write_backup_manifest ${WRITER_ARGS_QUOTED}"

    if gcloud run jobs describe "${JOB_NAME}" \
        --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        gcloud run jobs update "${JOB_NAME}" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "${SA}" \
            --set-cloudsql-instances "${DB}" \
            --set-secrets=/secrets/.env="${SECRET}":latest \
            --memory 512Mi \
            --task-timeout 300s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "${BASH_ARGS}" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    else
        gcloud run jobs create "${JOB_NAME}" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "${SA}" \
            --set-cloudsql-instances "${DB}" \
            --set-secrets=/secrets/.env="${SECRET}":latest \
            --memory 512Mi \
            --task-timeout 300s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "${BASH_ARGS}" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    fi

    gcloud run jobs execute "${JOB_NAME}" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --wait >/dev/null
    echo "  ✓ Manifest written to ${MANIFEST_OBJECT}"
    echo ""

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✓ Backup complete on {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Backup root:  ${BACKUP_URI}"
    echo "  Manifest:     ${MANIFEST_OBJECT}"
    echo ""
    echo "  Restore later with:"
    echo "    ${RESTORE_HINT}"

# ─────────────────────────────────────────────────────────────────────────
# _run-restore: orchestrate restore from a backup URI
# ─────────────────────────────────────────────────────────────────────────
#
# Four pre-flight gates run before any destructive operation:
#
#   1. ``doctor --strict`` — refuse if the deployment is unhealthy.
#      A half-broken target makes recovery harder, not easier.
#      Boring-self-hosting AC #15.
#
#   2. ``verify_backup_compatibility`` — refuse if the manifest's
#      schema version, migration head, or major Validibot version
#      is incompatible with current code.  Boring-self-hosting AC
#      #16.
#
#   3. Artifact integrity — refuse if the manifest's DB dump entry
#      does not match the GCS object that would be imported.
#
#   4. Interactive operator confirmation — restore is destructive
#      (overwrites the database, syncs media back).  An accidental
#      ``just gcp restore prod gs://...`` typed by mistake will
#      sit at the prompt rather than nuking production.
#
# After confirmation:
#   - Cloud SQL import from the manifest's DB dump object
#   - Media bucket restore via rsync from the backup's media prefix
#   - Print Secret Manager versions block from the manifest so the
#     operator can re-pin secrets if needed
#
# What this does NOT do (deferred):
#
#   - ``--components`` flag handling.  This MVP always restores
#     data + prints config; future iteration adds the AC #17 flag
#     surface (``data-only`` / ``config-only`` / ``full``).
#   - Active maintenance-mode toggling.  Cloud SQL import takes
#     the DB offline briefly; the operator can ``just gcp
#     mode-maintenance`` first if they want to be explicit.
[private]
_run-restore stage path image:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    BACKUP_URI="{{path}}"

    # Trim trailing slash so concatenations like ${BACKUP_URI}/... are clean.
    BACKUP_URI="${BACKUP_URI%/}"
    MANIFEST_URI="${BACKUP_URI}/manifest.json"

    # Stage configuration mirrors backup so DB / SA / secret names
    # are consistent.
    if [ "{{stage}}" = "prod" ]; then
        DB_INSTANCE="${APP_NAME}-db"
        SECRET="django-env"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        MEDIA_BUCKET="${APP_NAME}-media"
    else
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        MEDIA_BUCKET="${APP_NAME}-media-{{stage}}"
    fi

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Restore — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Source:       ${BACKUP_URI}"
    echo "  DB instance:  ${DB_INSTANCE}"
    echo "  Media bucket: gs://${MEDIA_BUCKET}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Pre-flight 1: manifest exists ──────────────────────────────────
    # Check before doctor / verify so a typo'd backup path fails
    # immediately with a clear error rather than after spending a
    # minute on doctor.
    if ! gcloud storage objects describe "${MANIFEST_URI}" \
        --project={{gcp_project}} &>/dev/null; then
        echo "Error: manifest.json not found at ${MANIFEST_URI}." >&2
        echo "" >&2
        echo "  Verify the backup URI is correct.  Backups are stored at" >&2
        echo "  gs://<project>-<app>-backups-<stage>/<backup-id>/." >&2
        echo "" >&2
        echo "  List recent backups for this stage:" >&2
        echo "    gcloud storage ls gs://{{gcp_project}}-${APP_NAME}-backups-{{stage}}/ \\" >&2
        echo "      --project={{gcp_project}}" >&2
        exit 1
    fi
    echo "Pre-flight 1/4: manifest located at ${MANIFEST_URI}"

    # ── Pre-flight 2: doctor --strict ──────────────────────────────────
    # AC #15: destructive recipes call doctor --strict first.
    echo "Pre-flight 2/4: running doctor --strict..."
    if ! just gcp doctor {{stage}} --strict; then
        echo "" >&2
        echo "Error: doctor --strict failed.  Refusing to restore against" >&2
        echo "an unhealthy deployment." >&2
        echo "" >&2
        echo "  Resolve the issues doctor reported, then re-run restore." >&2
        exit 1
    fi
    echo "  ✓ doctor --strict passed."
    echo ""

    # ── Pre-flight 3: verify_backup_compatibility ──────────────────────
    # AC #16: refuse cross-major-version jumps + schema-ahead backups.
    # Runs as a Cloud Run Job so it sees the LIVE migration head.
    echo "Pre-flight 3/4: verifying backup compatibility..."
    VERIFY_JOB="${APP_NAME}-verify-backup-{{stage}}"
    [ "{{stage}}" = "prod" ] && VERIFY_JOB="${APP_NAME}-verify-backup"
    DB="{{gcp_project}}:{{gcp_region}}:${DB_INSTANCE}"

    BASH_ARGS="-c,set -a && source /secrets/.env && set +a && \
        python manage.py verify_backup_compatibility --manifest '${MANIFEST_URI}'"

    if gcloud run jobs describe "${VERIFY_JOB}" \
        --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        gcloud run jobs update "${VERIFY_JOB}" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "${SA}" \
            --set-cloudsql-instances "${DB}" \
            --set-secrets=/secrets/.env="${SECRET}":latest \
            --memory 512Mi \
            --task-timeout 60s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "${BASH_ARGS}" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    else
        gcloud run jobs create "${VERIFY_JOB}" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "${SA}" \
            --set-cloudsql-instances "${DB}" \
            --set-secrets=/secrets/.env="${SECRET}":latest \
            --memory 512Mi \
            --task-timeout 60s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "${BASH_ARGS}" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    fi

    set +e
    EXECUTION=$(gcloud run jobs execute "${VERIFY_JOB}" \
        --region={{gcp_region}} --project={{gcp_project}} \
        --wait \
        --format='value(metadata.name)' 2>&1)
    VERIFY_RC=$?
    set -e

    # Always show the verifier's output (compatible OR refused) —
    # it's the operator's only window into what was checked.
    if [ -n "${EXECUTION}" ]; then
        gcloud beta run jobs executions logs read "${EXECUTION}" \
            --region={{gcp_region}} --project={{gcp_project}} 2>/dev/null \
            || echo "(Could not retrieve verifier logs — check GCP console)"
    fi

    if [ ${VERIFY_RC} -ne 0 ]; then
        echo "" >&2
        echo "Error: backup is not compatible with this deployment." >&2
        echo "  See the verifier output above for the specific reason(s)." >&2
        exit 1
    fi
    echo "  ✓ Backup is compatible with this deployment."
    echo ""

    # ── Pre-flight 4: DB artifact integrity ─────────────────────────────
    # Compatibility tells us the backup can be restored by this code.
    # Integrity tells us the object we are about to import is exactly
    # the object the manifest described.  The DB dump is the highest-
    # impact artifact, so a missing path / missing checksum / size
    # mismatch / sha mismatch is a hard stop before the destructive
    # confirmation prompt.
    echo "Pre-flight 4/4: verifying DB dump integrity..."
    MANIFEST_JSON=$(gcloud storage cat "${MANIFEST_URI}" \
        --project={{gcp_project}})
    DB_PATH=$(printf '%s' "${MANIFEST_JSON}" | jq -r '.data.db_dump.path // empty')
    DB_EXPECTED_SHA=$(printf '%s' "${MANIFEST_JSON}" | jq -r '.data.db_dump.checksum_sha256 // empty')
    DB_EXPECTED_SIZE=$(printf '%s' "${MANIFEST_JSON}" | jq -r '.data.db_dump.size_bytes // empty')

    if [ -z "${DB_PATH}" ] || [ "${DB_PATH}" = "null" ]; then
        echo "Error: manifest is missing data.db_dump.path." >&2
        exit 1
    fi
    if [ -z "${DB_EXPECTED_SHA}" ] || [ "${DB_EXPECTED_SHA}" = "null" ]; then
        echo "Error: manifest is missing data.db_dump.checksum_sha256." >&2
        exit 1
    fi
    if ! [[ "${DB_EXPECTED_SHA}" =~ ^[0-9a-fA-F]{64}$ ]]; then
        echo "Error: manifest DB sha256 is not a 64-character hex digest." >&2
        exit 1
    fi
    DB_EXPECTED_SHA=$(printf '%s' "${DB_EXPECTED_SHA}" | tr '[:upper:]' '[:lower:]')
    if ! [[ "${DB_EXPECTED_SIZE}" =~ ^[0-9]+$ ]]; then
        echo "Error: manifest DB size is missing or invalid." >&2
        exit 1
    fi

    if [[ "${DB_PATH}" == gs://* ]]; then
        DB_OBJECT="${DB_PATH}"
        if [[ "${DB_OBJECT}" != "${BACKUP_URI}/"* ]]; then
            echo "Error: manifest DB path is outside the backup root." >&2
            echo "  backup root: ${BACKUP_URI}" >&2
            echo "  db path:     ${DB_OBJECT}" >&2
            exit 1
        fi
    else
        if [[ "${DB_PATH}" = /* || "${DB_PATH}" == "../"* || "${DB_PATH}" == *"/../"* || "${DB_PATH}" == *"/.." ]]; then
            echo "Error: manifest DB path must be relative to the backup root." >&2
            echo "  db path: ${DB_PATH}" >&2
            exit 1
        fi
        DB_OBJECT="${BACKUP_URI}/${DB_PATH}"
    fi

    DB_ACTUAL_SIZE=$(gcloud storage objects describe "${DB_OBJECT}" \
        --project={{gcp_project}} \
        --format='value(size)')
    if [ "${DB_ACTUAL_SIZE}" != "${DB_EXPECTED_SIZE}" ]; then
        echo "Error: DB dump size mismatch." >&2
        echo "  object:   ${DB_OBJECT}" >&2
        echo "  expected: ${DB_EXPECTED_SIZE}" >&2
        echo "  actual:   ${DB_ACTUAL_SIZE}" >&2
        exit 1
    fi

    DB_ACTUAL_SHA=$(gcloud storage cat "${DB_OBJECT}" \
        --project={{gcp_project}} | sha256sum | awk '{print $1}')
    if [ "${DB_ACTUAL_SHA}" != "${DB_EXPECTED_SHA}" ]; then
        echo "Error: DB dump sha256 mismatch." >&2
        echo "  object:   ${DB_OBJECT}" >&2
        echo "  expected: ${DB_EXPECTED_SHA}" >&2
        echo "  actual:   ${DB_ACTUAL_SHA}" >&2
        exit 1
    fi
    echo "  ✓ DB dump matches manifest (${DB_EXPECTED_SIZE} bytes, sha256 ${DB_EXPECTED_SHA})."
    echo ""

    # ── Operator confirmation ──────────────────────────────────────────
    # Restore is destructive: the DB is overwritten, the media bucket
    # is overwritten.  Make the operator type the stage name to
    # confirm — accidental ``just gcp restore prod ...`` typed by
    # mistake stops here rather than nuking production.
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ⚠ DESTRUCTIVE OPERATION"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  About to:"
    echo "    1. Import ${DB_OBJECT} INTO Cloud SQL '${DB_INSTANCE}'"
    echo "       (this OVERWRITES the current database)"
    echo "    2. Rsync ${BACKUP_URI}/media/ INTO gs://${MEDIA_BUCKET}/"
    echo "       (this OVERWRITES current media files)"
    echo ""
    echo "  Type the stage name '{{stage}}' to confirm, or anything else to abort:"
    read -r CONFIRM
    if [ "${CONFIRM}" != "{{stage}}" ]; then
        echo "Aborted by operator (no changes made)."
        exit 1
    fi
    echo ""

    # ── Step 1: Cloud SQL import ───────────────────────────────────────
    # Cloud SQL takes the DB offline during import; running services
    # queue requests.  The operator can ``just gcp mode-maintenance``
    # first if they want a clean drain.
    echo "Step 1/3: Importing Cloud SQL database from ${DB_OBJECT}..."
    gcloud sql import sql "${DB_INSTANCE}" "${DB_OBJECT}" \
        --project={{gcp_project}} \
        --quiet
    echo "  ✓ DB import complete."
    echo ""

    # ── Step 2: Media rsync (reverse direction) ────────────────────────
    # Note ``-d`` (delete extras) IS appropriate here: a restored
    # media bucket should match the backup, not be a superset.  An
    # operator who wants additive restore can copy by hand.
    echo "Step 2/3: Restoring media bucket from ${BACKUP_URI}/media/..."
    if ! gcloud storage buckets describe "gs://${MEDIA_BUCKET}" \
        --project={{gcp_project}} &>/dev/null; then
        echo "Error: target media bucket gs://${MEDIA_BUCKET} not found." >&2
        echo "  Cannot restore media without the destination bucket." >&2
        exit 1
    fi
    gcloud storage rsync \
        "${BACKUP_URI}/media/" "gs://${MEDIA_BUCKET}" \
        --recursive \
        --delete-unmatched-destination-objects \
        --project={{gcp_project}}
    echo "  ✓ Media restore complete."
    echo ""

    # ── Step 3: Surface config component for operator action ───────────
    # We don't auto-restore config because the manifest stores
    # Secret Manager versions, not values.  Operators re-pin by
    # hand if needed.  Showing the manifest's config block lets
    # them see what's available without re-fetching the manifest.
    echo "Step 3/3: Reading config component from manifest..."
    CONFIG_JSON=$(printf '%s' "${MANIFEST_JSON}" | jq -r '.config // null')
    echo ""
    if [ "${CONFIG_JSON}" = "null" ]; then
        echo "  Manifest has no config component (data-only backup)."
    else
        echo "  Secret Manager versions active at backup time:"
        echo "${CONFIG_JSON}" | jq -r \
            '.secret_manager_versions | to_entries[] | "    \(.key): version \(.value)"'
        echo ""
        echo "  To re-pin a secret to the recorded version (if needed):"
        echo "    gcloud secrets versions disable <newer-version> \\"
        echo "      --secret=<name> --project={{gcp_project}}"
        echo "    # then redeploy so Cloud Run picks up the now-current version"
    fi
    echo ""

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✓ Restore complete on {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Recommended verification:"
    echo "    just gcp doctor {{stage}}"
    echo "    just gcp errors-since {{stage}} 5m"

# Manually refresh every application-data concern initialized after migrations.
# Normal deploys run the guarded form automatically; this recipe is for
# recovery or an explicit operator-requested refresh.
# Usage: just gcp setup-data dev | just gcp setup-data prod
[arg('stage', pattern='dev|staging|prod')]
setup-data stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="${APP_NAME}-setup"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        JOB_NAME="${APP_NAME}-setup-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    echo "Refreshing all initialized application data on {{stage}}..."

    # Use the image currently deployed on the web service (not :latest)
    if [ "{{stage}}" = "prod" ]; then WEB_SERVICE="${APP_NAME}-web"; else WEB_SERVICE="${APP_NAME}-web-{{stage}}"; fi
    DEPLOYED_IMAGE=$(gcloud run services describe "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(spec.template.spec.containers[0].image)" 2>/dev/null) || {
        echo "Error: Could not determine deployed image. Is the web service deployed?"
        echo "Falling back to :latest"
        DEPLOYED_IMAGE="{{gcp_image}}:latest"
    }
    echo "Using image: $DEPLOYED_IMAGE"

    # Delete existing job if present
    gcloud run jobs delete "$JOB_NAME" --region {{gcp_region}} --project {{gcp_project}} --quiet 2>/dev/null || true

    # Create and run job
    gcloud run jobs create "$JOB_NAME" \
        --image "$DEPLOYED_IMAGE" \
        --region {{gcp_region}} \
        --service-account "$SA" \
        --set-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --memory 1Gi \
        --execution-environment gen2 \
        --command "/bin/bash" \
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py initialize_validibot" \
        --project {{gcp_project}}

    # Execute and wait for completion
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --wait \
        --format="value(metadata.name)")

    echo "Execution: $EXECUTION"
    echo ""

    # Show logs from the completed execution (non-fatal: gcloud CLI can crash
    # on logs read even when the job succeeded - don't fail the recipe for that)
    gcloud beta run jobs executions logs read "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} || true

    echo ""
    echo "Application-data refresh completed on {{stage}}"

# Run any Django management command on a deployed environment.
#
# One stage-specific Cloud Run Job is retained at zero idle cost and reused.
# Per-execution environment overrides carry the encoded command, so routine
# validator operations do not create and delete dozens of identical Jobs.
#
# Usage: just gcp management-cmd prod "seed_plans --force --skip-stripe"
# Usage: just gcp management-cmd dev "shell"
# Usage: just gcp management-cmd prod "migrate --check"
[arg('stage', pattern='dev|staging|prod')]
management-cmd stage command: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    # Get current image from deployed service
    IMAGE=$(gcloud run services describe "$SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(spec.template.spec.containers[0].image)")

    if [ -z "$IMAGE" ]; then
        echo "Error: Could not get image from $SERVICE. Is it deployed?"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="${APP_NAME}-management"
    else
        JOB_NAME="${APP_NAME}-management-{{stage}}"
    fi
    MANAGEMENT_SPEC="2"
    COMMAND_TEXT={{quote(command)}}
    COMMAND_B64=$(printf '%s' "$COMMAND_TEXT" | base64 | tr -d '\n')
    JOB_JSON=$(mktemp)
    EXECUTION_FILE=$(mktemp)
    EXECUTION=""
    cleanup_local_files() {
        rm -f "$JOB_JSON" "$EXECUTION_FILE"
    }
    cancel_management_execution() {
        local candidate="$EXECUTION"
        trap - EXIT INT TERM
        if [ -z "$candidate" ]; then
            candidate=$(gcloud run jobs executions list \
                --job "$JOB_NAME" \
                --region {{gcp_region}} \
                --project {{gcp_project}} \
                --format=json 2>/dev/null \
                | jq -r '[.[] | select((.status.completionTime // "") == "")]
                    | sort_by(.metadata.creationTimestamp) | last
                    | .metadata.name // empty' || true)
        fi
        if [ -n "$candidate" ]; then
            echo "Cancelling interrupted management execution $candidate..." >&2
            gcloud run jobs executions cancel "$candidate" \
                --region {{gcp_region}} \
                --project {{gcp_project}} \
                --quiet >/dev/null 2>&1 || true
        fi
        cleanup_local_files
        exit 130
    }
    trap cleanup_local_files EXIT
    trap cancel_management_execution INT TERM

    echo "Running: python manage.py $COMMAND_TEXT"
    echo "Stage: {{stage}}"
    echo "Image: $IMAGE"
    echo ""

    reconcile_args=(
        --image "$IMAGE"
        --region {{gcp_region}}
        --service-account "$SA"
        --set-cloudsql-instances "$DB"
        --set-secrets=/secrets/.env="$SECRET":latest
        --memory 1Gi
        --task-timeout=600s
        --max-retries=0
        --execution-environment gen2
        --command /bin/bash
        --args "-c,set -a && source /secrets/.env && set +a && exec python manage.py run_encoded_management_command"
        --project {{gcp_project}}
    )
    RECONCILE_JOB=1
    if gcloud run jobs describe "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format=json >"$JOB_JSON" 2>/dev/null; then
        CURRENT_IMAGE=$(jq -r '[.. | objects | .image? // empty][0] // ""' "$JOB_JSON")
        CURRENT_SPEC=$(jq -r \
            '.metadata.labels["validibot-management-spec"] // ""' "$JOB_JSON")
        if [ "$CURRENT_IMAGE" = "$IMAGE" ] && [ "$CURRENT_SPEC" = "$MANAGEMENT_SPEC" ]; then
            RECONCILE_JOB=0
            echo "Reusing management Job: $JOB_NAME"
        fi
    fi
    if [ "$RECONCILE_JOB" = "1" ]; then
        if [ -s "$JOB_JSON" ]; then
            echo "Updating reusable management Job: $JOB_NAME"
            gcloud run jobs update "$JOB_NAME" "${reconcile_args[@]}" \
                --update-labels "app=${APP_NAME},role=management,stage={{stage}},validibot-management-spec=${MANAGEMENT_SPEC}"
        else
            echo "Creating reusable management Job: $JOB_NAME"
            gcloud run jobs create "$JOB_NAME" "${reconcile_args[@]}" \
                --labels "app=${APP_NAME},role=management,stage={{stage}},validibot-management-spec=${MANAGEMENT_SPEC}"
        fi
    fi

    # Execute and wait for completion. On failure, print the application log
    # so the useful Django error is not hidden behind gcloud's generic
    # non-zero exit message.
    if ! gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --update-env-vars "VALIDIBOT_MANAGEMENT_COMMAND_B64=$COMMAND_B64" \
        --task-timeout="${GCP_MANAGEMENT_COMMAND_TIMEOUT:-600s}" \
        --wait \
        --format="value(metadata.name)" >"$EXECUTION_FILE"; then
        EXECUTION=$(gcloud run jobs executions list \
            --job "$JOB_NAME" \
            --region {{gcp_region}} \
            --project {{gcp_project}} \
            --sort-by='~metadata.creationTimestamp' \
            --limit=1 \
            --format='value(metadata.name)' 2>/dev/null || true)
        if [ -n "$EXECUTION" ]; then
            echo ""
            echo "Execution: $EXECUTION"
            gcloud beta run jobs executions logs read "$EXECUTION" \
                --region {{gcp_region}} \
                --project {{gcp_project}} || true
        fi
        exit 1
    fi
    EXECUTION=$(tr -d '\r\n' <"$EXECUTION_FILE")

    echo "Execution: $EXECUTION"
    echo ""

    # Show logs from the completed execution (non-fatal: gcloud CLI can crash
    # on logs read even when the job succeeded - don't fail the recipe for that)
    gcloud beta run jobs executions logs read "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} || true

    cleanup_local_files
    trap - EXIT INT TERM

    echo "Command completed on {{stage}}"

# DESTRUCTIVE: completely reset a deployed environment.
# Deletes all validation runs, submissions, workflows, projects and validators,
# then rebuilds the validator catalogue from the current code. Users and
# organizations are preserved. Dry-run by default — pass the literal word
# `confirm` as the second argument to actually wipe data.
#
# Usage: just gcp reset-system prod            # preview only, deletes nothing
# Usage: just gcp reset-system prod confirm    # actually reset
[arg('stage', pattern='dev|staging|prod')]
reset-system stage confirm="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{confirm}}" = "confirm" ]; then
        echo "⚠️  About to RESET {{stage}}: deleting runs, submissions, workflows, projects and validators."
        # No TTY on Cloud Run Jobs, so --noinput is implied; pass it explicitly
        # to document intent. The --confirm phrase is the real gate.
        just gcp management-cmd {{stage}} "reset_system --confirm RESET-EVERYTHING --noinput"
    elif [ -n "{{confirm}}" ]; then
        echo "Unrecognised argument '{{confirm}}'. Pass 'confirm' to reset, or omit it for a dry-run preview."
        exit 1
    else
        echo "Dry-run preview (no data will be deleted). Pass 'confirm' to actually reset."
        just gcp management-cmd {{stage}} "reset_system"
    fi

# View logs from a Cloud Run job execution
# Usage: just gcp job-logs {app_name}-migrate-dev
job-logs job:
    gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name={{job}}" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,textPayload)"

# =============================================================================
# Initial Stage Setup
# =============================================================================
#
# Use these commands to set up a new environment (dev, staging, prod) from scratch.
#
# Full setup workflow:
#   1. just gcp init-stage dev          # Create infrastructure (service account,
#                                       #   Cloud SQL, GCS storage bucket, Cloud
#                                       #   Tasks queue, KMS permissions)
#   2. Create .envs/.dev/.google-cloud/.django manually (see .envs.example/).
#      Commercial add-ons may request additional env vars — their own
#      deployment guides list them.
#   3. just gcp secrets dev             # Upload secrets to Secret Manager
#   4. just gcp deploy-all dev          # Build, migrate, initialize, and deploy
#   5. just gcp validator-setup dev      # Verify, deploy, accept, and activate
#                                       #   every offered managed backend
#
# =============================================================================

# Initialize infrastructure for a new stage (creates service account, database, etc.)
# Run this ONCE when setting up a new environment. Idempotent - safe to re-run.
#
# Usage: just gcp init-stage dev
[arg('stage', pattern='dev|staging|prod')]
init-stage stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"


    echo "============================================="
    echo "Initializing GCP infrastructure for: {{stage}}"
    echo "============================================="
    echo ""

    # Prod uses names without suffix, dev/staging use stage suffix
    if [ "{{stage}}" = "prod" ]; then
        SA_NAME="${APP_NAME}-cloudrun-prod"
        DB_INSTANCE="${APP_NAME}-db"
        SECRET_NAME="django-env"
        QUEUE_NAME="${APP_NAME}-tasks"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider"
        PROVIDER_INVOKER_NAME="${APP_NAME}-val-invoker-prod"
    else
        SA_NAME="${APP_NAME}-cloudrun-{{stage}}"
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        SECRET_NAME="django-env-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider-{{stage}}"
        PROVIDER_INVOKER_NAME="${APP_NAME}-val-invoker-{{stage}}"
    fi
    SA_EMAIL="$SA_NAME@{{gcp_project}}.iam.gserviceaccount.com"
    PROVIDER_INVOKER_EMAIL="$PROVIDER_INVOKER_NAME@{{gcp_project}}.iam.gserviceaccount.com"

    # ``init-stage`` is the low-level bootstrap path. Existing infrastructure
    # may only be reconciled while the complete stage is LIVE. Database state
    # alone is not sufficient: MAINTENANCE deliberately leaves Cloud SQL
    # RUNNABLE while runtime traffic, queues, and schedulers stay isolated.
    # The maintenance wrapper opts into this path only after proving isolation
    # and arranging fail-closed cleanup.
    if gcloud sql instances describe "$DB_INSTANCE" \
        --project={{gcp_project}} &>/dev/null; then
        if [ "${GCP_INIT_STAGE_MAINTENANCE:-0}" != "1" ]; then
            CURRENT_MODE=$(just gcp _mode-current {{stage}})
            if [ "$CURRENT_MODE" != "LIVE" ]; then
                echo "Error: existing stage {{stage}} is not LIVE (current mode: $CURRENT_MODE)." >&2
                echo "Use the maintenance-safe wrapper to reconcile it without opening traffic:" >&2
                echo "  just gcp init-stage-maintenance {{stage}}" >&2
                exit 1
            fi
        fi
    fi

    # Step 0: Enable the GCP APIs every stage needs. Without these, later
    # steps fail with opaque "API [x] not enabled" errors. Idempotent —
    # enabling an already-enabled API is a no-op.
    echo "0. Enabling required APIs"
    gcloud services enable \
        run.googleapis.com \
        sqladmin.googleapis.com \
        cloudtasks.googleapis.com \
        cloudscheduler.googleapis.com \
        secretmanager.googleapis.com \
        artifactregistry.googleapis.com \
        cloudkms.googleapis.com \
        logging.googleapis.com \
        monitoring.googleapis.com \
        iam.googleapis.com \
        policytroubleshooter.googleapis.com \
        compute.googleapis.com \
        servicenetworking.googleapis.com \
        cloudresourcemanager.googleapis.com \
        --project={{gcp_project}}
    echo "   APIs enabled"
    echo ""

    # Step 0b: Create the custom validator-controller role the Cloud Run SA
    # needs to inspect validator Jobs/Services and trigger retained Jobs. The
    # historical role ID is retained for compatibility. It is GRANTED in Step
    # 2 (and on each validator job in ``validator-job-deploy``); creating it here —
    # rather than out of band — means a fresh project is never left
    # green-but-broken with a binding to a role that does not exist.
    # Create-or-reconcile, idempotent.
    JOB_RUNNER_ROLE_ID="${APP_NAME//-/_}_job_runner"
    echo "0b. Ensuring custom IAM role: $JOB_RUNNER_ROLE_ID"
    if gcloud iam roles describe "$JOB_RUNNER_ROLE_ID" --project={{gcp_project}} &>/dev/null; then
        gcloud iam roles update "$JOB_RUNNER_ROLE_ID" \
            --project={{gcp_project}} \
            --permissions="run.jobs.get,run.jobs.run,run.jobs.runWithOverrides,run.services.get,run.services.getIamPolicy" \
            --quiet >/dev/null
        echo "   Role exists (permissions reconciled)"
    else
        gcloud iam roles create "$JOB_RUNNER_ROLE_ID" \
            --project={{gcp_project}} \
            --title="Validibot Validator Controller" \
            --description="Inspect Validibot validator resources and trigger retained Jobs" \
            --permissions="run.jobs.get,run.jobs.run,run.jobs.runWithOverrides,run.services.get,run.services.getIamPolicy" \
            --stage=GA \
            --quiet >/dev/null
        echo "   Created"
    fi
    echo ""

    # Step 1: Create service account
    echo "1. Creating service account: $SA_NAME"
    if gcloud iam service-accounts describe "$SA_EMAIL" --project={{gcp_project}} &>/dev/null; then
        echo "   Service account already exists"
    else
        gcloud iam service-accounts create "$SA_NAME" \
            --display-name="Validibot {{stage}} Cloud Run" \
            --project={{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # Step 2: Grant IAM roles to service account
    echo "2. Granting IAM roles to service account"
    ROLES=(
        "roles/cloudsql.client"
        "roles/secretmanager.secretAccessor"
        "roles/run.invoker"
        "roles/cloudtasks.enqueuer"
        "roles/cloudtasks.viewer"
        "projects/{{gcp_project}}/roles/${APP_NAME//-/_}_job_runner"
    )
    for role in "${ROLES[@]}"; do
        # Check if binding already exists
        if gcloud projects get-iam-policy {{gcp_project}} \
            --flatten="bindings[].members" \
            --filter="bindings.role=$role AND bindings.members=serviceAccount:$SA_EMAIL" \
            --format="value(bindings.role)" 2>/dev/null | grep -q .; then
            echo "   $role (already bound)"
        else
            gcloud projects add-iam-policy-binding {{gcp_project}} \
                --member="serviceAccount:$SA_EMAIL" \
                --role="$role" \
                --condition=None \
                --quiet >/dev/null
            echo "   $role (added)"
        fi
    done
    echo ""

    # Step 2b: Grant SA-level permissions on itself (needed for Cloud Tasks OIDC tokens)
    echo "2b. Granting SA-level permissions (for Cloud Tasks OIDC)"
    SA_ROLES=(
        "roles/iam.serviceAccountTokenCreator"
        "roles/iam.serviceAccountUser"
    )
    for sa_role in "${SA_ROLES[@]}"; do
        role_short="${sa_role##*/}"
        if gcloud iam service-accounts get-iam-policy "$SA_EMAIL" --project={{gcp_project}} \
            --flatten="bindings[].members" \
            --filter="bindings.role=$sa_role AND bindings.members=serviceAccount:$SA_EMAIL" \
            --format="value(bindings.role)" 2>/dev/null | grep -q .; then
            echo "   $role_short (already bound)"
        else
            gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
                --member="serviceAccount:$SA_EMAIL" \
                --role="$sa_role" \
                --project={{gcp_project}} \
                --quiet >/dev/null
            echo "   $role_short (added)"
        fi
    done
    echo ""

    # Step 2c: Create the dedicated validator runtime identity. It receives no
    # ambient GCS role; per-attempt tokens provide all object authority. Its
    # only application permission is run.invoker on the worker for callbacks.
    if [ "{{stage}}" = "prod" ]; then
        VALIDATOR_SA_NAME="${APP_NAME}-validator-prod"
    else
        VALIDATOR_SA_NAME="${APP_NAME}-validator-{{stage}}"
    fi
    VALIDATOR_SA_EMAIL="$VALIDATOR_SA_NAME@{{gcp_project}}.iam.gserviceaccount.com"

    echo "2c. Creating validator service account: $VALIDATOR_SA_NAME"
    if gcloud iam service-accounts describe "$VALIDATOR_SA_EMAIL" --project={{gcp_project}} &>/dev/null; then
        echo "   Validator service account already exists"
    else
        gcloud iam service-accounts create "$VALIDATOR_SA_NAME" \
            --display-name="Validibot {{stage}} Validator Jobs" \
            --project={{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # The provider queue uses a distinct OIDC principal. It has no project
    # roles and receives run.invoker only on the managed validator Services.
    echo "2d. Creating provider-task invoker: $PROVIDER_INVOKER_NAME"
    if gcloud iam service-accounts describe "$PROVIDER_INVOKER_EMAIL" --project={{gcp_project}} &>/dev/null; then
        echo "   Provider-task invoker already exists"
    else
        gcloud iam service-accounts create "$PROVIDER_INVOKER_NAME" \
            --display-name="Validibot {{stage}} Validator Service Invoker" \
            --project={{gcp_project}}
        echo "   Created"
    fi

    # The application SA may attach this identity to provider tasks, and the
    # Google-managed Cloud Tasks agent may mint each short-lived OIDC token.
    gcloud iam service-accounts add-iam-policy-binding "$PROVIDER_INVOKER_EMAIL" \
        --member="serviceAccount:$SA_EMAIL" \
        --role="roles/iam.serviceAccountUser" \
        --project={{gcp_project}} \
        --quiet >/dev/null
    PROJECT_NUMBER=$(gcloud projects describe {{gcp_project}} --format='value(projectNumber)')
    TASKS_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
    # Enabling the API does not always materialize its managed service identity
    # immediately. Create it explicitly before granting token-minting rights.
    gcloud beta services identity create \
        --service=cloudtasks.googleapis.com \
        --project={{gcp_project}} >/dev/null
    gcloud iam service-accounts add-iam-policy-binding "$PROVIDER_INVOKER_EMAIL" \
        --member="serviceAccount:$TASKS_SERVICE_AGENT" \
        --role="roles/iam.serviceAccountTokenCreator" \
        --project={{gcp_project}} \
        --quiet >/dev/null
    echo "   OIDC delegation reconciled"
    echo ""

    # Step 3: Create Cloud SQL instance
    #
    # Tier selection:
    #   prod:    db-custom-2-8192 (2 vCPU, 8GB RAM) — dedicated cores for
    #            consistent performance under load, no noisy-neighbor effects.
    #   staging: db-g1-small (shared-core, 1.7GB RAM) — adequate for testing.
    #   dev:     db-f1-micro (shared-core, 0.6GB RAM) — minimal cost.
    #
    # Prod-only hardening:
    #   --availability-type=REGIONAL — automatic failover to a standby in
    #       another zone. Without this, a single zone outage takes the
    #       database offline.
    #   --deletion-protection — prevents accidental deletion via gcloud or
    #       the console. Must be explicitly disabled before deleting.
    #   --require-ssl — ensures all connections use TLS, even direct
    #       connections via authorized networks or gcloud sql connect.
    #       Cloud SQL Auth Proxy connections are already encrypted, but this
    #       closes the door on any unencrypted path.
    echo "3. Creating Cloud SQL instance: $DB_INSTANCE"
    if gcloud sql instances describe "$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   Database instance already exists"
    else
        if [ "{{stage}}" = "prod" ]; then
            TIER="db-custom-2-8192"
            EXTRA_FLAGS="--deletion-protection --require-ssl"
        elif [ "{{stage}}" = "staging" ]; then
            TIER="db-g1-small"
            EXTRA_FLAGS="--require-ssl"
        else
            TIER="db-f1-micro"
            EXTRA_FLAGS="--require-ssl"
        fi
        echo "   Creating $TIER instance (this may take several minutes)..."
        gcloud sql instances create "$DB_INSTANCE" \
            --database-version=POSTGRES_17 \
            --edition=ENTERPRISE \
            --tier="$TIER" \
            --region={{gcp_region}} \
            --storage-type=SSD \
            --storage-size=10GB \
            --storage-auto-increase \
            --backup \
            --project={{gcp_project}} \
            $EXTRA_FLAGS
        echo "   Created"
    fi
    echo ""

    # Step 3b: Enable private IP on Cloud SQL
    #
    # Private IP lets Cloud Run connect to the database over the VPC's
    # private network instead of the public internet. This requires:
    #   1. A private services access connection (VPC peering between your
    #      VPC and Google's services network). Created once per project.
    #   2. The Cloud SQL instance to have a private IP allocated.
    #
    # We keep the public IP as well so operators can connect via the
    # Cloud SQL Auth Proxy from any network. The public IP is protected
    # by --require-ssl and IAM authentication through the proxy.
    #
    # The deploy scripts auto-detect the private IP and enable Direct VPC
    # egress on Cloud Run when it's available.
    echo "3b. Enabling private IP on Cloud SQL"

    # Ensure the private services access connection exists (one-time per project).
    # This creates a VPC peering between the default VPC and Google's internal
    # services network, allocating a /20 IP range for private service IPs.
    PEERING_RANGE="${APP_NAME}-private-ip-range"
    if gcloud compute addresses describe "$PEERING_RANGE" --global --project={{gcp_project}} &>/dev/null 2>&1; then
        echo "   Private IP range already allocated"
    else
        echo "   Allocating private IP range for services..."
        gcloud compute addresses create "$PEERING_RANGE" \
            --global \
            --purpose=VPC_PEERING \
            --prefix-length=20 \
            --network=default \
            --project={{gcp_project}}
        echo "   Allocated"
    fi

    # Create the VPC peering connection (idempotent — errors if already exists)
    gcloud services vpc-peerings connect \
        --service=servicenetworking.googleapis.com \
        --ranges="$PEERING_RANGE" \
        --network=default \
        --project={{gcp_project}} 2>/dev/null || true

    # Enable private IP on the instance (keeps public IP too)
    CURRENT_PRIVATE=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project={{gcp_project}} \
        --format=json 2>/dev/null \
        | python3 -c "import json,sys; [print(a['ipAddress']) for a in json.load(sys.stdin).get('ipAddresses',[]) if a.get('type')=='PRIVATE']" 2>/dev/null || echo "")
    if [ -n "$CURRENT_PRIVATE" ]; then
        echo "   Private IP already enabled: $CURRENT_PRIVATE"
    else
        echo "   Enabling private IP on $DB_INSTANCE (this may take a few minutes)..."
        gcloud sql instances patch "$DB_INSTANCE" \
            --network=default \
            --project={{gcp_project}} \
            --quiet
        NEW_PRIVATE=$(gcloud sql instances describe "$DB_INSTANCE" \
            --project={{gcp_project}} \
            --format=json 2>/dev/null \
        | python3 -c "import json,sys; [print(a['ipAddress']) for a in json.load(sys.stdin).get('ipAddresses',[]) if a.get('type')=='PRIVATE']" 2>/dev/null || echo "")
        echo "   Private IP enabled: $NEW_PRIVATE"
        echo ""
        echo "   IMPORTANT: Update DATABASE_URL in your secrets to use this private IP:"
        echo "   postgres://validibot_user:PASSWORD@$NEW_PRIVATE:5432/validibot"
        echo ""
        echo "   Then run: just gcp secrets {{stage}}"
    fi
    echo ""

    # Step 4: Create database and user
    echo "4. Creating database and user"
    DB_NAME="validibot"
    DB_USER="validibot_user"

    # Check if database exists
    if gcloud sql databases describe "$DB_NAME" --instance="$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   Database '$DB_NAME' already exists"
    else
        gcloud sql databases create "$DB_NAME" \
            --instance="$DB_INSTANCE" \
            --project={{gcp_project}}
        echo "   Database created"
    fi

    # Check if user exists
    if gcloud sql users describe "$DB_USER" --instance="$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   User '$DB_USER' already exists"
    else
        # Generate random password
        DB_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
        gcloud sql users create "$DB_USER" \
            --instance="$DB_INSTANCE" \
            --password="$DB_PASSWORD" \
            --project={{gcp_project}}
        echo "   User created"
        echo ""
        echo "   SAVE THIS PASSWORD - it won't be shown again:"
        echo "   Password: $DB_PASSWORD"
        echo ""
        echo "   DATABASE_URL for .envs/.{{stage}}/.django:"
        echo "   postgres://$DB_USER:$DB_PASSWORD@//$DB_NAME?host=/cloudsql/{{gcp_project}}:{{gcp_region}}:$DB_INSTANCE"
    fi
    echo ""

    # Step 5: Create Cloud Tasks queue
    echo "5. Creating Cloud Tasks queue"
    if gcloud tasks queues describe "$QUEUE_NAME" --location={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        echo "   Queue '$QUEUE_NAME' already exists"
    else
        gcloud tasks queues create "$QUEUE_NAME" \
            --location={{gcp_region}} \
            --project={{gcp_project}}
        echo "   Queue created"
    fi
    echo ""

    # Long validator deliveries never consume the short application-worker
    # queue. Retry duration cannot outlive the 30-minute task/token contract.
    echo "5b. Reconciling validator provider queue: $PROVIDER_QUEUE_NAME"
    if gcloud tasks queues describe "$PROVIDER_QUEUE_NAME" --location={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        gcloud tasks queues update "$PROVIDER_QUEUE_NAME" \
            --location={{gcp_region}} \
            --max-dispatches-per-second=16 \
            --max-concurrent-dispatches=16 \
            --max-attempts=5 \
            --max-retry-duration=1800s \
            --min-backoff=2s \
            --max-backoff=60s \
            --max-doublings=5 \
            --project={{gcp_project}} >/dev/null
        echo "   Provider queue updated"
    else
        gcloud tasks queues create "$PROVIDER_QUEUE_NAME" \
            --location={{gcp_region}} \
            --max-dispatches-per-second=16 \
            --max-concurrent-dispatches=16 \
            --max-attempts=5 \
            --max-retry-duration=1800s \
            --min-backoff=2s \
            --max-backoff=60s \
            --max-doublings=5 \
            --project={{gcp_project}}
        echo "   Provider queue created"
    fi
    echo ""

    # Step 6: Create GCS bucket (single bucket with public/ and private/ prefixes)
    echo "6. Creating GCS bucket"
    if [ "{{stage}}" = "prod" ]; then
        STORAGE_BUCKET="${APP_NAME}-storage"
    else
        STORAGE_BUCKET="${APP_NAME}-storage-{{stage}}"
    fi

    if gcloud storage buckets describe "gs://$STORAGE_BUCKET" --project={{gcp_project}} &>/dev/null; then
        echo "   Bucket '$STORAGE_BUCKET' already exists"
    else
        gcloud storage buckets create "gs://$STORAGE_BUCKET" \
            --location={{gcp_region}} \
            --project={{gcp_project}} \
            --uniform-bucket-level-access
        echo "   Bucket '$STORAGE_BUCKET' created"
    fi

    # Grant objectAdmin role on the bucket (service account needs full access)
    gcloud storage buckets add-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="serviceAccount:$SA_EMAIL" \
        --role="roles/storage.objectAdmin" \
        --project={{gcp_project}} \
        --quiet >/dev/null
    echo "   Granted objectAdmin on $STORAGE_BUCKET"

    # Make public/ prefix publicly readable via IAM Condition
    # This allows avatars, workflow images, etc. to be served directly
    echo "   Configuring public/ prefix for public read access..."
    gcloud storage buckets add-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="allUsers" \
        --role="roles/storage.objectViewer" \
        --condition="expression=resource.name.startsWith(\"projects/_/buckets/$STORAGE_BUCKET/objects/public/\"),title=public-prefix-only" \
        --project={{gcp_project}} \
        --quiet >/dev/null 2>&1 || echo "   (public prefix IAM already configured)"
    echo "   Public prefix configured (public/ is publicly readable, private/ is protected)"

    # Validator jobs deliberately receive NO ambient GCS role. Django passes a
    # short-lived Credential Access Boundary token at execution time, limited
    # to one attempt prefix and to object read/create permissions. Remove both
    # historical bindings so compromised container code cannot bypass that
    # token by using the Cloud Run metadata identity directly.
    gcloud storage buckets remove-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="serviceAccount:$VALIDATOR_SA_EMAIL" \
        --role="roles/storage.objectAdmin" \
        --condition=None \
        --project={{gcp_project}} \
        --quiet >/dev/null 2>&1 || true
    gcloud storage buckets remove-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="serviceAccount:$VALIDATOR_SA_EMAIL" \
        --role="roles/storage.objectAdmin" \
        --condition="expression=resource.name.startsWith(\"projects/_/buckets/$STORAGE_BUCKET/objects/runs/\"),title=validator-runs-prefix-only" \
        --project={{gcp_project}} \
        --quiet >/dev/null 2>&1 || true
    echo "   Validator SA has no direct storage role (attempt tokens are issued at runtime)"
    echo ""

    # Step 7: Ensure the credential-signing key exists, then grant access.
    echo "7. Ensuring KMS signing key + permissions"
    KMS_KEYRING="${APP_NAME}-keys"
    if [ "{{stage}}" = "prod" ]; then
        KMS_KEY="credential-signing"
    else
        KMS_KEY="credential-signing-{{stage}}"
    fi
    # Ensure the keyring exists (region-scoped; creation is idempotent-guarded).
    if ! gcloud kms keyrings describe "$KMS_KEYRING" --location={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        gcloud kms keyrings create "$KMS_KEYRING" --location={{gcp_region}} --project={{gcp_project}}
        echo "   Created keyring $KMS_KEYRING"
    fi
    # Ensure the signing key exists. ES256 (ECDSA P-256 / SHA-256) is what the
    # Pro signing backend expects (it matches the local PEM key + SIGNING_ALGORITHM
    # ES256), so create it as an asymmetric-signing key with that algorithm.
    # Creating it here means GCP_KMS_SIGNING_KEY resolves to a real key after
    # init-stage instead of failing at first credential signing.
    if ! gcloud kms keys describe "$KMS_KEY" --location={{gcp_region}} --keyring="$KMS_KEYRING" --project={{gcp_project}} &>/dev/null; then
        gcloud kms keys create "$KMS_KEY" \
            --location={{gcp_region}} \
            --keyring="$KMS_KEYRING" \
            --purpose=asymmetric-signing \
            --default-algorithm=ec-sign-p256-sha256 \
            --project={{gcp_project}}
        echo "   Created signing key $KMS_KEY (ec-sign-p256-sha256 / ES256)"
    fi
    # Grant the runtime SA metadata, public-key-read, and signing access on the
    # key (fail loud). Public-key read is needed by register_signing_key; the
    # application never exports private key material from KMS.
    gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
        --location={{gcp_region}} \
        --keyring="$KMS_KEYRING" \
        --project={{gcp_project}} \
        --member="serviceAccount:$SA_EMAIL" \
        --role=roles/cloudkms.viewer \
        --quiet >/dev/null
    gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
        --location={{gcp_region}} \
        --keyring="$KMS_KEYRING" \
        --project={{gcp_project}} \
        --member="serviceAccount:$SA_EMAIL" \
        --role=roles/cloudkms.publicKeyViewer \
        --quiet >/dev/null
    gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
        --location={{gcp_region}} \
        --keyring="$KMS_KEYRING" \
        --project={{gcp_project}} \
        --member="serviceAccount:$SA_EMAIL" \
        --role=roles/cloudkms.signerVerifier \
        --quiet >/dev/null
    echo "   KMS permissions granted for $KMS_KEY"
    echo ""

    # Step 8: Create secret placeholder
    echo "8. Creating secret in Secret Manager"
    if gcloud secrets describe "$SECRET_NAME" --project={{gcp_project}} &>/dev/null; then
        echo "   Secret '$SECRET_NAME' already exists"
    else
        echo "placeholder" | gcloud secrets create "$SECRET_NAME" \
            --replication-policy="user-managed" \
            --locations="{{gcp_region}}" \
            --data-file=- \
            --project={{gcp_project}}
        echo "   Secret created (placeholder)"
    fi
    echo ""

    # Summary
    echo "============================================="
    echo "Infrastructure setup complete for {{stage}}"
    echo "============================================="
    echo ""
    echo "Resources created:"
    echo "  Service account: $SA_EMAIL"
    echo "  Custom role: $JOB_RUNNER_ROLE_ID (run.jobs.run + runWithOverrides)"
    echo "  Cloud SQL: $DB_INSTANCE (database: $DB_NAME)"
    echo "  Cloud Tasks queue: $QUEUE_NAME"
    echo "  GCS bucket: $STORAGE_BUCKET (public/ and private/ prefixes)"
    echo "  KMS signing key: $KMS_KEY (keyring $KMS_KEYRING; ES256)"
    echo "  KMS permissions: cloudkms.viewer, cloudkms.publicKeyViewer, cloudkms.signerVerifier"
    echo "  Secret: $SECRET_NAME"
    echo ""
    if [ "{{stage}}" = "prod" ]; then
        ENV_PATH=".envs/.production/.google-cloud/.django"
    else
        ENV_PATH=".envs/.{{stage}}/.google-cloud/.django"
    fi

    echo "Next steps:"
    echo "  1. Configure env file:  Edit $ENV_PATH with:"
    echo "                          - STORAGE_BUCKET=$STORAGE_BUCKET"
    echo "                          - POSTGRES_PASSWORD and DATABASE_URL (use password shown above)"
    echo "                          - DJANGO_SECRET_KEY (generate a new one)"
    echo "                          - DJANGO_ALLOWED_HOSTS (add Cloud Run URL after first deploy)"
    echo "                          - GCP_KMS_SIGNING_KEY (the full key resource above)"
    echo "                          - GCP_KMS_SIGNING_KEY_VERSION (the explicit active version)"
    echo "  2. Upload secrets:      just gcp secrets {{stage}}"
    echo "  3. Deploy services:     just gcp deploy-all {{stage}}  (migrates and initializes automatically)"
    echo "  4. Check Docker:        docker info"
    echo "  5. Inspect validators:  just gcp validator-status {{stage}}"
    echo "  6. Install validators:  just gcp validator-setup {{stage}}"
    echo ""
    echo "Commercial add-ons (optional):"
    echo "  If you've installed a commercial add-on (e.g. the GCS audit-archive"
    echo "  backend), run its own provisioning recipe now. See the add-on's"
    echo "  deployment docs for the exact command."
    echo ""

# Set the Artifact Registry image cleanup policy (or preview it).
#
# Cleanup policies are evaluated SERVER-SIDE by Artifact Registry on a recurring
# schedule (~daily) once set — you do NOT re-run this and there is no cron to
# maintain. Set it once and pruning happens automatically forever; re-run only
# to change the policy. The repo is shared across stages, so this is not
# stage-scoped.
#
# Policy (conservative and package-scoped):
#   - applies only to ``validibot-web``;
#   - keeps its 10 most-recent versions;
#   - deletes its untagged blobs after 7 days;
#   - deletes its other images after 30 days beyond the kept set.
#
# ``validibot-validator-backend-*`` can never match this policy. Backend
# digests use the explicit reference-aware cleanup command instead.
#
# Default mode is "dry-run": the policy is set but Artifact Registry only LOGS
# what it would delete (deletes nothing). Review it in Cloud Logging, then run
# with mode=apply to enable real pruning.
#
# Usage: just gcp registry-cleanup            # dry-run (safe preview)
#        just gcp registry-cleanup apply       # enable live pruning
registry-cleanup mode="dry-run": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    REPO="{{app_name}}"
    POLICY="ops/gcp/artifact-cleanup-policy.json"
    if [ ! -f "$POLICY" ]; then
        echo "Error: Artifact Registry cleanup policy not found: $POLICY" >&2
        exit 1
    fi

    if [ "{{mode}}" = "apply" ]; then
        echo "Enabling LIVE validibot-web cleanup policy on repo '$REPO'."
        echo "Validator backend packages are excluded and require explicit cleanup."
        gcloud artifacts repositories set-cleanup-policies "$REPO" \
            --location={{gcp_region}} \
            --project={{gcp_project}} \
            --policy="$POLICY" \
            --no-dry-run
        echo "✓ Live for validibot-web only: keep 10; untagged 7d; other 30d."
    else
        echo "Setting validibot-web cleanup policy in DRY-RUN on repo '$REPO'."
        echo "Validator backend packages are excluded."
        gcloud artifacts repositories set-cleanup-policies "$REPO" \
            --location={{gcp_region}} \
            --project={{gcp_project}} \
            --policy="$POLICY" \
            --dry-run
        echo "✓ Dry-run set. Review in Cloud Logging, then enable:  just gcp registry-cleanup apply"
    fi

# List all resources for a stage (useful for verification)
[arg('stage', pattern='dev|staging|prod')]
list-resources stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    echo "Resources for stage: {{stage}}"
    echo "================================"
    echo ""

    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
        WORKER="${APP_NAME}-worker"
        DB="${APP_NAME}-db"
        SECRET="django-env"
        SA="${APP_NAME}-cloudrun-prod"
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER="${APP_NAME}-worker-{{stage}}"
        DB="${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}"
    fi

    echo "Cloud Run Services:"
    gcloud run services describe "$SERVICE" --region={{gcp_region}} --project={{gcp_project}} --format="value(status.url)" 2>/dev/null && echo "  $SERVICE" || echo "  $SERVICE (not deployed)"
    gcloud run services describe "$WORKER" --region={{gcp_region}} --project={{gcp_project}} --format="value(status.url)" 2>/dev/null && echo "  $WORKER" || echo "  $WORKER (not deployed)"
    echo ""

    echo "Cloud SQL:"
    gcloud sql instances describe "$DB" --project={{gcp_project}} --format="value(state)" 2>/dev/null && echo "  $DB" || echo "  $DB (not found)"
    echo ""

    echo "Secrets:"
    gcloud secrets describe "$SECRET" --project={{gcp_project}} &>/dev/null && echo "  $SECRET" || echo "  $SECRET (not found)"
    echo ""

    echo "Service Account:"
    gcloud iam service-accounts describe "$SA@{{gcp_project}}.iam.gserviceaccount.com" --project={{gcp_project}} &>/dev/null && echo "  $SA" || echo "  $SA (not found)"

# =============================================================================
# Cloud Scheduler
# =============================================================================
#
# Cloud Scheduler replaces Celery Beat for periodic tasks. Each job calls an
# HTTP endpoint on the worker service using OIDC authentication.
#
# Prerequisites:
#   - Worker service deployed ({app_name}-worker)
#   - Cloud Scheduler API enabled: gcloud services enable cloudscheduler.googleapis.com
#   - Service account with Cloud Run Invoker role
#
# Jobs are configured in Australia/Sydney timezone by default.
#
# =============================================================================

# List all Cloud Scheduler jobs for this project
scheduler-list:
    gcloud scheduler jobs list \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Set up all scheduled jobs for a stage (dev, staging, prod)
# Creates jobs for session cleanup, idempotency key cleanup, callback receipts,
# submission purging, and stuck run cleanup.
#
# ⚠️  KEEP IN SYNC WITH THE PYTHON TASK REGISTRY
# ⚠️  validibot/core/tasks/registry.py (SCHEDULED_ADMIN_TASKS tuple +
# ⚠️  register_scheduled_admin_task() calls in downstream apps).
#
# The job list below is hardcoded because driving it from the registry
# requires booting Django under cloud settings from the host shell,
# which pulls in a cascade of env-var demands. Not worth the wiring for
# a list that changes every few months.
#
# When adding a scheduled task:
#   1. Add a ScheduledAdminTaskDefinition in validibot/core/tasks/registry.py
#      (or call register_scheduled_admin_task() from your AppConfig.ready)
#   2. Add the corresponding create_or_update_job block below
#   3. Re-run `just gcp scheduler-setup {stage}` to pick it up in GCP
#
# If a task exists in ONE place but not the other:
#   - Registry only → Docker Compose (Celery Beat) has it, GCP does NOT.
#     The task just silently never runs in prod. This is the failure
#     mode that hid enforce-audit-retention from prod until someone
#     noticed. Check `just gcp scheduler-list` against the registry
#     periodically.
#   - Justfile only → GCP Cloud Scheduler will POST to an endpoint that
#     may or may not exist. If the API endpoint isn't registered in the
#     scheduled_tasks router, you'll see 404s in worker logs.
[arg('stage', pattern='dev|staging|prod')]
scheduler-setup stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute stage-specific values
    if [ "{{stage}}" = "prod" ]; then
        WORKER_SERVICE="${APP_NAME}-worker"
        SCHEDULER_SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        JOB_SUFFIX=""
    else
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        SCHEDULER_SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Setting up Cloud Scheduler jobs for {{stage}} environment..."
    echo "Worker service: $WORKER_SERVICE"
    echo "Service account: $SCHEDULER_SA"
    echo ""

    # Get the worker service URL
    WORKER_URL=$(gcloud run services describe "$WORKER_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(status.url)" 2>/dev/null || echo "")

    if [ -z "$WORKER_URL" ]; then
        echo "ERROR: Worker service $WORKER_SERVICE not found."
        echo "Deploy the worker service first with: just gcp deploy-worker {{stage}}"
        exit 1
    fi

    echo "Worker URL: $WORKER_URL"
    echo ""

    # Helper function to create or update a scheduler job
    create_or_update_job() {
        local job_name=$1
        local schedule=$2
        local endpoint=$3
        local description=$4

        echo "Setting up: $job_name"
        echo "   Schedule: $schedule"
        echo "   Endpoint: $endpoint"

        if gcloud scheduler jobs describe "$job_name" \
            --project {{gcp_project}} \
            --location {{gcp_region}} &>/dev/null; then
            echo "   Updating existing job..."
            gcloud scheduler jobs update http "$job_name" \
                --project {{gcp_project}} \
                --location {{gcp_region}} \
                --schedule "$schedule" \
                --time-zone "{{gcp_scheduler_timezone}}" \
                --uri "${WORKER_URL}${endpoint}" \
                --http-method POST \
                --oidc-service-account-email "$SCHEDULER_SA" \
                --oidc-token-audience "$WORKER_URL" \
                --description "$description"
        else
            echo "   Creating new job..."
            gcloud scheduler jobs create http "$job_name" \
                --project {{gcp_project}} \
                --location {{gcp_region}} \
                --schedule "$schedule" \
                --time-zone "{{gcp_scheduler_timezone}}" \
                --uri "${WORKER_URL}${endpoint}" \
                --http-method POST \
                --oidc-service-account-email "$SCHEDULER_SA" \
                --oidc-token-audience "$WORKER_URL" \
                --description "$description"
        fi
        if [ "${GCP_DEPLOY_MAINTENANCE:-0}" = "1" ]; then
            gcloud scheduler jobs pause "$job_name" \
                --project {{gcp_project}} \
                --location {{gcp_region}} \
                --quiet
            echo "   Paused for maintenance-safe deploy"
        fi
        echo "   Done"
        echo ""
    }

    # Job 1: Clear expired sessions (daily at 2 AM)
    create_or_update_job \
        "${APP_NAME}-clear-sessions${JOB_SUFFIX}" \
        "0 2 * * *" \
        "/api/v1/scheduled/clear-sessions/" \
        "Clear expired Django sessions ({{stage}})"

    # Job 2: Cleanup idempotency keys (daily at 3 AM)
    create_or_update_job \
        "${APP_NAME}-cleanup-idempotency-keys${JOB_SUFFIX}" \
        "0 3 * * *" \
        "/api/v1/scheduled/cleanup-idempotency-keys/" \
        "Delete expired API idempotency keys - 24h TTL ({{stage}})"

    # Job 3: Cleanup callback receipts (weekly Sunday at 4 AM)
    create_or_update_job \
        "${APP_NAME}-cleanup-callback-receipts${JOB_SUFFIX}" \
        "0 4 * * 0" \
        "/api/v1/scheduled/cleanup-callback-receipts/" \
        "Delete old validator callback receipts - 30 day retention ({{stage}})"

    # Job 4: Purge expired submissions (hourly at :00)
    create_or_update_job \
        "${APP_NAME}-purge-expired-submissions${JOB_SUFFIX}" \
        "0 * * * *" \
        "/api/v1/scheduled/purge-expired-submissions/" \
        "Purge submission content past retention period ({{stage}})"

    # Job 5: Purge expired outputs (hourly at :00)
    create_or_update_job \
        "${APP_NAME}-purge-expired-outputs${JOB_SUFFIX}" \
        "0 * * * *" \
        "/api/v1/scheduled/purge-expired-outputs/" \
        "Purge validation outputs past retention period ({{stage}})"

    # Job 6: Process purge retries (every 5 minutes)
    create_or_update_job \
        "${APP_NAME}-process-purge-retries${JOB_SUFFIX}" \
        "*/5 * * * *" \
        "/api/v1/scheduled/process-purge-retries/" \
        "Retry failed submission purges ({{stage}})"

    # Job 7: Cleanup stuck runs (every 10 minutes)
    create_or_update_job \
        "${APP_NAME}-cleanup-stuck-runs${JOB_SUFFIX}" \
        "*/10 * * * *" \
        "/api/v1/scheduled/cleanup-stuck-runs/" \
        "Mark stuck validation runs as FAILED - 30min timeout ({{stage}})"

    # Job 8: Verify active validator Service deployments (hourly)
    create_or_update_job \
        "${APP_NAME}-verify-validator-deployments${JOB_SUFFIX}" \
        "17 * * * *" \
        "/api/v1/scheduled/verify-validator-deployments/" \
        "Detect validator Service provider drift ({{stage}})"

    # Job 9: Send periodic emails (every 6 hours)
    create_or_update_job \
        "${APP_NAME}-send-periodic-emails${JOB_SUFFIX}" \
        "0 */6 * * *" \
        "/api/v1/scheduled/send-periodic-emails/" \
        "Run periodic email handlers - trial lifecycle etc ({{stage}})"

    # Job 10: Enforce audit log retention (daily at 2:30 AM)
    # Archives AuditLogEntry rows past AUDIT_HOT_RETENTION_DAYS via
    # whatever ``AUDIT_ARCHIVE_BACKEND`` is configured (NullArchiveBackend
    # / FilesystemArchiveBackend in community, cloud add-ons can point
    # this at their own CMEK-encrypted object-store backend). The
    # retention command deletes only rows the backend confirmed. Matches
    # the cron registered at validibot/core/tasks/registry.py for the
    # "enforce-audit-retention" task.
    create_or_update_job \
        "${APP_NAME}-enforce-audit-retention${JOB_SUFFIX}" \
        "30 2 * * *" \
        "/api/v1/scheduled/enforce-audit-retention/" \
        "Archive + delete audit log rows past retention ({{stage}})"

    echo "All scheduler jobs configured for {{stage}}!"
    echo ""
    echo "View jobs: just gcp scheduler-list"
    echo "Run a job manually: just gcp scheduler-run <job-name>"

# Run a scheduler job manually (useful for testing)
scheduler-run job_name:
    gcloud scheduler jobs run {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Delete all scheduler jobs for a stage (use with caution)
[arg('stage', pattern='dev|staging|prod')]
scheduler-delete-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        JOB_SUFFIX=""
    else
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "This will delete ALL scheduler jobs for {{stage}} environment"
    read -p "Are you sure? (y/N) " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-purge-expired-outputs ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs ${APP_NAME}-verify-validator-deployments ${APP_NAME}-send-periodic-emails ${APP_NAME}-enforce-audit-retention; do
            job="${job_base}${JOB_SUFFIX}"
            echo "Deleting $job..."
            gcloud scheduler jobs delete "$job" \
                --project {{gcp_project}} \
                --location {{gcp_region}} \
                --quiet || echo "  (job not found)"
        done
        echo "Done."
    else
        echo "Cancelled."
    fi

# Pause all scheduler jobs for a stage
[arg('stage', pattern='dev|staging|prod')]
scheduler-pause-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        JOB_SUFFIX=""
    else
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Pausing all scheduler jobs for {{stage}}..."
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-purge-expired-outputs ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs ${APP_NAME}-verify-validator-deployments ${APP_NAME}-send-periodic-emails ${APP_NAME}-enforce-audit-retention; do
        job="${job_base}${JOB_SUFFIX}"
        echo "Pausing $job..."
        gcloud scheduler jobs pause "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null || echo "  (job not found or already paused)"
    done
    echo "All scheduler jobs paused for {{stage}}"

# Resume all scheduler jobs for a stage
[arg('stage', pattern='dev|staging|prod')]
scheduler-resume-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        JOB_SUFFIX=""
    else
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Resuming all scheduler jobs for {{stage}}..."
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-purge-expired-outputs ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs ${APP_NAME}-verify-validator-deployments ${APP_NAME}-send-periodic-emails ${APP_NAME}-enforce-audit-retention; do
        job="${job_base}${JOB_SUFFIX}"
        echo "Resuming $job..."
        gcloud scheduler jobs resume "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null || echo "  (job not found or already running)"
    done
    echo "All scheduler jobs resumed for {{stage}}"

# Pause a single scheduler job
scheduler-pause job_name:
    gcloud scheduler jobs pause {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Resume a paused scheduler job
scheduler-resume job_name:
    gcloud scheduler jobs resume {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# =============================================================================
# Validator Containers (Cloud Run Jobs)
# =============================================================================
#
# Validators run as separate Docker containers (Cloud Run Jobs) that are
# invoked by the worker service. Each validator type has its own image.
#
# Current validators:
#   - energyplus: EnergyPlus simulation validator
#   - fmu: Functional Mock-up Unit validator
#
# Validator backends live in the separate validibot-validator-backends repository
# at ../validibot-validator-backends. These recipes build from that sibling
# source checkout and stamp OCI labels from that repo's release tag / SHA.
#
# =============================================================================

# Build a specific validator container (energyplus, fmu, etc.)
#
# The sibling ``backends.toml`` release_version is the only version supplied
# to the Docker build. Dockerfiles intentionally have no version default.
#
# Usage: just gcp validator-build energyplus
validator-build name:
    #!/usr/bin/env bash
    set -euo pipefail
    IMAGE_SLUG="{{name}}"
    IMAGE_SLUG="${IMAGE_SLUG//_/-}"
    BACKEND_VERSION=$(python3 ops/gcp/validator_release_control.py \
        inventory --backend "{{name}}" --field release_version)
    docker build --platform linux/amd64 \
        -f ../validibot-validator-backends/validator_backends/{{name}}/Dockerfile \
        --build-arg VALIDATOR_BACKEND_VERSION="$BACKEND_VERSION" \
        --build-arg VALIDATOR_BACKEND_REVISION="{{validator_backend_git_sha}}" \
        --build-arg VALIDATOR_BACKEND_SLUG="{{name}}" \
        -t {{validator_repo}}/{{app_name}}-validator-backend-${IMAGE_SLUG}:{{validator_backend_git_sha}} \
        -t {{validator_repo}}/{{app_name}}-validator-backend-${IMAGE_SLUG}:latest \
        ../validibot-validator-backends

# Push a validator container
validator-push name:
    #!/usr/bin/env bash
    set -euo pipefail
    IMAGE_SLUG="{{name}}"
    IMAGE_SLUG="${IMAGE_SLUG//_/-}"
    docker push {{validator_repo}}/{{app_name}}-validator-backend-${IMAGE_SLUG}:{{validator_backend_git_sha}}
    docker push {{validator_repo}}/{{app_name}}-validator-backend-${IMAGE_SLUG}:latest

# Build and push in one step
validator-build-push name: (validator-build name) (validator-push name)

# Verify one signed backend release image and its byte-identical GAR mirror.
# Production deploy recipes call this before resolving the GAR tag to a digest.
[private]
_validator-release-verify-image name release_tag:
    #!/usr/bin/env bash
    set -euo pipefail
    just gcp _validator-release-require-registry-client
    IMAGE_SLUG="{{name}}"
    IMAGE_SLUG="${IMAGE_SLUG//_/-}"
    SOURCE_TAG="{{release_tag}}"
    if [[ ! "$SOURCE_TAG" =~ ^{{name}}-v([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$ ]]; then
        echo "Error: backend release tag must be {{name}}-vX.Y.Z." >&2
        exit 1
    fi
    VERSION="${BASH_REMATCH[1]}"
    IMAGE_TAG="v${VERSION}"
    INVENTORY_VERSION=$(python3 ops/gcp/validator_release_control.py \
        inventory --backend "{{name}}" --field release_version)
    if [ "$VERSION" != "$INVENTORY_VERSION" ] \
        && [ "${VALIDATOR_ALLOW_HISTORICAL:-0}" != "1" ]; then
        echo "Error: $SOURCE_TAG differs from backends.toml release_version $INVENTORY_VERSION." >&2
        exit 1
    fi
    if ! git -C ../validibot-validator-backends rev-parse \
        --verify "refs/tags/$SOURCE_TAG" >/dev/null 2>&1; then
        echo "No published signed release exists for {{name}} $VERSION." >&2
        echo "Missing: signed Git tag $SOURCE_TAG" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    if ! git -C ../validibot-validator-backends verify-tag \
        "$SOURCE_TAG" >/dev/null 2>&1; then
        echo "Invalid: signed Git tag $SOURCE_TAG failed verification." >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    GHCR_IMAGE="ghcr.io/mcquilleninteractive/validibot-validator-backend-${IMAGE_SLUG}"
    if command -v crane >/dev/null 2>&1; then
        GHCR_DIGEST=$(crane digest "${GHCR_IMAGE}:${IMAGE_TAG}" 2>/dev/null || true)
    else
        GHCR_DETAILS=$(docker buildx imagetools inspect \
            "${GHCR_IMAGE}:${IMAGE_TAG}" 2>/dev/null || true)
        GHCR_DIGEST=$(awk '/^Digest:/ {print $2; exit}' <<<"$GHCR_DETAILS")
    fi
    if [ -z "$GHCR_DIGEST" ]; then
        echo "Missing: GHCR image ${GHCR_IMAGE}:${IMAGE_TAG}" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    if [[ ! "$GHCR_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "Invalid: GHCR image ${GHCR_IMAGE}:${IMAGE_TAG} has no exact digest." >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    if ! gh attestation verify "oci://${GHCR_IMAGE}@${GHCR_DIGEST}" \
        --owner mcquilleninteractive >/dev/null 2>&1; then
        echo "Missing or invalid: image build attestation for ${GHCR_IMAGE}@${GHCR_DIGEST}" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi

    RELEASE_DIR=$(mktemp -d)
    trap 'rm -rf "$RELEASE_DIR"' EXIT
    RECORD_NAME="validibot-validator-backend-${IMAGE_SLUG}-v${VERSION}.json"
    SBOM_NAME="validibot-validator-backend-${IMAGE_SLUG}.spdx.json"
    if ! gh release view "$SOURCE_TAG" \
        --repo mcquilleninteractive/validibot-validator-backends >/dev/null 2>&1; then
        echo "Missing: GitHub Release $SOURCE_TAG" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    download_release_artifact() {
        local artifact="$1"
        local description="$2"
        if ! gh release download "$SOURCE_TAG" \
            --repo mcquilleninteractive/validibot-validator-backends \
            --dir "$RELEASE_DIR" --pattern "$artifact" >/dev/null 2>&1 \
            || [ ! -s "$RELEASE_DIR/$artifact" ]; then
            echo "Missing: $description $artifact" >&2
            echo "No GCP resources were changed." >&2
            exit 1
        fi
    }
    download_release_artifact "$RECORD_NAME" "backend release JSON"
    download_release_artifact "${RECORD_NAME}.sha256" "release JSON checksum"
    download_release_artifact "$SBOM_NAME" "SPDX SBOM"
    if ! (
        cd "$RELEASE_DIR"
        shasum -a 256 -c "${RECORD_NAME}.sha256" >/dev/null
    ); then
        echo "Invalid: release JSON checksum ${RECORD_NAME}.sha256" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    if ! gh attestation verify "$RELEASE_DIR/$RECORD_NAME" \
        --owner mcquilleninteractive >/dev/null 2>&1; then
        echo "Missing or invalid: attestation for backend release JSON $RECORD_NAME" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    if ! RECORD_SHA=$(python3 ops/gcp/validator_release_control.py \
        release-record --backend "{{name}}" --version "$VERSION" \
        --path "$RELEASE_DIR/$RECORD_NAME"); then
        echo "Invalid: backend release JSON $RECORD_NAME" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    RECORD_IMAGE_DIGEST=$(jq -r '.image_digest' "$RELEASE_DIR/$RECORD_NAME")
    if [ "$RECORD_IMAGE_DIGEST" != "$GHCR_DIGEST" ]; then
        echo "Error: release JSON digest $RECORD_IMAGE_DIGEST differs from GHCR $GHCR_DIGEST." >&2
        exit 1
    fi
    if [ "${VALIDATOR_VERIFY_GAR:-1}" = "0" ]; then
        echo "Verified public release artifacts for {{name}} $VERSION at ${GHCR_DIGEST}."
        exit 0
    fi
    GAR_IMAGE="{{validator_repo}}/{{app_name}}-validator-backend-${IMAGE_SLUG}"
    if ! GAR_DIGEST=$(gcloud artifacts docker images list "$GAR_IMAGE" \
        --include-tags \
        --filter="tags:${IMAGE_TAG}" \
        --limit=1 \
        --project={{gcp_project}} \
        --format='value(version)'); then
        echo "Missing: Artifact Registry mirror ${GAR_IMAGE}:${IMAGE_TAG}" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    if [ -z "$GAR_DIGEST" ]; then
        echo "Missing: Artifact Registry mirror ${GAR_IMAGE}:${IMAGE_TAG}" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    if [ "$GAR_DIGEST" != "$GHCR_DIGEST" ]; then
        echo "Error: GAR mirror digest $GAR_DIGEST differs from signed GHCR digest $GHCR_DIGEST." >&2
        exit 1
    fi
    echo "Verified {{name}} $VERSION at ${GHCR_DIGEST}; release JSON SHA-256 ${RECORD_SHA}."

# Verify the signed release and GAR mirror for every managed backend image.
# Usage: just gcp validator-release-verify energyplus energyplus-v0.15.1
validator-release-verify name release_tag: _require-gcp-config
    just gcp _validator-release-verify-image {{name}} {{release_tag}}

# Copy one verified GHCR release image into GAR without rebuilding it. The
# source digest is attested first; verification after the copy proves that the
# production registry contains exactly the signed release bytes.
[private]
_validator-release-require-registry-client:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v crane >/dev/null 2>&1; then
        exit 0
    fi
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        exit 0
    fi
    echo "Error: validator release mirroring requires either crane or a running Docker daemon." >&2
    echo "Install crane (preferred) or start Docker Desktop, then retry; no GCP resources were changed." >&2
    exit 1

[private]
_validator-release-mirror-image name release_tag:
    #!/usr/bin/env bash
    set -euo pipefail
    IMAGE_SLUG="{{name}}"
    IMAGE_SLUG="${IMAGE_SLUG//_/-}"
    SOURCE_TAG="{{release_tag}}"
    if [[ ! "$SOURCE_TAG" =~ ^{{name}}-v([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$ ]]; then
        echo "Error: backend release tag must be {{name}}-vX.Y.Z." >&2
        exit 1
    fi
    VERSION="${BASH_REMATCH[1]}"
    IMAGE_TAG="v${VERSION}"
    just gcp _validator-release-require-registry-client
    VALIDATOR_VERIFY_GAR=0 \
        just gcp _validator-release-verify-image "{{name}}" "$SOURCE_TAG"
    GHCR_IMAGE="ghcr.io/mcquilleninteractive/validibot-validator-backend-${IMAGE_SLUG}"
    if command -v crane >/dev/null 2>&1; then
        GHCR_DIGEST=$(crane digest "${GHCR_IMAGE}:${IMAGE_TAG}" 2>/dev/null || true)
    else
        GHCR_DETAILS=$(docker buildx imagetools inspect \
            "${GHCR_IMAGE}:${IMAGE_TAG}" 2>/dev/null || true)
        GHCR_DIGEST=$(awk '/^Digest:/ {print $2; exit}' <<<"$GHCR_DETAILS")
    fi
    if [ -z "$GHCR_DIGEST" ]; then
        echo "Missing: GHCR image ${GHCR_IMAGE}:${IMAGE_TAG}" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    if [[ ! "$GHCR_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "Invalid: GHCR image ${GHCR_IMAGE}:${IMAGE_TAG} has no exact digest." >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    GAR_IMAGE="{{validator_repo}}/{{app_name}}-validator-backend-${IMAGE_SLUG}"
    GAR_DIGEST=$(gcloud artifacts docker images list "$GAR_IMAGE" \
        --include-tags \
        --filter="tags:${IMAGE_TAG}" \
        --limit=1 \
        --project={{gcp_project}} \
        --format='value(version)' 2>/dev/null || true)
    if [ "$GAR_DIGEST" = "$GHCR_DIGEST" ]; then
        echo "GAR already contains verified {{name}} $VERSION (${GHCR_DIGEST}); skipping the copy."
        exit 0
    fi
    echo "Mirroring verified {{name}} $VERSION (${GHCR_DIGEST}) to GAR..."
    if command -v crane >/dev/null 2>&1; then
        crane copy "${GHCR_IMAGE}@${GHCR_DIGEST}" "${GAR_IMAGE}:${IMAGE_TAG}"
    else
        docker buildx imagetools create \
            --tag "${GAR_IMAGE}:${IMAGE_TAG}" \
            "${GHCR_IMAGE}@${GHCR_DIGEST}"
    fi
    just gcp _validator-release-verify-image {{name}} "$SOURCE_TAG"

# Mirror all signed release images from GHCR into the production GAR registry.
# Usage: just gcp validator-release-mirror energyplus energyplus-v0.15.1
validator-release-mirror name release_tag: _require-gcp-config
    just gcp _validator-release-mirror-image {{name}} {{release_tag}}

# Reconcile stage-wide validator identities once per operator transaction.
# Release-specific provider resources then need only their own exact invoker
# policy; they do not rewrite the same project/worker bindings five times.
[private]
_validator-runtime-iam-reconcile stage:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        MAIN_SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-prod@{{gcp_project}}.iam.gserviceaccount.com"
        WORKER_SERVICE="${APP_NAME}-worker"
    else
        MAIN_SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
    fi
    for service_account in "$MAIN_SA" "$VALIDATOR_SA"; do
        gcloud iam service-accounts describe "$service_account" \
            --project={{gcp_project}} >/dev/null
    done
    JOB_RUNNER_ROLE="projects/{{gcp_project}}/roles/${APP_NAME//-/_}_job_runner"
    PROJECT_POLICY=$(gcloud projects get-iam-policy {{gcp_project}} --format=json)
    if ! jq -e --arg role "$JOB_RUNNER_ROLE" \
        --arg member "serviceAccount:$MAIN_SA" '
            .bindings[]? | select(.role == $role) | .members[]? == $member
        ' <<<"$PROJECT_POLICY" >/dev/null; then
        echo "Granting the stage runtime permission to execute validator Jobs..."
        gcloud projects add-iam-policy-binding {{gcp_project}} \
            --member="serviceAccount:$MAIN_SA" \
            --role="$JOB_RUNNER_ROLE" \
            --quiet >/dev/null
    else
        echo "Validator Job runner IAM is already configured."
    fi
    WORKER_POLICY=$(gcloud run services get-iam-policy "$WORKER_SERVICE" \
        --region {{gcp_region}} --project {{gcp_project}} --format=json)
    if ! jq -e --arg member "serviceAccount:$VALIDATOR_SA" '
            .bindings[]? | select(.role == "roles/run.invoker")
            | .members[]? == $member
        ' <<<"$WORKER_POLICY" >/dev/null; then
        echo "Granting the validator runtime permission to call $WORKER_SERVICE..."
        gcloud run services add-iam-policy-binding "$WORKER_SERVICE" \
            --region {{gcp_region}} --project {{gcp_project}} \
            --member="serviceAccount:$VALIDATOR_SA" \
            --role="roles/run.invoker" --quiet >/dev/null
    else
        echo "Validator callback IAM is already configured."
    fi

# Create one release-specific Cloud Run Job. Existing resources are reused only
# when the later import verifies every immutable field; this recipe never
# updates a release name in place.
# Usage: just gcp validator-job-deploy energyplus prod energyplus-v0.15.1
[arg('stage', pattern='dev|staging|prod')]
validator-job-deploy name stage release_tag="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    IMAGE_SLUG="{{name}}"
    IMAGE_SLUG="${IMAGE_SLUG//_/-}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    case "{{name}}" in
        energyplus) JOB_CPU=4; JOB_MEMORY=8Gi ;;
        fmu|shacl|schematron|portfolio_manager) JOB_CPU=2; JOB_MEMORY=4Gi ;;
        *) echo "Error: unknown validator Job '{{name}}'" >&2; exit 1 ;;
    esac
    if [ "{{stage}}" = "prod" ]; then
        VALIDATOR_SA="${APP_NAME}-validator-prod@{{gcp_project}}.iam.gserviceaccount.com"
    else
        VALIDATOR_SA="${APP_NAME}-validator-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
    fi
    if [ "${VALIDATOR_RUNTIME_IAM_READY:-0}" != "1" ]; then
        just gcp _validator-runtime-iam-reconcile {{stage}}
    fi

    RELEASE_TAG="{{release_tag}}"
    if [ -z "$RELEASE_TAG" ]; then
        RELEASE_TAG="${VALIDATOR_BACKEND_RELEASE_TAG:-}"
    fi
    if [ "{{stage}}" = "prod" ]; then
        if [[ ! "$RELEASE_TAG" =~ ^{{name}}-v([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$ ]]; then
            echo "Error: production validator Job deploys require {{name}}-vX.Y.Z." >&2
            exit 1
        fi
        BACKEND_RELEASE="${BASH_REMATCH[1]}"
        IMAGE_TAG="v${BACKEND_RELEASE}"
        SOURCE_TAG="$RELEASE_TAG"
        if [ "${VALIDATOR_RELEASE_VERIFIED:-0}" != "1" ]; then
            just gcp _validator-release-verify-image {{name}} "$RELEASE_TAG"
        fi
    elif [ -n "$RELEASE_TAG" ]; then
        if [[ ! "$RELEASE_TAG" =~ ^{{name}}-v([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$ ]]; then
            echo "Error: release tag must be {{name}}-vX.Y.Z." >&2
            exit 1
        fi
        BACKEND_RELEASE="${BASH_REMATCH[1]}"
        IMAGE_TAG="v${BACKEND_RELEASE}"
        SOURCE_TAG="$RELEASE_TAG"
    else
        just gcp validator-build-push {{name}}
        IMAGE_TAG="{{validator_backend_git_sha}}"
        BACKEND_RELEASE=$(python3 ops/gcp/validator_release_control.py \
            inventory --backend "{{name}}" --field release_version)
        SOURCE_TAG="{{name}}-v${BACKEND_RELEASE}"
    fi
    JOB_NAME=$(python3 ops/gcp/validator_release_control.py \
        name --backend "{{name}}" --version "$BACKEND_RELEASE" \
        --kind job --stage "{{stage}}")

    # Resolve the pushed revision tag to its immutable GAR digest. Cloud Run
    # records the exact bytes it will execute, and the configured production
    # image policy can enforce digest pinning without trusting a mutable tag.
    # ``artifacts docker images list`` avoids requiring the optional
    # Container Analysis API used by ``images describe``.
    IMAGE_REPOSITORY="{{validator_repo}}/{{app_name}}-validator-backend-${IMAGE_SLUG}"
    IMAGE_DIGEST=$(gcloud artifacts docker images list "$IMAGE_REPOSITORY" \
        --include-tags \
        --filter="tags:${IMAGE_TAG}" \
        --limit=1 \
        --project {{gcp_project}} \
        --format='value(version)')
    if [[ ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "Error: could not resolve $IMAGE_REPOSITORY:$IMAGE_TAG to one digest." >&2
        exit 1
    fi
    IMAGE_REF="${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"

    if [ -n "$RELEASE_TAG" ]; then
        RELEASE_DIR=$(mktemp -d)
        trap 'rm -rf "$RELEASE_DIR"' EXIT
        RECORD_NAME="validibot-validator-backend-${IMAGE_SLUG}-v${BACKEND_RELEASE}.json"
        gh release download "$SOURCE_TAG" \
            --repo mcquilleninteractive/validibot-validator-backends \
            --dir "$RELEASE_DIR" \
            --pattern "$RECORD_NAME"
        RELEASE_RECORD_SHA=$(python3 ops/gcp/validator_release_control.py \
            release-record --backend "{{name}}" --version "$BACKEND_RELEASE" \
            --path "$RELEASE_DIR/$RECORD_NAME")
    else
        RELEASE_RECORD_SHA=$(printf 'development:%s:%s' "{{name}}" "{{validator_backend_git_sha}}" \
            | shasum -a 256 | awk '{print $1}')
    fi

    CREATE_JOB=1
    CURRENT_JOB_JSON=$(mktemp)
    trap 'rm -f "$CURRENT_JOB_JSON"; if [ -n "${RELEASE_DIR:-}" ]; then rm -rf "$RELEASE_DIR"; fi' EXIT
    if gcloud run jobs describe "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format=json >"$CURRENT_JOB_JSON" 2>/dev/null; then
        CURRENT_IMAGE="$(
            jq -r '
                [
                  .. | objects | .image? // empty
                ][0] // ""
            ' "$CURRENT_JOB_JSON"
        )"
        if [[ ! "$CURRENT_IMAGE" =~ @?(sha256:[0-9a-f]{64})$ ]]; then
            echo "Error: existing release Job $JOB_NAME is not digest-selected." >&2
            exit 1
        fi
        CURRENT_DIGEST="${BASH_REMATCH[1]}"
        if [ "$CURRENT_DIGEST" != "$IMAGE_DIGEST" ]; then
            echo "Error: immutable release Job $JOB_NAME already uses $CURRENT_DIGEST, not $IMAGE_DIGEST." >&2
            exit 1
        fi
        CREATE_JOB=0
        echo "Reusing existing release Job $JOB_NAME at $IMAGE_DIGEST; import will reverify its full specification."
    fi

    if [ "$CREATE_JOB" = "1" ]; then
        echo "Creating immutable release Job $JOB_NAME..."
        DEPLOYMENT_REVISION="v${BACKEND_RELEASE//./-}-$(date -u +%Y%m%d%H%M%S)"
        gcloud run jobs create "$JOB_NAME" \
            --image "$IMAGE_REF" \
            --region {{gcp_region}} \
            --service-account "$VALIDATOR_SA" \
            --cpu "$JOB_CPU" \
            --memory "$JOB_MEMORY" \
            --max-retries 0 \
            --task-timeout {{gcp_validator_task_timeout}} \
            --execution-environment gen2 \
            --network default \
            --subnet default \
            --vpc-egress all-traffic \
            --set-env-vars GCP_APP_NAME={{app_name}},VALIDIBOT_STAGE={{stage}},DEPLOYMENT_TARGET=gcp,VALIDIBOT_EXECUTION_SHAPE=job,VALIDIBOT_BACKEND_SLUG={{name}},VALIDIBOT_BACKEND_IMAGE_DIGEST="$IMAGE_DIGEST",VALIDIBOT_BACKEND_RELEASE="$BACKEND_RELEASE",VALIDIBOT_SOURCE_RELEASE_TAG="$SOURCE_TAG",VALIDIBOT_RELEASE_RECORD_SHA256="$RELEASE_RECORD_SHA" \
            --labels validator={{name}},release="${IMAGE_TAG//./-}",revision="$DEPLOYMENT_REVISION",stage={{stage}},execution-shape=job \
            --project {{gcp_project}}
    fi
    echo "$JOB_NAME deployed at $IMAGE_REF"

    echo "Stage-wide Job execution and callback IAM was reconciled before deployment."

# Deploy all managed validator Jobs without deploying Services. This is a
# diagnostic/recovery surface; routine releases use validator-deploy-all.
# Versions are read independently from sibling backends.toml.
[arg('stage', pattern='dev|staging|prod')]
validator-jobs-deploy-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    for backend in energyplus fmu shacl schematron portfolio_manager; do
        SOURCE_TAG=$(python3 ops/gcp/validator_release_control.py \
            inventory --backend "$backend" --field source_tag)
        just gcp validator-job-deploy "$backend" {{stage}} "$SOURCE_TAG"
    done

# Deploy one digest-pinned private validator Cloud Run Service. The same image
# retains its one-shot Job entrypoint; VALIDIBOT_EXECUTION_SHAPE selects the
# shared HTTP parent only for this Service revision.
# Usage: just gcp validator-service-deploy energyplus prod energyplus-v0.15.1
[arg('stage', pattern='dev|staging|prod')]
validator-service-deploy name stage release_tag="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    IMAGE_SLUG="{{name}}"
    IMAGE_SLUG="${IMAGE_SLUG//_/-}"
    if [ "{{stage}}" = "prod" ]; then
        PROVIDER_INVOKER="${APP_NAME}-val-invoker-prod@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-prod@{{gcp_project}}.iam.gserviceaccount.com"
    else
        PROVIDER_INVOKER="${APP_NAME}-val-invoker-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
    fi
    if [ "${VALIDATOR_RUNTIME_IAM_READY:-0}" != "1" ]; then
        just gcp _validator-runtime-iam-reconcile {{stage}}
    fi

    case "{{name}}" in
        energyplus) CPU=4; MEMORY=8Gi; DEFAULT_MAX_INSTANCES=4 ;;
        fmu) CPU=2; MEMORY=4Gi; DEFAULT_MAX_INSTANCES=4 ;;
        shacl) CPU=2; MEMORY=4Gi; DEFAULT_MAX_INSTANCES=8 ;;
        schematron) CPU=2; MEMORY=4Gi; DEFAULT_MAX_INSTANCES=8 ;;
        portfolio_manager) CPU=2; MEMORY=4Gi; DEFAULT_MAX_INSTANCES=8 ;;
        *) echo "Error: unknown validator Service '{{name}}'" >&2; exit 1 ;;
    esac
    # Validator Services are intentionally scale-to-zero.  Do not make a
    # release rollout depend on a hidden environment override that could turn
    # twenty otherwise idle validators into twenty warm billable instances.
    MIN_INSTANCES=0
    MAX_INSTANCES="${VALIDATOR_SERVICE_MAX_INSTANCES:-$DEFAULT_MAX_INSTANCES}"
    if ! [[ "$MIN_INSTANCES" =~ ^[0-9]+$ && "$MAX_INSTANCES" =~ ^[1-9][0-9]*$ ]] \
        || [ "$MIN_INSTANCES" -gt "$MAX_INSTANCES" ]; then
        echo "Error: invalid Service min/max instance bounds." >&2
        exit 1
    fi
    for service_account in "$PROVIDER_INVOKER" "$VALIDATOR_SA"; do
        gcloud iam service-accounts describe "$service_account" \
            --project={{gcp_project}} >/dev/null
    done

    RELEASE_TAG="{{release_tag}}"
    if [ -z "$RELEASE_TAG" ]; then
        RELEASE_TAG="${VALIDATOR_BACKEND_RELEASE_TAG:-}"
    fi
    if [ "{{stage}}" = "prod" ]; then
        if [[ ! "$RELEASE_TAG" =~ ^{{name}}-v([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$ ]]; then
            echo "Error: production validator Services require {{name}}-vX.Y.Z." >&2
            exit 1
        fi
        BACKEND_RELEASE="${BASH_REMATCH[1]}"
        IMAGE_TAG="v${BACKEND_RELEASE}"
        SOURCE_TAG="$RELEASE_TAG"
        if [ "${VALIDATOR_RELEASE_VERIFIED:-0}" != "1" ]; then
            just gcp _validator-release-verify-image {{name}} "$RELEASE_TAG"
        fi
    elif [ -n "$RELEASE_TAG" ]; then
        if [[ ! "$RELEASE_TAG" =~ ^{{name}}-v([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$ ]]; then
            echo "Error: release tag must be {{name}}-vX.Y.Z." >&2
            exit 1
        fi
        BACKEND_RELEASE="${BASH_REMATCH[1]}"
        IMAGE_TAG="v${BACKEND_RELEASE}"
        SOURCE_TAG="$RELEASE_TAG"
    else
        just gcp validator-build-push {{name}}
        IMAGE_TAG="{{validator_backend_git_sha}}"
        BACKEND_RELEASE=$(python3 ops/gcp/validator_release_control.py \
            inventory --backend "{{name}}" --field release_version)
        SOURCE_TAG="{{name}}-v${BACKEND_RELEASE}"
    fi
    SERVICE_NAME=$(python3 ops/gcp/validator_release_control.py \
        name --backend "{{name}}" --version "$BACKEND_RELEASE" \
        --kind service --stage "{{stage}}")

    IMAGE_REPOSITORY="{{validator_repo}}/{{app_name}}-validator-backend-${IMAGE_SLUG}"
    IMAGE_DIGEST=$(gcloud artifacts docker images list "$IMAGE_REPOSITORY" \
        --include-tags \
        --filter="tags:${IMAGE_TAG}" \
        --limit=1 \
        --project={{gcp_project}} \
        --format='value(version)')
    if [[ ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "Error: could not resolve $IMAGE_REPOSITORY:$IMAGE_TAG to one digest." >&2
        exit 1
    fi
    IMAGE_REF="${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"

    if [ -n "$RELEASE_TAG" ]; then
        RELEASE_DIR=$(mktemp -d)
        trap 'rm -rf "$RELEASE_DIR"' EXIT
        RECORD_NAME="validibot-validator-backend-${IMAGE_SLUG}-v${BACKEND_RELEASE}.json"
        gh release download "$SOURCE_TAG" \
            --repo mcquilleninteractive/validibot-validator-backends \
            --dir "$RELEASE_DIR" \
            --pattern "$RECORD_NAME"
        RELEASE_RECORD_SHA=$(python3 ops/gcp/validator_release_control.py \
            release-record --backend "{{name}}" --version "$BACKEND_RELEASE" \
            --path "$RELEASE_DIR/$RECORD_NAME")
    else
        RELEASE_RECORD_SHA=$(printf 'development:%s:%s' "{{name}}" "{{validator_backend_git_sha}}" \
            | shasum -a 256 | awk '{print $1}')
    fi

    CREATE_SERVICE=1
    CURRENT_SERVICE_JSON=$(mktemp)
    trap 'rm -f "$CURRENT_SERVICE_JSON"; if [ -n "${RELEASE_DIR:-}" ]; then rm -rf "$RELEASE_DIR"; fi' EXIT
    if gcloud run services describe "$SERVICE_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format=json >"$CURRENT_SERVICE_JSON" 2>/dev/null; then
        CURRENT_IMAGE=$(jq -r '.spec.template.spec.containers[0].image // .template.containers[0].image // ""' "$CURRENT_SERVICE_JSON")
        if [[ ! "$CURRENT_IMAGE" =~ @?(sha256:[0-9a-f]{64})$ ]]; then
            echo "Error: existing release Service $SERVICE_NAME is not digest-selected." >&2
            exit 1
        fi
        if [ "${BASH_REMATCH[1]}" != "$IMAGE_DIGEST" ]; then
            echo "Error: immutable release Service $SERVICE_NAME uses ${BASH_REMATCH[1]}, not $IMAGE_DIGEST." >&2
            exit 1
        fi
        CREATE_SERVICE=0
        echo "Reusing existing release Service $SERVICE_NAME at $IMAGE_DIGEST."
        echo "Leaving the Service definition unchanged; import will reverify its full specification."
    fi

    if [ "$CREATE_SERVICE" = "1" ]; then
        echo "Creating private release Service $SERVICE_NAME at $IMAGE_REF..."
        gcloud run deploy "$SERVICE_NAME" \
            --image "$IMAGE_REF" \
            --region {{gcp_region}} \
            --service-account "$VALIDATOR_SA" \
            --port 8080 \
            --ingress internal \
            --no-allow-unauthenticated \
            --concurrency 1 \
            --timeout 1649s \
            --cpu "$CPU" \
            --memory "$MEMORY" \
            --min "$MIN_INSTANCES" \
            --max "$MAX_INSTANCES" \
            --cpu-boost \
            --execution-environment gen2 \
            --network default \
            --subnet default \
            --vpc-egress all-traffic \
            --set-env-vars GCP_APP_NAME={{app_name}},VALIDIBOT_STAGE={{stage}},DEPLOYMENT_TARGET=gcp,VALIDIBOT_EXECUTION_SHAPE=service,VALIDIBOT_BACKEND_SLUG={{name}},VALIDIBOT_BACKEND_IMAGE_DIGEST="$IMAGE_DIGEST",VALIDIBOT_BACKEND_RELEASE="$BACKEND_RELEASE",VALIDIBOT_SOURCE_RELEASE_TAG="$SOURCE_TAG",VALIDIBOT_RELEASE_RECORD_SHA256="$RELEASE_RECORD_SHA" \
            --labels validator={{name}},release="${IMAGE_TAG//./-}",stage={{stage}},execution-shape=service,desired-min-instances="$MIN_INSTANCES" \
            --project {{gcp_project}}
    fi

    # Reconcile the exact invocation policy. The provider-task identity is the
    # sole caller; even another individually named principal is rejected.
    SERVICE_POLICY=$(gcloud run services get-iam-policy "$SERVICE_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format=json)
    if ! jq -e --arg member "serviceAccount:$PROVIDER_INVOKER" '
            .bindings[]? | select(.role == "roles/run.invoker")
            | .members[]? == $member
        ' <<<"$SERVICE_POLICY" >/dev/null; then
        gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
            --region {{gcp_region}} \
            --project {{gcp_project}} \
            --member="serviceAccount:$PROVIDER_INVOKER" \
            --role="roles/run.invoker" \
            --quiet >/dev/null
        SERVICE_POLICY=$(gcloud run services get-iam-policy "$SERVICE_NAME" \
            --region {{gcp_region}} --project {{gcp_project}} --format=json)
    fi
    jq -r '.bindings[]? | select(.role == "roles/run.invoker") | .members[]?' \
        <<<"$SERVICE_POLICY" \
        | while IFS= read -r invoker_member; do
            if [ "$invoker_member" = "serviceAccount:$PROVIDER_INVOKER" ]; then
                continue
            fi
            echo "Removing unexpected Service invoker: $invoker_member"
            gcloud run services remove-iam-policy-binding "$SERVICE_NAME" \
            --region {{gcp_region}} \
            --project {{gcp_project}} \
            --member="$invoker_member" \
            --role="roles/run.invoker" \
            --quiet >/dev/null
        done
    echo "$SERVICE_NAME is private, concurrency=1, min=$MIN_INSTANCES, max=$MAX_INSTANCES."

# Provision all managed Services without deploying Jobs or changing application
# routing. This is a diagnostic/recovery surface; routine releases use
# validator-deploy-all.
# Deploy all managed validator Services only (diagnostic/recovery).
[arg('stage', pattern='dev|staging|prod')]
validator-services-deploy-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    for backend in energyplus fmu shacl schematron portfolio_manager; do
        SOURCE_TAG=$(python3 ops/gcp/validator_release_control.py \
            inventory --backend "$backend" --field source_tag)
        just gcp validator-service-deploy "$backend" {{stage}} "$SOURCE_TAG"
    done

# Stage one complete validator backend release without changing application
# routing. The signed image is mirrored once, then a release-specific Job and
# Service are created from the same explicit release. Production must already
# be in maintenance mode while the candidate pair is imported and accepted.
# Usage: just gcp validator-deploy energyplus prod energyplus-v0.15.1
[arg('stage', pattern='dev|staging|prod')]
validator-deploy name stage release_tag: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{release_tag}}" =~ ^{{name}}-v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?$ ]]; then
        echo "Error: release_tag must be {{name}}-vX.Y.Z." >&2
        exit 1
    fi
    case "{{name}}" in
        energyplus|fmu|shacl|schematron|portfolio_manager) ;;
        *) echo "Error: unknown managed validator '{{name}}'." >&2; exit 1 ;;
    esac
    if [ "{{stage}}" = "prod" ]; then
        just gcp _maintenance-assert-offline {{stage}}
    fi
    if [ "${VALIDATOR_RUNTIME_IAM_READY:-0}" != "1" ]; then
        just gcp _validator-runtime-iam-reconcile {{stage}}
    fi
    just gcp _validator-release-mirror-image "{{name}}" "{{release_tag}}"
    VALIDATOR_RUNTIME_IAM_READY=1 VALIDATOR_RELEASE_VERIFIED=1 \
        just gcp validator-job-deploy "{{name}}" {{stage}} "{{release_tag}}"
    VALIDATOR_RUNTIME_IAM_READY=1 VALIDATOR_RELEASE_VERIFIED=1 \
        just gcp validator-service-deploy "{{name}}" {{stage}} "{{release_tag}}"
    echo "Staged {{name}} {{release_tag}} as a Job and Service on {{stage}}; routing is unchanged."

# Stage every backend at its own exact version from sibling backends.toml.
# It never registers or activates a route.
[arg('stage', pattern='dev|staging|prod')]
validator-deploy-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then
        just gcp _maintenance-assert-offline {{stage}}
    fi
    for backend in energyplus fmu shacl schematron portfolio_manager; do
        SOURCE_TAG=$(python3 ops/gcp/validator_release_control.py \
            inventory --backend "$backend" --field source_tag)
        just gcp validator-deploy "$backend" {{stage}} "$SOURCE_TAG"
    done
    echo "Staged every independent backend release on {{stage}}; routing is unchanged."

# Verify/register one release-specific Job without changing routing.
[arg('stage', pattern='dev|staging|prod')]
validator-deployments-sync name stage version="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    VERSION_ARG=""
    if [ -n "{{version}}" ]; then VERSION_ARG="--version {{version}}"; fi
    JOB_NAME=$(python3 ops/gcp/validator_release_control.py \
        name --backend "{{name}}" $VERSION_ARG --kind job --stage "{{stage}}")
    just gcp management-cmd {{stage}} \
        "sync_gcp_validator_deployments --backend={{name}} --job-name=$JOB_NAME"

# Verify/register one release-specific Service without changing routing.
[arg('stage', pattern='dev|staging|prod')]
validator-services-register name stage version="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    VERSION_ARG=""
    if [ -n "{{version}}" ]; then VERSION_ARG="--version {{version}}"; fi
    SERVICE_NAME=$(python3 ops/gcp/validator_release_control.py \
        name --backend "{{name}}" $VERSION_ARG --kind service --stage "{{stage}}")
    just gcp management-cmd {{stage}} \
        "sync_gcp_validator_services --backend={{name}} --service-name=$SERVICE_NAME"

# Route every compatible semantic Validator row for one exact backend release.
# Normal mode is Service PRIMARY + Job LONG_RUNNING. Job-only mode is Service
# INACTIVE + Job PRIMARY. The Django service changes all rows transactionally.
[arg('stage', pattern='dev|staging|prod')]
validator-backend-route name stage version mode="normal" cause="SUPERSEDED_BY_ACCEPTED_RELEASE" allow_unaccepted="" reason_b64="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{version}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?$ ]]; then
        echo "Error: version must be X.Y.Z." >&2
        exit 1
    fi
    case "{{mode}}" in normal|job-only|inactive) ;; *) echo "Error: mode must be normal, job-only, or inactive." >&2; exit 1 ;; esac
    ALLOW_ARG=""
    if [ "{{allow_unaccepted}}" = "allow-unaccepted" ]; then
        ALLOW_ARG="--allow-unaccepted"
    elif [ -n "{{allow_unaccepted}}" ]; then
        echo "Error: optional flag must be allow-unaccepted." >&2
        exit 1
    fi
    REASON_ARG=""
    if [ -n "{{reason_b64}}" ]; then
        if [[ ! "{{reason_b64}}" =~ ^[A-Za-z0-9+/]+={0,2}$ ]]; then
            echo "Error: operator reason must be strict base64." >&2
            exit 1
        fi
        REASON_ARG="--reason-b64={{reason_b64}}"
    fi
    just gcp management-cmd {{stage}} \
        "activate_validator_backend_release --backend={{name}} --release-version={{version}} --mode={{mode}} --cause={{cause}} $ALLOW_ARG $REASON_ARG"

# Set mutable service-level capacity, then verify and audit the live settings
# without creating a new Cloud Run revision. Validator Services always retain
# minimum zero; this command changes only the maximum instance limit.
# Usage: just gcp validator-service-capacity shacl prod 0.15.0 0 8
[arg('stage', pattern='dev|staging|prod')]
validator-service-capacity name stage version minimum maximum: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{version}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?$ ]]; then
        echo "Error: version must be X.Y.Z." >&2
        exit 1
    fi
    if ! [[ "{{minimum}}" =~ ^[0-9]+$ && "{{maximum}}" =~ ^[1-9][0-9]*$ ]] \
        || [ "{{minimum}}" -gt "{{maximum}}" ]; then
        echo "Error: capacity requires 0 <= minimum <= maximum." >&2
        exit 1
    fi
    if [ "{{minimum}}" != "0" ]; then
        echo "Error: validator Services use scale-to-zero; minimum must be 0." >&2
        exit 1
    fi
    SERVICE_NAME=$(python3 ops/gcp/validator_release_control.py \
        name --backend "{{name}}" --version "{{version}}" \
        --kind service --stage "{{stage}}")
    gcloud run services update "$SERVICE_NAME" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --min={{minimum}} \
        --max={{maximum}} \
        --quiet
    just gcp management-cmd {{stage}} \
        "sync_gcp_validator_services --backend={{name}} --service-name=$SERVICE_NAME"

# Diagnostic compatibility alias for one backend's Job-only mode.
[arg('stage', pattern='dev|staging|prod')]
validator-services-rollback name stage version: _require-gcp-config
    just gcp validator-backend-route {{name}} {{stage}} {{version}} job-only SHAPE_ROLLBACK

# Removed all-backend Service-only retirement path. Provider resources are
# retired only as complete backend release pairs by validator-cleanup.
[private]
[arg('stage', pattern='dev|staging|prod')]
validator-services-retire stage release_tag: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Error: use validator-cleanup; Service-only retirement is disabled." >&2
    exit 2
    if [[ ! "{{release_tag}}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Error: release_tag must be vX.Y.Z." >&2
        exit 1
    fi
    if [ "${VALIDATOR_SERVICE_RETIRE_CONFIRM:-}" != "{{release_tag}}" ]; then
        echo "Error: set VALIDATOR_SERVICE_RETIRE_CONFIRM={{release_tag}} to confirm immutable Service deletion." >&2
        exit 1
    fi
    RELEASE_LABEL="{{release_tag}}"
    RELEASE_LABEL="${RELEASE_LABEL//./-}"
    ALL_PRESENT=1
    for name in energyplus fmu shacl schematron portfolio-manager; do
        SERVICE_NAME="{{app_name}}-validator-service-${name}-${RELEASE_LABEL}"
        if [ "{{stage}}" != "prod" ]; then SERVICE_NAME="${SERVICE_NAME}-{{stage}}"; fi
        if gcloud run services describe "$SERVICE_NAME" \
            --region={{gcp_region}} \
            --project={{gcp_project}} >/dev/null 2>&1; then
            gcloud run services update "$SERVICE_NAME" \
                --region={{gcp_region}} \
                --project={{gcp_project}} \
                --min=0 \
                --quiet
        else
            ALL_PRESENT=0
            echo "$SERVICE_NAME is already absent; continuing a previously preflighted cleanup."
        fi
    done
    if [ "$ALL_PRESENT" -eq 1 ]; then
        VALIDATOR_BACKEND_RELEASE_TAG={{release_tag}} just gcp validator-services-register {{stage}}
    else
        echo "Skipping group provider re-verification because at least one resource was already deleted."
    fi
    just gcp management-cmd {{stage}} "retire_validator_service_release {{release_tag}} --json"

    for name in energyplus fmu shacl schematron portfolio-manager; do
        SERVICE_NAME="{{app_name}}-validator-service-${name}-${RELEASE_LABEL}"
        if [ "{{stage}}" != "prod" ]; then SERVICE_NAME="${SERVICE_NAME}-{{stage}}"; fi
        if gcloud run services describe "$SERVICE_NAME" \
            --region={{gcp_region}} \
            --project={{gcp_project}} >/dev/null 2>&1; then
            gcloud run services delete "$SERVICE_NAME" \
                --region={{gcp_region}} \
                --project={{gcp_project}} \
                --quiet
        fi
    done
    just gcp management-cmd {{stage}} "retire_validator_service_release {{release_tag}} --provider-deleted --json"

# Safe application-level route/readiness inventory.
[arg('stage', pattern='dev|staging|prod')]
validator-deployments-list stage: _require-gcp-config
    just gcp management-cmd {{stage}} "list_validator_deployments --json"

# Capture the remote database projection from the management-command logs.
# The command emits one base64 marker so Cloud Logging prefixes cannot corrupt
# the JSON document consumed by the local release controller.
[private]
[arg('stage', pattern='dev|staging|prod')]
_validator-state-export stage output: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    LOG_OUTPUT=$(just gcp management-cmd {{stage}} \
        "export_validator_release_state --base64")
    ENCODED=$(printf '%s\n' "$LOG_OUTPUT" \
        | sed -n 's/.*VALIDIBOT_RELEASE_STATE_JSON=//p' \
        | tail -1)
    if [ -z "$ENCODED" ]; then
        echo "Error: remote application did not return validator release-state JSON." >&2
        exit 1
    fi
    if ! printf '%s' "$ENCODED" | base64 --decode >"{{output}}" 2>/dev/null; then
        printf '%s' "$ENCODED" | base64 -D >"{{output}}"
    fi
    jq -e '.schema_version == "validibot.validator-release-state.v1"' \
        "{{output}}" >/dev/null

# Build the complete local read-only view from inventory, database, Cloud Run,
# Artifact Registry, and retained accepted release JSON files.
[private]
[arg('stage', pattern='dev|staging|prod')]
_validator-status-json stage output: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    just gcp _maintenance-require-database {{stage}}
    WORK_DIR=$(mktemp -d)
    trap 'rm -rf "$WORK_DIR"' EXIT
    just gcp _validator-state-export {{stage}} "$WORK_DIR/database.json"
    gcloud run services list --region={{gcp_region}} --project={{gcp_project}} \
        --format=json >"$WORK_DIR/services.json"
    gcloud run jobs list --region={{gcp_region}} --project={{gcp_project}} \
        --format=json >"$WORK_DIR/jobs.json"
    jq -s '.[0] + .[1]' "$WORK_DIR/services.json" "$WORK_DIR/jobs.json" \
        >"$WORK_DIR/cloud-run.json"
    gcloud artifacts docker images list "{{validator_repo}}" \
        --include-tags --project={{gcp_project}} --format=json \
        >"$WORK_DIR/gar.json"
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        STORAGE_BUCKET="${APP_NAME}-storage"
    else
        STORAGE_BUCKET="${APP_NAME}-storage-{{stage}}"
    fi
    : >"$WORK_DIR/records.ndjson"
    while IFS= read -r RECORD_URI; do
        [ -z "$RECORD_URI" ] && continue
        RECORD_FILE="$WORK_DIR/record.json"
        gcloud storage cat "$RECORD_URI" >"$RECORD_FILE"
        RECORD_SHA=$(shasum -a 256 "$RECORD_FILE" | awk '{print $1}')
        jq --arg retained_sha256 "$RECORD_SHA" \
            '. + {_retained_sha256: $retained_sha256}' \
            "$RECORD_FILE" >>"$WORK_DIR/records.ndjson"
    done < <(gcloud storage ls \
        "gs://${STORAGE_BUCKET}/operations/validator-backend-releases/*/*.json" \
        2>/dev/null || true)
    jq -s '.' "$WORK_DIR/records.ndjson" >"$WORK_DIR/records.json"
    python3 ops/gcp/validator_release_control.py status \
        --database-json "$WORK_DIR/database.json" \
        --cloud-run-json "$WORK_DIR/cloud-run.json" \
        --gar-json "$WORK_DIR/gar.json" \
        --release-records-json "$WORK_DIR/records.json" >"{{output}}"

# Reconcile a stage to one canonical, latest release-specific provider pair
# per backend. This is the bootstrap policy for the current empty installation:
# after the latest pair is active and all attempts are terminal, superseded
# Services and Jobs are deleted immediately, including legacy stable names.
# Database deployment rows remain as historical evidence and are marked
# RETIRED when they identify a complete old pair.
[arg('stage', pattern='dev|staging|prod')]
validator-latest-only stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{validator_provider_retention_mode}}" in
        latest-only|drain-and-retain) ;;
        *) echo "Error: VALIDATOR_PROVIDER_RETENTION_MODE must be latest-only or drain-and-retain." >&2; exit 1 ;;
    esac
    APP_NAME="{{app_name}}"
    WORK_DIR=$(mktemp -d)
    trap 'rm -rf "$WORK_DIR"' EXIT

    MODE=$(just gcp _mode-current {{stage}})
    if [ "$MODE" != "MAINTENANCE" ]; then
        echo "Error: latest-only reconciliation requires MAINTENANCE; current mode is $MODE." >&2
        exit 1
    fi
    just gcp _maintenance-require-database {{stage}}
    just gcp _validator-status-json {{stage}} "$WORK_DIR/status.json"
    if jq -e 'all(.backends[]; .active_version == null)' "$WORK_DIR/status.json" >/dev/null; then
        echo "No active validator releases are installed; nothing to reconcile."
        exit 0
    fi
    if jq -e 'any(.backends[]; .active_version == null)' "$WORK_DIR/status.json" >/dev/null; then
        echo "Error: latest-only reconciliation requires every managed backend to have an active release." >&2
        jq -r '.backends[] | select(.active_version == null) | "  \(.backend): no active release"' "$WORK_DIR/status.json" >&2
        exit 1
    fi
    if ! jq -e 'all(.backends[]; .active_version == .file_version and .routing_mode == "normal" and (.blockers | length == 0) and ((.unfinished_attempts // 0) == 0))' "$WORK_DIR/status.json" >/dev/null; then
        echo "Error: every backend must already be on its latest offered release with no blockers or unfinished attempts." >&2
        jq -r '.backends[] | select(.active_version != .file_version or .routing_mode != "normal" or (.blockers | length > 0) or ((.unfinished_attempts // 0) != 0)) | "  \(.backend): offered=\(.file_version) active=\(.active_version // \"—\") mode=\(.routing_mode) blockers=\(.blockers | join(\"; \")) unfinished=\(.unfinished_attempts // 0)"' "$WORK_DIR/status.json" >&2
        exit 1
    fi

    just gcp _validator-state-export {{stage}} "$WORK_DIR/database.json"
    : >"$WORK_DIR/expected-services"
    : >"$WORK_DIR/expected-jobs"
    while IFS=$'\t' read -r backend version; do
        service_name=$(python3 ops/gcp/validator_release_control.py name \
            --backend "$backend" --version "$version" --kind service --stage "{{stage}}")
        job_name=$(python3 ops/gcp/validator_release_control.py name \
            --backend "$backend" --version "$version" --kind job --stage "{{stage}}")
        printf '%s\n' "$service_name" >>"$WORK_DIR/expected-services"
        printf '%s\n' "$job_name" >>"$WORK_DIR/expected-jobs"
        for expected_name in "$service_name" "$job_name"; do
            if ! jq -e --arg backend "$backend" --arg version "$version" --arg expected "$expected_name" '
                any(.deployments[]; .backend == $backend and .version == $version and
                    ((.routing_role == "PRIMARY" or .routing_role == "LONG_RUNNING") and
                     ((.provider_resource_name | split("/") | last) == $expected)))
            ' "$WORK_DIR/database.json" >/dev/null; then
                echo "Error: active $backend $version route is not the canonical release-specific provider $expected_name." >&2
                echo "Deploy and activate the latest release before starting the stage." >&2
                exit 1
            fi
        done
    done < <(jq -r '.backends[] | [.backend, .file_version] | @tsv' "$WORK_DIR/status.json")

    is_managed_validator_resource() {
        case "$1" in
            vb-vs-*|vb-vj-*|${APP_NAME}-validator-service-*|${APP_NAME}-validator-backend-*)
                return 0
                ;;
            *)
                return 1
                ;;
        esac
    }
    inventory_for_deletion() {
        local kind="$1"
        local expected_file="$2"
        local name
        while IFS= read -r name; do
            [ -n "$name" ] || continue
            is_managed_validator_resource "$name" || continue
            grep -Fqx "$name" "$expected_file" && continue
            printf '%s\n' "$name"
        done < <(gcloud run "$kind" list \
            --region={{gcp_region}} --project={{gcp_project}} \
            --filter="metadata.labels.stage:{{stage}} AND metadata.labels.validator:*" \
            --format='value(metadata.name)')
    }
    inventory_for_deletion services "$WORK_DIR/expected-services" >"$WORK_DIR/delete-services"
    inventory_for_deletion jobs "$WORK_DIR/expected-jobs" >"$WORK_DIR/delete-jobs"

    echo "Latest-only validator provider plan for {{stage}}:"
    if [ -s "$WORK_DIR/delete-services" ]; then
        sed 's/^/  DELETE Service /' "$WORK_DIR/delete-services"
    fi
    if [ -s "$WORK_DIR/delete-jobs" ]; then
        sed 's/^/  DELETE Job     /' "$WORK_DIR/delete-jobs"
    fi
    if [ ! -s "$WORK_DIR/delete-services" ] && [ ! -s "$WORK_DIR/delete-jobs" ]; then
        echo "  No superseded managed validator providers found."
    elif [ "${VALIDATOR_LATEST_ONLY_AUTOMATIC:-0}" != "1" ]; then
        read -r -p "Type DELETE SUPERSEDED VALIDATOR PROVIDERS to continue: " REPLY
        if [ "$REPLY" != "DELETE SUPERSEDED VALIDATOR PROVIDERS" ]; then
            echo "Cancelled."
            exit 1
        fi
    fi

    while IFS= read -r name; do
        [ -n "$name" ] || continue
        echo "Deleting superseded Service $name..."
        gcloud run services update "$name" \
            --region={{gcp_region}} --project={{gcp_project}} --min=0 --quiet
        gcloud run services delete "$name" \
            --region={{gcp_region}} --project={{gcp_project}} --quiet
    done <"$WORK_DIR/delete-services"
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        echo "Deleting superseded Job $name..."
        gcloud run jobs delete "$name" \
            --region={{gcp_region}} --project={{gcp_project}} --quiet
    done <"$WORK_DIR/delete-jobs"
    inventory_for_deletion services "$WORK_DIR/expected-services" >"$WORK_DIR/remaining-services"
    inventory_for_deletion jobs "$WORK_DIR/expected-jobs" >"$WORK_DIR/remaining-jobs"
    if [ -s "$WORK_DIR/remaining-services" ] || [ -s "$WORK_DIR/remaining-jobs" ]; then
        echo "Error: superseded validator providers remain after deletion; refusing to retire database rows." >&2
        [ ! -s "$WORK_DIR/remaining-services" ] || sed 's/^/  Service still present: /' "$WORK_DIR/remaining-services" >&2
        [ ! -s "$WORK_DIR/remaining-jobs" ] || sed 's/^/  Job still present: /' "$WORK_DIR/remaining-jobs" >&2
        exit 1
    fi

    # Mark every deleted inactive provider row as absent, including resources
    # already missing from Cloud Run. Legacy provider names may have no row;
    # those are intentionally just removed from the provider inventory.
    while IFS=$'\t' read -r backend version resource role deleted_at; do
        [ -n "$resource" ] || continue
        [ "$role" = "INACTIVE" ] || continue
        [ -n "$deleted_at" ] && continue
        basename="${resource##*/}"
        grep -Fqx "$basename" "$WORK_DIR/expected-services" && continue
        grep -Fqx "$basename" "$WORK_DIR/expected-jobs" && continue
        just gcp management-cmd {{stage}} \
            "record_validator_provider_deletion --resource=$resource"
    done < <(jq -r '(.deployment_history // .deployments)[] | [.backend, .version, .provider_resource_name, .routing_role, (.provider_deleted_at // "")] | @tsv' "$WORK_DIR/database.json")

    # Complete the immediate retirement checkpoint for old release pairs and
    # wholly unaccepted failed candidates. The management command still refuses
    # active routes, incomplete or partially accepted pairs, missing provider
    # deletion checkpoints, and nonterminal attempts; only the seven-day clock
    # and the accepted-pair requirement are bypassed for this empty installation.
    jq -r '(.deployment_history // .deployments)[] | select(.routing_role == "INACTIVE" and .retired_at == null) | [.backend, .version] | @tsv' "$WORK_DIR/database.json" \
        | sort -u \
        | while IFS=$'\t' read -r backend version; do
            active_version=$(jq -r --arg backend "$backend" '.backends[] | select(.backend == $backend) | .active_version' "$WORK_DIR/status.json")
            [ "$version" != "$active_version" ] || continue
            just gcp management-cmd {{stage}} \
                "retire_validator_backend_release --backend=$backend --release-version=$version --reason='Latest-only no-user reconciliation.' --immediate --allow-unaccepted-candidate"
        done
    echo "Latest-only validator reconciliation completed for {{stage}}."

# Retain the exact accepted release record in the private validation bucket.
[private]
[arg('stage', pattern='dev|staging|prod')]
_validator-retain-release-record name stage release_tag: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{release_tag}}" =~ ^{{name}}-v([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$ ]]; then
        echo "Error: release tag must be {{name}}-vX.Y.Z." >&2
        exit 1
    fi
    VERSION="${BASH_REMATCH[1]}"
    IMAGE_SLUG="{{name}}"
    IMAGE_SLUG="${IMAGE_SLUG//_/-}"
    WORK_DIR=$(mktemp -d)
    trap 'rm -rf "$WORK_DIR"' EXIT
    RECORD_NAME="validibot-validator-backend-${IMAGE_SLUG}-v${VERSION}.json"
    gh release download "{{release_tag}}" \
        --repo mcquilleninteractive/validibot-validator-backends \
        --dir "$WORK_DIR" --pattern "$RECORD_NAME"
    python3 ops/gcp/validator_release_control.py release-record \
        --backend "{{name}}" --version "$VERSION" \
        --path "$WORK_DIR/$RECORD_NAME" >/dev/null
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        STORAGE_BUCKET="${APP_NAME}-storage"
    else
        STORAGE_BUCKET="${APP_NAME}-storage-{{stage}}"
    fi
    gcloud storage cp "$WORK_DIR/$RECORD_NAME" \
        "gs://${STORAGE_BUCKET}/operations/validator-backend-releases/{{name}}/${VERSION}.json"

# Acceptance temporarily resumes the shared task queues. Prove that no user
# work can be released with the private canaries before changing any route.
[private]
[arg('stage', pattern='dev|staging|prod')]
_validator-release-require-idle stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    just gcp _maintenance-assert-offline {{stage}}
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        QUEUE_NAME="${APP_NAME}-tasks"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider"
    else
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider-{{stage}}"
    fi
    WORK_DIR=$(mktemp -d)
    trap 'rm -rf "$WORK_DIR"' EXIT
    QUEUE_BLOCKERS=0
    inspect_queue() {
        local queue="$1"
        local label="$2"
        local first_task
        if ! first_task=$(gcloud tasks list \
            --queue "$queue" \
            --location {{gcp_region}} \
            --project {{gcp_project}} \
            --limit 1 \
            --format='value(name)' 2>"$WORK_DIR/${label}.err"); then
            echo "Error: could not inspect Cloud Tasks queue $queue." >&2
            sed 's/^/  /' "$WORK_DIR/${label}.err" >&2
            exit 1
        fi
        if [ -n "$first_task" ]; then
            echo "  Queue $queue still contains task $first_task." >&2
            QUEUE_BLOCKERS=$((QUEUE_BLOCKERS + 1))
        fi
    }
    inspect_queue "$QUEUE_NAME" main
    inspect_queue "$PROVIDER_QUEUE_NAME" provider
    just gcp _validator-state-export {{stage}} "$WORK_DIR/database.json"
    UNFINISHED_ATTEMPTS=$(jq -r \
        '[(.deployment_history // .deployments)[]? | (.unfinished_attempts // 0)] | add // 0' \
        "$WORK_DIR/database.json")
    if ! [[ "$UNFINISHED_ATTEMPTS" =~ ^[0-9]+$ ]]; then
        echo "Error: database release state returned an invalid unfinished-attempt count." >&2
        exit 1
    fi
    if [ "$QUEUE_BLOCKERS" -ne 0 ] || [ "$UNFINISHED_ATTEMPTS" -ne 0 ]; then
        echo "Error: validator release maintenance requires both task queues to be empty and every managed execution attempt to be terminal." >&2
        if [ "$UNFINISHED_ATTEMPTS" -ne 0 ]; then
            echo "  Unfinished managed execution attempts: $UNFINISHED_ATTEMPTS" >&2
            jq -r '
              (.deployment_history // .deployments)[]
              | select((.unfinished_attempts // 0) > 0)
              | "    \(.backend) \(.version) \(.kind): \(.unfinished_attempts)"
            ' "$WORK_DIR/database.json" >&2
        fi
        echo "Queued tasks were preserved. The previous lifecycle mode will be restored without exposing work to a candidate release." >&2
        exit 1
    fi
    echo "Validator release preflight passed: both queues are empty and all managed execution attempts are terminal."

# Routine read-only status command.
[arg('stage', pattern='dev|staging|prod')]
validator-status stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    LIFECYCLE_STATUS=$(just gcp mode-status {{stage}})
    MODE=$(awk '/^[[:space:]]*Overall:/ {print $2; exit}' <<< "$LIFECYCLE_STATUS")
    if [ "$MODE" = "PARKED" ]; then
        printf '%s\n' "$LIFECYCLE_STATUS"
        echo "Validator release detail is unavailable while Cloud SQL is stopped."
        echo "This is expected in PARKED mode; a stopped database is not a validator release error."
        echo "To inspect ACTIVE and ROLLBACK versions, first run:"
        echo "  just gcp mode-maintenance {{stage}}"
        exit 0
    fi
    if [ "$MODE" != "LIVE" ] && [ "$MODE" != "MAINTENANCE" ]; then
        printf '%s\n' "$LIFECYCLE_STATUS" >&2
        echo "Error: {{stage}} is PARTIALLY_TRANSITIONED; reconcile its lifecycle before inspecting validator releases." >&2
        exit 1
    fi
    STATUS_JSON=$(mktemp)
    trap 'rm -f "$STATUS_JSON"' EXIT
    just gcp _validator-status-json {{stage}} "$STATUS_JSON"
    jq -r '
      ["BACKEND","FILE SAYS","ACTIVE","ROLLBACK","MODE","ACTION"],
      (.backends[] | [
        .backend,
        .file_version,
        (.active_version // "—"),
        (.rollback_version // "—"),
        .routing_mode,
        .recommended_action
      ]) | @tsv
    ' "$STATUS_JSON" | column -t -s $'\t'

# Install all five independently versioned backends. Provider pairs are
# accepted one at a time but returned to inactive until every backend passes;
# the final five backend transactions run before public dispatch resumes.
[arg('stage', pattern='dev|staging|prod')]
validator-setup stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    just gcp _validator-release-require-registry-client
    case "{{validator_provider_retention_mode}}" in
        latest-only|drain-and-retain) ;;
        *) echo "Error: VALIDATOR_PROVIDER_RETENTION_MODE must be latest-only or drain-and-retain." >&2; exit 1 ;;
    esac
    STATUS_JSON=$(mktemp)
    INITIAL_MODE=$(just gcp _mode-current {{stage}})
    case "$INITIAL_MODE" in
        LIVE|MAINTENANCE|PARKED) ;;
        *)
            echo "Error: {{stage}} is PARTIALLY_TRANSITIONED; reconcile it before validator setup." >&2
            exit 1
            ;;
    esac
    MODE_CHANGED=0
    cleanup() {
        local status=$?
        trap - EXIT INT TERM
        if [ "$MODE_CHANGED" = "1" ]; then
            if ! just gcp _mode-restore {{stage}} "$INITIAL_MODE"; then
                echo "Error: failed to restore {{stage}} to its initial $INITIAL_MODE mode." >&2
                just gcp _enforce-maintenance {{stage}} || true
                status=1
            fi
        fi
        rm -f "$STATUS_JSON"
        exit "$status"
    }
    trap cleanup EXIT INT TERM
    ensure_uninstalled() {
        just gcp _validator-status-json {{stage}} "$STATUS_JSON"
        if jq -e '.backends[] | select(.active_version != null)' "$STATUS_JSON" >/dev/null; then
            echo "Error: setup is only for an installation with no active backend releases." >&2
            echo "Use validator-update to reconcile an existing installation." >&2
            return 1
        fi
    }
    python3 ops/gcp/validator_release_control.py inventory
    echo "GCP project: {{gcp_project}}"
    echo "Region:      {{gcp_region}}"
    read -r -p "Type the GCP project ID to continue: " REPLY
    if [ "$REPLY" != "{{gcp_project}}" ]; then echo "Cancelled."; exit 1; fi
    # LIVE and MAINTENANCE have a usable database, so reject an existing
    # installation before changing any lifecycle signal. PARKED must first
    # enter MAINTENANCE because its database is deliberately stopped.
    if [ "$INITIAL_MODE" != "PARKED" ]; then
        ensure_uninstalled
    fi
    MODE_CHANGED=1
    GCP_YES=1 just gcp mode-maintenance {{stage}}
    if [ "$INITIAL_MODE" = "PARKED" ]; then
        ensure_uninstalled
    fi
    just gcp _validator-release-require-idle {{stage}}
    just gcp _validator-runtime-iam-reconcile {{stage}}
    for backend in energyplus fmu shacl schematron portfolio_manager; do
        VERSION=$(python3 ops/gcp/validator_release_control.py \
            inventory --backend "$backend" --field release_version)
        SOURCE_TAG="${backend}-v${VERSION}"
        VALIDATOR_RUNTIME_IAM_READY=1 \
            just gcp validator-deploy "$backend" {{stage}} "$SOURCE_TAG"
        just gcp validator-acceptance "$backend" {{stage}} "$SOURCE_TAG" 3 inactive
    done
    GROUP_COMMAND="activate_validator_backend_release_group --mode=normal --cause=SUPERSEDED_BY_ACCEPTED_RELEASE"
    ACTIVATION_ARGS=()
    for backend in energyplus fmu shacl schematron portfolio_manager; do
        VERSION=$(python3 ops/gcp/validator_release_control.py \
            inventory --backend "$backend" --field release_version)
        SOURCE_TAG="${backend}-v${VERSION}"
        just gcp _validator-retain-release-record "$backend" {{stage}} "$SOURCE_TAG"
        GROUP_COMMAND="${GROUP_COMMAND} --release=${backend}=${VERSION}"
        ACTIVATION_ARGS+=("--release=${backend}=${VERSION}")
    done
    just gcp _validator-status-json {{stage}} "$STATUS_JSON"
    python3 ops/gcp/validator_release_control.py activation-check \
        --status-json "$STATUS_JSON" "${ACTIVATION_ARGS[@]}"
    just gcp management-cmd {{stage}} "$GROUP_COMMAND"
    if [ "{{validator_provider_retention_mode}}" = "latest-only" ]; then
        VALIDATOR_LATEST_ONLY_AUTOMATIC=1 \
            just gcp validator-latest-only {{stage}}
    else
        echo "Provider retention policy is drain-and-retain; retaining superseded pairs for rollback and drain."
    fi
    VALIDATOR_LATEST_ONLY_RECONCILED=1 \
        just gcp _mode-restore {{stage}} "$INITIAL_MODE"
    MODE_CHANGED=0
    trap - EXIT INT TERM
    rm -f "$STATUS_JSON"

# Reconcile one backend, or every backend with a newer offered version or
# missing semantic Validator rows. Older or rolled-back offered versions stop
# before provider mutation.
[arg('stage', pattern='dev|staging|prod')]
validator-update stage backend="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    just gcp _validator-release-require-registry-client
    case "{{validator_provider_retention_mode}}" in
        latest-only|drain-and-retain) ;;
        *) echo "Error: VALIDATOR_PROVIDER_RETENTION_MODE must be latest-only or drain-and-retain." >&2; exit 1 ;;
    esac
    WORK_DIR=$(mktemp -d)
    INITIAL_MODE=$(just gcp _mode-current {{stage}})
    case "$INITIAL_MODE" in
        LIVE|MAINTENANCE|PARKED) ;;
        *)
            echo "Error: {{stage}} is PARTIALLY_TRANSITIONED; reconcile it before validator update." >&2
            exit 1
            ;;
    esac
    MODE_CHANGED=0
    BACKENDS=()
    TOUCHED_BACKENDS=()
    LEGACY_RECORD_BLOCKER="active accepted release record is not retained"
    restore_previous_routes() {
        local status=$?
        local cleanup_status=$status
        trap - EXIT INT TERM
        if [ "$status" -ne 0 ] && [ -f "$WORK_DIR/selected.json" ]; then
            echo ""
            echo "Update stopped before the final activation transaction."
            echo "Restoring each backend to its previously accepted route, or"
            echo "parking an unaccepted candidate as inactive so it cannot receive work."
            # Bash 3.2 treats an empty array expansion as an unbound variable
            # under ``set -u``. A read-only preflight can fail before any
            # backend is touched, so guard the loop explicitly.
            if [ "${#TOUCHED_BACKENDS[@]}" -gt 0 ]; then
                for selected_backend in "${TOUCHED_BACKENDS[@]}"; do
                    candidate_version=$(jq -r --arg backend "$selected_backend" \
                        '.[] | select(.backend == $backend) | .file_version' \
                        "$WORK_DIR/selected.json")
                    old_version=$(jq -r --arg backend "$selected_backend" \
                        '.[] | select(.backend == $backend) | .active_version // ""' \
                        "$WORK_DIR/selected.json")
                    old_accepted="false"
                    if [ -n "$old_version" ]; then
                        old_accepted=$(jq -r --arg backend "$selected_backend" \
                            --arg version "$old_version" \
                            '.[] | select(.backend == $backend) |
                             (.release_health[$version].all_accepted // false) | tostring' \
                            "$WORK_DIR/selected.json")
                    fi
                    if [ -n "$old_version" ] && [ "$old_accepted" = "true" ]; then
                        echo "  $selected_backend: restoring accepted release $old_version in normal mode."
                        if ! just gcp validator-backend-route "$selected_backend" {{stage}} \
                            "$old_version" normal ACCEPTANCE_FAILURE; then
                            echo "Error: failed to restore $selected_backend $old_version." >&2
                            cleanup_status=1
                        fi
                    else
                        echo "  $selected_backend: no accepted route is available; deactivating candidate $candidate_version."
                        if ! just gcp validator-backend-route "$selected_backend" {{stage}} \
                            "$candidate_version" inactive ACCEPTANCE_FAILURE allow-unaccepted; then
                            echo "Error: failed to deactivate $selected_backend $candidate_version." >&2
                            cleanup_status=1
                        fi
                    fi
                done
            fi
        fi
        if [ "$MODE_CHANGED" = "1" ]; then
            if ! just gcp _mode-restore {{stage}} "$INITIAL_MODE"; then
                echo "Error: failed to restore {{stage}} to its initial $INITIAL_MODE mode." >&2
                just gcp _enforce-maintenance {{stage}} || true
                cleanup_status=1
            fi
        fi
        rm -rf "$WORK_DIR"
        exit "$cleanup_status"
    }
    trap restore_previous_routes EXIT INT TERM
    select_status_rows() {
        local status_file="$1"
        local selected_file="$2"
        if [ -n "{{backend}}" ]; then
            jq --arg backend "{{backend}}" \
                '[.backends[] | select(
                    .backend == $backend and .recommended_action != "none"
                )]' "$status_file" >"$selected_file"
        else
            jq '[.backends[] | select(.recommended_action != "none")]' \
                "$status_file" >"$selected_file"
        fi
    }
    # A parked database cannot serve the read-only release preflight. Starting
    # it remains isolated and is restored to PARKED by the trap on every exit.
    if [ "$INITIAL_MODE" = "PARKED" ]; then
        MODE_CHANGED=1
        GCP_YES=1 just gcp mode-maintenance {{stage}}
    fi
    just gcp _validator-status-json {{stage}} "$WORK_DIR/status.json"
    select_status_rows "$WORK_DIR/status.json" "$WORK_DIR/selected.json"
    if [ "$(jq 'length' "$WORK_DIR/selected.json")" -eq 0 ]; then
        echo "No backend release or semantic Validator rows require reconciliation."
        exit 0
    fi
    # Releases accepted before retained records were introduced need one
    # append-only evidence migration. It is safe to perform after operator
    # confirmation, then status revalidates the record against the database,
    # provider resources, and registry digest before deployment continues.
    if jq -e --arg legacy_record_blocker "$LEGACY_RECORD_BLOCKER" '
        .[]
        | select(.recommended_action | startswith("blocked:"))
        | select(.blockers != [$legacy_record_blocker])
    ' "$WORK_DIR/selected.json" >/dev/null; then
        jq -r --arg legacy_record_blocker "$LEGACY_RECORD_BLOCKER" '
            .[]
            | select(.recommended_action | startswith("blocked:"))
            | select(.blockers != [$legacy_record_blocker])
            | "\(.backend): \(.recommended_action)"
        ' "$WORK_DIR/selected.json" >&2
        echo "No GCP resources were changed." >&2
        exit 1
    fi
    echo ""
    echo "Validator release update for {{stage}}"
    echo "Capacity policy: every release-specific validator Service uses min=0."
    echo "Acceptance records cold-start latency as evidence; it does not require warm instances."
    echo "Selected backend changes:"
    jq -r '.[] | "  \(.backend): \(.active_version // "not installed") -> \(.file_version) (\(.recommended_action))"' \
        "$WORK_DIR/selected.json"
    if jq -e --arg legacy_record_blocker "$LEGACY_RECORD_BLOCKER" '
        .[] | select(.blockers == [$legacy_record_blocker])
    ' "$WORK_DIR/selected.json" >/dev/null; then
        echo "  One-time migration: retain signed evidence for the accepted outgoing release."
    fi
    CONFIRM="{{backend}}"
    if [ -z "$CONFIRM" ]; then CONFIRM="update"; fi
    read -r -p "Type $CONFIRM to continue: " REPLY
    if [ "$REPLY" != "$CONFIRM" ]; then echo "Cancelled."; exit 1; fi
    while IFS= read -r selected_backend; do
        BACKENDS+=("$selected_backend")
    done < <(jq -r '.[].backend' "$WORK_DIR/selected.json")
    BACKEND_COUNT=${#BACKENDS[@]}
    MODE_CHANGED=1
    echo ""
    echo "Putting {{stage}} into MAINTENANCE so validator routes and queues can be changed safely."
    GCP_YES=1 just gcp mode-maintenance {{stage}}
    just gcp _validator-release-require-idle {{stage}}
    just gcp _validator-runtime-iam-reconcile {{stage}}
    MIGRATED_LEGACY_RECORD=0
    for selected_backend in "${BACKENDS[@]}"; do
        if jq -e --arg backend "$selected_backend" \
            --arg legacy_record_blocker "$LEGACY_RECORD_BLOCKER" '
            .[]
            | select(.backend == $backend)
            | select(.blockers == [$legacy_record_blocker])
        ' "$WORK_DIR/selected.json" >/dev/null; then
            old_version=$(jq -r --arg backend "$selected_backend" \
                '.[] | select(.backend == $backend) | .active_version // ""' \
                "$WORK_DIR/selected.json")
            if [ -z "$old_version" ]; then
                echo "Error: $selected_backend has no active release to retain." >&2
                exit 1
            fi
            echo "Retaining signed evidence for accepted $selected_backend $old_version..."
            just gcp _validator-retain-release-record "$selected_backend" {{stage}} \
                "${selected_backend}-v${old_version}"
            MIGRATED_LEGACY_RECORD=1
        fi
    done
    if [ "$MIGRATED_LEGACY_RECORD" = "1" ]; then
        just gcp _validator-status-json {{stage}} \
            "$WORK_DIR/status-after-record-migration.json"
        select_status_rows "$WORK_DIR/status-after-record-migration.json" \
            "$WORK_DIR/selected-after-record-migration.json"
        mv "$WORK_DIR/selected-after-record-migration.json" \
            "$WORK_DIR/selected.json"
        BACKENDS=()
        while IFS= read -r selected_backend; do
            BACKENDS+=("$selected_backend")
        done < <(jq -r '.[].backend' "$WORK_DIR/selected.json")
        if [ "${#BACKENDS[@]}" -eq 0 ]; then
            echo "Outgoing release evidence retained and verified."
            echo "No validator release deployment is required."
            just gcp _mode-restore {{stage}} "$INITIAL_MODE"
            MODE_CHANGED=0
            trap - EXIT INT TERM
            rm -rf "$WORK_DIR"
            exit 0
        fi
        BACKEND_COUNT=${#BACKENDS[@]}
        for selected_backend in "${BACKENDS[@]}"; do
            if ! jq -e --arg backend "$selected_backend" '
                .backends[]
                | select(.backend == $backend)
                | (.blockers | length == 0)
            ' "$WORK_DIR/status-after-record-migration.json" >/dev/null; then
                jq -r --arg backend "$selected_backend" '
                    .backends[]
                    | select(.backend == $backend)
                    | "\(.backend): \(.recommended_action)"
                ' "$WORK_DIR/status-after-record-migration.json" >&2
                echo "Error: retained release evidence did not match the active deployment." >&2
                exit 1
            fi
        done
        echo "Outgoing release evidence retained and verified."
    fi
    BACKEND_INDEX=0
    for selected_backend in "${BACKENDS[@]}"; do
        BACKEND_INDEX=$((BACKEND_INDEX + 1))
        TOUCHED_BACKENDS+=("$selected_backend")
        version=$(jq -r --arg backend "$selected_backend" \
            '.[] | select(.backend == $backend) | .file_version' \
            "$WORK_DIR/selected.json")
        old_version=$(jq -r --arg backend "$selected_backend" \
            '.[] | select(.backend == $backend) | .active_version // ""' \
            "$WORK_DIR/selected.json")
        source_tag="${selected_backend}-v${version}"
        echo ""
        echo "[$BACKEND_INDEX/$BACKEND_COUNT] $selected_backend $version"
        echo "  Stage: deploy the immutable Job and scale-to-zero Service pair."
        if [ -n "$old_version" ] && [ "$old_version" != "$version" ]; then
            old_source_tag="${selected_backend}-v${old_version}"
            VALIDATOR_ALLOW_HISTORICAL=1 \
                just gcp validator-release-verify "$selected_backend" \
                    "$old_source_tag"
            just gcp validator-deployments-sync "$selected_backend" {{stage}} \
                "$old_version"
            just gcp validator-services-register "$selected_backend" {{stage}} \
                "$old_version"
        fi
        VALIDATOR_RUNTIME_IAM_READY=1 \
            just gcp validator-deploy "$selected_backend" {{stage}} "$source_tag"
        echo "  Accept: run correctness, concurrency, storage, route, provenance, and timing observations."
        just gcp validator-acceptance "$selected_backend" {{stage}} "$source_tag" \
            3 inactive "$old_version"
        echo "  Passed: the accepted pair is parked until every selected backend is ready."
    done
    echo ""
    echo "All selected backend pairs passed acceptance and are parked inactive."
    echo "Retaining signed release evidence before the group activation transaction."
    GROUP_COMMAND="activate_validator_backend_release_group --mode=normal --cause=SUPERSEDED_BY_ACCEPTED_RELEASE"
    ACTIVATION_ARGS=()
    for selected_backend in "${BACKENDS[@]}"; do
        version=$(jq -r --arg backend "$selected_backend" \
            '.[] | select(.backend == $backend) | .file_version' \
            "$WORK_DIR/selected.json")
        source_tag="${selected_backend}-v${version}"
        just gcp _validator-retain-release-record "$selected_backend" {{stage}} \
            "$source_tag"
        GROUP_COMMAND="${GROUP_COMMAND} --release=${selected_backend}=${version}"
        ACTIVATION_ARGS+=("--release=${selected_backend}=${version}")
    done
    echo "Verifying the complete activation set before changing any route..."
    just gcp _validator-status-json {{stage}} \
        "$WORK_DIR/status-before-activation.json"
    python3 ops/gcp/validator_release_control.py activation-check \
        --status-json "$WORK_DIR/status-before-activation.json" \
        "${ACTIVATION_ARGS[@]}"
    echo "Activating all accepted backend pairs together in normal mode."
    just gcp management-cmd {{stage}} "$GROUP_COMMAND"
    if [ "{{validator_provider_retention_mode}}" = "latest-only" ]; then
        VALIDATOR_LATEST_ONLY_AUTOMATIC=1 \
            just gcp validator-latest-only {{stage}}
    else
        echo "Provider retention policy is drain-and-retain; retaining superseded pairs for rollback and drain."
    fi
    echo "All selected validator backends are active; restoring {{stage}} to $INITIAL_MODE."
    VALIDATOR_LATEST_ONLY_RECONCILED=1 \
        just gcp _mode-restore {{stage}} "$INITIAL_MODE"
    MODE_CHANGED=0
    trap - EXIT INT TERM
    rm -rf "$WORK_DIR"

# Roll back one backend release, enter Job-only mode, restore normal mode, or
# recover an exact version previously accepted by this installation.
[arg('stage', pattern='dev|staging|prod')]
validator-rollback stage backend operation="release": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    WORK_DIR=$(mktemp -d)
    INITIAL_MODE=$(just gcp _mode-current {{stage}})
    case "$INITIAL_MODE" in
        LIVE|MAINTENANCE|PARKED) ;;
        *)
            echo "Error: {{stage}} is PARTIALLY_TRANSITIONED; reconcile it before validator rollback." >&2
            exit 1
            ;;
    esac
    MODE_CHANGED=0
    restore_initial_mode() {
        local status=$?
        trap - EXIT INT TERM
        if [ "$MODE_CHANGED" = "1" ]; then
            if ! just gcp _mode-restore {{stage}} "$INITIAL_MODE"; then
                echo "Error: failed to restore {{stage}} to its initial $INITIAL_MODE mode." >&2
                just gcp _enforce-maintenance {{stage}} || true
                status=1
            fi
        fi
        rm -rf "$WORK_DIR"
        exit "$status"
    }
    trap restore_initial_mode EXIT INT TERM
    if [ "$INITIAL_MODE" = "PARKED" ]; then
        MODE_CHANGED=1
        GCP_YES=1 just gcp mode-maintenance {{stage}}
    fi
    just gcp _validator-status-json {{stage}} "$WORK_DIR/status.json"
    CURRENT=$(jq -r --arg backend "{{backend}}" \
        '.backends[] | select(.backend == $backend) | .active_version // ""' \
        "$WORK_DIR/status.json")
    if [ -z "$CURRENT" ]; then
        echo "Error: backend {{backend}} has no active release." >&2
        exit 1
    fi
    EXACT_RECOVERY=0
    RECOVERY_REASON_B64=""
    case "{{operation}}" in
        job-mode)
            TARGET="$CURRENT"; MODE="job-only"; CAUSE="SHAPE_ROLLBACK" ;;
        normal-mode)
            TARGET="$CURRENT"; MODE="normal"; CAUSE="SHAPE_ROLLBACK" ;;
        release)
            TARGET=$(jq -r --arg backend "{{backend}}" \
                '.backends[] | select(.backend == $backend) | .rollback_version // ""' \
                "$WORK_DIR/status.json")
            MODE="normal"; CAUSE="RELEASE_ROLLBACK_FROM" ;;
        *)
            if [[ ! "{{operation}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?$ ]]; then
                echo "Error: operation must be release, job-mode, normal-mode, or an exact version." >&2
                exit 1
            fi
            TARGET="{{operation}}"; MODE="normal"; CAUSE="RELEASE_ROLLBACK_FROM"
            EXACT_RECOVERY=1
            just gcp _validator-state-export {{stage}} "$WORK_DIR/database.json"
            if ! jq -e --arg backend "{{backend}}" --arg version "$TARGET" '
                [.deployments[] | select(.backend == $backend and .version == $version)]
                | length >= 2 and all(.accepted_at != null)
            ' "$WORK_DIR/database.json" >/dev/null; then
                echo "Error: exact recovery requires accepted deployment history." >&2
                exit 1
            fi
            ;;
    esac
    if [ -z "$TARGET" ]; then
        echo "Error: no eligible previous accepted release exists." >&2
        exit 1
    fi
    if [ "$EXACT_RECOVERY" = "1" ]; then
        ROLLBACK_EVENT=$(jq -c --arg backend "{{backend}}" --arg version "$TARGET" '
            .backends[]
            | select(.backend == $backend)
            | .rolled_back_from[]?
            | select(.version == $version)
        ' "$WORK_DIR/status.json" | tail -n 1)
        if [ -n "$ROLLBACK_EVENT" ]; then
            echo "This installation previously rolled back from {{backend}} $TARGET."
            echo "Recorded at:     $(jq -r '.at // "unknown"' <<<"$ROLLBACK_EVENT")"
            echo "Previous reason: $(jq -r '.reason // "not recorded"' <<<"$ROLLBACK_EVENT")"
            read -r -p "Explain what was repaired before reusing this release: " RECOVERY_REASON
            if [ -z "${RECOVERY_REASON//[[:space:]]/}" ]; then
                echo "Error: reusing a rolled-back release requires a reason." >&2
                exit 1
            fi
            if [ "${#RECOVERY_REASON}" -gt 1000 ]; then
                echo "Error: recovery reason cannot exceed 1000 characters." >&2
                exit 1
            fi
            RECOVERY_REASON_B64=$(printf '%s' "$RECOVERY_REASON" | base64 | tr -d '\n')
        fi
    fi
    echo "{{backend}}: $CURRENT -> $TARGET ($MODE)"
    read -r -p "Type {{backend}} to continue: " REPLY
    if [ "$REPLY" != "{{backend}}" ]; then echo "Cancelled."; exit 1; fi
    MODE_CHANGED=1
    GCP_YES=1 just gcp mode-maintenance {{stage}}
    if [ "{{operation}}" != "job-mode" ] && [ "{{operation}}" != "normal-mode" ]; then
        SOURCE_TAG="{{backend}}-v${TARGET}"
        VALIDATOR_ALLOW_HISTORICAL=1 \
            just gcp validator-deploy "{{backend}}" {{stage}} "$SOURCE_TAG"
        just gcp validator-deployments-sync "{{backend}}" {{stage}} "$TARGET"
        just gcp validator-services-register "{{backend}}" {{stage}} "$TARGET"
        if [ "$EXACT_RECOVERY" = "1" ]; then
            just gcp validator-acceptance "{{backend}}" {{stage}} "$SOURCE_TAG"
        fi
    fi
    just gcp validator-backend-route "{{backend}}" {{stage}} "$TARGET" "$MODE" \
        "$CAUSE" "" "$RECOVERY_REASON_B64"
    just gcp _mode-restore {{stage}} "$INITIAL_MODE"
    MODE_CHANGED=0
    trap - EXIT INT TERM
    rm -rf "$WORK_DIR"

# Calculate an exact seven-day cleanup plan, require its derived identifier,
# refresh all safety inputs, and delete only unchanged planned resources.
[arg('stage', pattern='dev|staging|prod')]
validator-cleanup stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    WORK_DIR=$(mktemp -d)
    trap 'rm -rf "$WORK_DIR"' EXIT
    just gcp _validator-status-json {{stage}} "$WORK_DIR/status.json"
    just gcp _validator-state-export {{stage}} "$WORK_DIR/database.json"
    python3 ops/gcp/validator_release_control.py cleanup-plan \
        --database-json "$WORK_DIR/database.json" \
        --status-json "$WORK_DIR/status.json" >"$WORK_DIR/plan.json"
    jq '.' "$WORK_DIR/plan.json"
    PLAN_ID=$(jq -r '.plan_id' "$WORK_DIR/plan.json")
    if [ "$(jq '.delete | length' "$WORK_DIR/plan.json")" -eq 0 ]; then
        echo "No provider release pair is eligible for deletion."
        exit 0
    fi
    read -r -p "Type $PLAN_ID to delete exactly the resources shown: " REPLY
    if [ "$REPLY" != "$PLAN_ID" ]; then echo "Cancelled."; exit 1; fi
    just gcp _validator-status-json {{stage}} "$WORK_DIR/status-refreshed.json"
    just gcp _validator-state-export {{stage}} "$WORK_DIR/database-refreshed.json"
    python3 ops/gcp/validator_release_control.py cleanup-plan \
        --database-json "$WORK_DIR/database-refreshed.json" \
        --status-json "$WORK_DIR/status-refreshed.json" \
        >"$WORK_DIR/plan-refreshed.json"
    if [ "$(jq -r '.plan_id' "$WORK_DIR/plan-refreshed.json")" != "$PLAN_ID" ]; then
        echo "Error: cleanup inputs changed; review the new plan." >&2
        jq '.' "$WORK_DIR/plan-refreshed.json"
        exit 1
    fi
    jq -c '.delete[]' "$WORK_DIR/plan-refreshed.json" |
    while IFS= read -r release; do
        backend=$(jq -r '.backend' <<<"$release")
        version=$(jq -r '.version' <<<"$release")
        jq -r '.resources[]' <<<"$release" |
        while IFS= read -r resource; do
            name="${resource##*/}"
            if [[ "$resource" == *"/services/"* ]]; then
                if gcloud run services describe "$name" --region={{gcp_region}} \
                    --project={{gcp_project}} >/dev/null 2>&1; then
                    gcloud run services update "$name" --region={{gcp_region}} \
                        --project={{gcp_project}} --min=0 --quiet
                    gcloud run services delete "$name" --region={{gcp_region}} \
                        --project={{gcp_project}} --quiet
                fi
                if gcloud run services describe "$name" --region={{gcp_region}} \
                    --project={{gcp_project}} >/dev/null 2>&1; then
                    echo "Error: Service $name still exists after deletion." >&2
                    exit 1
                fi
            else
                if gcloud run jobs describe "$name" --region={{gcp_region}} \
                    --project={{gcp_project}} >/dev/null 2>&1; then
                    gcloud run jobs delete "$name" --region={{gcp_region}} \
                        --project={{gcp_project}} --quiet
                fi
                if gcloud run jobs describe "$name" --region={{gcp_region}} \
                    --project={{gcp_project}} >/dev/null 2>&1; then
                    echo "Error: Job $name still exists after deletion." >&2
                    exit 1
                fi
            fi
            just gcp management-cmd {{stage}} \
                "record_validator_provider_deletion --resource=$resource"
        done
        just gcp management-cmd {{stage}} \
            "retire_validator_backend_release --backend=$backend --release-version=$version --reason='Provider pair deleted after verified seven-day drain.'"
    done

# Report persisted p50/p95 timing stages without blending Job and Service
# deployment revisions. JSON output can be retained with rollout evidence.
# Usage: just gcp validator-latency-report prod 168 20
[arg('stage', pattern='dev|staging|prod')]
validator-latency-report stage since_hours="168" minimum_samples="20": _require-gcp-config
    just gcp management-cmd {{stage}} "report_validator_execution_latency --since-hours={{since_hours}} --minimum-samples={{minimum_samples}} --json"

# Run one backend-specific acceptance suite while public dispatch is offline.
# The candidate is temporarily routed through the pair service, and successful
# canaries record accepted_at on the exact Service and Job rows. Each shape
# uses three concurrent attempts: enough to prove fan-out without turning every
# release into a load test. Validator Services deliberately remain at min=0;
# timing observations are not a warm-instance gate.
[arg('stage', pattern='dev|staging|prod')]
validator-acceptance name stage release_tag attempts="3" final_mode="normal" outgoing_version="": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{release_tag}}" =~ ^{{name}}-v([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?)$ ]]; then
        echo "Error: release_tag must be {{name}}-vX.Y.Z." >&2
        exit 1
    fi
    VERSION="${BASH_REMATCH[1]}"
    if ! [[ "{{attempts}}" =~ ^[1-9][0-9]*$ ]] || [ "{{attempts}}" -gt 20 ]; then
        echo "Error: attempts must be between 1 and 20." >&2
        exit 1
    fi
    if [ "{{stage}}" = "prod" ] && [ "{{attempts}}" -ne 3 ]; then
        echo "Error: production acceptance requires exactly 3 concurrent attempts." >&2
        exit 1
    fi
    case "{{final_mode}}" in
        normal|inactive) ;;
        *) echo "Error: final_mode must be normal or inactive." >&2; exit 1 ;;
    esac
    just gcp _maintenance-assert-offline {{stage}}
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        QUEUE_NAME="${APP_NAME}-tasks"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider"
    else
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider-{{stage}}"
    fi
    JOB_NAME=$(python3 ops/gcp/validator_release_control.py \
        name --backend "{{name}}" --version "$VERSION" \
        --kind job --stage "{{stage}}")
    SERVICE_NAME=$(python3 ops/gcp/validator_release_control.py \
        name --backend "{{name}}" --version "$VERSION" \
        --kind service --stage "{{stage}}")
    CERTIFY_COMMAND="certify_validator_backend_release --backend={{name}} --release-tag={{release_tag}} --job-name=$JOB_NAME --service-name=$SERVICE_NAME --attempts={{attempts}} --timeout-seconds=3000 --final-mode={{final_mode}}"
    if [ -n "{{outgoing_version}}" ]; then
        if [[ ! "{{outgoing_version}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?$ ]]; then
            echo "Error: outgoing_version must be X.Y.Z when supplied." >&2
            exit 1
        fi
        OUTGOING_JOB_NAME=$(python3 ops/gcp/validator_release_control.py \
            name --backend "{{name}}" --version "{{outgoing_version}}" \
            --kind job --stage "{{stage}}")
        OUTGOING_SERVICE_NAME=$(python3 ops/gcp/validator_release_control.py \
            name --backend "{{name}}" --version "{{outgoing_version}}" \
            --kind service --stage "{{stage}}")
        CERTIFY_COMMAND+=" --outgoing-version={{outgoing_version}} --outgoing-job-name=$OUTGOING_JOB_NAME --outgoing-service-name=$OUTGOING_SERVICE_NAME"
    fi
    CANDIDATE_TOUCHED=0
    ACCEPTANCE_PASSED=0
    pause_acceptance_queues() {
        gcloud tasks queues pause "$QUEUE_NAME" \
            --location {{gcp_region}} --project {{gcp_project}} --quiet \
            >/dev/null 2>&1 || true
        gcloud tasks queues pause "$PROVIDER_QUEUE_NAME" \
            --location {{gcp_region}} --project {{gcp_project}} --quiet \
            >/dev/null 2>&1 || true
    }
    cleanup_acceptance() {
        local status=$?
        local cleanup_status=$status
        trap - EXIT INT TERM
        pause_acceptance_queues
        if [ "$ACCEPTANCE_PASSED" != "1" ] && [ "$CANDIDATE_TOUCHED" = "1" ]; then
            echo ""
            echo "Acceptance did not complete successfully."
            echo "Parking {{name}} $VERSION as inactive so an unaccepted release cannot receive work."
            if ! just gcp validator-backend-route "{{name}}" {{stage}} "$VERSION" \
                inactive ACCEPTANCE_FAILURE allow-unaccepted; then
                echo "Error: failed to deactivate the failed acceptance candidate." >&2
                cleanup_status=1
            fi
        fi
        if ! just gcp _enforce-maintenance {{stage}}; then
            echo "Error: failed to leave {{stage}} in enforced maintenance mode." >&2
            cleanup_status=1
        fi
        exit "$cleanup_status"
    }
    trap cleanup_acceptance EXIT INT TERM
    echo ""
    echo "Validator acceptance: {{name}} $VERSION on {{stage}}"
    echo "Policy: each execution shape gets 3 concurrent release-smoke attempts; the scale-to-zero Service is not warmed."
    just gcp _maintenance-start-database {{stage}}
    echo "Resuming isolated queues for the two acceptance bursts."
    gcloud tasks queues resume "$QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} --quiet
    gcloud tasks queues resume "$PROVIDER_QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} --quiet
    CANDIDATE_TOUCHED=1
    echo "Running import, Service canaries, Job canaries, and final routing in one remote management execution."
    GCP_MANAGEMENT_COMMAND_TIMEOUT=3600s \
        just gcp management-cmd {{stage}} \
        "$CERTIFY_COMMAND"
    echo "Both acceptance phases passed. Pausing the isolated queues."
    gcloud tasks queues pause "$QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} --quiet
    gcloud tasks queues pause "$PROVIDER_QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} --quiet
    ACCEPTANCE_PASSED=1
    trap - EXIT INT TERM
    echo "Acceptance passed for {{name}} $VERSION on {{stage}}."
    echo "The pair is accepted and left in {{final_mode}} mode."

# Historical all-backend acceptance remains private for migration diagnosis.
# It is not used by setup or routine updates after independent releases.
[private]
[arg('stage', pattern='dev|staging|prod')]
_validator-acceptance-legacy stage release_tag attempts="3": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Error: all-backend acceptance is disabled; use validator-acceptance for one backend." >&2
    exit 2
    if [[ ! "{{release_tag}}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Error: release_tag must be vX.Y.Z." >&2
        exit 1
    fi
    if ! [[ "{{attempts}}" =~ ^[1-9][0-9]*$ ]] || [ "{{attempts}}" -gt 20 ]; then
        echo "Error: attempts must be between 1 and 20." >&2
        exit 1
    fi
    if [ "{{stage}}" = "prod" ] && [ "{{attempts}}" -ne 3 ]; then
        echo "Error: production acceptance requires exactly 3 attempts per backend." >&2
        exit 1
    fi

    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        QUEUE_NAME="${APP_NAME}-tasks"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider"
    else
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider-{{stage}}"
    fi
    ROUTES_ACTIVATED=0
    ACCEPTANCE_PASSED=0

    pause_acceptance_queues() {
        gcloud tasks queues pause "$QUEUE_NAME" \
            --location {{gcp_region}} --project {{gcp_project}} --quiet \
            >/dev/null 2>&1 || true
        gcloud tasks queues pause "$PROVIDER_QUEUE_NAME" \
            --location {{gcp_region}} --project {{gcp_project}} --quiet \
            >/dev/null 2>&1 || true
    }
    restore_acceptance_state() {
        local original_status=$?
        local cleanup_status=$original_status
        trap - EXIT INT TERM
        set +e
        pause_acceptance_queues
        if [ "$ACCEPTANCE_PASSED" != "1" ] && [ "$ROUTES_ACTIVATED" = "1" ]; then
            echo "Acceptance did not pass; restoring Job routes..."
            VALIDATOR_BACKEND_RELEASE_TAG={{release_tag}} \
                just gcp validator-services-rollback {{stage}} \
                || cleanup_status=1
        fi
        just gcp _enforce-maintenance {{stage}} || cleanup_status=1
        exit "$cleanup_status"
    }
    assert_queue_empty() {
        local queue="$1"
        local task
        task=$(gcloud tasks list \
            --queue "$queue" \
            --location {{gcp_region}} \
            --project {{gcp_project}} \
            --limit 1 \
            --format='value(name)')
        if [ -n "$task" ]; then
            echo "Error: queue $queue contains existing work; acceptance will not mix with it." >&2
            exit 1
        fi
    }
    assert_release_resources_exist() {
        local release_label="{{release_tag}}"
        release_label="${release_label//./-}"
        local missing=0
        local backend job job_release service service_release
        for backend in energyplus fmu shacl schematron portfolio-manager; do
            job="${APP_NAME}-validator-backend-${backend}"
            if [ "{{stage}}" != "prod" ]; then job="${job}-{{stage}}"; fi
            job_release=$(gcloud run jobs describe "$job" \
                --region {{gcp_region}} \
                --project {{gcp_project}} \
                --format='value(metadata.labels.release)' 2>/dev/null || true)
            if [ "$job_release" != "$release_label" ]; then
                echo "Missing candidate Job: $job (expected release $release_label)" >&2
                missing=1
            fi

            service="${APP_NAME}-validator-service-${backend}-${release_label}"
            if [ "{{stage}}" != "prod" ]; then service="${service}-{{stage}}"; fi
            service_release=$(gcloud run services describe "$service" \
                --region {{gcp_region}} \
                --project {{gcp_project}} \
                --format='value(metadata.labels.release)' 2>/dev/null || true)
            if [ "$service_release" != "$release_label" ]; then
                echo "Missing candidate Service: $service (expected release $release_label)" >&2
                missing=1
            fi
        done
        if [ "$missing" = "1" ]; then
            echo "Stage the complete signed backend release first:" >&2
            echo "  just gcp validator-deploy-all {{stage}} {{release_tag}}" >&2
            exit 1
        fi
    }

    just gcp _maintenance-assert-offline {{stage}}
    trap restore_acceptance_state EXIT
    trap 'exit 130' INT TERM
    assert_queue_empty "$QUEUE_NAME"
    assert_queue_empty "$PROVIDER_QUEUE_NAME"
    assert_release_resources_exist
    just gcp validator-release-verify {{release_tag}}
    just gcp validator-storage-isolation {{stage}}
    just gcp _maintenance-start-database {{stage}}

    # Refresh the retained Jobs from the same release before they become the
    # tested rollback route. This keeps acceptance and rollback on one patch.
    just gcp management-cmd {{stage}} "sync_gcp_validator_deployments --activate-primary"
    VALIDATOR_BACKEND_RELEASE_TAG={{release_tag}} \
        just gcp validator-services-activate {{stage}}
    ROUTES_ACTIVATED=1
    gcloud tasks queues resume "$QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} --quiet
    gcloud tasks queues resume "$PROVIDER_QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} --quiet

    GCP_MANAGEMENT_COMMAND_TIMEOUT=3600s \
        just gcp management-cmd {{stage}} \
        "run_validator_acceptance --release-tag={{release_tag}} --attempts={{attempts}} --timeout-seconds=3000 --require-persisted-report --ambient-isolation-verified"

    # Exercise the same operator rollback path used during an incident, then
    # return the accepted Services to primary and re-observe their live state.
    pause_acceptance_queues
    VALIDATOR_BACKEND_RELEASE_TAG={{release_tag}} \
        just gcp validator-services-rollback {{stage}}
    VALIDATOR_BACKEND_RELEASE_TAG={{release_tag}} \
        just gcp validator-services-activate {{stage}}
    just gcp management-cmd {{stage}} "verify_gcp_validator_deployments --json"

    ACCEPTANCE_PASSED=1
    echo "Validator acceptance passed for {{release_tag}} on {{stage}}."
    echo "The private JSON report is in the stage validation bucket."
    echo "Storage isolation and capability delivery are fixed deployment invariants; no configuration finalization is required."

# Reconcile the validator dashboard, log metrics, and alert policies. Policies
# create incidents even without a notification channel; set comma-separated
# full channel resource names to deliver email/SMS/PagerDuty notifications.
# Usage: just gcp validator-observability prod
[arg('stage', pattern='dev|staging|prod')]
validator-observability stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        PROVIDER_QUEUE="${APP_NAME}-validator-provider"
        WORKER_SERVICE="${APP_NAME}-worker"
        SERVICE_REGEX="${APP_NAME}-validator-service-(energyplus|fmu|shacl|schematron|portfolio-manager)-v[0-9]+-[0-9]+-[0-9]+"
    else
        PROVIDER_QUEUE="${APP_NAME}-validator-provider-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        SERVICE_REGEX="${APP_NAME}-validator-service-(energyplus|fmu|shacl|schematron|portfolio-manager)(-v[0-9]+-[0-9]+-[0-9]+)?-{{stage}}"
    fi
    CHANNELS_JSON=$(jq -cn --arg value "${VALIDATOR_MONITORING_NOTIFICATION_CHANNELS:-}" \
        '$value | split(",") | map(gsub("^[[:space:]]+|[[:space:]]+$"; "")) | map(select(length > 0))')
    if [ "$CHANNELS_JSON" = "[]" ]; then
        echo "WARNING: no VALIDATOR_MONITORING_NOTIFICATION_CHANNELS configured; incidents stay in Cloud Monitoring." >&2
    fi

    upsert_log_metric() {
        local metric_name="$1"
        local description="$2"
        local event_name="$3"
        local log_filter="resource.type=\"cloud_run_revision\" resource.labels.service_name=\"$WORKER_SERVICE\" jsonPayload.event=\"$event_name\""
        if gcloud logging metrics describe "$metric_name" --project={{gcp_project}} >/dev/null 2>&1; then
            gcloud logging metrics update "$metric_name" \
                --project={{gcp_project}} \
                --description="$description" \
                --log-filter="$log_filter" \
                --quiet >/dev/null
        else
            gcloud logging metrics create "$metric_name" \
                --project={{gcp_project}} \
                --description="$description" \
                --log-filter="$log_filter" \
                --quiet >/dev/null
        fi
        echo "Log metric reconciled: $metric_name"
    }

    upsert_policy() {
        local display_name="$1"
        local condition_name="$2"
        local filter="$3"
        local threshold="$4"
        local duration="$5"
        local aligner="$6"
        local reducer="${7:-}"
        local aggregation
        local policy
        local existing
        aggregation=$(jq -cn \
            --arg aligner "$aligner" \
            --arg reducer "$reducer" \
            '{alignmentPeriod:"60s",perSeriesAligner:$aligner} + if $reducer == "" then {} else {crossSeriesReducer:$reducer} end')
        policy=$(jq -cn \
            --arg display_name "$display_name" \
            --arg condition_name "$condition_name" \
            --arg filter "$filter" \
            --arg duration "$duration" \
            --arg stage "{{stage}}" \
            --argjson threshold "$threshold" \
            --argjson aggregation "$aggregation" \
            --argjson channels "$CHANNELS_JSON" \
            '{displayName:$display_name,combiner:"OR",enabled:true,notificationChannels:$channels,userLabels:{component:"validator-execution",stage:$stage},conditions:[{displayName:$condition_name,conditionThreshold:{filter:$filter,comparison:"COMPARISON_GT",thresholdValue:$threshold,duration:$duration,aggregations:[$aggregation],trigger:{count:1}}}],documentation:{mimeType:"text/markdown",content:"Managed by `just gcp validator-observability`. Inspect the validator execution dashboard and rollout runbook before changing routing or capacity."}}')
        existing=$(gcloud monitoring policies list \
            --project={{gcp_project}} \
            --filter="displayName=\"$display_name\"" \
            --format='value(name)')
        if [ "$(printf '%s\n' "$existing" | sed '/^$/d' | wc -l | tr -d ' ')" -gt 1 ]; then
            echo "Error: duplicate Monitoring policies named '$display_name'." >&2
            exit 1
        fi
        if [ -n "$existing" ]; then
            gcloud monitoring policies update "$existing" \
                --project={{gcp_project}} \
                --policy="$policy" \
                --quiet >/dev/null
        else
            gcloud monitoring policies create \
                --project={{gcp_project}} \
                --policy="$policy" \
                --quiet >/dev/null
        fi
        echo "Alert policy reconciled: $display_name"
    }

    CALLBACK_METRIC="validibot_validator_callback_failure_{{stage}}"
    TIMEOUT_METRIC="validibot_validator_attempt_timeout_{{stage}}"
    DRIFT_METRIC="validibot_validator_deployment_drift_{{stage}}"
    upsert_log_metric "$CALLBACK_METRIC" "Validator callback processing failures ({{stage}})" "validator_callback_failure"
    upsert_log_metric "$TIMEOUT_METRIC" "Validator attempts reaching their absolute deadline ({{stage}})" "validator_attempt_timeout"
    upsert_log_metric "$DRIFT_METRIC" "Primary validator Service deployment drift ({{stage}})" "validator_deployment_drift"

    QUEUE_FILTER="metric.type=\"cloudtasks.googleapis.com/queue/depth\" resource.type=\"cloud_tasks_queue\" resource.labels.queue_id=\"$PROVIDER_QUEUE\" resource.labels.location=\"{{gcp_region}}\""
    QUEUE_ERROR_FILTER="metric.type=\"cloudtasks.googleapis.com/queue/task_attempt_count\" resource.type=\"cloud_tasks_queue\" resource.labels.queue_id=\"$PROVIDER_QUEUE\" resource.labels.location=\"{{gcp_region}}\" metric.labels.response_code!=\"ok\""
    PENDING_LATENCY_FILTER="metric.type=\"run.googleapis.com/request_latency/pending\" resource.type=\"cloud_run_revision\" resource.labels.service_name=monitoring.regex.full_match(\"$SERVICE_REGEX\")"
    PENDING_FILTER="metric.type=\"run.googleapis.com/pending_queue/pending_requests\" resource.type=\"cloud_run_revision\" resource.labels.service_name=monitoring.regex.full_match(\"$SERVICE_REGEX\")"
    IDLE_FILTER="metric.type=\"run.googleapis.com/container/instance_count\" resource.type=\"cloud_run_revision\" resource.labels.service_name=monitoring.regex.full_match(\"$SERVICE_REGEX\") metric.labels.state=\"idle\""
    upsert_policy "Validibot {{stage}} validator provider backlog" "Provider queue depth exceeds 10" "$QUEUE_FILTER" 10 300s ALIGN_MAX
    upsert_policy "Validibot {{stage}} validator provider delivery errors" "Provider task returned an error" "$QUEUE_ERROR_FILTER" 0 0s ALIGN_SUM
    upsert_policy "Validibot {{stage}} validator startup latency" "Service pending/startup p95 exceeds target" "$PENDING_LATENCY_FILTER" "${VALIDATOR_SERVICE_PENDING_P95_ALERT_MS:-30000}" 300s ALIGN_PERCENTILE_95
    upsert_policy "Validibot {{stage}} validator saturation" "Service requests remain pending" "$PENDING_FILTER" 0 300s ALIGN_MAX
    upsert_policy "Validibot {{stage}} unexpected warm validator capacity" "Idle instances exceed the approved total" "$IDLE_FILTER" "${VALIDATOR_SERVICE_EXPECTED_MAX_IDLE_INSTANCES:-5}" 900s ALIGN_MAX REDUCE_SUM
    upsert_policy "Validibot {{stage}} validator callback failures" "Callback processing failed" "metric.type=\"logging.googleapis.com/user/$CALLBACK_METRIC\" resource.type=\"cloud_run_revision\"" 0 0s ALIGN_SUM
    upsert_policy "Validibot {{stage}} validator attempt timeouts" "Attempt reached its absolute deadline" "metric.type=\"logging.googleapis.com/user/$TIMEOUT_METRIC\" resource.type=\"cloud_run_revision\"" 0 0s ALIGN_SUM
    upsert_policy "Validibot {{stage}} validator deployment drift" "Hourly provider verification detected drift" "metric.type=\"logging.googleapis.com/user/$DRIFT_METRIC\" resource.type=\"cloud_run_revision\"" 0 0s ALIGN_SUM

    DASHBOARD_TEMPLATE="just/gcp/monitoring/validator-execution-dashboard.json"
    DASHBOARD_CONFIG=$(mktemp)
    DASHBOARD_UPDATE=$(mktemp)
    trap 'rm -f "$DASHBOARD_CONFIG" "$DASHBOARD_UPDATE"' EXIT
    # Render through jq instead of sed: the service regex deliberately
    # contains alternation pipes, which are unsafe in delimiter-based sed
    # substitutions and fail on BSD sed (the default on macOS).
    jq \
        --arg stage "{{stage}}" \
        --arg region "{{gcp_region}}" \
        --arg provider_queue "$PROVIDER_QUEUE" \
        --arg service_regex "$SERVICE_REGEX" \
        'walk(if type == "string" then
            gsub("__STAGE__"; $stage)
            | gsub("__REGION__"; $region)
            | gsub("__PROVIDER_QUEUE__"; $provider_queue)
            | gsub("__SERVICE_REGEX__"; $service_regex)
        else . end)' \
        "$DASHBOARD_TEMPLATE" > "$DASHBOARD_CONFIG"
    DASHBOARD_DISPLAY="Validibot {{stage}} validator execution"
    DASHBOARD_NAME=$(gcloud monitoring dashboards list \
        --project={{gcp_project}} \
        --filter="displayName=\"$DASHBOARD_DISPLAY\"" \
        --format='value(name)')
    if [ "$(printf '%s\n' "$DASHBOARD_NAME" | sed '/^$/d' | wc -l | tr -d ' ')" -gt 1 ]; then
        echo "Error: duplicate Monitoring dashboards named '$DASHBOARD_DISPLAY'." >&2
        exit 1
    fi
    if [ -n "$DASHBOARD_NAME" ]; then
        DASHBOARD_ETAG=$(gcloud monitoring dashboards describe "$DASHBOARD_NAME" \
            --project={{gcp_project}} \
            --format='value(etag)')
        jq --arg etag "$DASHBOARD_ETAG" '.etag=$etag' "$DASHBOARD_CONFIG" > "$DASHBOARD_UPDATE"
        gcloud monitoring dashboards update "$DASHBOARD_NAME" \
            --project={{gcp_project}} \
            --config-from-file="$DASHBOARD_UPDATE" \
            --quiet >/dev/null
    else
        gcloud monitoring dashboards create \
            --project={{gcp_project}} \
            --config-from-file="$DASHBOARD_CONFIG" \
            --quiet >/dev/null
    fi
    echo "Dashboard reconciled: $DASHBOARD_DISPLAY"

# Exercise the real GCS Credential Access Boundary token with the deployed
# Django image and trusted stage identity. The management command creates only
# a random probe prefix, checks allowed/denied operations, and cleans the prefix.
# Usage: just gcp validator-storage-capability-probe dev|staging|prod
[arg('stage', pattern='dev|staging|prod')]
validator-storage-capability-probe stage: _require-gcp-config
    just gcp management-cmd {{stage}} "probe_validator_gcs_capability --json"

# Reconcile and prove the mandatory absence of ambient validator GCS access.
# Fresh stages never grant it; repeated runs remove historical bindings before
# Policy Troubleshooter checks inherited, group, primitive, custom, and
# conditional access paths.
# Usage: just gcp validator-storage-isolation dev|staging|prod
[arg('stage', pattern='dev|staging|prod')]
validator-storage-isolation stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        STORAGE_BUCKET="${APP_NAME}-storage"
        VALIDATOR_SA_EMAIL="${APP_NAME}-validator-prod@{{gcp_project}}.iam.gserviceaccount.com"
    else
        STORAGE_BUCKET="${APP_NAME}-storage-{{stage}}"
        VALIDATOR_SA_EMAIL="${APP_NAME}-validator-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
    fi

    echo "Removing historical validator storage bindings..."
    gcloud storage buckets remove-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="serviceAccount:$VALIDATOR_SA_EMAIL" \
        --role="roles/storage.objectAdmin" \
        --condition=None \
        --project={{gcp_project}} \
        --quiet >/dev/null 2>&1 || true
    gcloud storage buckets remove-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="serviceAccount:$VALIDATOR_SA_EMAIL" \
        --role="roles/storage.objectAdmin" \
        --condition="expression=resource.name.startsWith(\"projects/_/buckets/$STORAGE_BUCKET/objects/runs/\"),title=validator-runs-prefix-only" \
        --project={{gcp_project}} \
        --quiet >/dev/null 2>&1 || true

    PROJECT_STORAGE_ROLES=$(gcloud projects get-iam-policy {{gcp_project}} \
        --flatten="bindings[].members" \
        --filter="bindings.members=serviceAccount:$VALIDATOR_SA_EMAIL AND bindings.role:roles/storage" \
        --format="value(bindings.role)" 2>/dev/null || true)
    BUCKET_STORAGE_ROLES=$(gcloud storage buckets get-iam-policy "gs://$STORAGE_BUCKET" \
        --flatten="bindings[].members" \
        --filter="bindings.members=serviceAccount:$VALIDATOR_SA_EMAIL AND bindings.role:roles/storage" \
        --format="value(bindings.role)" 2>/dev/null || true)
    if [ -n "$PROJECT_STORAGE_ROLES" ] || [ -n "$BUCKET_STORAGE_ROLES" ]; then
        echo "ERROR: validator service account still has storage roles:"
        printf '%s\n%s\n' "$PROJECT_STORAGE_ROLES" "$BUCKET_STORAGE_ROLES" | sed '/^$/d'
        echo "Remove those bindings before declaring runtime storage isolation complete."
        exit 1
    fi

    echo "Checking effective validator permissions with Policy Troubleshooter..."
    gcloud services enable policytroubleshooter.googleapis.com \
        --project={{gcp_project}} \
        --quiet
    BUCKET_RESOURCE="//storage.googleapis.com/projects/_/buckets/$STORAGE_BUCKET"
    BUCKET_CONTEXT="projects/_/buckets/$STORAGE_BUCKET"
    OBJECT_CONTEXT="$BUCKET_CONTEXT/objects/runs/validibot-capability-probes/effective-access-check"
    REQUEST_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    check_effective_permission() {
        local permission="$1"
        local resource_name="$2"
        local resource_type="$3"
        local state
        state=$(gcloud policy-intelligence troubleshoot-policy iam \
            "$BUCKET_RESOURCE" \
            --principal-email="$VALIDATOR_SA_EMAIL" \
            --permission="$permission" \
            --resource-name="$resource_name" \
            --resource-service="storage.googleapis.com" \
            --resource-type="$resource_type" \
            --request-time="$REQUEST_TIME" \
            --project={{gcp_project}} \
            --format="value(overallAccessState)")
        echo "  $permission: $state"
        if [ "$state" != "CANNOT_ACCESS" ]; then
            echo "ERROR: effective permission $permission was not conclusively denied."
            echo "Policy Troubleshooter returned: ${state:-<empty>}"
            exit 1
        fi
    }

    check_effective_permission "storage.objects.get" "$OBJECT_CONTEXT" "storage.googleapis.com/Object"
    check_effective_permission "storage.objects.create" "$OBJECT_CONTEXT" "storage.googleapis.com/Object"
    check_effective_permission "storage.objects.delete" "$OBJECT_CONTEXT" "storage.googleapis.com/Object"
    check_effective_permission "storage.objects.update" "$OBJECT_CONTEXT" "storage.googleapis.com/Object"
    check_effective_permission "storage.objects.list" "$BUCKET_CONTEXT" "storage.googleapis.com/Bucket"

    echo "Verified: $VALIDATOR_SA_EMAIL has no effective object read/list/create/update/delete permission."
    echo "Also run the downscoped-token provider probe before enabling traffic:"
    echo "  just gcp validator-storage-capability-probe {{stage}}"

# =============================================================================
# Validator Assets Management
# =============================================================================
#
# Validator assets (weather files, etc.) are stored in GCS under:
#   gs://{bucket}/validator_assets/{asset_type}/
#
# Weather data files go in the weather_data subdirectory.
# Each environment has its own bucket.
#
# =============================================================================

# Sync weather data files from local ../weather_data directory to GCS
# Usage: just gcp sync-weather dev|staging|prod|all
sync-weather env:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    WEATHER_SOURCE="../weather_data"
    ASSETS_PREFIX="validator_assets/weather_data"

    if [ ! -d "$WEATHER_SOURCE" ]; then
        echo "Error: Weather data directory not found: $WEATHER_SOURCE"
        echo "Please create the directory and add EPW files to sync."
        exit 1
    fi

    EPW_COUNT=$(find "$WEATHER_SOURCE" -name "*.epw" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$EPW_COUNT" -eq 0 ]; then
        echo "Error: No EPW files found in $WEATHER_SOURCE"
        exit 1
    fi
    echo "Found $EPW_COUNT EPW file(s) to sync"

    sync_to_bucket() {
        local bucket=$1
        local env_name=$2
        local gcs_path="gs://$bucket/$ASSETS_PREFIX/"
        echo ""
        echo "Syncing to $env_name bucket: $gcs_path"

        if ! gsutil -m rsync -r "$WEATHER_SOURCE/" "$gcs_path"; then
            echo "ERROR: gsutil rsync failed for $env_name"
            exit 1
        fi

        local remote_count
        remote_count=$(gsutil ls "$gcs_path*.epw" 2>/dev/null | wc -l | tr -d ' ')

        if [ "$remote_count" -eq 0 ]; then
            echo "ERROR: No EPW files found in bucket after sync"
            exit 1
        fi

        if [ "$remote_count" -ne "$EPW_COUNT" ]; then
            echo "WARNING: Local has $EPW_COUNT files, bucket has $remote_count files"
        fi

        echo "$env_name sync complete ($remote_count files in bucket)"
    }

    case "{{env}}" in
        dev)
            sync_to_bucket "${APP_NAME}-storage-dev" "dev"
            ;;
        staging)
            sync_to_bucket "${APP_NAME}-storage-staging" "staging"
            ;;
        prod)
            echo "WARNING: You are about to sync weather files to PRODUCTION"
            read -p "Are you sure? (yes/no): " confirm
            if [ "$confirm" != "yes" ]; then
                echo "Aborted."
                exit 1
            fi
            sync_to_bucket "${APP_NAME}-storage" "prod"
            ;;
        all)
            sync_to_bucket "${APP_NAME}-storage-dev" "dev"
            sync_to_bucket "${APP_NAME}-storage-staging" "staging"
            echo ""
            echo "WARNING: About to sync to PRODUCTION"
            read -p "Continue with prod sync? (yes/no): " confirm
            if [ "$confirm" == "yes" ]; then
                sync_to_bucket "${APP_NAME}-storage" "prod"
            else
                echo "Skipped prod sync."
            fi
            ;;
        *)
            echo "Error: Invalid environment '{{env}}'"
            echo "Usage: just gcp sync-weather dev|staging|prod|all"
            exit 1
            ;;
    esac

    echo ""
    echo "Weather data sync complete"

# List weather files in a bucket
# Usage: just gcp list-weather dev|staging|prod
list-weather env:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    ASSETS_PREFIX="validator_assets/weather_data"

    case "{{env}}" in
        dev)
            BUCKET="${APP_NAME}-storage-dev"
            ;;
        staging)
            BUCKET="${APP_NAME}-storage-staging"
            ;;
        prod)
            BUCKET="${APP_NAME}-storage"
            ;;
        *)
            echo "Error: Invalid environment '{{env}}'"
            echo "Usage: just gcp list-weather dev|staging|prod"
            exit 1
            ;;
    esac

    echo "Weather files in gs://$BUCKET/$ASSETS_PREFIX/:"
    gsutil ls -l "gs://$BUCKET/$ASSETS_PREFIX/" 2>/dev/null || echo "  (no files found)"

# =============================================================================
# Maintenance Mode
# =============================================================================
#
# MAINTENANCE is an enforced runtime-isolated state, not merely a traffic hint:
# public ingress is blocked, all services have a zero minimum, queues and
# schedulers are paused, and Cloud SQL is RUNNABLE for operator work. PARKED
# adds an explicit database stop for longer idle periods. Ordinary maintenance
# work never stops Cloud SQL, and EXIT traps restore runtime isolation without
# changing the database state.

[private]
_maintenance-require-database stage:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then DB_INSTANCE="${APP_NAME}-db"; else DB_INSTANCE="${APP_NAME}-db-{{stage}}"; fi
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} --format="value(state)" 2>/dev/null || echo "unknown")
    ACTIVE_OPERATION=$(gcloud sql operations list \
        --instance "$DB_INSTANCE" --project {{gcp_project}} \
        --filter="status=RUNNING" --limit=1 --format="value(name)" 2>/dev/null || echo "unknown")
    if [ "$DB_STATUS" != "RUNNABLE" ] || [ -n "$ACTIVE_OPERATION" ]; then
        echo "Error: Cloud SQL instance $DB_INSTANCE is not ready (state=$DB_STATUS active-operation=${ACTIVE_OPERATION:-none})." >&2
        echo "Inspect the lifecycle with 'just gcp mode-status {{stage}}'." >&2
        echo "If the stage is intentionally offline, use 'just gcp mode-maintenance {{stage}}'." >&2
        exit 1
    fi

[private]
_maintenance-start-database stage:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then DB_INSTANCE="${APP_NAME}-db"; else DB_INSTANCE="${APP_NAME}-db-{{stage}}"; fi
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$DB_STATUS" = "unknown" ]; then
        echo "Error: Cloud SQL instance $DB_INSTANCE was not found." >&2
        exit 1
    fi
    DB_POLICY=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} --format="value(settings.activationPolicy)")
    DEADLINE=$((SECONDS + ${GCP_SQL_TRANSITION_TIMEOUT_SECONDS:-1800}))
    if [ "$DB_POLICY" != "ALWAYS" ]; then
        echo "Starting Cloud SQL instance $DB_INSTANCE..."
        PATCH_SUBMITTED=0
        while [ "$SECONDS" -lt "$DEADLINE" ]; do
            if PATCH_OUTPUT=$(gcloud sql instances patch "$DB_INSTANCE" \
                --activation-policy ALWAYS --project {{gcp_project}} --quiet --async 2>&1); then
                PATCH_SUBMITTED=1
                break
            fi
            if [[ "$PATCH_OUTPUT" != *"another operation was already in progress"* ]]; then
                echo "$PATCH_OUTPUT" >&2
                exit 1
            fi
            echo "Waiting for the active Cloud SQL operation before starting $DB_INSTANCE..."
            sleep 10
        done
        if [ "$PATCH_SUBMITTED" != "1" ]; then
            echo "Error: could not submit the Cloud SQL start operation within the transition timeout." >&2
            exit 1
        fi
    fi
    while [ "$SECONDS" -lt "$DEADLINE" ]; do
        DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
            --project {{gcp_project}} --format="value(state)" 2>/dev/null || echo "unknown")
        ACTIVE_OPERATION=$(gcloud sql operations list \
            --instance "$DB_INSTANCE" --project {{gcp_project}} \
            --filter="status=RUNNING" --limit=1 --format="value(name)" 2>/dev/null || echo "unknown")
        if [ "$DB_STATUS" = "RUNNABLE" ] && [ -z "$ACTIVE_OPERATION" ]; then
            echo "Cloud SQL instance $DB_INSTANCE is ready."
            exit 0
        fi
        echo "Waiting for Cloud SQL (state=$DB_STATUS active-operation=${ACTIVE_OPERATION:-none})..."
        sleep 10
    done
    echo "Error: Cloud SQL instance $DB_INSTANCE did not become RUNNABLE within the transition timeout." >&2
    exit 1

[private]
_maintenance-assert-offline stage:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
        MCP_SERVICE="${APP_NAME}-mcp"
        QUEUE_NAME="${APP_NAME}-tasks"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        MCP_SERVICE="${APP_NAME}-mcp-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider-{{stage}}"
        JOB_SUFFIX="-{{stage}}"
    fi
    ISOLATION_ERRORS=0
    record_isolation_error() {
        echo "Error: $*" >&2
        ISOLATION_ERRORS=$((ISOLATION_ERRORS + 1))
    }
    check_isolated_service() {
        local service="$1"
        local expect_mcp_disabled="${2:-0}"
        local required="${3:-0}"
        local service_json ingress service_min revision_min mcp_enabled
        if ! service_json=$(gcloud run services describe "$service" \
            --region {{gcp_region}} --project {{gcp_project}} --format=json 2>/dev/null); then
            if [ "$required" = "1" ]; then
                record_isolation_error "required Cloud Run service $service could not be inspected."
            fi
            return
        fi
        ingress=$(jq -r '.metadata.annotations["run.googleapis.com/ingress"] // "unknown"' <<< "$service_json")
        service_min=$(jq -r '.spec.scaling.minInstanceCount // .metadata.annotations["run.googleapis.com/minScale"] // "0"' <<< "$service_json")
        revision_min=$(jq -r '.spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // "0"' <<< "$service_json")
        mcp_enabled=$(jq -r '[.spec.template.spec.containers[0].env[]? | select(.name == "VALIDIBOT_MCP_ENABLED") | .value][0] // "true"' <<< "$service_json")
        if [ "$ingress" != "internal" ] || [ "$service_min" != "0" ] || [ "$revision_min" != "0" ] || { [ "$expect_mcp_disabled" = "1" ] && [ "$mcp_enabled" != "false" ]; }; then
            if [ "$expect_mcp_disabled" = "1" ]; then
                record_isolation_error "$service is not isolated (ingress=$ingress service-min=$service_min revision-min=$revision_min VALIDIBOT_MCP_ENABLED=$mcp_enabled)."
            else
                record_isolation_error "$service is not isolated (ingress=$ingress service-min=$service_min revision-min=$revision_min)."
            fi
        fi
    }
    check_isolated_service "$WEB_SERVICE" 0 1
    check_isolated_service "$WORKER_SERVICE" 0 1
    check_isolated_service "$MCP_SERVICE" 1
    if SERVICES=$(gcloud run services list \
        --region {{gcp_region}} --project {{gcp_project}} \
        --filter="metadata.labels.stage:{{stage}} AND metadata.labels.validator:*" \
        --format='value(metadata.name)'); then
        while IFS= read -r service; do
            [ -z "$service" ] && continue
            check_isolated_service "$service"
        done <<< "$SERVICES"
    else
        record_isolation_error "validator Cloud Run services could not be listed."
    fi
    QUEUE_STATE=$(gcloud tasks queues describe "$QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
    PROVIDER_STATE=$(gcloud tasks queues describe "$PROVIDER_QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
    if [ "$QUEUE_STATE" != "PAUSED" ]; then
        record_isolation_error "main queue is $QUEUE_STATE, not PAUSED."
    fi
    if [ "$PROVIDER_STATE" != "PAUSED" ] && [ "$PROVIDER_STATE" != "NOT_FOUND" ]; then
        record_isolation_error "provider queue is $PROVIDER_STATE, not PAUSED."
    fi
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-purge-expired-outputs ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs ${APP_NAME}-verify-validator-deployments ${APP_NAME}-send-periodic-emails ${APP_NAME}-enforce-audit-retention; do
        job="${job_base}${JOB_SUFFIX}"
        state=$(gcloud scheduler jobs describe "$job" \
            --project {{gcp_project}} --location {{gcp_region}} \
            --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
        if [ "$state" != "PAUSED" ] && [ "$state" != "NOT_FOUND" ]; then
            record_isolation_error "scheduler job $job is $state, not PAUSED."
        fi
    done
    if [ "$ISOLATION_ERRORS" != "0" ]; then
        echo "Error: {{stage}} runtime is not safely isolated ($ISOLATION_ERRORS issue(s))." >&2
        exit 1
    fi
    echo "Confirmed {{stage}} runtime is isolated."

[private]
_enforce-maintenance stage:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
        MCP_SERVICE="${APP_NAME}-mcp"
        QUEUE_NAME="${APP_NAME}-tasks"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        MCP_SERVICE="${APP_NAME}-mcp-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider-{{stage}}"
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Enforcing maintenance mode on {{stage}}..."
    MAINTENANCE_ERRORS=0
    record_maintenance_error() {
        echo "Error: $*" >&2
        MAINTENANCE_ERRORS=$((MAINTENANCE_ERRORS + 1))
    }
    ensure_offline_service() {
        local service="$1"
        local disable_mcp="${2:-0}"
        local required="${3:-0}"
        local service_json ingress service_min revision_min mcp_enabled
        if ! service_json=$(gcloud run services describe "$service" \
            --region {{gcp_region}} --project {{gcp_project}} --format=json 2>/dev/null); then
            if [ "$required" = "1" ]; then
                record_maintenance_error "required Cloud Run service $service could not be inspected."
                return
            fi
            echo "   Service $service is not present; skipping."
            return
        fi
        ingress=$(jq -r '.metadata.annotations["run.googleapis.com/ingress"] // "unknown"' <<< "$service_json")
        service_min=$(jq -r '.spec.scaling.minInstanceCount // .metadata.annotations["run.googleapis.com/minScale"] // "0"' <<< "$service_json")
        revision_min=$(jq -r '.spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // "0"' <<< "$service_json")
        mcp_enabled=$(jq -r '[.spec.template.spec.containers[0].env[]? | select(.name == "VALIDIBOT_MCP_ENABLED") | .value][0] // "true"' <<< "$service_json")
        if [ "$ingress" = "internal" ] && [ "$service_min" = "0" ] && [ "$revision_min" = "0" ] && { [ "$disable_mcp" != "1" ] || [ "$mcp_enabled" = "false" ]; }; then
            echo "   Service $service is already internal with zero minimum capacity."
            return
        fi
        update_args=(
            gcloud run services update "$service"
            --region {{gcp_region}} --project {{gcp_project}}
            --ingress internal --min=0 --min-instances=default --quiet
        )
        if [ "$disable_mcp" = "1" ]; then
            update_args+=(--update-env-vars VALIDIBOT_MCP_ENABLED=false)
        fi
        if ! "${update_args[@]}"; then
            record_maintenance_error "could not make Cloud Run service $service maintenance-safe."
        fi
    }
    ensure_validator_service_safe() {
        local service="$1"
        local service_json ingress service_min revision_min
        if ! service_json=$(gcloud run services describe "$service" \
            --region {{gcp_region}} --project {{gcp_project}} --format=json 2>/dev/null); then
            record_maintenance_error "validator Cloud Run service $service could not be inspected."
            return
        fi
        ingress=$(jq -r '.metadata.annotations["run.googleapis.com/ingress"] // "unknown"' <<< "$service_json")
        service_min=$(jq -r '.spec.scaling.minInstanceCount // .metadata.annotations["run.googleapis.com/minScale"] // "0"' <<< "$service_json")
        revision_min=$(jq -r '.spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // "0"' <<< "$service_json")
        if [ "$ingress" != "internal" ] || [ "$revision_min" != "0" ]; then
            record_maintenance_error "$service has immutable definition drift (ingress=$ingress revision-min=$revision_min); refusing to create an unaccepted revision."
            return
        fi
        if [ "$service_min" = "0" ]; then
            echo "   Validator Service $service is internal with zero minimum capacity."
            return
        fi
        echo "   Cooling validator Service $service to service-level min=0."
        if ! gcloud run services update "$service" \
            --region {{gcp_region}} --project {{gcp_project}} \
            --min=0 --quiet; then
            record_maintenance_error "could not cool validator Cloud Run service $service."
        fi
    }

    # A failed desired revision can make even a no-op Cloud Run update fail.
    # Inspect first so already-safe services are left alone, and aggregate any
    # update failures so every remaining runtime and work safeguard is still
    # attempted before this recipe returns an error.
    ensure_offline_service "$WEB_SERVICE" 0 1
    ensure_offline_service "$WORKER_SERVICE" 0 1
    ensure_offline_service "$MCP_SERVICE" 1
    if SERVICES=$(gcloud run services list \
        --region {{gcp_region}} --project {{gcp_project}} \
        --filter="metadata.labels.stage:{{stage}} AND metadata.labels.validator:*" \
        --format='value(metadata.name)'); then
        while IFS= read -r service; do
            [ -z "$service" ] && continue
            ensure_validator_service_safe "$service"
        done <<< "$SERVICES"
    else
        record_maintenance_error "could not list validator Cloud Run services."
    fi

    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-purge-expired-outputs ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs ${APP_NAME}-verify-validator-deployments ${APP_NAME}-send-periodic-emails ${APP_NAME}-enforce-audit-retention; do
        job="${job_base}${JOB_SUFFIX}"
        scheduler_state=$(gcloud scheduler jobs describe "$job" \
            --project {{gcp_project}} --location {{gcp_region}} \
            --format='value(state)' 2>/dev/null || echo "NOT_FOUND")
        case "$scheduler_state" in
            PAUSED) echo "   Scheduler job $job is already paused." ;;
            ENABLED)
                if ! gcloud scheduler jobs pause "$job" \
                    --project {{gcp_project}} --location {{gcp_region}} --quiet; then
                    record_maintenance_error "could not pause scheduler job $job."
                fi
                ;;
            NOT_FOUND) ;;
            *) record_maintenance_error "scheduler job $job has unexpected state $scheduler_state." ;;
        esac
    done
    pause_queue() {
        local queue="$1"
        local required="$2"
        local state
        state=$(gcloud tasks queues describe "$queue" \
            --location {{gcp_region}} --project {{gcp_project}} \
            --format='value(state)' 2>/dev/null || echo "NOT_FOUND")
        case "$state" in
            PAUSED) echo "   Task queue $queue is already paused." ;;
            RUNNING)
                if ! gcloud tasks queues pause "$queue" \
                    --location {{gcp_region}} --project {{gcp_project}} --quiet; then
                    record_maintenance_error "could not pause task queue $queue."
                fi
                ;;
            NOT_FOUND)
                if [ "$required" = "1" ]; then
                    record_maintenance_error "required task queue $queue is missing."
                fi
                ;;
            *) record_maintenance_error "task queue $queue has unexpected state $state." ;;
        esac
    }
    pause_queue "$QUEUE_NAME" 1
    pause_queue "$PROVIDER_QUEUE_NAME" 0

    if [ "$MAINTENANCE_ERRORS" != "0" ]; then
        echo "Maintenance enforcement completed all possible safeguards with $MAINTENANCE_ERRORS error(s)." >&2
        exit 1
    fi
    echo "{{stage}} runtime is isolated: services min=0/internal, work paused, database state unchanged."

[private]
_maintenance-stop-database stage:
    #!/usr/bin/env bash
    set -euo pipefail
    just gcp _maintenance-assert-offline {{stage}}
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then DB_INSTANCE="${APP_NAME}-db"; else DB_INSTANCE="${APP_NAME}-db-{{stage}}"; fi
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$DB_STATUS" = "unknown" ]; then
        echo "Error: Cloud SQL instance $DB_INSTANCE was not found." >&2
        exit 1
    fi
    DB_POLICY=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} --format="value(settings.activationPolicy)")
    DEADLINE=$((SECONDS + ${GCP_SQL_TRANSITION_TIMEOUT_SECONDS:-1800}))
    if [ "$DB_POLICY" != "NEVER" ]; then
        echo "Stopping Cloud SQL instance $DB_INSTANCE..."
        PATCH_SUBMITTED=0
        while [ "$SECONDS" -lt "$DEADLINE" ]; do
            if PATCH_OUTPUT=$(gcloud sql instances patch "$DB_INSTANCE" \
                --activation-policy NEVER --project {{gcp_project}} --quiet --async 2>&1); then
                PATCH_SUBMITTED=1
                break
            fi
            if [[ "$PATCH_OUTPUT" != *"another operation was already in progress"* ]]; then
                echo "$PATCH_OUTPUT" >&2
                exit 1
            fi
            echo "Waiting for the active Cloud SQL operation before stopping $DB_INSTANCE..."
            sleep 10
        done
        if [ "$PATCH_SUBMITTED" != "1" ]; then
            echo "Error: could not submit the Cloud SQL stop operation within the transition timeout." >&2
            exit 1
        fi
    fi
    while [ "$SECONDS" -lt "$DEADLINE" ]; do
        DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
            --project {{gcp_project}} --format="value(state)" 2>/dev/null || echo "unknown")
        ACTIVE_OPERATION=$(gcloud sql operations list \
            --instance "$DB_INSTANCE" --project {{gcp_project}} \
            --filter="status=RUNNING" --limit=1 --format="value(name)" 2>/dev/null || echo "unknown")
        if [ "$DB_STATUS" = "STOPPED" ] && [ -z "$ACTIVE_OPERATION" ]; then
            echo "Cloud SQL instance $DB_INSTANCE is stopped."
            exit 0
        fi
        echo "Waiting for Cloud SQL to stop (state=$DB_STATUS active-operation=${ACTIVE_OPERATION:-none})..."
        sleep 10
    done
    echo "Error: Cloud SQL instance $DB_INSTANCE did not become STOPPED within the transition timeout." >&2
    exit 1

# Lifecycle states are reconciled rather than toggled. Every successful public
# transition verifies its requested target, while any error or interruption
# restores maintenance isolation before returning the original failure status.

# Reconcile a stage to MAINTENANCE: runtime isolated, work paused, database
# RUNNABLE. Cloud SQL remains billable in this state for consecutive operator
# tasks; use mode-parked when the stage will be idle for a longer period.
[arg('stage', pattern='dev|staging|prod')]
mode-maintenance stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ] && [ "${GCP_YES:-0}" != "1" ]; then
        echo "WARNING: This will take PRODUCTION offline while Cloud SQL remains running and billable."
        read -r -p "Type 'maintenance' to confirm: " REPLY
        if [ "$REPLY" != "maintenance" ]; then echo "Cancelled."; exit 1; fi
    fi
    cleanup() {
        local status=$?
        trap - EXIT INT TERM
        if [ "$status" -ne 0 ]; then
            if ! just gcp _enforce-maintenance {{stage}}; then
                echo "Error: failed to restore maintenance isolation after the interrupted transition." >&2
                exit 1
            fi
        fi
        exit "$status"
    }
    trap cleanup EXIT INT TERM
    just gcp _enforce-maintenance {{stage}}
    just gcp _maintenance-start-database {{stage}}
    MODE=$(just gcp _mode-current {{stage}})
    if [ "$MODE" != "MAINTENANCE" ]; then
        echo "Error: {{stage}} did not converge to MAINTENANCE (current mode: $MODE)." >&2
        exit 1
    fi
    trap - EXIT INT TERM
    echo "{{stage}} is in MAINTENANCE: runtime isolated, work paused, database available."
    echo "Cloud SQL is still running and billed; use 'just gcp mode-parked {{stage}}' when it can be stopped."

# Reconcile a stage to PARKED. This takes a live stage offline before stopping
# Cloud SQL, so it is safe to use from LIVE, MAINTENANCE, PARKED, or a partial
# transition.
[arg('stage', pattern='dev|staging|prod')]
mode-parked stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ] && [ "${GCP_YES:-0}" != "1" ]; then
        echo "WARNING: This will take LIVE PRODUCTION offline, isolate runtime and work, and stop Cloud SQL."
        read -r -p "Type 'park' to confirm: " REPLY
        if [ "$REPLY" != "park" ]; then echo "Cancelled."; exit 1; fi
    fi
    cleanup() {
        local status=$?
        trap - EXIT INT TERM
        if [ "$status" -ne 0 ]; then
            if ! just gcp _enforce-maintenance {{stage}}; then
                echo "Error: failed to restore maintenance isolation after the interrupted transition." >&2
                exit 1
            fi
        fi
        exit "$status"
    }
    trap cleanup EXIT INT TERM
    just gcp _enforce-maintenance {{stage}}
    just gcp _maintenance-stop-database {{stage}}
    MODE=$(just gcp _mode-current {{stage}})
    if [ "$MODE" != "PARKED" ]; then
        echo "Error: {{stage}} did not converge to PARKED (current mode: $MODE)." >&2
        exit 1
    fi
    trap - EXIT INT TERM
    echo "{{stage}} is PARKED: runtime isolated, work paused, database stopped."

# Reconcile existing stage infrastructure while preserving MAINTENANCE.
[arg('stage', pattern='dev|staging|prod')]
init-stage-maintenance stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    just gcp _maintenance-assert-offline {{stage}}
    cleanup() {
        local status=$?
        trap - EXIT INT TERM
        if [ "$status" -ne 0 ]; then
            if ! just gcp _enforce-maintenance {{stage}}; then
                echo "Error: failed to restore maintenance isolation after init-stage failed." >&2
                exit 1
            fi
        fi
        exit "$status"
    }
    trap cleanup EXIT INT TERM
    GCP_YES=1 just gcp mode-maintenance {{stage}}
    GCP_INIT_STAGE_MAINTENANCE=1 just gcp init-stage {{stage}}
    just gcp _enforce-maintenance {{stage}}
    MODE=$(just gcp _mode-current {{stage}})
    if [ "$MODE" != "MAINTENANCE" ]; then
        echo "Error: init-stage did not leave {{stage}} in MAINTENANCE (current mode: $MODE)." >&2
        exit 1
    fi
    trap - EXIT INT TERM

# Build and deploy the newest app revision without bringing the stage online.
# Cloud SQL stays available for maintenance work; a failed or interrupted
# command leaves runtime and asynchronous work isolated.
[arg('stage', pattern='dev|staging|prod')]
deploy-maintenance stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    # Reject local release-readiness failures before confirmation or any
    # production-state mutation. ``build`` repeats this guard so direct builds
    # remain equally safe.
    just gcp _build-preflight
    if [ "{{stage}}" = "prod" ] && [ "${GCP_YES:-0}" != "1" ]; then
        echo "WARNING: This will replace the staged PRODUCTION revision while keeping it offline."
        read -r -p "Type 'prod' to confirm: " REPLY
        if [ "$REPLY" != "prod" ]; then echo "Cancelled."; exit 1; fi
    fi
    just gcp _maintenance-assert-offline {{stage}}
    # Build and push while the existing isolated runtime is unchanged. A local
    # build or registry failure therefore cannot trigger a maintenance-state
    # reconciliation or alter the staged revision.
    just gcp build
    just gcp push
    cleanup() {
        local status=$?
        trap - EXIT INT TERM
        if [ "$status" -ne 0 ]; then
            if ! just gcp _enforce-maintenance {{stage}}; then
                echo "Error: failed to restore maintenance isolation after the interrupted deploy." >&2
                exit 1
            fi
        fi
        exit "$status"
    }
    trap cleanup EXIT INT TERM
    GCP_YES=1 just gcp mode-maintenance {{stage}}
    just gcp _migrate {{stage}}
    GCP_DEPLOY_MAINTENANCE=1 just gcp _deploy-web {{stage}}
    GCP_DEPLOY_MAINTENANCE=1 just gcp _deploy-worker {{stage}}
    GCP_DEPLOY_MAINTENANCE=1 just gcp scheduler-setup {{stage}}
    GCP_DEPLOY_MAINTENANCE=1 just gcp _maybe-deploy-mcp {{stage}}
    just gcp _enforce-maintenance {{stage}}
    MODE=$(just gcp _mode-current {{stage}})
    if [ "$MODE" != "MAINTENANCE" ]; then
        echo "Error: maintenance deploy did not leave {{stage}} in MAINTENANCE (current mode: $MODE)." >&2
        exit 1
    fi
    trap - EXIT INT TERM
    echo "Latest revision deployed to {{stage}}; MAINTENANCE remains active."

# Run a Django management command during MAINTENANCE without opening traffic.
[arg('stage', pattern='dev|staging|prod')]
maintenance-management-cmd stage command: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    just gcp _maintenance-assert-offline {{stage}}
    cleanup() {
        local status=$?
        trap - EXIT INT TERM
        if [ "$status" -ne 0 ]; then
            if ! just gcp _enforce-maintenance {{stage}}; then
                echo "Error: failed to restore maintenance isolation after the management command." >&2
                exit 1
            fi
        fi
        exit "$status"
    }
    trap cleanup EXIT INT TERM
    GCP_YES=1 just gcp mode-maintenance {{stage}}
    just gcp management-cmd {{stage}} "{{command}}"
    just gcp _enforce-maintenance {{stage}}
    MODE=$(just gcp _mode-current {{stage}})
    if [ "$MODE" != "MAINTENANCE" ]; then
        echo "Error: management command did not leave {{stage}} in MAINTENANCE (current mode: $MODE)." >&2
        exit 1
    fi
    trap - EXIT INT TERM

# Reconcile a stage to LIVE. Database readiness, runtime capacity, queues, and
# every managed scheduler job are restored before either public ingress opens.
[arg('stage', pattern='dev|staging|prod')]
mode-live stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{validator_provider_retention_mode}}" in
        latest-only|drain-and-retain) ;;
        *) echo "Error: VALIDATOR_PROVIDER_RETENTION_MODE must be latest-only or drain-and-retain." >&2; exit 1 ;;
    esac
    APP_NAME="{{app_name}}"
    CURRENT_MODE=$(just gcp _mode-current {{stage}})
    if [ "$CURRENT_MODE" = "LIVE" ]; then
        echo "{{stage}} is already LIVE."
        exit 0
    fi
    if [ "{{stage}}" = "prod" ] && [ "${GCP_YES:-0}" != "1" ]; then
        echo "WARNING: This will bring PRODUCTION online."
        read -r -p "Type 'live' to confirm: " REPLY
        if [ "$REPLY" != "live" ]; then echo "Cancelled."; exit 1; fi
    fi
    cleanup() {
        local status=$?
        trap - EXIT INT TERM
        if [ "$status" -ne 0 ]; then
            if ! just gcp _enforce-maintenance {{stage}}; then
                echo "Error: failed to restore maintenance isolation after the interrupted LIVE transition." >&2
                exit 1
            fi
        fi
        exit "$status"
    }
    trap cleanup EXIT INT TERM
    # Begin every non-LIVE reconciliation from the known-safe isolated state.
    # This repairs partially opened runtime components before queues, capacity,
    # schedulers, and public ingress are restored in their strict order.
    just gcp _enforce-maintenance {{stage}}
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
        MCP_SERVICE="${APP_NAME}-mcp"
        QUEUE_NAME="${APP_NAME}-tasks"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider"
        JOB_SUFFIX=""
        WEB_MIN=1
        WEB_NEG="${APP_NAME}-neg"
        MCP_NEG="${APP_NAME}-mcp-neg"
        BUILD_FILE=".envs/.production/.google-cloud/.build"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        MCP_SERVICE="${APP_NAME}-mcp-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider-{{stage}}"
        JOB_SUFFIX="-{{stage}}"
        WEB_MIN=0
        WEB_NEG="${APP_NAME}-neg-{{stage}}"
        MCP_NEG="${APP_NAME}-mcp-neg-{{stage}}"
        BUILD_FILE=".envs/.{{stage}}/.google-cloud/.build"
    fi
    if [ -f "$BUILD_FILE" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$BUILD_FILE"
        set +a
    fi
    MCP_ENABLED="${VALIDIBOT_MCP_ENABLED:-true}"

    just gcp _maintenance-start-database {{stage}}
    # Before queues or schedulers can resume, prove the latest offered
    # release is the canonical route and remove superseded validator providers.
    # This is intentionally a no-user bootstrap policy; reconciliation still
    # refuses active routes, blockers, or unfinished attempts.
    if [ "{{validator_provider_retention_mode}}" = "latest-only" ]; then
        if [ "${VALIDATOR_LATEST_ONLY_RECONCILED:-0}" = "1" ]; then
            echo "Latest-only validator reconciliation already completed in this operator transaction."
        else
            VALIDATOR_LATEST_ONLY_AUTOMATIC=1 \
                just gcp validator-latest-only {{stage}}
        fi
    else
        echo "Provider retention policy is drain-and-retain; retaining superseded pairs for rollback and drain."
    fi
    if gcloud compute network-endpoint-groups describe "$WEB_NEG" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then WEB_INGRESS="internal-and-cloud-load-balancing"; else WEB_INGRESS="all"; fi
    if gcloud compute network-endpoint-groups describe "$MCP_NEG" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then MCP_INGRESS="internal-and-cloud-load-balancing"; else MCP_INGRESS="all"; fi

    # Maintenance already left worker capacity and every scheduler definition
    # in their desired immutable shape. Reopening a stage changes only paused
    # lifecycle state; it does not redeploy unrelated services or rewrite all
    # scheduler definitions.
    resume_queue() {
        local queue="$1"
        local state
        state=$(gcloud tasks queues describe "$queue" \
            --location {{gcp_region}} --project {{gcp_project}} \
            --format='value(state)' 2>/dev/null || echo "NOT_FOUND")
        case "$state" in
            RUNNING) echo "Task queue $queue is already running." ;;
            PAUSED)
                gcloud tasks queues resume "$queue" \
                    --location {{gcp_region}} --project {{gcp_project}} --quiet
                ;;
            *) echo "Error: task queue $queue is not resumable (state=$state)." >&2; exit 1 ;;
        esac
    }
    resume_queue "$QUEUE_NAME"
    if gcloud tasks queues describe "$PROVIDER_QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} &>/dev/null; then
        resume_queue "$PROVIDER_QUEUE_NAME"
    fi
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-purge-expired-outputs ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs ${APP_NAME}-verify-validator-deployments ${APP_NAME}-send-periodic-emails ${APP_NAME}-enforce-audit-retention; do
        job="${job_base}${JOB_SUFFIX}"
        scheduler_state=$(gcloud scheduler jobs describe "$job" \
            --project {{gcp_project}} --location {{gcp_region}} \
            --format='value(state)' 2>/dev/null || echo "NOT_FOUND")
        case "$scheduler_state" in
            ENABLED) echo "Scheduler job $job is already enabled." ;;
            PAUSED)
                gcloud scheduler jobs resume "$job" \
                    --project {{gcp_project}} --location {{gcp_region}} --quiet
                ;;
            *) echo "Error: required scheduler job $job is not resumable (state=$scheduler_state)." >&2; exit 1 ;;
        esac
    done

    # Public ingress is deliberately the final mutation after all dependent
    # runtime work has been restored.
    gcloud run services update "$WEB_SERVICE" \
        --region {{gcp_region}} --project {{gcp_project}} \
        --ingress "$WEB_INGRESS" --min="$WEB_MIN" --quiet
    if gcloud run services describe "$MCP_SERVICE" --region {{gcp_region}} --project {{gcp_project}} &>/dev/null; then
        # The MCP revision performs this same license request once during
        # startup. Wait for the newly opened Django route first so a brief
        # load-balancer propagation delay cannot make that revision fail.
        if [[ "$MCP_ENABLED" =~ ^(true|True|TRUE|1|yes|Yes|YES)$ ]]; then
            if [ -z "${VALIDIBOT_MCP_API_BASE_URL:-}" ]; then
                echo "Error: VALIDIBOT_MCP_API_BASE_URL is required to enable MCP." >&2
                exit 1
            fi
            MCP_LICENSE_URL="${VALIDIBOT_MCP_API_BASE_URL%/}/api/v1/license/features/"
            MCP_LICENSE_READY=0
            echo "Waiting for the MCP license check endpoint..."
            for attempt in {1..12}; do
                if curl --fail --silent --show-error --location \
                    --connect-timeout 5 --max-time 10 "$MCP_LICENSE_URL" 2>/dev/null \
                    | jq -e '.features | index("mcp_server") != null' >/dev/null; then
                    MCP_LICENSE_READY=1
                    break
                fi
                if [ "$attempt" -lt 12 ]; then sleep 5; fi
            done
            if [ "$MCP_LICENSE_READY" != "1" ]; then
                echo "Error: the MCP license endpoint did not become ready." >&2
                exit 1
            fi
        fi
        gcloud run services update "$MCP_SERVICE" \
            --region {{gcp_region}} --project {{gcp_project}} \
            --ingress "$MCP_INGRESS" --min=0 \
            --update-env-vars "VALIDIBOT_MCP_ENABLED=$MCP_ENABLED" --quiet
    fi
    MODE=$(just gcp _mode-current {{stage}})
    if [ "$MODE" != "LIVE" ]; then
        echo "Error: {{stage}} did not converge to LIVE (current mode: $MODE)." >&2
        exit 1
    fi
    trap - EXIT INT TERM
    echo "{{stage}} is LIVE: database ready, runtime restored, queues and schedulers enabled."

# Report the reconciled lifecycle mode. LIVE requires every managed scheduler
# job to be exactly ENABLED; a missing, paused, or unknown job is not LIVE.
[arg('stage', pattern='dev|staging|prod')]
mode-status stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
        MCP_SERVICE="${APP_NAME}-mcp"
        DB_INSTANCE="${APP_NAME}-db"
        QUEUE_NAME="${APP_NAME}-tasks"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        MCP_SERVICE="${APP_NAME}-mcp-{{stage}}"
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        PROVIDER_QUEUE_NAME="${APP_NAME}-validator-provider-{{stage}}"
        JOB_SUFFIX="-{{stage}}"
    fi
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} --format="value(state)" 2>/dev/null || echo "unknown")
    DB_ACTIVE_OPERATION=$(gcloud sql operations list \
        --instance "$DB_INSTANCE" --project {{gcp_project}} \
        --filter="status=RUNNING" --limit=1 --format="value(name)" 2>/dev/null || echo "unknown")
    QUEUE_STATUS=$(gcloud tasks queues describe "$QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
    PROVIDER_STATUS=$(gcloud tasks queues describe "$PROVIDER_QUEUE_NAME" \
        --location {{gcp_region}} --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
    WEB_INGRESS="unknown"
    WEB_SERVICE_MIN="unknown"
    WEB_REVISION_MIN="unknown"
    RUNTIME_ISOLATION_ISSUES=0
    LIVE_RUNTIME_ISSUES=0
    RUNTIME_INSPECTION_ERRORS=0
    echo "Stage status for {{stage}}"
    check_service() {
        local service="$1"
        local expect_mcp_disabled="${2:-0}"
        local required="${3:-0}"
        local service_json ingress service_min revision_min mcp_enabled
        if ! service_json=$(gcloud run services describe "$service" \
            --region {{gcp_region}} --project {{gcp_project}} --format=json 2>/dev/null); then
            if [ "$required" = "1" ]; then
                echo "  Runtime:       $service could not be inspected"
                RUNTIME_ISOLATION_ISSUES=$((RUNTIME_ISOLATION_ISSUES + 1))
                LIVE_RUNTIME_ISSUES=$((LIVE_RUNTIME_ISSUES + 1))
                RUNTIME_INSPECTION_ERRORS=$((RUNTIME_INSPECTION_ERRORS + 1))
            else
                echo "  Runtime:       $service not present"
            fi
            return
        fi
        ingress=$(jq -r '.metadata.annotations["run.googleapis.com/ingress"] // "unknown"' <<< "$service_json")
        service_min=$(jq -r '.spec.scaling.minInstanceCount // .metadata.annotations["run.googleapis.com/minScale"] // "0"' <<< "$service_json")
        revision_min=$(jq -r '.spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // "0"' <<< "$service_json")
        mcp_enabled=$(jq -r '[.spec.template.spec.containers[0].env[]? | select(.name == "VALIDIBOT_MCP_ENABLED") | .value][0] // "true"' <<< "$service_json")
        if [ "$service" = "$WEB_SERVICE" ]; then
            WEB_INGRESS="$ingress"
            WEB_SERVICE_MIN="$service_min"
            WEB_REVISION_MIN="$revision_min"
        fi
        if [ "$expect_mcp_disabled" = "1" ]; then
            echo "  Runtime:       $service ingress=$ingress service-min=$service_min revision-min=$revision_min enabled=$mcp_enabled"
        else
            echo "  Runtime:       $service ingress=$ingress service-min=$service_min revision-min=$revision_min"
        fi
        if [ "$ingress" != "internal" ] || [ "$service_min" != "0" ] || [ "$revision_min" != "0" ] || { [ "$expect_mcp_disabled" = "1" ] && [ "$mcp_enabled" != "false" ]; }; then
            RUNTIME_ISOLATION_ISSUES=$((RUNTIME_ISOLATION_ISSUES + 1))
        fi
        if [ "$service" = "$WEB_SERVICE" ] || [ "$service" = "$MCP_SERVICE" ]; then
            if [ "$ingress" != "all" ] && [ "$ingress" != "internal-and-cloud-load-balancing" ]; then
                LIVE_RUNTIME_ISSUES=$((LIVE_RUNTIME_ISSUES + 1))
            fi
        fi
    }
    check_service "$WEB_SERVICE" 0 1
    check_service "$WORKER_SERVICE" 0 1
    check_service "$MCP_SERVICE" 1
    if SERVICES=$(gcloud run services list \
        --region {{gcp_region}} --project {{gcp_project}} \
        --filter="metadata.labels.stage:{{stage}} AND metadata.labels.validator:*" \
        --format='value(metadata.name)'); then
        while IFS= read -r service; do
            [ -z "$service" ] && continue
            check_service "$service"
        done <<< "$SERVICES"
    else
        echo "  Runtime:       validator services could not be listed"
        RUNTIME_ISOLATION_ISSUES=$((RUNTIME_ISOLATION_ISSUES + 1))
        LIVE_RUNTIME_ISSUES=$((LIVE_RUNTIME_ISSUES + 1))
        RUNTIME_INSPECTION_ERRORS=$((RUNTIME_INSPECTION_ERRORS + 1))
    fi
    SCHEDULER_EXPECTED=10
    SCHEDULER_ENABLED=0
    SCHEDULER_PAUSED=0
    SCHEDULER_OTHER=0
    SCHEDULER_MISSING=0
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-purge-expired-outputs ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs ${APP_NAME}-verify-validator-deployments ${APP_NAME}-send-periodic-emails ${APP_NAME}-enforce-audit-retention; do
        job="${job_base}${JOB_SUFFIX}"
        if state=$(gcloud scheduler jobs describe "$job" \
            --project {{gcp_project}} --location {{gcp_region}} \
            --format="value(state)" 2>/dev/null); then
            case "$state" in
                ENABLED) SCHEDULER_ENABLED=$((SCHEDULER_ENABLED + 1)) ;;
                PAUSED) SCHEDULER_PAUSED=$((SCHEDULER_PAUSED + 1)) ;;
                *) SCHEDULER_OTHER=$((SCHEDULER_OTHER + 1)) ;;
            esac
        else
            SCHEDULER_MISSING=$((SCHEDULER_MISSING + 1))
        fi
    done
    echo "  Web ingress:   $WEB_INGRESS"
    echo "  Web minimum:   service=$WEB_SERVICE_MIN revision=$WEB_REVISION_MIN"
    echo "  Database:      $DB_STATUS"
    echo "  DB operation:  ${DB_ACTIVE_OPERATION:-none}"
    echo "  Main queue:    $QUEUE_STATUS"
    echo "  Provider queue: $PROVIDER_STATUS"
    echo "  Scheduler jobs: enabled=$SCHEDULER_ENABLED paused=$SCHEDULER_PAUSED other=$SCHEDULER_OTHER missing=$SCHEDULER_MISSING expected=$SCHEDULER_EXPECTED"
    RUNTIME_ISOLATED=0
    if [ "$WEB_INGRESS" = "internal" ] && [ "$WEB_SERVICE_MIN" = "0" ] && [ "$WEB_REVISION_MIN" = "0" ] && [ "$QUEUE_STATUS" = "PAUSED" ] && [ "$RUNTIME_ISOLATION_ISSUES" = "0" ] && [ "$SCHEDULER_ENABLED" = "0" ] && [ "$SCHEDULER_OTHER" = "0" ] && { [ "$PROVIDER_STATUS" = "PAUSED" ] || [ "$PROVIDER_STATUS" = "NOT_FOUND" ]; }; then
        RUNTIME_ISOLATED=1
    fi
    if [ "$RUNTIME_ISOLATED" = "1" ] && [ "$DB_STATUS" = "RUNNABLE" ] && [ -z "$DB_ACTIVE_OPERATION" ]; then
        echo "  Overall:       MAINTENANCE"
    elif [ "$RUNTIME_ISOLATED" = "1" ] && [ "$DB_STATUS" = "STOPPED" ] && [ -z "$DB_ACTIVE_OPERATION" ]; then
        echo "  Overall:       PARKED"
    elif [ "$RUNTIME_INSPECTION_ERRORS" = "0" ] && [ "$LIVE_RUNTIME_ISSUES" = "0" ] && [ "$DB_STATUS" = "RUNNABLE" ] && [ -z "$DB_ACTIVE_OPERATION" ] && [ "$QUEUE_STATUS" = "RUNNING" ] && [ "$SCHEDULER_ENABLED" = "$SCHEDULER_EXPECTED" ] && [ "$SCHEDULER_PAUSED" = "0" ] && [ "$SCHEDULER_OTHER" = "0" ] && [ "$SCHEDULER_MISSING" = "0" ] && { [ "$PROVIDER_STATUS" = "RUNNING" ] || [ "$PROVIDER_STATUS" = "NOT_FOUND" ]; }; then
        echo "  Overall:       LIVE"
    else
        echo "  Overall:       PARTIALLY_TRANSITIONED"
    fi
    echo ""

# =============================================================================
# Load Balancer & DNS
# =============================================================================
#
# Custom domain options for Cloud Run:
#
# Option A: Cloud Run Domain Mappings (simpler, no extra cost)
#   Supported in: asia-east1, asia-northeast1, asia-southeast1,
#   europe-north1, europe-west1, europe-west4, us-central1, us-east1,
#   us-east4, us-west1.
#   NOT supported in: australia-southeast1, australia-southeast2, and others.
#   Note: Domain mappings are still in preview and may have latency issues.
#   Set up via: gcloud beta run domain-mappings create ...
#
# Option B: Global Application Load Balancer (recommended for production)
#   Works in ALL regions. Required for regions without domain mapping support
#   (e.g. australia-southeast1). Provides a stable static IP, CDN
#   integration, and full control over SSL and routing.
#
# This section implements Option B. If your region supports domain mappings
# and you prefer the simpler setup, see the Cloud Run docs:
# https://cloud.google.com/run/docs/mapping-custom-domains
#
# Resources created (Option B):
#   - Global Static IP
#   - Network Endpoint Group (Serverless NEG) -> Points to Cloud Run
#   - Backend Service -> Routes traffic to NEG
#   - URL Map -> Directs all requests to Backend Service
#   - SSL Certificate -> Google-managed (auto-renewing)
#   - Target HTTPS Proxy -> Terminates SSL
#   - Forwarding Rule -> Listens on IP:443
#   - Target HTTP Proxy -> Optional for port 80
#   - Forwarding Rule -> Optional for IP:80
#
# Cost: Global external load balancers have a non-zero base cost even at low
# traffic. Check GCP pricing before you run this in production.
#
# =============================================================================

# Set up the Global Load Balancer
# Usage: just gcp lb-setup prod example.com
# Usage: just gcp lb-setup prod "example.com,www.example.com"
[arg('stage', pattern='dev|staging|prod')]
lb-setup stage domains: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "========================================================"
    echo "Setting up Global Load Balancer for: {{stage}}"
    DOMAINS_CSV="$(echo "{{domains}}" | tr -d '[:space:]')"
    if [ -z "$DOMAINS_CSV" ]; then
        echo "Error: domains must be a comma-separated list, e.g. 'example.com' or 'example.com,www.example.com'"
        exit 1
    fi
    echo "Domains: $DOMAINS_CSV"
    echo "========================================================"
    echo ""

    # Compute resource names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
        SUFFIX=""
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
        SUFFIX="-{{stage}}"
    fi

    IP_NAME="${APP_NAME}-ip${SUFFIX}"
    NEG_NAME="${APP_NAME}-neg${SUFFIX}"
    BACKEND_NAME="${APP_NAME}-backend${SUFFIX}"
    URL_MAP_NAME="${APP_NAME}-url-map${SUFFIX}"
    CERT_NAME="${APP_NAME}-cert${SUFFIX}"
    PROXY_NAME="${APP_NAME}-https-proxy${SUFFIX}"
    RULE_NAME="${APP_NAME}-lb-rule${SUFFIX}"
    HTTP_PROXY_NAME="${APP_NAME}-http-proxy${SUFFIX}"
    HTTP_RULE_NAME="${APP_NAME}-lb-rule-http${SUFFIX}"

    # 0. Verify the Cloud Run service exists
    echo "0. Checking Cloud Run service ($SERVICE)..."
    if gcloud run services describe "$SERVICE" \
        --region={{gcp_region}} --project {{gcp_project}} &>/dev/null; then
        echo "   Found"
    else
        echo "Error: Cloud Run service '$SERVICE' not found in region {{gcp_region}}."
        echo "Deploy it first: just gcp deploy {{stage}}"
        exit 1
    fi
    echo ""

    # 1. Reserve Static IP
    echo "1. Checking Static IP ($IP_NAME)..."
    if gcloud compute addresses describe "$IP_NAME" --global --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute addresses create "$IP_NAME" --global --project {{gcp_project}}
        echo "   Created"
    fi
    IP_ADDRESS=$(gcloud compute addresses describe "$IP_NAME" --global --project {{gcp_project}} --format="value(address)")
    echo "   -> IP: $IP_ADDRESS"
    echo ""

    # 2. Create NEG (Connects to Cloud Run)
    echo "2. Checking Network Endpoint Group ($NEG_NAME)..."
    if gcloud compute network-endpoint-groups describe "$NEG_NAME" \
        --region={{gcp_region}} --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute network-endpoint-groups create "$NEG_NAME" \
            --region={{gcp_region}} \
            --network-endpoint-type=serverless \
            --cloud-run-service="$SERVICE" \
            --project {{gcp_project}}
        echo "   Created (pointing to $SERVICE)"
    fi
    echo ""

    # 3. Create Backend Service
    echo "3. Checking Backend Service ($BACKEND_NAME)..."
    if ! gcloud compute backend-services describe "$BACKEND_NAME" --global --project {{gcp_project}} &>/dev/null; then
        gcloud compute backend-services create "$BACKEND_NAME" \
            --global \
            --protocol=HTTP \
            --project {{gcp_project}}
        echo "   Created service"
    else
        echo "   Service exists"
    fi

    CURRENT_TIMEOUT=$(gcloud compute backend-services describe "$BACKEND_NAME" --global --project {{gcp_project}} --format="value(timeoutSec)" 2>/dev/null || echo "")
    if [[ -n "$CURRENT_TIMEOUT" && "$CURRENT_TIMEOUT" != "30" ]]; then
        echo "   ! Backend Service timeoutSec is $CURRENT_TIMEOUT (unsupported for Serverless NEGs)."
        echo "     Resetting Backend Service timeout to default (30s) to satisfy GCP requirements."
        echo "     Note: Cloud Run request timeout is configured on the Cloud Run service (we deploy with {{gcp_cloud_run_request_timeout}})."
        gcloud compute backend-services update "$BACKEND_NAME" \
            --global \
            --timeout=30s \
            --project {{gcp_project}}
        echo "   Timeout configuration fixed"
    fi

    # Add NEG to Backend Service (idempotent check)
    if gcloud compute backend-services describe "$BACKEND_NAME" --global --project {{gcp_project}} \
        --format="value(backends[].group)" 2>/dev/null | grep -q "/$NEG_NAME$"; then
        echo "   Backend already attached"
    else
        gcloud compute backend-services add-backend "$BACKEND_NAME" \
            --global \
            --network-endpoint-group="$NEG_NAME" \
            --network-endpoint-group-region={{gcp_region}} \
            --project {{gcp_project}}
        echo "   Attached NEG to backend"
    fi
    echo ""

    # 4. Create URL Map
    echo "4. Checking URL Map ($URL_MAP_NAME)..."
    if gcloud compute url-maps describe "$URL_MAP_NAME" --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute url-maps create "$URL_MAP_NAME" \
            --default-service "$BACKEND_NAME" \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # 5. Create Managed SSL Certificate
    echo "5. Checking SSL Certificate ($CERT_NAME)..."
    if gcloud compute ssl-certificates describe "$CERT_NAME" --global --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute ssl-certificates create "$CERT_NAME" \
            --domains "$DOMAINS_CSV" \
            --global \
            --project {{gcp_project}}
        echo "   Created for: $DOMAINS_CSV"
    fi
    echo ""

    # 6. Create Target HTTPS Proxy
    echo "6. Checking Target Proxy ($PROXY_NAME)..."
    if gcloud compute target-https-proxies describe "$PROXY_NAME" --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute target-https-proxies create "$PROXY_NAME" \
            --ssl-certificates="$CERT_NAME" \
            --url-map="$URL_MAP_NAME" \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # 7. Create Forwarding Rule
    echo "7. Checking Forwarding Rule ($RULE_NAME)..."
    if gcloud compute forwarding-rules describe "$RULE_NAME" --global --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute forwarding-rules create "$RULE_NAME" \
            --address="$IP_NAME" \
            --target-https-proxy="$PROXY_NAME" \
            --global \
            --ports=443 \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # 8. Create Target HTTP Proxy (optional but recommended for http->https redirects)
    echo "8. Checking HTTP Target Proxy ($HTTP_PROXY_NAME)..."
    if gcloud compute target-http-proxies describe "$HTTP_PROXY_NAME" --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute target-http-proxies create "$HTTP_PROXY_NAME" \
            --url-map="$URL_MAP_NAME" \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # 9. Create HTTP Forwarding Rule (port 80)
    echo "9. Checking HTTP Forwarding Rule ($HTTP_RULE_NAME)..."
    if gcloud compute forwarding-rules describe "$HTTP_RULE_NAME" --global --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute forwarding-rules create "$HTTP_RULE_NAME" \
            --address="$IP_NAME" \
            --target-http-proxy="$HTTP_PROXY_NAME" \
            --global \
            --ports=80 \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    echo "========================================================"
    echo "Load Balancer Setup Complete"
    echo "========================================================"
    echo ""
    echo "ACTION REQUIRED: Update your DNS records"
    echo ""
    echo "Create these records for: $DOMAINS_CSV"
    echo "--------------------------------------------------------"
    echo "Type:  A"
    echo "Value: $IP_ADDRESS"
    echo "TTL:   1 hour (or default)"
    echo "--------------------------------------------------------"
    echo ""
    echo "Note: Google-managed certificates can take 15-60 minutes to provision"
    echo "after DNS propagation. Until then, you may see SSL errors."

    echo ""
    echo "Optional hardening (recommended after you verify the LB works):"
    echo "  Restrict direct access to the *.run.app URL:"
    echo "    gcloud run services update \"$SERVICE\" \\"
    echo "      --ingress internal-and-cloud-load-balancing \\"
    echo "      --region {{gcp_region}} \\"
    echo "      --project {{gcp_project}}"

# =============================================================================
# Verification & Validation
# =============================================================================
#
# Commands to verify that infrastructure and deployments are working correctly.
#
# =============================================================================

# Validate entire GCP infrastructure (prod only)
validate-all: validate-gcp

# Validate GCP infrastructure is healthy
validate-gcp:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    echo "Validating GCP Infrastructure"
    echo "======================================"
    echo ""

    TESTS_PASSED=0
    TESTS_FAILED=0

    GREEN='\033[0;32m'
    RED='\033[0;31m'
    NC='\033[0m'

    echo "1. Checking Cloud Run service..."
    if gcloud run services describe ${APP_NAME}-web \
        --region={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null; then
        echo -e "${GREEN}OK${NC} Cloud Run service '${APP_NAME}-web' exists"
        ((TESTS_PASSED++))

        URL=$(gcloud run services describe ${APP_NAME}-web \
            --region={{gcp_region}} \
            --project={{gcp_project}} \
            --format="value(status.url)")
        echo "  URL: $URL"
    else
        echo -e "${RED}FAIL${NC} Cloud Run service not found"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "2. Checking service health..."
    if just gcp health-check prod; then
        echo -e "${GREEN}OK${NC} Service responds to requests"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}FAIL${NC} Service not responding"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "3. Checking for recent errors in logs..."
    ERROR_COUNT=$(gcloud logging read \
        "resource.type=cloud_run_revision AND resource.labels.service_name=${APP_NAME}-web AND severity>=ERROR" \
        --project={{gcp_project}} \
        --limit=10 \
        --format=json | jq '. | length')

    if [ "$ERROR_COUNT" -eq 0 ]; then
        echo -e "${GREEN}OK${NC} No recent errors in logs"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}FAIL${NC} Found $ERROR_COUNT recent errors"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "4. Checking Cloud SQL instance..."
    if gcloud sql instances describe ${APP_NAME}-db \
        --project={{gcp_project}} &>/dev/null; then
        echo -e "${GREEN}OK${NC} Cloud SQL instance exists"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}FAIL${NC} Cloud SQL instance not found"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "5. Checking GCS buckets..."
    BUCKET_COUNT=$(gcloud storage buckets list \
        --project={{gcp_project}} \
        --format=json | jq '. | length')

    if [ "$BUCKET_COUNT" -ge 2 ]; then
        echo -e "${GREEN}OK${NC} Found $BUCKET_COUNT storage buckets"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}FAIL${NC} Expected at least 2 buckets, found $BUCKET_COUNT"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "======================================"
    echo "GCP Validation Summary"
    echo "======================================"
    echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    echo ""

    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "${GREEN}All GCP tests passed!${NC}"
        exit 0
    else
        echo -e "${RED}Some GCP tests failed${NC}"
        exit 1
    fi

# Run tests against live GCP infrastructure
# Loads env from .envs/.test-on-gcp/.django and runs pytest
# Usage:
#   just gcp test-on-gcp                           # Run all GCP integration tests
#   just gcp test-on-gcp -k "connectivity"         # Run only connectivity tests
#   just gcp test-on-gcp --collect-only            # See which tests would run
test-on-gcp *args:
    #!/usr/bin/env bash
    set -euo pipefail

    ENV_FILE=".envs/.test-on-gcp/.django"

    if [ ! -f "$ENV_FILE" ]; then
        echo "Error: $ENV_FILE not found"
        echo ""
        echo "Create it by copying your GCP env file:"
        echo "  mkdir -p .envs/.test-on-gcp"
        echo "  cp .envs/.production/.google-cloud/.django $ENV_FILE"
        echo ""
        echo "Then set DJANGO_SETTINGS_MODULE=config.settings.test in the copy."
        exit 1
    fi

    echo "Loading environment from $ENV_FILE"
    set -a
    source "$ENV_FILE"
    set +a

    echo "Running tests against GCP..."
    echo ""
    uv run pytest tests/tests_integration/ {{args}} -v --log-cli-level=INFO

# Run E2E tests against deployed staging environment
# Tests the full flow: API -> Cloud Run Job -> Callback -> Worker
test-e2e *args:
    @if [ -z "${E2E_TEST_API_URL:-}" ]; then \
        echo "Error: E2E_TEST_API_URL not set"; \
        echo ""; \
        echo "Usage:"; \
        echo "  E2E_TEST_API_URL=https://your-staging-url.run.app/api/v1 \\"; \
        echo "  E2E_TEST_API_TOKEN=your-api-token \\"; \
        echo "  E2E_TEST_WORKFLOW_ID=workflow-uuid \\"; \
        echo "  just gcp test-e2e"; \
        exit 1; \
    fi
    @echo "Running E2E tests against: ${E2E_TEST_API_URL}"
    uv run --group dev pytest tests/tests_integration/test_e2e_workflow.py {{args}} -v --log-cli-level=INFO

# Verify a deployed environment with smoke tests
# Usage: just gcp verify-deployment dev | just gcp verify-deployment prod
[arg('stage', pattern='dev|staging|prod')]
verify-deployment stage *args: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "=============================================="
    echo "Post-Deployment Verification: {{stage}}"
    echo "=============================================="
    echo ""

    if ! command -v gcloud &> /dev/null; then
        echo "Error: gcloud CLI not found. Install Google Cloud SDK first."
        exit 1
    fi

    if ! gcloud auth print-identity-token &> /dev/null; then
        echo "Error: No valid gcloud credentials."
        echo "Run: gcloud auth login"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
    fi

    echo "Target services:"
    echo "  Web:    $WEB_SERVICE"
    echo "  Worker: $WORKER_SERVICE"
    echo ""

    echo "Checking service deployment..."
    WEB_URL=$(gcloud run services describe "$WEB_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null) || {
        echo "Error: Web service $WEB_SERVICE not found or not accessible"
        exit 1
    }
    WORKER_URL=$(gcloud run services describe "$WORKER_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null) || {
        echo "Error: Worker service $WORKER_SERVICE not found or not accessible"
        exit 1
    }

    echo "  Web URL:    $WEB_URL"
    echo "  Worker URL: $WORKER_URL"
    echo ""
    echo "Running smoke tests..."
    echo ""

    # Pass superuser credentials for authenticated tests (if available).
    # These are set in the .django env file. If not in the environment,
    # authenticated tests are skipped automatically.
    if [ -n "${SUPERUSER_USERNAME:-}" ] && [ -n "${SUPERUSER_PASSWORD:-}" ]; then
        echo "  Superuser credentials found - authenticated tests will run"
    else
        echo "  No superuser credentials - authenticated tests will be skipped"
        echo "  To enable: export SUPERUSER_USERNAME=... SUPERUSER_PASSWORD=..."
    fi
    echo ""

    SMOKE_TEST_STAGE={{stage}} uv run pytest tests/smoke/ {{args}} -v

    echo ""
    echo "=============================================="
    echo "PDV Complete: {{stage}}"
    echo "=============================================="

# Quick deployment verification (just check services are up)
[arg('stage', pattern='dev|staging|prod')]
verify-deployment-quick stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "Quick verification for {{stage}}..."

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
    fi

    WEB_URL=$(gcloud run services describe "$WEB_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null) || {
        echo "Web service not deployed"
        exit 1
    }

    WORKER_URL=$(gcloud run services describe "$WORKER_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null) || {
        echo "Worker service not deployed"
        exit 1
    }

    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$WEB_URL" --max-time 30) || HTTP_CODE="000"
    if [ "$HTTP_CODE" = "200" ]; then
        echo "Web service responding ($WEB_URL)"
    else
        echo "Web service returned $HTTP_CODE"
        exit 1
    fi

    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$WORKER_URL" --max-time 30) || HTTP_CODE="000"
    if [ "$HTTP_CODE" = "403" ]; then
        echo "Worker service IAM protected ($WORKER_URL)"
    else
        echo "Worker service returned $HTTP_CODE (expected 403)"
        exit 1
    fi

    echo ""
    echo "Quick verification passed for {{stage}}"

# =============================================================================
# Security Audit
# =============================================================================

# Check for common GCP security misconfigurations
# Run before releases or periodically to catch drift.
#
# Checks:
#   1. User-managed SA keys (should be zero)
#   2. SA role sprawl (lists roles per SA for review)
#   3. Worker ingress posture (should be internal)
#   4. Public bucket access (should only be on public/ prefix)
#   5. Validator Services remain private with only the dedicated invoker
#   6. Optional MCP can read only its exact stage secret
#
# Usage: just gcp security-audit prod
[arg('stage', pattern='dev|staging|prod')]
security-audit stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    PASS=0
    WARN=0

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "============================================="
    echo "Security audit for: {{stage}}"
    echo "============================================="
    echo ""

    # Determine stage-specific resource names
    if [ "{{stage}}" = "prod" ]; then
        MAIN_SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-prod@{{gcp_project}}.iam.gserviceaccount.com"
        PROVIDER_INVOKER="${APP_NAME}-val-invoker-prod@{{gcp_project}}.iam.gserviceaccount.com"
        MCP_SA="${APP_NAME}-mcp-prod@{{gcp_project}}.iam.gserviceaccount.com"
        MCP_SECRET="mcp-env"
        WORKER_SERVICE="${APP_NAME}-worker"
        STORAGE_BUCKET="${APP_NAME}-storage"
    else
        MAIN_SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        PROVIDER_INVOKER="${APP_NAME}-val-invoker-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        MCP_SA="${APP_NAME}-mcp-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        MCP_SECRET="mcp-env-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        STORAGE_BUCKET="${APP_NAME}-storage-{{stage}}"
    fi
    AUDITED_SAS=("$MAIN_SA" "$VALIDATOR_SA" "$PROVIDER_INVOKER")
    if gcloud iam service-accounts describe "$MCP_SA" \
        --project={{gcp_project}} &>/dev/null; then
        AUDITED_SAS+=("$MCP_SA")
        MCP_PRESENT=1
    else
        MCP_PRESENT=0
    fi

    # 1. Check for user-managed SA keys (should be zero)
    echo "1. Checking for user-managed SA keys..."
    for sa in "${AUDITED_SAS[@]}"; do
        sa_short="${sa%%@*}"
        KEYS=$(gcloud iam service-accounts keys list \
            --iam-account="$sa" \
            --managed-by=user \
            --project={{gcp_project}} \
            --format="value(name)" 2>/dev/null || echo "")
        if [ -z "$KEYS" ]; then
            echo "   PASS: $sa_short has no user-managed keys"
            ((PASS+=1))
        else
            KEY_COUNT=$(echo "$KEYS" | wc -l | tr -d ' ')
            echo "   WARN: $sa_short has $KEY_COUNT user-managed key(s) - consider deleting"
            ((WARN+=1))
        fi
    done
    echo ""

    # 2. Check SA role assignments
    echo "2. Checking SA role assignments..."
    for sa in "${AUDITED_SAS[@]}"; do
        sa_short="${sa%%@*}"
        echo "   $sa_short:"
        ROLES=$(gcloud projects get-iam-policy {{gcp_project}} \
            --flatten="bindings[].members" \
            --filter="bindings.members:$sa" \
            --format="value(bindings.role)" 2>/dev/null || echo "(none)")
        if [ -z "$ROLES" ]; then
            echo "     (no project-level roles)"
        else
            echo "$ROLES" | while read -r role; do
                echo "     - $role"
            done
            if [ "$sa" != "$MAIN_SA" ]; then
                echo "     WARN: dedicated validator/MCP identities must have no project-level roles"
                ((WARN+=1))
            fi
        fi
    done
    echo ""

    # 3. Check worker ingress posture
    echo "3. Checking worker ingress..."
    WORKER_INGRESS=$(gcloud run services describe "$WORKER_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(metadata.annotations.'run.googleapis.com/ingress')" 2>/dev/null || echo "unknown")
    if [ "$WORKER_INGRESS" = "internal" ]; then
        echo "   PASS: Worker ingress is 'internal'"
        ((PASS+=1))
    elif [ "$WORKER_INGRESS" = "unknown" ]; then
        echo "   SKIP: Worker service not found (not yet deployed?)"
    else
        echo "   WARN: Worker ingress is '$WORKER_INGRESS' (expected 'internal')"
        ((WARN+=1))
    fi
    echo ""

    # 4. Check bucket public access
    echo "4. Checking bucket public access..."
    # Find bindings where allUsers or allAuthenticatedUsers have access without conditions
    UNCONDITIONAL_PUBLIC=$(gcloud storage buckets get-iam-policy "gs://$STORAGE_BUCKET" \
        --project={{gcp_project}} \
        --format=json 2>/dev/null \
        | jq -r '
            .bindings[]?
            | select((.members // []) | any(. == "allUsers" or . == "allAuthenticatedUsers"))
            | select(.condition == null)
            | "UNCONDITIONAL: \(.role)"
        ' 2>/dev/null || echo "")
    if [ -z "$UNCONDITIONAL_PUBLIC" ]; then
        echo "   PASS: No unconditional public access on $STORAGE_BUCKET"
        ((PASS+=1))
    else
        echo "   WARN: Unconditional public access found on $STORAGE_BUCKET:"
        echo "$UNCONDITIONAL_PUBLIC" | while read -r line; do
            echo "     $line"
        done
        ((WARN+=1))
    fi
    echo ""

    # 5. Validator Service invocation policy
    echo "5. Checking validator Service IAM..."
    SERVICES=$(gcloud run services list \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --filter="metadata.labels.stage:{{stage}} AND metadata.labels.validator:*" \
        --format='value(metadata.name)' 2>/dev/null || true)
    if [ -z "$SERVICES" ]; then
        if [ "{{stage}}" = "prod" ]; then
            echo "   WARN: no production validator Services found"
            ((WARN+=1))
        else
            echo "   SKIP: no validator Services found"
        fi
    else
        while IFS= read -r SERVICE; do
            [ -z "$SERVICE" ] && continue
            POLICY=$(gcloud run services get-iam-policy "$SERVICE" \
                --region={{gcp_region}} \
                --project={{gcp_project}} \
                --format=json)
            BROAD=$(jq -r '[.bindings[]?.members[]? | select(. == "allUsers" or . == "allAuthenticatedUsers")] | length' <<< "$POLICY")
            EXPECTED=$(jq -r --arg member "serviceAccount:$PROVIDER_INVOKER" \
                '[.bindings[]? | select(.role == "roles/run.invoker") | .members[]? | select(. == $member)] | length' <<< "$POLICY")
            INVOKER_COUNT=$(jq -r \
                '[.bindings[]? | select(.role == "roles/run.invoker") | .members[]?] | length' <<< "$POLICY")
            if [ "$BROAD" -eq 0 ] && [ "$EXPECTED" -eq 1 ] && [ "$INVOKER_COUNT" -eq 1 ]; then
                echo "   PASS: $SERVICE grants only the dedicated invoker"
                ((PASS+=1))
            else
                echo "   WARN: $SERVICE has unexpected invoker IAM (broad=$BROAD expected=$EXPECTED total=$INVOKER_COUNT)"
                ((WARN+=1))
            fi
        done <<< "$SERVICES"
    fi
    echo ""

    # 6. MCP may read its own stage secret, never every project secret.
    echo "6. Checking MCP Secret Manager scope..."
    if [ "$MCP_PRESENT" -eq 0 ]; then
        echo "   SKIP: MCP service account not provisioned"
    elif ! gcloud secrets describe "$MCP_SECRET" \
        --project={{gcp_project}} &>/dev/null; then
        echo "   WARN: MCP service account exists but $MCP_SECRET does not"
        ((WARN+=1))
    else
        PROJECT_POLICY=$(gcloud projects get-iam-policy {{gcp_project}} --format=json)
        SECRET_POLICY=$(gcloud secrets get-iam-policy "$MCP_SECRET" \
            --project={{gcp_project}} --format=json)
        MCP_MEMBER="serviceAccount:$MCP_SA"
        PROJECT_SECRET_ACCESS=$(jq -r --arg member "$MCP_MEMBER" '
            [.bindings[]?
             | select(.role == "roles/secretmanager.secretAccessor")
             | .members[]?
             | select(. == $member)]
            | length
        ' <<< "$PROJECT_POLICY")
        EXACT_SECRET_ACCESS=$(jq -r --arg member "$MCP_MEMBER" '
            [.bindings[]?
             | select(.role == "roles/secretmanager.secretAccessor")
             | .members[]?
             | select(. == $member)]
            | length
        ' <<< "$SECRET_POLICY")
        if [ "$PROJECT_SECRET_ACCESS" -eq 0 ] && [ "$EXACT_SECRET_ACCESS" -eq 1 ]; then
            echo "   PASS: MCP can read only $MCP_SECRET"
            ((PASS+=1))
        else
            echo "   WARN: MCP secret scope is unexpected (project=$PROJECT_SECRET_ACCESS exact=$EXACT_SECRET_ACCESS)"
            ((WARN+=1))
        fi
    fi
    echo ""

    # Summary
    echo "============================================="
    echo "Results: $PASS passed, $WARN warnings"
    echo "============================================="
    if [ "$WARN" -gt 0 ]; then
        exit 1
    fi

# =============================================================================
# Database Access
# =============================================================================

# Start a Cloud SQL Auth Proxy for local database access.
#
# The proxy creates an encrypted tunnel from your laptop to the Cloud SQL
# instance using your Google Cloud IAM identity. It listens on localhost:5432,
# so you can connect with any PostgreSQL client (psql, pgAdmin, DBeaver, etc.):
#
#   psql "postgresql://validibot_user:PASSWORD@localhost:5432/validibot"
#
# This is the recommended way to access the database after the public IP is
# removed. It works from any network (home, coffee shop, VPN) because
# authentication is identity-based (your gcloud credentials), not IP-based.
#
# Prerequisites:
#   - cloud-sql-proxy binary installed (brew install cloud-sql-proxy)
#   - Authenticated with gcloud (gcloud auth application-default login)
#
# Press Ctrl+C to stop the proxy.
#
# Usage: just gcp db-proxy dev | just gcp db-proxy prod
[arg('stage', pattern='dev|staging|prod')]
db-proxy stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [ "{{stage}}" = "prod" ]; then
        DB_INSTANCE="${APP_NAME}-db"
    else
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
    fi

    CONNECTION_NAME="{{gcp_project}}:{{gcp_region}}:${DB_INSTANCE}"

    # Check if cloud-sql-proxy is installed
    if ! command -v cloud-sql-proxy &>/dev/null; then
        echo "Error: cloud-sql-proxy is not installed."
        echo ""
        echo "Install it with:"
        echo "  brew install cloud-sql-proxy        # macOS"
        echo "  # or download from https://cloud.google.com/sql/docs/postgres/sql-proxy"
        echo ""
        echo "Then authenticate:"
        echo "  gcloud auth application-default login"
        exit 1
    fi

    echo "Starting Cloud SQL Auth Proxy for {{stage}}..."
    echo ""
    echo "  Instance:  $CONNECTION_NAME"
    echo "  Listening: localhost:5432"
    echo ""
    echo "Connect with:"
    echo "  psql \"postgresql://validibot_user:PASSWORD@localhost:5432/validibot\""
    echo ""
    echo "Press Ctrl+C to stop."
    echo ""

    cloud-sql-proxy "$CONNECTION_NAME" \
        --port 5432 \
        --auto-iam-authn

# =============================================================================
# Helpers
# =============================================================================

# Authenticate Docker with Google Artifact Registry (run once)
auth:
    gcloud auth configure-docker {{gcp_region}}-docker.pkg.dev

# Open the Cloud Run console in browser for a stage
# Usage: just gcp console dev | just gcp console prod
[arg('stage', pattern='dev|staging|prod')]
console stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    open "https://console.cloud.google.com/run/detail/{{gcp_region}}/$SERVICE/metrics?project={{gcp_project}}"

# Connect local Django shell to a Cloud SQL database via proxy
# Uses Cloud SQL Proxy for secure connection.
# Requires: DATABASE_PASSWORD environment variable set
# Usage: DATABASE_PASSWORD='yourpass' just gcp local-to-gcp-shell dev
[arg('stage', pattern='dev|staging|prod')]
local-to-gcp-shell stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ -z "${DATABASE_PASSWORD:-}" ]; then
        echo "Error: DATABASE_PASSWORD environment variable not set"
        echo ""
        echo "Usage:"
        echo "  DATABASE_PASSWORD='your-password' just gcp local-to-gcp-shell {{stage}}"
        echo ""
        echo "Get the password from Secret Manager or your .envs/.{{stage}}/.django file"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        DB_INSTANCE="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
    else
        DB_INSTANCE="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
    fi

    echo "Starting Cloud SQL Proxy for {{stage}}..."
    cloud-sql-proxy "$DB_INSTANCE" &
    PROXY_PID=$!

    sleep 2

    export DATABASE_URL="postgres://validibot_user:${DATABASE_PASSWORD}@localhost:5432/validibot"
    export DJANGO_SETTINGS_MODULE="config.settings.local"

    echo ""
    echo "Connected to {{stage}} database via Cloud SQL Proxy"
    echo "Be careful - changes affect live data!"
    echo "Press Ctrl+D to exit shell, then Ctrl+C to stop proxy"
    echo ""

    uv run python manage.py shell || true

    echo "Stopping Cloud SQL Proxy..."
    kill $PROXY_PID 2>/dev/null || true

# =============================================================================
# Operator-Facing Recipes (ADR-2026-04-27)
# =============================================================================
#
# These recipes mirror the operator-facing recipes in ``just/self-hosted/``
# so we have cross-target parity by construction (see ADR section 1).
#
# GCP recipes take an explicit stage (dev/staging/prod) — see ADR
# section 4. This is the one intentional asymmetry between
# ``just self-hosted`` (no stage) and ``just gcp`` (stage required).
#
# Implementation status:
#   - doctor:                 ✅ Cloud Run Job runs check_validibot
#   - errors-since:           ✅ wraps gcloud logging read
#   - validators:             ✅ Cloud Run Job inventory + pinning compliance
#   - backup:                 ✅ Cloud SQL export + media rsync + manifest
#   - restore:                ✅ DB import + media rsync + compatibility verifier
#   - smoke-test:             stub (Phase 2 — end-to-end demo workflow)
#   - collect-support-bundle: stub (Phase 6 — redacted support archive)
#
# Stubs print a "not yet implemented" notice and exit 64 so the parity
# surface is visible in ``just gcp --list`` and cross-target parity
# tests work against the recipe set today.

[private]
_phase0-stub recipe stage:
    #!/usr/bin/env bash
    echo "just gcp {{recipe}} {{stage}}: not yet implemented (Phase 0 stub)." >&2
    echo "" >&2
    echo "Implementation lands in a later phase per ADR-2026-04-27" >&2
    echo "(Boring Self-Hosting and Operator Experience)." >&2
    exit 64

# Health diagnostic — same JSON schema as ``just self-hosted doctor``,
# with GCP-specific checks (Cloud Run revision, Cloud SQL instance,
# Cloud Tasks queue, GCS IAM bindings).
#
# How this works:
#
#   1. Ensures a stage-specific Cloud Run Job exists
#      (``validibot-doctor`` for prod, ``validibot-doctor-{stage}``
#      otherwise) running the deployed image.  The Job's command is
#      ``python manage.py check_validibot --target gcp --stage <stage>``.
#      Same SA, same Cloud SQL instance, same secret as the web /
#      worker services so the doctor sees what the runtime sees.
#
#   2. Executes the Job with ``gcloud run jobs execute --wait`` and
#      passes any extra args (``--strict``, ``--json``, etc.)
#      through.  The Job's exit code propagates so CI gates work.
#
#   3. Tails the execution logs back to the operator's terminal so
#      the JSON / pretty output is visible alongside the exit code.
#
# Why a Cloud Run Job rather than running locally:
#
# Running ``check_validibot`` on the operator's laptop only checks
# things visible from outside (DNS, public endpoints).  It misses
# Cloud SQL connectivity from inside the VPC, Secret Manager
# bindings, Service Account permissions on private resources — all
# the failures that actually break production.  Running the Job in
# the deployed environment exercises the same network and identity
# the runtime uses.

# Resolves the deployed image from ``gcloud run services
# describe`` rather than passing the local ``{{gcp_image}}:{{git_sha}}``
# — operators with stale checkouts, dirty trees, or branches that
# diverge from prod must not run diagnostics against an image that
# isn't actually serving traffic.  The doctor's value is precisely
# that it inspects what's *deployed*; using anything else makes
# the check a lie.

# Run check_validibot against the deployed GCP environment.
[arg('stage', pattern='dev|staging|prod')]
doctor stage *args: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    DEPLOYED_IMAGE=$(just gcp _resolve-deployed-image {{stage}})
    just gcp _run-doctor-job {{stage}} "${DEPLOYED_IMAGE}" "{{args}}"

# Show recent ERROR / EXCEPTION lines from Cloud Run logs.
#
# Wraps ``gcloud logging read`` against the deployed services for the
# stage.  Default freshness is 1h, which matches the operator's
# typical "what just broke?" window.  Adjust via the second arg:
#
#   just gcp errors-since prod 1h
#   just gcp errors-since staging 6h
#   just gcp errors-since prod 30m
#
# Output is pretty-printed for the operator's terminal.  For
# machine-readable output (e.g. for grafana log-ingest), add
# ``--format=json`` to the gcloud invocation directly — wrapping
# that in this recipe would mean every operator who wants pretty
# output has to pass an extra flag, which is the wrong default.

# Show recent ERROR-level Cloud Run log entries (defaults to last 1h).
[arg('stage', pattern='dev|staging|prod')]
errors-since stage duration="1h": _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Stage-aware filter.  Cloud Run revisions are named
    # ``<service>-<revision>`` and the service name encodes the
    # stage; filtering at the resource level avoids paging through
    # unrelated logs from sibling stages.
    #
    # We surface BOTH ``cloud_run_revision`` (Services: web, worker)
    # AND ``cloud_run_job`` (Jobs: validators, migrate, setup, doctor,
    # backup) because the operator-visible failure surface includes
    # both.  Slice 4 / 5 / 7 work added Jobs that produce errors
    # operators need to see in this view.
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
        # Cloud Run Jobs for prod use the unsuffixed name.
        JOB_LABEL_FILTER="(resource.labels.job_name:\"${APP_NAME}-validator-\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-migrate\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-setup\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-doctor\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-backup-manifest\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-verify-backup\")"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        JOB_LABEL_FILTER="(resource.labels.job_name:\"${APP_NAME}-validator-\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-migrate-{{stage}}\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-setup-{{stage}}\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-doctor-{{stage}}\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-backup-manifest-{{stage}}\""
        JOB_LABEL_FILTER="${JOB_LABEL_FILTER} OR resource.labels.job_name=\"${APP_NAME}-verify-backup-{{stage}}\")"
    fi

    # ``severity>=ERROR`` is the minimum signal operators care about;
    # WARNING is too noisy for an at-a-glance "what just broke?"
    # view.  The OR-bridged service + job filter shows both surfaces
    # in one listing — the on-caller is the same person regardless
    # of which container produced the error.
    SERVICE_FILTER='resource.type="cloud_run_revision"'
    SERVICE_FILTER="${SERVICE_FILTER} AND (resource.labels.service_name=\"${WEB_SERVICE}\""
    SERVICE_FILTER="${SERVICE_FILTER} OR resource.labels.service_name=\"${WORKER_SERVICE}\")"

    JOB_FILTER='resource.type="cloud_run_job"'
    JOB_FILTER="${JOB_FILTER} AND ${JOB_LABEL_FILTER}"

    FILTER="(${SERVICE_FILTER}) OR (${JOB_FILTER})"
    FILTER="(${FILTER}) AND severity>=ERROR"

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Errors in the last {{duration}} — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Services:  ${WEB_SERVICE}, ${WORKER_SERVICE}"
    echo "  Jobs:      validators / migrate / setup / doctor / backup-manifest / verify-backup"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ``--limit=100`` keeps a single invocation bounded; an operator
    # seeing a flood of errors will scroll back via the cloud
    # console anyway, and 100 entries is enough to see the shape of
    # the failure.  ``--order=desc`` shows newest first (matches
    # ``tail -f`` mental model).  The format pulls service_name OR
    # job_name into a single SOURCE column so the operator sees one
    # interleaved listing rather than two parallel sections.
    gcloud logging read "$FILTER" \
        --freshness="{{duration}}" \
        --limit=100 \
        --order=desc \
        --format='table(
            timestamp.date(format="%Y-%m-%dT%H:%M:%S"):label=TIME,
            severity:label=SEV,
            resource.type:label=KIND,
            resource.labels.service_name.if(resource.labels.service_name):label=SOURCE,
            resource.labels.job_name.if(resource.labels.job_name):label=JOB,
            textPayload.if(textPayload):label=MESSAGE
        )' \
        --project={{gcp_project}}

    echo ""
    echo "Tip: for a single error in detail, use:"
    echo "  gcloud logging read '<filter>' --format=json --project={{gcp_project}}"

# Run the validibot.smoke-test.v1 command as a Cloud Run Job against the
# deployed environment. The Job needs the same Cloud SQL + Secret Manager
# wiring as the doctor / backup Jobs because the smoke test creates a
# real Submission and ValidationRun in the deployment's database.
# Returns 0 if the deployment can validate things end-to-end, non-zero
# otherwise (worker stuck, validator misconfigured, etc.).
#
# Forwards extra args (``--json``, ``--timeout-seconds``) through to the
# underlying command, e.g. ``just gcp smoke-test prod --json``.
#
# End-to-end smoke test — runs a tiny JSON Schema validation through the real pipeline.
[arg('stage', pattern='dev|staging|prod')]
smoke-test stage *args: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    DEPLOYED_IMAGE=$(just gcp _resolve-deployed-image {{stage}})
    just gcp _run-smoke-test-job {{stage}} "${DEPLOYED_IMAGE}" "{{args}}"

# ─────────────────────────────────────────────────────────────────────────
# _run-smoke-test-job: stage smoke-test execution as a Cloud Run Job
# ─────────────────────────────────────────────────────────────────────────
#
# Mirrors ``_run-doctor-job`` but for the smoke_test management
# command. The Job needs:
#   - access to the deployed Cloud SQL instance (creates a real
#     ValidationRun row);
#   - the same django-env secret the runtime uses (so settings line
#     up — DEPLOYMENT_TARGET, storage bucket names, etc.);
#   - a slightly longer task-timeout than doctor because the smoke
#     test waits for the worker to pick up a job and run it.
#
# The Job runs synchronously via ``--wait`` so the recipe's exit
# status is the smoke test's exit status — Phase 4's upgrade recipe
# can call this and trust the verdict.
[private]
_run-smoke-test-job stage image args:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="${APP_NAME}-smoke-test"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        JOB_NAME="${APP_NAME}-smoke-test-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    SMOKE_CMD="python manage.py smoke_test --target gcp --stage {{stage}} {{args}}"
    BASH_ARGS="-c,set -a && source /secrets/.env && set +a && ${SMOKE_CMD}"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Smoke test — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Job:     $JOB_NAME"
    echo "  Image:   {{image}}"
    echo "  Args:    {{args}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # Task timeout 180s gives the smoke test up to its default
    # 120s polling window plus headroom for cold-start and Job
    # bring-up. Adjust upward if a deployment has a very slow
    # worker startup; the Python --timeout-seconds flag still
    # bounds the wait inside the Job.
    if gcloud run jobs describe "$JOB_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        gcloud run jobs update "$JOB_NAME" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "$SA" \
            --set-cloudsql-instances "$DB" \
            --set-secrets=/secrets/.env="$SECRET":latest \
            --memory 512Mi \
            --task-timeout 180s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "$BASH_ARGS" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    else
        gcloud run jobs create "$JOB_NAME" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "$SA" \
            --set-cloudsql-instances "$DB" \
            --set-secrets=/secrets/.env="$SECRET":latest \
            --memory 512Mi \
            --task-timeout 180s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "$BASH_ARGS" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    fi

    set +e
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --wait \
        --format="value(metadata.name)" 2>&1)
    RC=$?
    set -e

    if [ -n "$EXECUTION" ]; then
        echo ""
        gcloud beta run jobs executions logs read "$EXECUTION" \
            --region {{gcp_region}} \
            --project {{gcp_project}} 2>/dev/null \
            || echo "(Could not retrieve execution logs — check GCP console)"
    fi

    echo ""
    if [ $RC -eq 0 ]; then
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  ✓ Smoke test passed on {{stage}}"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    else
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  ✗ Smoke test failed on {{stage}} (exit $RC)"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "  Diagnose with:"
        echo "    just gcp doctor {{stage}}             # configuration sanity"
        echo "    just gcp errors-since {{stage}} 5m    # recent service errors"
    fi
    exit $RC

# Application-level backup — Cloud SQL export + GCS media rsync +
# manifest.json with Validibot version, migration head, and file
# checksums.  The manifest format is shared with self-hosted
# backups (``validibot.backup.v1``); restore tooling will read
# either flavour without per-target branches.
#
# How this works:
#
#   1. Compute a UTC ``backup_id`` (``YYYYMMDDTHHMMSSZ``) and the
#      backup destination URI under
#      ``gs://<project>-validibot-backups-<stage>/<backup_id>/``.
#      The bucket is auto-created on first use, with object
#      versioning enabled and a retention policy operators tune via
#      env vars.
#
#   2. Cloud SQL export → ``gs://.../<backup_id>/db.sql.gz``.
#      ``gcloud sql export sql --offload`` runs the export from a
#      read replica when one exists, avoiding load on the primary.
#
#   3. Media rsync → ``gs://.../<backup_id>/media/``.  The source is
#      the deployed media bucket; gsutil tracks per-file metadata
#      that the manifest writer then consumes.
#
#   4. Compute the dump's sha256 by streaming it through gsutil cat
#      | sha256sum.  Skipping this would mean the manifest can't
#      verify the most critical artifact at restore time.
#
#   5. Run a Cloud Run Job that invokes ``manage.py
#      write_backup_manifest`` against the deployed environment.
#      The Job sees the live migration head + Postgres version,
#      which is exactly what restore needs to verify against.
#
#   6. Report the backup URI back to the operator with the exact
#      restore command they'd run later.
#
# Why a Cloud Run Job for the manifest writer:
#
#   The manifest's ``compatibility`` block needs Django's view of
#   the migration state, not the operator laptop's.  Running the
#   writer on the deployed image guarantees the manifest reflects
#   what the live database actually has.

# Run an application-level backup (Cloud SQL + media + manifest).
[arg('stage', pattern='dev|staging|prod')]
backup stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    # Resolve the deployed image so the manifest writer captures
    # the LIVE migration head + Validibot version, not whatever
    # the operator's local checkout happens to be.
    DEPLOYED_IMAGE=$(just gcp _resolve-deployed-image {{stage}})
    just gcp _run-backup {{stage}} "${DEPLOYED_IMAGE}"

# Restore from a backup produced by ``just gcp backup``.
#
# Reads ``manifest.json`` at the backup root, refuses on
# incompatibility, runs ``doctor --strict`` as a pre-flight, and on
# operator confirmation imports the DB dump and rsyncs the media
# bucket back into the deployed environment.
#
# Compatibility checks (boring self-hosting ADR AC #16):
#
#   - Schema version must be ``validibot.backup.v1``
#   - Backup's migration head must NOT be ahead of current code
#     (operator must upgrade the deployment first if it is)
#   - No cross-major Validibot version jumps
#
# Pre-flight check (AC #15):
#
#   - ``doctor --strict`` must exit 0; restore refuses to run
#     against an unhealthy deployment because the import operation
#     is destructive and a half-broken target makes recovery much
#     harder.
#
# What the recipe does NOT do (deferred work):
#
#   - ``--components config-only`` / ``data-only`` / ``full``
#     selection (AC #17).  This MVP always restores the data
#     component; config restoration is informational only because
#     the manifest stores Secret Manager *versions*, not values.
#     Operators see the recorded versions printed at the end and
#     re-pin them by hand if needed.
#   - Down-time orchestration (maintenance mode toggling, traffic
#     drain).  Cloud SQL import takes the DB offline briefly; the
#     deployed Cloud Run services queue requests during that
#     window.  Future iteration may add explicit
#     ``mode-maintenance`` / ``mode-live`` wrapping.

# Restore from a backup (DB import + media rsync + manifest verification).
[arg('stage', pattern='dev|staging|prod')]
restore stage path: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    DEPLOYED_IMAGE=$(just gcp _resolve-deployed-image {{stage}})
    just gcp _run-restore {{stage}} {{path}} "${DEPLOYED_IMAGE}"

# =============================================================================
# Support Bundle (Phase 6 of ADR-2026-04-27)
# =============================================================================
#
# Same lifecycle as the self-hosted ``collect-support-bundle`` recipe,
# different substrate. The app-side snapshot is captured by running
# ``collect_support_bundle`` as a Cloud Run Job (so it sees the live
# Cloud SQL connection); host-side artefacts are captured via gcloud:
#
#   - Cloud Run service + revision state
#   - Recent Cloud Run + Cloud SQL logs
#   - Validator job inventory
#
# Output: ``support-bundles/<stage>-<timestamp>.zip`` on the operator's
# laptop. Same redaction guarantee — sensitive setting values never
# leave the deployment unredacted.

# Generate a redacted support bundle zip for a GCP stage.
# Usage: just gcp collect-support-bundle prod [--output <path>]
[arg('stage', pattern='dev|staging|prod')]
collect-support-bundle stage *flags: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail

    OUTPUT=""
    # shellcheck disable=SC2086
    set -- {{flags}}
    while [ $# -gt 0 ]; do
        case "$1" in
            --output) shift; OUTPUT="${1:-}" ;;
            --output=*) OUTPUT="${1#--output=}" ;;
            "") ;;
            *)
                echo "ERROR: unknown flag: $1" >&2
                echo "Usage: just gcp collect-support-bundle <stage> [--output <path>]" >&2
                exit 1
                ;;
        esac
        shift
    done

    APP_NAME="{{app_name}}"
    BUNDLE_ID=$(date -u +%Y%m%dT%H%M%SZ)
    if [ -z "${OUTPUT}" ]; then
        OUTPUT="support-bundles/support-bundle-{{stage}}-${BUNDLE_ID}.zip"
    fi
    mkdir -p "$(dirname "${OUTPUT}")"

    WORKDIR=$(mktemp -d)
    trap 'rm -rf "$WORKDIR"' EXIT

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
        DB_INSTANCE="${APP_NAME}-db"
        SECRET="django-env"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        JOB_NAME="${APP_NAME}-support-bundle"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        JOB_NAME="${APP_NAME}-support-bundle-{{stage}}"
    fi

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Support bundle — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Bundle ID:  ${BUNDLE_ID}"
    echo "  Output:     ${OUTPUT}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Step 1: App-side snapshot via Cloud Run Job ───────────────────
    # Mirrors _run-doctor-job's pattern: resolve the deployed image,
    # invoke ``collect_support_bundle`` against it as a one-off Job.
    # We capture the JSON output by reading the Job's execution logs
    # — Cloud Run captures stdout there.
    echo "Step 1/4: Capturing app-side snapshot via Cloud Run Job..."
    DEPLOYED_IMAGE=$(just gcp _resolve-deployed-image {{stage}})
    DB="{{gcp_project}}:{{gcp_region}}:${DB_INSTANCE}"
    BASH_ARGS="-c,set -a && source /secrets/.env && set +a && python manage.py collect_support_bundle --no-indent"

    if gcloud run jobs describe "${JOB_NAME}" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        gcloud run jobs update "${JOB_NAME}" \
            --image "${DEPLOYED_IMAGE}" \
            --region {{gcp_region}} \
            --service-account "${SA}" \
            --set-cloudsql-instances "${DB}" \
            --set-secrets=/secrets/.env="${SECRET}":latest \
            --memory 512Mi \
            --task-timeout 120s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "${BASH_ARGS}" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    else
        gcloud run jobs create "${JOB_NAME}" \
            --image "${DEPLOYED_IMAGE}" \
            --region {{gcp_region}} \
            --service-account "${SA}" \
            --set-cloudsql-instances "${DB}" \
            --set-secrets=/secrets/.env="${SECRET}":latest \
            --memory 512Mi \
            --task-timeout 120s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "${BASH_ARGS}" \
            --project {{gcp_project}} \
            --quiet >/dev/null
    fi

    # Capture stdout (the execution name) and stderr (gcloud progress
    # banners, warnings) separately. Earlier this used ``2>&1`` which
    # mixed the two streams; if gcloud printed any progress text while
    # still exiting 0, ``EXECUTION`` would carry a multi-line value
    # and the subsequent logging-filter call would receive garbage,
    # silently losing app-snapshot.json from the bundle. Now stderr
    # goes to a side log we can show on failure, and stdout is
    # validated against the expected Cloud Run execution-name shape.
    set +e
    EXECUTION_STDERR="${WORKDIR}/_app_snapshot_execute_stderr.log"
    EXECUTION=$(gcloud run jobs execute "${JOB_NAME}" \
        --region={{gcp_region}} --project={{gcp_project}} --wait \
        --format='value(metadata.name)' 2>"${EXECUTION_STDERR}")
    SNAPSHOT_RC=$?
    set -e

    # Validate the captured value: Cloud Run execution names look like
    # ``<job-name>-<5-char-hash>``. We don't try to enforce the exact
    # hash length (it's stable today but Cloud Run owns the naming
    # scheme), only that it's a single line of plausible-looking
    # characters and not blank. If it's not, treat it as a snapshot
    # failure rather than passing garbage to the logging filter.
    if [ ${SNAPSHOT_RC} -eq 0 ] && [ -n "${EXECUTION}" ] \
        && [ "$(printf '%s' "${EXECUTION}" | wc -l | tr -d ' ')" = "0" ] \
        && printf '%s' "${EXECUTION}" | grep -qE '^[A-Za-z0-9._-]+$'; then
        # Read the execution's logs via the structured ``textPayload``
        # field. Earlier this used ``awk '/^{/,EOF'`` to slice from the
        # first ``{`` to EOF, which broke whenever Cloud Run prepended
        # a timestamp banner or wrapped the payload — the bundle then
        # silently lost the snapshot. Reading the textPayload directly
        # gives us the raw stdout the Job wrote, with no log-line
        # prefixes to strip.
        gcloud logging read \
            "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB_NAME}\" AND labels.\"run.googleapis.com/execution_name\"=\"${EXECUTION}\"" \
            --project={{gcp_project}} \
            --limit=1000 \
            --order=asc \
            --format='value(textPayload)' 2>/dev/null \
            | awk 'NF' > "${WORKDIR}/app-snapshot.json"
        # The ``--no-indent`` form of the management command emits the
        # whole JSON on one line, so a single non-empty textPayload
        # entry is all we need. We still pass through awk to strip
        # any blank lines that might come from the log query.
        if [ -s "${WORKDIR}/app-snapshot.json" ] \
            && head -c1 "${WORKDIR}/app-snapshot.json" | grep -q '{'; then
            echo "  ✓ App snapshot captured."
        else
            echo "  ⚠ Job ran but produced no parseable JSON — check execution logs."
            echo "    gcloud beta run jobs executions logs read ${EXECUTION} \\"
            echo "      --region={{gcp_region}} --project={{gcp_project}}"
            rm -f "${WORKDIR}/app-snapshot.json"
        fi
    else
        echo "  ⚠ Cloud Run Job execution failed or returned an unrecognised"
        echo "    execution name — bundle will be host-side only."
        if [ -s "${EXECUTION_STDERR}" ]; then
            echo "    gcloud stderr:"
            sed 's/^/      /' "${EXECUTION_STDERR}"
            # Move the stderr into the bundle so support sees what
            # went wrong without needing the operator to retype it.
            mv "${EXECUTION_STDERR}" \
                "${WORKDIR}/app-snapshot-execute-stderr.log"
        fi
    fi
    # If we got here via the success path, drop the now-stale stderr
    # log so it doesn't clutter the bundle.
    rm -f "${EXECUTION_STDERR}"
    echo ""

    # ── Step 2: Cloud Run service state ───────────────────────────────
    echo "Step 2/4: Capturing Cloud Run service state..."
    gcloud run services describe "${WEB_SERVICE}" \
        --region={{gcp_region}} --project={{gcp_project}} \
        > "${WORKDIR}/web-service.txt" 2>&1 || true
    gcloud run services describe "${WORKER_SERVICE}" \
        --region={{gcp_region}} --project={{gcp_project}} \
        > "${WORKDIR}/worker-service.txt" 2>&1 || true
    gcloud sql instances describe "${DB_INSTANCE}" \
        --project={{gcp_project}} \
        > "${WORKDIR}/sql-instance.txt" 2>&1 || true
    echo "  ✓ Cloud Run + Cloud SQL state captured."
    echo ""

    # ── Step 3: Recent logs ───────────────────────────────────────────
    # 1h freshness, severity>=WARNING captures both errors and recent
    # warnings. We deliberately don't lower severity further; INFO logs
    # blow up bundle size without adding diagnostic value.
    #
    # The output is piped through ``redact-text.py`` (a stdlib-only
    # standalone copy of the support_bundle redaction patterns)
    # before landing in the bundle. Without redaction, log payloads
    # containing tokens, embedded URL auth, or hex-shaped secrets
    # would leak verbatim — undermining the app-side snapshot's
    # name-based redaction.
    echo "Step 3/4: Capturing recent logs (last 1h, WARNING+, redacted)..."
    REDACTOR="deploy/self-hosted/scripts/redact-text.py"
    SERVICE_FILTER="resource.type=\"cloud_run_revision\" AND (resource.labels.service_name=\"${WEB_SERVICE}\" OR resource.labels.service_name=\"${WORKER_SERVICE}\") AND severity>=WARNING"
    # The redactor is invoked via ``python3 <path>``, so the file's
    # executable bit doesn't matter — we check ``-f`` (file exists)
    # rather than ``-x``. Earlier the check was ``-x`` and the file
    # ships as 100644 in some clones, which made every GCP bundle
    # silently fall through to raw logs.
    if [ -f "${REDACTOR}" ] && command -v python3 >/dev/null 2>&1; then
        gcloud logging read "${SERVICE_FILTER}" \
            --freshness=1h \
            --limit=200 \
            --order=desc \
            --format='value(timestamp,severity,resource.labels.service_name,textPayload)' \
            --project={{gcp_project}} 2>&1 \
            | python3 "${REDACTOR}" > "${WORKDIR}/recent-cloud-logs.txt" || true
        echo "  ✓ Recent logs captured (piped through redact-text.py)."
    else
        # The redactor isn't available. The previous fallback wrote
        # raw logs into the bundle with a "logs are RAW" banner. That
        # broke the redacted-bundle contract: operators reading the
        # README expect the bundle never carries unredacted output.
        # The new fallback OMITS logs entirely and writes a placeholder
        # (shipped as a kit file rather than a heredoc, because just's
        # recipe parser chokes on the special characters in the
        # placeholder text).
        if [ -f deploy/self-hosted/support-bundle-logs-omitted.txt ]; then
            cp deploy/self-hosted/support-bundle-logs-omitted.txt \
                "${WORKDIR}/recent-cloud-logs.txt"
        else
            echo "Logs omitted: redactor unavailable. See README." \
                > "${WORKDIR}/recent-cloud-logs.txt"
        fi
        echo "  ⚠ ${REDACTOR} or python3 unavailable — logs OMITTED."
        echo "    Bundle includes a placeholder explaining what happened."
    fi
    echo ""

    # ── Step 4: Versions + manifest ───────────────────────────────────
    echo "Step 4/4: Writing manifest..."
    {
        echo "stage: {{stage}}"
        echo "bundle_id: ${BUNDLE_ID}"
        echo "captured_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo ""
        echo "[gcloud]"
        gcloud version 2>/dev/null || echo "(gcloud version unavailable)"
        echo ""
        echo "[deployed image]"
        echo "${DEPLOYED_IMAGE}"
    } > "${WORKDIR}/versions.txt"

    if [ -f deploy/self-hosted/support-bundle-README.txt ]; then
        cp deploy/self-hosted/support-bundle-README.txt "${WORKDIR}/README.txt"
    fi

    if ! command -v zip >/dev/null 2>&1; then
        echo "Error: 'zip' not found on host — install it (apt install zip)." >&2
        exit 1
    fi
    (cd "${WORKDIR}" && zip -q -r "$(cd "$(dirname "${OLDPWD}/${OUTPUT}")" && pwd)/$(basename "${OUTPUT}")" .)
    BUNDLE_SIZE=$(du -sh "${OUTPUT}" 2>/dev/null | awk '{print $1}')

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✓ Bundle written: ${OUTPUT} (${BUNDLE_SIZE})"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Review with:"
    echo "    unzip -l ${OUTPUT}"
    echo "    unzip -p ${OUTPUT} app-snapshot.json | jq ."

# Validator inventory — list every validator backend Cloud Run Job
# for the stage with its configured image, digest, image-policy
# compliance, and last execution status.
#
# What this surface is for:
#
#   ``VALIDATOR_BACKEND_IMAGE_POLICY`` selects tag, digest, or
#   signed-digest on GCP just as it does elsewhere. ``check_validibot``
#   reports that deployment-wide choice via ``VB712`` / ``VB713``.
#   Operators also need a *per-validator* view: which backends satisfy
#   the configured policy and when each was last executed successfully.
#   ``just gcp validators prod`` is that view.
#
# What this surface is NOT for:
#
#   This is read-only inventory, not a deploy / redeploy entry
#   point — for that, use ``validator-deploy`` or
#   ``validator-deploy-all``. It also doesn't repeat signed-release
#   verification; the column reports whether the configured provider
#   image is digest-pinned, which is fast to check from gcloud alone.

# List validator backend Cloud Run Jobs with image, pinning, and last-run status.
[arg('stage', pattern='dev|staging|prod')]
validators stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # The validator-job-deploy recipe labels every validator Job with
    # ``validator=<name>``, so we filter by the label rather than
    # by name pattern.  Label-based filtering is more robust than
    # parsing names that may evolve (e.g. if we ever support
    # tenant-scoped validator jobs).
    if [ "{{stage}}" = "prod" ]; then
        STAGE_LABEL="prod"
    else
        STAGE_LABEL="{{stage}}"
    fi

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Validator inventory — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # List every validator Job for the stage.  ``--filter`` accepts
    # GCP label selectors via the ``metadata.labels.<key>:<value>``
    # syntax; we use it twice (validator label exists, stage label
    # matches) to scope the listing precisely.
    JOBS=$(gcloud run jobs list \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --filter="metadata.labels.stage:${STAGE_LABEL} AND metadata.labels.validator:*" \
        --format="value(metadata.name)" 2>/dev/null || true)

    if [ -z "$JOBS" ]; then
        echo "  No validator backends found for stage '{{stage}}'."
        echo ""
        echo "  Deploy one signed release to all Jobs and Services with:"
        echo "    just gcp validator-deploy-all {{stage}} vX.Y.Z"
        echo "  Or deploy one backend with:"
        echo "    just gcp validator-deploy energyplus {{stage}} vX.Y.Z"
        exit 0
    fi

    # Per-job inventory.  For each Job we read:
    #   - the configured image (full reference including digest if pinned)
    #   - the validator name from the labels
    #   - the most recent execution's completion timestamp + status
    #
    # We deliberately don't summarise multiple executions — operators
    # who need execution history have ``gcloud run jobs executions
    # list``; this view is "current configuration + last result," at
    # a glance.
    printf "%-40s %-12s %-9s %-22s %s\n" "JOB" "VALIDATOR" "PINNING" "LAST RUN" "IMAGE"
    printf "%-40s %-12s %-9s %-22s %s\n" "----------------------------------------" "------------" "---------" "----------------------" "-----"

    while IFS= read -r JOB_NAME; do
        [ -z "$JOB_NAME" ] && continue

        # Single describe call extracts everything we need; using
        # ``--format`` keeps the parse in gcloud rather than shell
        # so we don't have to deal with YAML quoting.
        DESCRIBE=$(gcloud run jobs describe "$JOB_NAME" \
            --region={{gcp_region}} \
            --project={{gcp_project}} \
            --format='value(spec.template.spec.template.spec.containers[0].image,metadata.labels.validator)' 2>/dev/null \
            || echo -e "(unknown)\t(unknown)")
        IMAGE=$(echo "$DESCRIBE" | awk -F'\t' '{print $1}')
        VALIDATOR=$(echo "$DESCRIBE" | awk -F'\t' '{print $2}')

        # Image-pinning check.  Match the same heuristic the doctor
        # uses (``image_policy.is_digest_pinned`` — substring
        # ``@sha256:``).  Keeping the check here in shell rather
        # than calling out to Python keeps the recipe operationally
        # simple; the doctor still owns the authoritative check.
        if [[ "$IMAGE" == *"@sha256:"* ]]; then
            PINNING="digest"
        elif [[ "$IMAGE" == *":"* ]]; then
            PINNING="tag"
        else
            PINNING="(none)"
        fi

        # Last execution status.  The list-executions call returns
        # newest first; we take the head and read the completion
        # state.  An empty result means "never executed" — common
        # for a freshly deployed validator.
        LAST_EXEC=$(gcloud run jobs executions list \
            --job="$JOB_NAME" \
            --region={{gcp_region}} \
            --project={{gcp_project}} \
            --limit=1 \
            --format='value(metadata.creationTimestamp,status.conditions[0].type,status.conditions[0].status)' 2>/dev/null \
            || echo "")

        if [ -z "$LAST_EXEC" ]; then
            LAST_RUN="(never)"
        else
            TS=$(echo "$LAST_EXEC" | awk -F'\t' '{print $1}' | cut -c1-19)
            COND_STATUS=$(echo "$LAST_EXEC" | awk -F'\t' '{print $3}')
            if [ "$COND_STATUS" = "True" ]; then
                LAST_RUN="$TS ✓"
            else
                LAST_RUN="$TS ✗"
            fi
        fi

        printf "%-40s %-12s %-9s %-22s %s\n" \
            "$JOB_NAME" "$VALIDATOR" "$PINNING" "$LAST_RUN" "$IMAGE"
    done <<< "$JOBS"

    echo ""
    echo "Pinning legend:"
    echo "  digest  — image is pinned by sha256 digest (production-recommended)"
    echo "  tag     — image is on a floating tag (rejected under digest/signed-digest policy)"
    echo "  (none)  — no image set (misconfigured Job)"
    echo ""
    echo "Tip: run 'just gcp doctor {{stage}}' for the deployment-wide"
    echo "image-policy check (VB712 / VB713)."

# List the immutable Cloud Run Service side of the validator deployment seam.
# Application routing/readiness remains authoritative in
# ``validator-deployments-list``; this view exposes live provider drift.
[arg('stage', pattern='dev|staging|prod')]
validator-services stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Validator Cloud Run Services — {{stage}}"
    SERVICES=$(gcloud run services list \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --filter="metadata.labels.stage:{{stage}} AND metadata.labels.validator:*" \
        --format='value(metadata.name)' 2>/dev/null || true)
    if [ -z "$SERVICES" ]; then
        echo "No validator Services found."
        exit 0
    fi
    printf "%-48s %-28s %-5s %-7s %-7s %s\n" \
        "SERVICE" "READY REVISION" "CONC" "MIN" "MAX" "IMAGE"
    while IFS= read -r SERVICE; do
        [ -z "$SERVICE" ] && continue
        SERVICE_JSON=$(gcloud run services describe "$SERVICE" \
            --region={{gcp_region}} \
            --project={{gcp_project}} \
            --format=json)
        jq -r '[
            .metadata.name,
            .status.latestReadyRevisionName,
            (.spec.template.spec.containerConcurrency // ""),
            (.metadata.annotations["run.googleapis.com/minScale"]
                // .spec.template.metadata.annotations["autoscaling.knative.dev/minScale"]
                // "0"),
            (.metadata.annotations["run.googleapis.com/maxScale"]
                // .spec.template.metadata.annotations["autoscaling.knative.dev/maxScale"]
                // ""),
            .spec.template.spec.containers[0].image
        ] | @tsv' <<< "$SERVICE_JSON" \
            | awk -F'\t' '{printf "%-48s %-28s %-5s %-7s %-7s %s\n", $1, $2, $3, $4, $5, $6}'
    done <<< "$SERVICES"
    echo ""
    echo "Application routing: just gcp validator-deployments-list {{stage}}"

# =============================================================================
# Cleanup (Phase 5 of ADR-2026-04-27)
# =============================================================================
#
# Long-running GCP deployments accumulate artefacts that aren't part of
# any working set: completed Cloud Run Job executions, expired backup
# objects in GCS, old validator Job revisions. This recipe scans, lists
# candidates, and removes them after operator confirmation.
#
# What this cleans (per stage):
#   - Cloud Run Job executions older than ${EXECUTION_RETAIN_DAYS}
#     (default 30d). Cloud Run keeps execution metadata indefinitely
#     by default, which can pile up across high-cardinality validator
#     stages. We don't touch the Jobs themselves — only their
#     execution history.
#   - Manifested backup objects in the per-stage backup bucket older
#     than ${BACKUP_RETAIN_DAYS} (default 30d). Determined by parsing
#     each backup's manifest.json::created_at.
#
# What this does NOT clean:
#   - Cloud Run Services or Job DEFINITIONS — those are part of the
#     deployment surface; ``just gcp deploy-all`` manages them.
#   - Artifact Registry images — those are managed by GCP's own
#     lifecycle policies. Configure retention there directly if you
#     want to reclaim image storage.
#   - Cloud SQL automatic backups — those are managed by Cloud SQL's
#     retention policy, not by us.
#
# Pattern adopted from Discourse's ``./launcher cleanup``: always show
# what would be deleted before deleting, even without ``--dry-run``.
# Safe to run on a Cloud Scheduler cron with ``--yes`` to skip the
# interactive confirm.

# Prune old Cloud Run Job executions and expired GCS backups (Phase 5).
# Usage: just gcp cleanup <stage> [--dry-run] [--yes]
[arg('stage', pattern='dev|staging|prod')]
cleanup stage *flags: _require-gcp-config
    @just gcp _run-cleanup {{stage}} "{{flags}}"

# Run the GCP cleanup orchestration.
[private]
_run-cleanup stage flags:
    #!/usr/bin/env bash
    set -euo pipefail

    DRY_RUN=0
    YES=0
    # shellcheck disable=SC2086
    set -- {{flags}}
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) DRY_RUN=1 ;;
            --yes) YES=1 ;;
            "") ;;
            *)
                echo "ERROR: unknown flag: $1" >&2
                echo "Usage: just gcp cleanup <stage> [--dry-run] [--yes]" >&2
                exit 1
                ;;
        esac
        shift
    done

    APP_NAME="{{app_name}}"
    EXECUTION_RETAIN_DAYS="${EXECUTION_RETAIN_DAYS:-30}"
    BACKUP_RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-30}"

    # Backup bucket name follows the same convention as ``just gcp
    # backup``: <project>-<app>-backups-<stage>. Operators who
    # overrode it via GCP_BACKUP_BUCKET in their backup runs need to
    # set the same here.
    BACKUP_BUCKET="${GCP_BACKUP_BUCKET:-{{gcp_project}}-${APP_NAME}-backups-{{stage}}}"

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Cleanup — {{stage}} ($([ ${DRY_RUN} -eq 1 ] && echo 'DRY RUN' || echo 'live'))"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Job execution retention: ${EXECUTION_RETAIN_DAYS}d"
    echo "  Backup retention:        ${BACKUP_RETAIN_DAYS}d"
    echo "  Backup bucket:           gs://${BACKUP_BUCKET}/"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Scope 1: old Cloud Run Job executions (stage-scoped) ──────────
    # ``gcloud run jobs executions list`` returns every execution
    # across every Job in the project. We filter:
    #
    #   1. by stage (so ``cleanup dev`` never touches prod or staging);
    #   2. by completion timestamp (older than the retention window).
    #
    # Stage scoping uses the parent Job's name pattern, which Cloud
    # Run records on each execution as
    # ``metadata.labels."run.googleapis.com/job"``. The naming
    # conventions match the deploy recipes:
    #
    #   - prod: ``<app>-<purpose>`` (no stage suffix)
    #   - dev/staging: ``<app>-<purpose>-<stage>``
    #
    # Without this filter, a ``cleanup dev`` would happily delete
    # production execution history — the bug this fix addresses.
    echo "Scope 1/2: Cloud Run Job executions for {{stage}} older than ${EXECUTION_RETAIN_DAYS}d..."
    if ! command -v jq >/dev/null 2>&1; then
        echo "  ERROR: 'jq' not found on host — install it (apt install jq)." >&2
        exit 1
    fi

    # ISO 8601 cutoff — gcloud emits timestamps in this format, so
    # string compare is sufficient (and faster than two ``date``
    # round-trips per execution).
    if CUTOFF_ISO=$(date -u -d "${EXECUTION_RETAIN_DAYS} days ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null); then
        : # GNU
    else
        # BSD date doesn't accept "X days ago" — compute via
        # ``-v`` instead.
        CUTOFF_ISO=$(date -u -v "-${EXECUTION_RETAIN_DAYS}d" +%Y-%m-%dT%H:%M:%SZ)
    fi

    # Build the stage-specific job-name regexes for the jq filter:
    #   - INCLUDE matches Jobs in this stage.
    #   - EXCLUDE rejects non-prod jobs from the prod set (since prod
    #     jobs are unsuffixed, they'd otherwise match the dev/staging
    #     names by simple prefix).
    if [ "{{stage}}" = "prod" ]; then
        STAGE_INCLUDE_RE="^${APP_NAME}-"
        STAGE_EXCLUDE_RE="-(dev|staging)$"
    else
        STAGE_INCLUDE_RE="^${APP_NAME}-.*-{{stage}}$"
        STAGE_EXCLUDE_RE=""
    fi

    EXECUTIONS_JSON=$(gcloud run jobs executions list \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format=json 2>/dev/null || echo '[]')

    # The parent-job label key contains a slash, which jq accesses
    # via bracket-string syntax. We surface the parsed job name as
    # the third pipe-separated field so the listing below can show
    # operators which Job each execution belonged to.
    EXECUTION_CANDIDATES=$(echo "${EXECUTIONS_JSON}" | jq -r \
        --arg cutoff "${CUTOFF_ISO}" \
        --arg include "${STAGE_INCLUDE_RE}" \
        --arg exclude "${STAGE_EXCLUDE_RE}" \
        '
        .[] | . as $e |
        ($e.metadata.labels["run.googleapis.com/job"] // "") as $job |
        select(
            ($e.metadata.creationTimestamp // "") < $cutoff
            and ($e.status.completionTime // "") != ""
            and ($job | test($include))
            and (if $exclude != "" then ($job | test($exclude) | not) else true end)
        ) | "\($e.metadata.namespace)|\($e.metadata.name)|\($job)|\($e.metadata.creationTimestamp)"
    ' || true)

    EXECUTION_COUNT=$(echo "${EXECUTION_CANDIDATES}" | grep -c . || true)
    if [ "${EXECUTION_COUNT}" -eq 0 ]; then
        echo "  (none)"
    else
        # Field order (matches the jq output): namespace|name|job|created.
        # Showing the parent JOB column makes stage scoping auditable
        # — operators reviewing the dry-run see exactly which jobs
        # the cleanup would touch.
        echo "${EXECUTION_CANDIDATES}" | head -20 | while IFS='|' read -r NAMESPACE NAME JOB CREATED; do
            [ -z "${NAME}" ] && continue
            printf "  %-40s  job=%-30s  %s\n" "${NAME}" "${JOB}" "${CREATED:0:19}"
        done
        if [ "${EXECUTION_COUNT}" -gt 20 ]; then
            echo "  ... and $((EXECUTION_COUNT - 20)) more."
        fi
    fi
    echo ""

    # ── Scope 2: expired GCS backup objects ───────────────────────────
    # The bucket contains one prefix per backup_id (timestamp). We
    # walk top-level prefixes, fetch each manifest.json, parse
    # ``created_at``, and select prefixes older than the cutoff. A
    # missing manifest.json means "not a Validibot backup" — we
    # leave those alone.
    echo "Scope 2/2: Manifested backups older than ${BACKUP_RETAIN_DAYS}d..."
    BACKUP_CUTOFF_S=$(($(date -u +%s) - BACKUP_RETAIN_DAYS * 86400))
    BACKUP_CANDIDATES=""
    if gcloud storage buckets describe "gs://${BACKUP_BUCKET}" \
        --project={{gcp_project}} &>/dev/null; then
        # gsutil list prints one prefix per line ending with /.
        # Bare ``ls`` (not ``-r``) gives just the top-level entries,
        # which is exactly the per-backup_id structure we want.
        for PREFIX in $(gcloud storage ls "gs://${BACKUP_BUCKET}/" --project={{gcp_project}} 2>/dev/null || true); do
            MANIFEST_URI="${PREFIX}manifest.json"
            CREATED=$(gcloud storage cat "${MANIFEST_URI}" --project={{gcp_project}} 2>/dev/null | jq -r '.created_at // empty' 2>/dev/null || true)
            [ -z "${CREATED}" ] && continue
            if CREATED_S=$(date -d "${CREATED}" +%s 2>/dev/null); then
                : # GNU
            elif CREATED_S=$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "${CREATED%.*Z}+0000" +%s 2>/dev/null); then
                : # BSD
            else
                continue
            fi
            if [ "${CREATED_S}" -lt "${BACKUP_CUTOFF_S}" ]; then
                BACKUP_CANDIDATES="${BACKUP_CANDIDATES}${PREFIX}|${CREATED}\n"
            fi
        done
    else
        echo "  Backup bucket gs://${BACKUP_BUCKET} not found (no backups have run on this stage)."
    fi
    if [ -z "${BACKUP_CANDIDATES}" ]; then
        echo "  (none)"
    else
        printf "${BACKUP_CANDIDATES}" | while IFS='|' read -r PREFIX CREATED; do
            [ -z "${PREFIX}" ] && continue
            printf "  %s  (created %s)\n" "${PREFIX}" "${CREATED:0:19}"
        done
    fi
    echo ""

    # ── Decision point ────────────────────────────────────────────────
    HAS_WORK=0
    [ "${EXECUTION_COUNT}" -gt 0 ] && HAS_WORK=1
    [ -n "${BACKUP_CANDIDATES}" ] && HAS_WORK=1

    if [ ${HAS_WORK} -eq 0 ]; then
        echo "Nothing to clean up."
        exit 0
    fi

    if [ ${DRY_RUN} -eq 1 ]; then
        echo "Dry run — no changes made."
        exit 0
    fi

    if [ ${YES} -eq 0 ]; then
        echo "Type 'yes' to delete the items listed above:"
        read -r CONFIRM
        if [ "${CONFIRM}" != "yes" ]; then
            echo "Aborted by operator (no changes made)."
            exit 1
        fi
    fi

    # ── Delete ────────────────────────────────────────────────────────
    if [ "${EXECUTION_COUNT}" -gt 0 ]; then
        echo "${EXECUTION_CANDIDATES}" | while IFS='|' read -r NAMESPACE NAME _ _; do
            [ -z "${NAME}" ] && continue
            gcloud run jobs executions delete "${NAME}" \
                --region={{gcp_region}} \
                --project={{gcp_project}} \
                --quiet >/dev/null 2>&1 || true
        done
        echo "  ✓ ${EXECUTION_COUNT} Cloud Run Job execution(s) removed."
    fi

    if [ -n "${BACKUP_CANDIDATES}" ]; then
        printf "${BACKUP_CANDIDATES}" | while IFS='|' read -r PREFIX _; do
            [ -z "${PREFIX}" ] && continue
            gcloud storage rm --recursive "${PREFIX}" \
                --project={{gcp_project}} \
                --quiet >/dev/null 2>&1 || true
        done
        echo "  ✓ Expired backup objects removed from gs://${BACKUP_BUCKET}/."
    fi

    echo ""
    echo "Cleanup complete on {{stage}}."
