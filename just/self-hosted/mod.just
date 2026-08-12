# =============================================================================
# Validibot - Self-Hosted Deployment
# =============================================================================
#
# Commands for deploying and managing a self-hosted Validibot instance.
# Self-hosted means a customer-operated single-VM deployment using
# Docker Compose — typically on DigitalOcean, AWS EC2, Hetzner, or
# on-prem. See ADR-2026-04-27 (Boring Self-Hosting and Operator
# Experience) for the full self-hosted operator experience.
#
# This module is the operator-facing entry point. The same recipes are
# mirrored in ``just/gcp/`` for cross-target parity (where applicable);
# the only intentional asymmetry is stage handling — self-hosted is
# single-stage per VM (no stage argument), while GCP is multi-stage.
#
# When to use self-hosted:
#   - Customers running their own copy on DigitalOcean / EC2 / Hetzner
#   - On-premises deployments
#   - Air-gapped or restricted network environments
#   - Data-custody-sensitive evaluations
#
# Architecture:
#   - Docker Compose for container orchestration (docker-compose.production.yml)
#   - PostgreSQL for the database (containerized or external)
#   - Celery + Redis for background tasks
#   - Optional Caddy reverse proxy via the ``caddy`` Compose profile
#     (off by default; bring your own proxy or set COMPOSE_PROFILES=caddy)
#   - Local filesystem or S3-compatible storage for files
#
# Prerequisites:
#   - Docker and Docker Compose installed
#   - Sufficient disk space for database and uploads
#   - SSL certificate (Let's Encrypt via Caddy or your own proxy)
#
# Quick start:
#   1. Copy .envs.example/.production/.self-hosted/.django to .envs/.production/.self-hosted/.django
#   2. Copy .envs.example/.production/.self-hosted/.postgres to .envs/.production/.self-hosted/.postgres
#   3. Copy .envs.example/.production/.self-hosted/.build to .envs/.production/.self-hosted/.build
#   4. Edit the environment files with your settings
#   5. just self-hosted check-env
#   6. just self-hosted bootstrap
#
# =============================================================================

# Use bash for shell commands
set shell := ["bash", "-cu"]
# Just modules otherwise run relative to their own source directory. Self-hosted
# paths are repository-root-relative, matching the operator commands and docs.
set working-directory := '../..'

# =============================================================================
# Configuration
# =============================================================================

# Docker Compose file for production deployment
# This should be a production-ready compose file (not the local dev one)
compose_file := "docker-compose.production.yml"

# Compose project name (used for container naming)
compose_project := "validibot"

# Environment files for the Docker Compose production deployment
django_env_file := ".envs/.production/.self-hosted/.django"
postgres_env_file := ".envs/.production/.self-hosted/.postgres"
build_env_file := ".envs/.production/.self-hosted/.build"
compose_env_args := `bash -lc 'if [ -f ".envs/.production/.self-hosted/.build" ]; then printf -- "--env-file .envs/.production/.self-hosted/.build"; fi'`

# Git SHA for image tagging
git_sha := `git rev-parse --short HEAD`

# App version baked into the locally-built image as VALIDIBOT_VERSION
# (consumed by backup compatibility checks and doctor metadata).
#
# Version-stamping policy: pyproject.toml is the source of truth on
# every deploy path. The upgrade recipe overrides this via env var
# (``VALIDIBOT_VERSION="${TARGET}"`` after ``git checkout``); daily
# deploys use whatever pyproject says at the current commit. See the
# detailed rationale in ``just/common.just`` (deliberately repeated
# here because just modules have isolated scope and the constant
# assignment must live in the module that uses it).
validibot_version := `awk -F'"' '/^[[:space:]]*version[[:space:]]*=/ {print "v" $2; exit}' "$(git rev-parse --show-toplevel)/pyproject.toml"`

# Validator backend image metadata. This is only used for local images built
# from a sibling source checkout; runtime trust uses the resolved image digest.
validator_backend_git_sha := `git -C ../validibot-validator-backends rev-parse --short HEAD 2>/dev/null || echo "dev"`

# =============================================================================
# Container Lifecycle
# =============================================================================

# Source recipe-level knobs from .envs/.production/.self-hosted/.build
# if present. Kept as a helper so every lifecycle recipe (up, build,
# build-only) picks up the same variables. The file's build-time vars
# (VALIDIBOT_COMMERCIAL_PACKAGE, etc) are separately passed to docker
# compose via ``--env-file`` through ``compose_env_args`` above.
[private]
_load-build-env:
    #!/usr/bin/env bash
    overrides=""
    for variable_name in \
        VALIDATOR_CONTAINER_SOCKET \
        VALIDIBOT_COMMERCIAL_PACKAGE VALIDIBOT_COMMERCIAL_NETRC; do
        if [ "${!variable_name+x}" = x ]; then
            printf -v override 'export %s=%q\n' "$variable_name" "${!variable_name}"
            overrides+="$override"
        fi
    done
    if [ -f .envs/.production/.self-hosted/.build ]; then
        echo "set -a"
        # shellcheck disable=SC1091
        echo "source .envs/.production/.self-hosted/.build"
        echo "set +a"
    fi
    # Explicit recipe environment overrides the shared file, matching Docker
    # Compose's shell-over-env-file precedence and the documented CLI contract.
    printf '%s' "$overrides"
    echo 'export VALIDIBOT_VERSION="${VALIDIBOT_VERSION:-{{validibot_version}}}"'
    echo 'export VALIDIBOT_REVISION="${VALIDIBOT_REVISION:-{{git_sha}}}"'

# Start all services in production mode. The licensed MCP endpoint is part of
# the web service and needs no separate Compose profile.
up:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just self-hosted _load-build-env)"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d

# Stop all services (containers remain, data persists).
down:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} down

# Build images and start services.
build:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just self-hosted _load-build-env)"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d --build

# Build images without starting (useful for CI/CD).
build-only:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just self-hosted _load-build-env)"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} build

# Wait until the web container accepts management commands
_wait-web:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Waiting for web container to become ready..."
    for _ in {1..60}; do
        if docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web true >/dev/null 2>&1; then
            echo "Web container is ready."
            exit 0
        fi
        sleep 2
    done
    echo "Web container did not become ready in time."
    echo "Check logs with: just self-hosted logs-service web"
    exit 1

# First-time install for self-hosted customers
# Builds and starts the stack, applies migrations, seeds default data,
# registers OIDC clients (for MCP OAuth), and verifies the installation.
#
# ensure_oidc_clients creates the predefined public OAuth clients configured
# in the Django environment and is safe to run repeatedly.
bootstrap: check-env build _wait-web
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py check_migration_history
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py migrate --noinput
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py initialize_validibot --if-needed
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py ensure_oidc_clients
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py check_validibot
    @echo ""
    @echo "Bootstrap complete!"
    @echo "Next steps:"
    @echo "  1. Put Validibot behind your reverse proxy"
    @echo "  2. Visit your configured SITE_URL"
    @echo "  3. Sign in with the superuser credentials from {{django_env_file}}"

# Show status of all containers
status:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps

# Follow logs from all services
logs:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} logs -f

# Follow logs from a specific service
# Usage: just self-hosted logs-service web
logs-service service:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} logs -f {{service}}

# Restart all services
restart: down up

# Restart a specific service
# Usage: just self-hosted restart-service web
restart-service service:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} restart {{service}}

# Stop and remove containers, networks (preserves volumes/data)
clean:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} down

# Phase 4 of ADR-2026-04-27 applies the same destructive-confirmation
# pattern as upgrade and restore: typing the short hostname unlocks
# the wipe. The doctor pre-flight is intentionally NOT applied here —
# operators sometimes want to wipe a half-broken stack to start from
# scratch, and refusing the wipe because "doctor reports errors" is
# exactly the wrong behaviour.
#
# Stop and remove containers AND volumes (deletes ALL data).
clean-all:
    #!/usr/bin/env bash
    set -euo pipefail
    HOST=$(hostname -s 2>/dev/null || echo "self-hosted")
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ⚠ DESTRUCTIVE OPERATION"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  This will:"
    echo "    1. Stop and remove every Validibot container"
    echo "    2. DELETE the postgres volume (your database)"
    echo "    3. DELETE the validibot_storage volume (uploads, evidence)"
    echo "    4. DELETE the redis volume (queue state)"
    echo ""
    echo "  Verified backups in backups/ are NOT touched."
    echo ""
    echo "  Type the short hostname '${HOST}' to confirm, anything else aborts:"
    read -r CONFIRM
    if [ "${CONFIRM}" != "${HOST}" ]; then
        echo "Aborted by operator (no changes made)."
        exit 1
    fi
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} down -v
    echo ""
    echo "All containers and volumes removed."
    echo "Restore from a backup with:  just self-hosted restore <backup-path>"

# =============================================================================
# Deployment
# =============================================================================

# Full deployment: build and start all services
# This is the main command for deploying updates
deploy: build
    @echo ""
    @echo "Deployment complete!"
    @echo ""
    @echo "Services running:"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps

# Pull latest images (for pre-built images from a registry)
pull:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} pull

# Deploy using pre-built images (pull then start)
deploy-prebuilt: pull up
    @echo "Deployment complete using pre-built images!"

# =============================================================================
# Database & Django Commands
# =============================================================================

# Run database migrations
migrate:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py check_migration_history
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py migrate --noinput

# Refresh every application-data concern required after migrations.
setup-data:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py initialize_validibot

# Run any Django management command
# Usage: just self-hosted manage "createsuperuser"
# Usage: just self-hosted manage "shell"
# Usage: just self-hosted manage "collectstatic --noinput"
manage command:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec web python manage.py {{command}}

# Open a bash shell in the web container
shell:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec web bash

# Open Django shell
shell-django:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec web python manage.py shell

# Connect to the PostgreSQL database
shell-db:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec postgres psql -U validibot_user validibot

# =============================================================================
# Backup & Restore — utility recipes
# =============================================================================
#
# The canonical operator-facing backup/restore is the manifested
# ``just self-hosted backup`` / ``just self-hosted restore`` pair
# defined further down (Phase 3 of ADR-2026-04-27). Those produce a
# self-describing backup directory with a ``manifest.json`` and pass
# the same compatibility verification GCP uses.
#
# The recipes in THIS section are utility-grade ad-hoc dumps with no
# manifest and no verification — useful when you want a database-only
# snapshot before a manual experiment, but not the right tool for a
# scheduled backup story.
#
# When in doubt, use ``just self-hosted backup`` instead.

# Create a database-only backup (no manifest, no integrity verification).
# Saves to backups/validibot_db_<timestamp>.sql.gz.
# Prefer ``just self-hosted backup`` for scheduled or pilot use.
backup-db:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p backups
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="backups/validibot_db_${TIMESTAMP}.sql.gz"
    echo "Creating database backup: $BACKUP_FILE"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T postgres \
        pg_dump -U validibot_user validibot | gzip > "$BACKUP_FILE"
    echo "Backup complete: $BACKUP_FILE"
    ls -lh "$BACKUP_FILE"

# Restore database from a backup file
# Usage: just self-hosted restore-db backups/validibot_db_20240101_120000.sql.gz
restore-db backup_file:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f "{{backup_file}}" ]; then
        echo "Error: Backup file not found: {{backup_file}}"
        exit 1
    fi
    echo "WARNING: This will overwrite the current database!"
    read -p "Are you sure? Type 'yes' to confirm: " -r
    if [[ "$REPLY" == "yes" ]]; then
        echo "Restoring from: {{backup_file}}"
        gunzip -c "{{backup_file}}" | docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T postgres \
            psql -U validibot_user validibot
        echo "Restore complete!"
    else
        echo "Cancelled."
    fi

# List available backups (both manifested and db-only).
#
# Manifested backups are timestamped subdirectories of ``backups/``
# containing a ``manifest.json``; those are the canonical Phase 3
# artifacts that ``just self-hosted restore`` consumes. Db-only
# dumps from the utility ``backup-db`` recipe live as ``.sql.gz``
# files at the top of ``backups/`` and are listed separately.
list-backups:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d backups ]; then
        echo "  No backups directory yet — nothing has been backed up."
        exit 0
    fi
    echo "Manifested backups (just self-hosted restore <path>):"
    FOUND=0
    while IFS= read -r MANIFEST; do
        [ -z "$MANIFEST" ] && continue
        DIR=$(dirname "$MANIFEST")
        SIZE=$(du -sh "$DIR" 2>/dev/null | awk '{print $1}')
        printf "  %s  (%s)\n" "$DIR" "$SIZE"
        FOUND=1
    done < <(find backups -mindepth 2 -maxdepth 2 -name manifest.json 2>/dev/null | sort)
    if [ "$FOUND" -eq 0 ]; then
        echo "  (none)"
    fi
    echo ""
    echo "Db-only dumps (backup-db / restore-db):"
    if ls backups/*.sql.gz >/dev/null 2>&1; then
        ls -lh backups/*.sql.gz | awk '{printf "  %s  (%s)\n", $NF, $5}'
    else
        echo "  (none)"
    fi

# Backup media files
backup-media:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p backups
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="backups/validibot_media_${TIMESTAMP}.tar.gz"
    echo "Creating media backup: $BACKUP_FILE"
    # Assumes media is in a named volume 'validibot_media'
    docker run --rm \
        -v {{compose_project}}_media:/data:ro \
        -v $(pwd)/backups:/backup \
        alpine tar czf "/backup/validibot_media_${TIMESTAMP}.tar.gz" -C /data .
    echo "Backup complete: $BACKUP_FILE"
    ls -lh "$BACKUP_FILE"

# =============================================================================
# Health Checks
# =============================================================================

# Quick health check - verify services are responding
health-check:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Checking service health..."
    echo ""

    service_status() {
        local service="$1"
        local container_id
        container_id=$(docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps -q "$service" 2>/dev/null || true)
        if [ -z "$container_id" ]; then
            echo "not running"
            return
        fi
        docker inspect --format '{{"{{.State.Status}}"}}' "$container_id" 2>/dev/null || echo "unknown"
    }

    # Check web service
    WEB_STATUS=$(service_status web)
    if [ "$WEB_STATUS" = "running" ]; then
        echo "Web service:    OK (running)"
    else
        echo "Web service:    FAIL ($WEB_STATUS)"
    fi

    # Check worker service
    WORKER_STATUS=$(service_status worker)
    if [ "$WORKER_STATUS" = "running" ]; then
        echo "Worker service: OK (running)"
    else
        echo "Worker service: FAIL ($WORKER_STATUS)"
    fi

    # Check database
    DB_STATUS=$(service_status postgres)
    if [ "$DB_STATUS" = "running" ]; then
        echo "Database:       OK (running)"
    else
        echo "Database:       FAIL ($DB_STATUS)"
    fi

    # Check Redis
    REDIS_STATUS=$(service_status redis)
    if [ "$REDIS_STATUS" = "running" ]; then
        echo "Redis:          OK (running)"
    else
        echo "Redis:          FAIL ($REDIS_STATUS)"
    fi

    echo ""

# Check if the web service is responding to HTTP requests
# Assumes the web service is exposed on localhost:8000 or via reverse proxy
health-check-http:
    #!/usr/bin/env bash
    set -euo pipefail
    # Try localhost:8000 first (direct), then localhost:80 (via proxy)
    for URL in "http://localhost:8000" "http://localhost"; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL" --max-time 5 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ]; then
            echo "HTTP check: OK ($URL returned $HTTP_CODE)"
            exit 0
        fi
    done
    echo "HTTP check: FAIL (services not responding)"
    exit 1

# =============================================================================
# Maintenance
# =============================================================================

# Enter maintenance mode (stop web, keep database running)
# Useful for database maintenance or major upgrades
maintenance-on:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Entering maintenance mode..."
    echo "Stopping web and worker services..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} stop web worker
    echo ""
    echo "Maintenance mode enabled."
    echo "Database and Redis are still running for maintenance tasks."
    echo ""
    echo "To exit maintenance mode: just self-hosted maintenance-off"

# Exit maintenance mode (restart web and worker)
maintenance-off:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Exiting maintenance mode..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} start web worker
    echo ""
    echo "Services restarted. Maintenance mode disabled."

# =============================================================================
# Updates
# =============================================================================

# Deprecated — use ``just self-hosted upgrade --to <version>`` instead.
#
# The legacy ``update`` recipe pulled ``latest``, backed up only the
# database (not the manifested data archive), and skipped both the
# doctor pre-flight and the smoke-test post-flight. Phase 4 of
# ADR-2026-04-27 replaces it with ``upgrade``, which is version-pinned,
# uses the manifested backup, and gates destructive work behind
# ``doctor --strict``.
#
# Deprecated — points at ``just self-hosted upgrade --to <version>``.
update:
    #!/usr/bin/env bash
    echo "ERROR: 'just self-hosted update' is deprecated." >&2
    echo "" >&2
    echo "Phase 4 of ADR-2026-04-27 replaces it with the manifested" >&2
    echo "upgrade recipe, which is version-pinned, gated by doctor," >&2
    echo "and verified by smoke-test:" >&2
    echo "" >&2
    echo "  just self-hosted upgrade --to v0.9.0" >&2
    echo "" >&2
    echo "See docs/operations/self-hosting/upgrades.md for the full" >&2
    echo "rationale and operator workflow." >&2
    exit 64

# =============================================================================
# SSL / HTTPS Setup
# =============================================================================
#
# For production deployments, you'll need SSL/HTTPS. Options include:
#
# 1. Traefik (recommended for Docker)
#    - Automatic Let's Encrypt certificates
#    - Add Traefik to your docker-compose.production.yml
#    - See: https://doc.traefik.io/traefik/https/acme/
#
# 2. Nginx + Certbot
#    - Traditional approach
#    - Add Nginx container or install on host
#    - Use certbot for Let's Encrypt
#
# 3. Cloudflare Tunnel
#    - No need to expose ports
#    - Free SSL through Cloudflare
#    - Good for home servers
#
# 4. Caddy
#    - Automatic HTTPS by default
#    - Simple configuration
#    - Built-in Let's Encrypt support
#
# Example Traefik labels for docker-compose.production.yml:
#
#   web:
#     labels:
#       - "traefik.enable=true"
#       - "traefik.http.routers.validibot.rule=Host(`validibot.example.com`)"
#       - "traefik.http.routers.validibot.tls.certresolver=letsencrypt"
#       - "traefik.http.services.validibot.loadbalancer.server.port=8000"
#
# =============================================================================

# =============================================================================
# Scaling (Basic)
# =============================================================================

# Scale worker containers
# Usage: just self-hosted scale-workers 3
scale-workers count:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d --scale worker={{count}}
    @echo "Worker count scaled to {{count}}"

# =============================================================================
# Troubleshooting
# =============================================================================

# Show resource usage
resources:
    docker stats --no-stream $(docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps -q)

# Show container logs with errors only
errors:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} logs --tail=100 | grep -i -E "(error|exception|traceback|failed)"

# Show recent container events
events:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} events --since 1h

# Inspect a service's configuration
# Usage: just self-hosted inspect web
inspect service:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} config --services | grep -q {{service}} && \
        docker inspect $(docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps -q {{service}}) | jq '.[0]' || \
        echo "Service not found: {{service}}"

# =============================================================================
# Environment Setup
# =============================================================================

# Check if environment file exists and has required variables
check-env:
    #!/usr/bin/env bash
    set -euo pipefail
    DJANGO_ENV_FILE="{{django_env_file}}"
    POSTGRES_ENV_FILE="{{postgres_env_file}}"
    BUILD_ENV_FILE="{{build_env_file}}"

    for env_file in "$DJANGO_ENV_FILE" "$POSTGRES_ENV_FILE"; do
        if [ ! -f "$env_file" ]; then
            echo "ERROR: Environment file not found: $env_file"
            echo ""
            echo "Create it from the examples:"
            echo "  cp .envs.example/.production/.self-hosted/.django $DJANGO_ENV_FILE"
            echo "  cp .envs.example/.production/.self-hosted/.postgres $POSTGRES_ENV_FILE"
            exit 1
        fi
    done

    echo "Checking required Django environment variables..."
    REQUIRED_DJANGO_VARS=(
        "DJANGO_SECRET_KEY"
        "DJANGO_ALLOWED_HOSTS"
        "SITE_URL"
        "WORKER_API_KEY"
    )
    REQUIRED_POSTGRES_VARS=(
        "POSTGRES_HOST"
        "POSTGRES_PORT"
        "POSTGRES_DB"
        "POSTGRES_USER"
        "POSTGRES_PASSWORD"
    )

    MISSING=0
    for var in "${REQUIRED_DJANGO_VARS[@]}"; do
        if ! grep -Eq "^$var=.+" "$DJANGO_ENV_FILE"; then
            echo "  MISSING: $var"
            MISSING=$((MISSING + 1))
        else
            echo "  OK: $var"
        fi
    done

    echo ""
    echo "Checking required PostgreSQL environment variables..."
    for var in "${REQUIRED_POSTGRES_VARS[@]}"; do
        if ! grep -Eq "^$var=.+" "$POSTGRES_ENV_FILE"; then
            echo "  MISSING: $var"
            MISSING=$((MISSING + 1))
        else
            echo "  OK: $var"
        fi
    done

    PLACEHOLDER_MATCHES=0
    for env_file in "$DJANGO_ENV_FILE" "$POSTGRES_ENV_FILE"; do
        if grep -Eq 'change-this|!!!CHANGE-THIS|your-secret-key-here' "$env_file"; then
            echo ""
            echo "ERROR: Placeholder values still exist in $env_file"
            PLACEHOLDER_MATCHES=$((PLACEHOLDER_MATCHES + 1))
        fi
    done

    if [ -f "$BUILD_ENV_FILE" ]; then
        echo ""
        echo "Checking optional Docker build settings..."

        # Parse shell quoting once while retaining the documented precedence:
        # explicit recipe environment, then .build, then recipe defaults.
        overrides=""
        for variable_name in \
            VALIDATOR_CONTAINER_SOCKET \
            VALIDIBOT_COMMERCIAL_PACKAGE VALIDIBOT_COMMERCIAL_NETRC; do
            if [ "${!variable_name+x}" = x ]; then
                printf -v override 'export %s=%q\n' "$variable_name" "${!variable_name}"
                overrides+="$override"
            fi
        done
        set -a
        # shellcheck disable=SC1090
        source "$BUILD_ENV_FILE"
        set +a
        eval "$overrides"

        COMMERCIAL_PACKAGE=${VALIDIBOT_COMMERCIAL_PACKAGE:-}
        COMMERCIAL_NETRC=${VALIDIBOT_COMMERCIAL_NETRC:-}

        if [ -n "$COMMERCIAL_PACKAGE" ]; then
            if [[ "$COMMERCIAL_PACKAGE" =~ ^validibot-(pro|enterprise)==[A-Za-z0-9][A-Za-z0-9._!+-]*$ ]] || \
               [[ "$COMMERCIAL_PACKAGE" =~ ^https://pypi\.validibot\.com/packages/validibot_(pro|enterprise)-[^/]+\.whl#sha256=[0-9a-f]{64}$ ]]; then
                echo "  OK: VALIDIBOT_COMMERCIAL_PACKAGE is an exact trusted reference"
            else
                echo "  ERROR: VALIDIBOT_COMMERCIAL_PACKAGE must be version-pinned or a trusted hash-pinned wheel"
                PLACEHOLDER_MATCHES=$((PLACEHOLDER_MATCHES + 1))
            fi

            if [ -z "$COMMERCIAL_NETRC" ] || [ ! -f "$COMMERCIAL_NETRC" ]; then
                echo "  ERROR: VALIDIBOT_COMMERCIAL_NETRC must name an existing credential file"
                PLACEHOLDER_MATCHES=$((PLACEHOLDER_MATCHES + 1))
            else
                if NETRC_MODE=$(stat -c '%a' "$COMMERCIAL_NETRC" 2>/dev/null); then
                    :
                else
                    NETRC_MODE=$(stat -f '%Lp' "$COMMERCIAL_NETRC")
                fi
                case "$NETRC_MODE" in
                    ?00|??00)
                        echo "  OK: VALIDIBOT_COMMERCIAL_NETRC exists with private permissions"
                        ;;
                    *)
                        echo "  ERROR: VALIDIBOT_COMMERCIAL_NETRC must not be accessible by group or other users"
                        PLACEHOLDER_MATCHES=$((PLACEHOLDER_MATCHES + 1))
                        ;;
                esac
            fi
        fi
    fi

    echo ""
    echo "Checking validator container socket..."
    VALIDATOR_CONTAINER_SOCKET="${VALIDATOR_CONTAINER_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/1000}/docker.sock}"
    if [ ! -S "$VALIDATOR_CONTAINER_SOCKET" ]; then
        echo "  ERROR: Docker-compatible socket not found: $VALIDATOR_CONTAINER_SOCKET"
        echo "         Start rootless Docker, or set VALIDATOR_CONTAINER_SOCKET"
        echo "         explicitly to a supported compatibility socket."
        PLACEHOLDER_MATCHES=$((PLACEHOLDER_MATCHES + 1))
    elif [ "$VALIDATOR_CONTAINER_SOCKET" = "/var/run/docker.sock" ]; then
        echo "  WARNING: Explicit rootful Docker socket selected"
        echo "           Rootless Docker is the secure default for fresh installs."
    else
        echo "  OK: Docker-compatible socket exists: $VALIDATOR_CONTAINER_SOCKET"
    fi

    echo ""
    if [ "$MISSING" -gt 0 ] || [ "$PLACEHOLDER_MATCHES" -gt 0 ]; then
        echo "Environment validation failed."
        exit 1
    fi

    echo "Environment files look ready."

# Generate a new Django secret key
generate-secret-key:
    @python -c "import secrets; print(secrets.token_urlsafe(50))"

# =============================================================================
# Validators
# =============================================================================
#
# For Docker Compose deployments, validators run as separate Docker containers
# that are invoked by the worker service.
#
# Option 1: Run validators as Docker-in-Docker (DinD)
#   - Worker container has access to Docker socket
#   - Can spawn validator containers dynamically
#
# Option 2: Pre-deploy validator containers
#   - Validators run as always-on services
#   - Worker communicates via HTTP
#
# Option 3: Use an external validator service
#   - Validators hosted elsewhere (cloud, separate server)
#   - Configure VALIDATOR_SERVICE_URL
#
# =============================================================================

# Build a validator image locally from a sibling validibot-validator-backends checkout.
#
# The backend image version is baked into each backend's Dockerfile as the
# ``ARG VALIDATOR_BACKEND_VERSION`` default — that's the single source of
# truth and what gets stamped onto the image as the
# ``org.opencontainers.image.version`` OCI label. Release engineering can
# override via ``VALIDATOR_BACKEND_VERSION=0.1.1-rc1 just self-hosted
# validator-build energyplus``; otherwise the Dockerfile default wins.
validator-build name:
    #!/usr/bin/env bash
    set -euo pipefail
    IMAGE_SLUG="{{name}}"
    IMAGE_SLUG="${IMAGE_SLUG//_/-}"
    docker build --platform linux/amd64 \
        -f ../validibot-validator-backends/validator_backends/{{name}}/Dockerfile \
        ${VALIDATOR_BACKEND_VERSION:+--build-arg VALIDATOR_BACKEND_VERSION="${VALIDATOR_BACKEND_VERSION}"} \
        --build-arg VALIDATOR_BACKEND_REVISION="{{validator_backend_git_sha}}" \
        --build-arg VALIDATOR_BACKEND_SLUG="{{name}}" \
        -t validibot-validator-backend-${IMAGE_SLUG}:{{validator_backend_git_sha}} \
        -t validibot-validator-backend-${IMAGE_SLUG}:latest \
        ../validibot-validator-backends

# Build all validator images
validators-build-all:
    just self-hosted validator-build energyplus
    just self-hosted validator-build fmu
    just self-hosted validator-build shacl
    just self-hosted validator-build schematron
    just self-hosted validator-build portfolio_manager

# =============================================================================
# Operator-Facing Stub Recipes (Phase 0 of ADR-2026-04-27)
# =============================================================================
#
# These recipes are operator-facing entry points with the same names
# they will eventually have. Today they print a "not yet implemented"
# notice and exit non-zero. This makes the operator surface visible
# in ``just self-hosted --list`` and lets cross-target parity tests
# in ``just/gcp/mod.just`` work today.
#
# The full implementations land in later ADR phases:
#   - doctor: Phase 1 (target-aware health diagnostic)
#   - smoke-test: Phase 2 (end-to-end demo workflow)
#   - backup, restore: Phase 3 (application-level backup MVP)
#   - upgrade: Phase 4 (versioned upgrade ritual)
#   - validators (operator surface): Phase 5
#   - collect-support-bundle: Phase 6 (redacted support archive)
#
# Self-hosted is single-stage per VM (see ADR section 4) — these
# recipes do not take a stage argument. The mirrored ``just/gcp/``
# recipes do.

[private]
_phase0-stub recipe:
    #!/usr/bin/env bash
    echo "just self-hosted {{recipe}}: not yet implemented (Phase 0 stub)." >&2
    echo "" >&2
    echo "Implementation lands in a later phase per ADR-2026-04-27" >&2
    echo "(Boring Self-Hosting and Operator Experience). See:" >&2
    echo "  docs/operations/self-hosting/overview.md" >&2
    echo "" >&2
    echo "For now, these existing recipes work today:" >&2
    echo "  just self-hosted bootstrap     # first-time install" >&2
    echo "  just self-hosted deploy        # deploy or upgrade" >&2
    echo "  just self-hosted check-env     # validate env files" >&2
    echo "  just self-hosted check-dns     # verify SITE_URL DNS before TLS" >&2
    echo "  just self-hosted health-check  # quick service health" >&2
    echo "  just self-hosted backup-db     # database-only backup" >&2
    echo "  just self-hosted update        # update with backup + migrations" >&2
    exit 64

# Verify SITE_URL resolves to this host's public IP before TLS issuance.
#
# Caddy requests a Let's Encrypt certificate on first boot via the ACME
# HTTP-01 challenge. If DNS doesn't yet point at this host, the
# challenge fails AND counts against the Let's Encrypt rate limit
# (currently 50 certs per registered domain per week, with stricter
# limits on duplicate failures). Running this once before enabling the
# caddy Compose profile prevents wasted attempts.
#
# Reads SITE_URL from .envs/.production/.self-hosted/.django, resolves
# the hostname via dig, and compares to this host's public IP detected
# via api.ipify.org. Exit codes:
#   0  — DNS resolves to this host. Safe to issue certificates.
#   1  — DNS resolves elsewhere or the env file isn't ready. Fix first.
#   2  — Could not determine this host's public IP. Verify manually.
check-dns:
    #!/usr/bin/env bash
    set -euo pipefail

    ENV_FILE=".envs/.production/.self-hosted/.django"
    if [ ! -f "$ENV_FILE" ]; then
        echo "ERROR: $ENV_FILE not found." >&2
        echo "       Copy it from .envs.example/.production/.self-hosted/.django" >&2
        echo "       and edit SITE_URL before running check-dns." >&2
        exit 1
    fi

    SITE_URL=$(grep -E '^SITE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    if [ -z "$SITE_URL" ]; then
        echo "ERROR: SITE_URL is empty in $ENV_FILE." >&2
        exit 1
    fi

    # Extract hostname (strip protocol, port, trailing path)
    HOSTNAME=$(echo "$SITE_URL" | sed -E 's|^https?://||; s|/.*||; s|:.*||')
    if [ -z "$HOSTNAME" ]; then
        echo "ERROR: Could not extract hostname from SITE_URL=$SITE_URL." >&2
        exit 1
    fi

    echo "Checking DNS for: $HOSTNAME"

    # Resolve the hostname via dig if available, fall back to host
    if command -v dig >/dev/null 2>&1; then
        RESOLVED_IP=$(dig +short "$HOSTNAME" A | tail -1)
    elif command -v host >/dev/null 2>&1; then
        RESOLVED_IP=$(host -t A "$HOSTNAME" | awk '/has address/ {print $NF; exit}')
    else
        echo "ERROR: Neither dig nor host is installed. Install one and retry." >&2
        exit 1
    fi

    if [ -z "$RESOLVED_IP" ]; then
        echo "FAIL: $HOSTNAME does not resolve to any A record." >&2
        echo "      Add a DNS A-record pointing $HOSTNAME at this host." >&2
        exit 1
    fi
    echo "DNS resolves: $HOSTNAME -> $RESOLVED_IP"

    # Detect this host's public IP via a public service
    PUBLIC_IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo "")
    if [ -z "$PUBLIC_IP" ]; then
        echo "WARN: Could not detect this host's public IP." >&2
        echo "      Manually verify that $HOSTNAME points at this host." >&2
        exit 2
    fi
    echo "Host public IP: $PUBLIC_IP"

    if [ "$RESOLVED_IP" = "$PUBLIC_IP" ]; then
        echo ""
        echo "OK: DNS for $HOSTNAME points at this host."
        echo "    Safe to enable the caddy profile and let it request a TLS cert."
        exit 0
    else
        echo "" >&2
        echo "FAIL: DNS for $HOSTNAME points at $RESOLVED_IP," >&2
        echo "      but this host is at $PUBLIC_IP." >&2
        echo "      Fix DNS before issuing TLS certificates." >&2
        exit 1
    fi

# Build a local Pro image from an exact wheel reference.
#
# Pro images are NOT published to public registries (see ADR section
# 20). Operators always build locally from a wheel they fetched with
# their license credentials. This recipe wraps ``docker build`` with
# BuildKit secrets so the private PyPI credentials never end up in
# image layers, Compose files, logs, or support bundles.
#
# Usage:
#   just self-hosted build-pro-image VALIDIBOT_COMMERCIAL_PACKAGE
#
# where the argument is one of:
#   - validibot-pro==0.5.1 (exact-pinned name+version)
#   - https://pypi.validibot.com/.../*.whl#sha256=... (full URL)
#
# Ranges (>=, ~=, ^) are rejected. The recipe reads
# VALIDIBOT_COMMERCIAL_NETRC from the environment and mounts it only as a
# BuildKit secret.
#
# Phase 0 status: stub. The wheel-validation logic and BuildKit secret
# wiring land in a later phase.
build-pro-image package="":
    @just self-hosted _phase0-stub build-pro-image

# Health diagnostic — settings, db, cache, storage, validators, signing.
#
# Runs ``python manage.py check_validibot --target self_hosted`` inside
# the worker container, which is the service that owns the validator-engine
# socket. Forwards any extra args through to the management
# command, so ``just self-hosted doctor --json --strict`` works as
# expected. See docs/operations/self-hosting/doctor-check-ids.md for
# the table of check IDs and their fixes.
#
# Self-hosted is single-stage per VM (ADR section 4); no stage arg.
doctor *args:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T worker python manage.py check_validibot --target self_hosted {{args}}

# Show recent ERROR / EXCEPTION / TRACEBACK lines from all services.
#
# Useful during incident response. Default window is the last hour;
# pass an explicit duration like ``just self-hosted errors-since 24h``
# or ``just self-hosted errors-since 10m``. Accepts whatever
# ``docker compose logs --since`` accepts.
errors-since duration="1h":
    @echo "Errors / exceptions / tracebacks since {{duration}}:"
    @echo "---"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} logs --since {{duration}} 2>&1 | grep -i -E "(error|exception|traceback|failed)" || echo "(no errors found)"

# Run the validibot.smoke-test.v1 command in the web container.
# Forwards extra args through, so ``just self-hosted smoke-test --json``
# and ``just self-hosted smoke-test --timeout-seconds 30`` work as
# expected. See docs/operations/self-hosting/smoke-test.md for the
# check-ID table and what each step verifies.
#
# End-to-end smoke test — runs a tiny JSON Schema validation through the real pipeline.
smoke-test *args:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py smoke_test --target self_hosted {{args}}

# =============================================================================
# Application-level Backup & Restore (Phase 3 of ADR-2026-04-27)
# =============================================================================
#
# These recipes produce / consume a manifested backup directory that is
# the cross-target contract:
#
#   backups/<backup_id>/
#     manifest.json        validibot.backup.v1 schema (see backup_manifest.py)
#     db.sql.zst           pg_dump --format=plain | zstd
#     data.tar.zst         tar --zstd of DATA_STORAGE_ROOT contents
#     checksums.sha256     sha256sums in BSD-style format
#
# The same manifest schema is produced by GCP backups; the same
# ``verify_backup_compatibility`` management command consumes them.
# That symmetry is what makes the cross-target operator surface
# legible: you read the manifest and immediately know what's in the
# backup and how to restore it, regardless of which substrate
# produced it.
#
# Why this exists alongside ``backup-db`` / ``restore-db``:
# ``backup-db`` is a 5-line ad-hoc dump for "I'm about to run a
# scary experiment, give me a 30-second insurance policy" use cases.
# These recipes are the boring, manifested, integrity-checked story
# for scheduled backups, pilot installs, and pre-upgrade rituals.

# Postgres dump + DATA_STORAGE_ROOT archive + manifest.json with
# Validibot version, migration head, and checksums. See ADR-2026-04-27
# section 8 for the manifest format.
#
# Manifested backup — produces backups/<id>/{manifest.json, db.sql.zst, data.tar.zst}.
backup:
    @just self-hosted _run-backup

# Run the manifested backup orchestration.
#
# Produces ``backups/<backup_id>/{manifest.json, db.sql.zst,
# data.tar.zst, checksums.sha256}`` — the four artifacts the
# operator restore path consumes.
#
# Six steps, each printed so an operator following along sees
# progress for what would otherwise be a long-running silent
# command:
#
#   1. Compute backup ID + create the destination directory.
#   2. ``pg_dump`` streamed through zstd into ``db.sql.zst``.
#   3. ``tar --zstd`` of DATA_STORAGE_ROOT contents into
#      ``data.tar.zst``.
#   4. sha256 + size for both artifacts.
#   5. ``write_backup_manifest`` invoked inside the web container —
#      the writer needs Django's view of the live migration state,
#      which only the running app has. Inventory is piped via stdin
#      (``--media-inventory -``) to avoid copying a temp file into
#      the container; manifest is emitted to stdout and captured on
#      the host.
#   6. ``checksums.sha256`` written next to the artifacts.
#
# The recipe accepts no arguments — self-hosted is single-stage per
# VM (ADR section 4). Operators wanting per-environment backup
# trees should run ``BACKUP_DIR=/var/backups/validibot just
# self-hosted backup`` (the env var is honoured below).
[private]
_run-backup:
    #!/usr/bin/env bash
    set -euo pipefail

    BACKUP_DIR="${BACKUP_DIR:-backups}"
    BACKUP_ID=$(date -u +%Y%m%dT%H%M%SZ)
    BACKUP_PATH="${BACKUP_DIR}/${BACKUP_ID}"
    DB_FILE="${BACKUP_PATH}/db.sql.zst"
    DATA_FILE="${BACKUP_PATH}/data.tar.zst"
    MANIFEST_FILE="${BACKUP_PATH}/manifest.json"
    CHECKSUMS_FILE="${BACKUP_PATH}/checksums.sha256"

    # Read database credentials from the .postgres env file so the
    # recipe doesn't hard-code values that operators set per-install.
    POSTGRES_ENV="{{postgres_env_file}}"
    if [ ! -f "${POSTGRES_ENV}" ]; then
        echo "Error: ${POSTGRES_ENV} not found." >&2
        echo "       Run 'just self-hosted check-env' to set up env files." >&2
        exit 1
    fi
    POSTGRES_USER=$(grep -E '^POSTGRES_USER=' "${POSTGRES_ENV}" | cut -d= -f2-)
    POSTGRES_DB=$(grep -E '^POSTGRES_DB=' "${POSTGRES_ENV}" | cut -d= -f2-)
    if [ -z "${POSTGRES_USER}" ] || [ -z "${POSTGRES_DB}" ]; then
        echo "Error: POSTGRES_USER or POSTGRES_DB missing from ${POSTGRES_ENV}." >&2
        exit 1
    fi

    # Ask the running web container for DATA_STORAGE_ROOT rather than
    # parsing the .django env file. Settings logic may compute the
    # path, default it, or apply per-edition overrides; the running
    # process is authoritative.
    DATA_ROOT=$(docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        python -c "from django.conf import settings; print(settings.DATA_STORAGE_ROOT)" \
        2>/dev/null | tr -d '\r' | tail -1)
    if [ -z "${DATA_ROOT}" ]; then
        echo "Error: could not read DATA_STORAGE_ROOT from the web container." >&2
        echo "       Is the stack running? Try 'just self-hosted up' first." >&2
        exit 1
    fi

    HOST=$(hostname -s 2>/dev/null || echo "self-hosted")

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Backup — ${HOST}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Backup ID:    ${BACKUP_ID}"
    echo "  Destination:  ${BACKUP_PATH}/"
    echo "  Database:     ${POSTGRES_DB} (user ${POSTGRES_USER})"
    echo "  Data root:    ${DATA_ROOT}  (in web container)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # Step 1 — ensure destination exists. ``mkdir -p`` is idempotent
    # so a re-run after a transient failure is safe.
    echo "Step 1/6: Preparing ${BACKUP_PATH}/..."
    mkdir -p "${BACKUP_PATH}"
    echo "  ✓ Directory ready."
    echo ""

    # Step 2 — Postgres dump streamed through zstd. ``pg_dump
    # --format=plain`` matches ADR section 8 (``plain-sql-zstd``).
    # Streaming via stdout/stdin avoids touching local disk inside
    # the postgres container, which doesn't have spare room for
    # multi-GB dumps. The pipe through ``zstd -c`` runs on the host
    # so it doesn't need to be installed in the postgres image.
    echo "Step 2/6: Dumping Postgres to ${DB_FILE}..."
    if ! command -v zstd >/dev/null 2>&1; then
        echo "Error: 'zstd' not found on host — install it (apt install zstd)." >&2
        exit 1
    fi
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T postgres \
        pg_dump --format=plain --no-owner --no-privileges \
        -U "${POSTGRES_USER}" "${POSTGRES_DB}" \
        | zstd -19 -c > "${DB_FILE}"
    DB_SIZE=$(stat -f%z "${DB_FILE}" 2>/dev/null || stat -c%s "${DB_FILE}")
    DB_SHA=$(sha256sum "${DB_FILE}" | awk '{print $1}')
    echo "  ✓ ${DB_SIZE} bytes, sha256 ${DB_SHA:0:16}..."
    echo ""

    # Step 3 — DATA_STORAGE_ROOT archive. ``tar -C <root> .``
    # produces an archive whose paths are relative to the contents
    # of ${DATA_ROOT}, not its parent — so restore extracts back into
    # ${DATA_ROOT} cleanly without nested directories. Empty data
    # roots produce an empty tar, which is legal for fresh installs.
    echo "Step 3/6: Archiving ${DATA_ROOT}/ to ${DATA_FILE}..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        tar --zstd -cf - -C "${DATA_ROOT}" . > "${DATA_FILE}"
    DATA_SIZE=$(stat -f%z "${DATA_FILE}" 2>/dev/null || stat -c%s "${DATA_FILE}")
    DATA_SHA=$(sha256sum "${DATA_FILE}" | awk '{print $1}')
    echo "  ✓ ${DATA_SIZE} bytes, sha256 ${DATA_SHA:0:16}..."
    echo ""

    # Step 4 — build the inventory line. Self-hosted has a single
    # data archive (vs. GCP's many media files), so ``media_files``
    # is a one-element list. ``application/zstd`` is the registered
    # type for the underlying compression; the manifest reader
    # doesn't care, but operators inspecting with ``jq`` get an
    # accurate type.
    echo "Step 4/6: Building inventory..."
    INVENTORY_LINE=$(jq -nc \
        --arg path data.tar.zst \
        --argjson size "${DATA_SIZE}" \
        --arg sha "${DATA_SHA}" \
        '{path: $path, size_bytes: $size, content_type: "application/zstd", checksum_sha256: $sha}')
    echo "  ✓ 1 archive entry."
    echo ""

    # Step 5 — manifest writer in the web container. The writer
    # needs to see Django's live migration state and Postgres
    # version, which only the running container has. Two streams:
    # stdin carries the inventory JSONL; stdout streams back the
    # manifest JSON which the host redirects to manifest.json.
    # ``--restore-command-hint`` records the exact restore command
    # so an operator viewing the manifest in isolation knows what
    # to type.
    echo "Step 5/6: Writing manifest via web container..."
    RESTORE_HINT="just self-hosted restore ${BACKUP_PATH}"
    BACKUP_URI="file://$(cd "${BACKUP_PATH}" && pwd)/"
    echo "${INVENTORY_LINE}" | \
        docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
            python manage.py write_backup_manifest \
                --backup-id "${BACKUP_ID}" \
                --target self_hosted \
                --backup-uri "${BACKUP_URI}" \
                --db-file "db.sql.zst" \
                --db-sha256 "${DB_SHA}" \
                --db-size-bytes "${DB_SIZE}" \
                --db-content-type "application/zstd" \
                --media-inventory - \
                --restore-command-hint "${RESTORE_HINT}" \
                --output - \
        > "${MANIFEST_FILE}"
    echo "  ✓ ${MANIFEST_FILE}"
    echo ""

    # Step 6 — sidecar checksums.sha256 in BSD-style format that
    # ``sha256sum -c`` consumes. Lets operators verify the backup
    # without parsing the manifest JSON. Listing all four files
    # (manifest, db, data, checksums itself can't list itself but
    # would be a self-reference) keeps this a one-stop integrity
    # tool.
    echo "Step 6/6: Writing ${CHECKSUMS_FILE}..."
    (cd "${BACKUP_PATH}" && sha256sum manifest.json db.sql.zst data.tar.zst > checksums.sha256)
    echo "  ✓ Checksums written."
    echo ""

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✓ Backup complete"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Backup root:  ${BACKUP_PATH}/"
    echo "  Manifest:     ${MANIFEST_FILE}"
    echo ""
    echo "  Restore later with:"
    echo "    ${RESTORE_HINT}"
    echo ""
    echo "  Verify integrity any time with:"
    echo "    (cd ${BACKUP_PATH} && sha256sum -c checksums.sha256)"

# Restore from a manifested backup, with four pre-flight gates:
# manifest exists, doctor --strict, verify_backup_compatibility, and
# DB integrity. ADR-2026-04-27 sections 8 + AC #15-#16.
#
# After confirmation: drops and recreates the public schema, streams
# the DB dump through psql, applies any newer migrations, wipes
# DATA_STORAGE_ROOT, extracts the data archive, and touches
# ``.last-restore-test`` (consumed by doctor's VB411).
#
# Manifested restore — usage: just self-hosted restore <backup-dir>
restore path="":
    @just self-hosted _run-restore "{{path}}"

# Run the manifested restore orchestration.
#
# Mirrors the four pre-flight gates of GCP's _run-restore. Each
# gate fails closed with a clear error so a typo, an unhealthy
# deployment, or a tampered backup can't progress to the
# destructive steps.
[private]
_run-restore path:
    #!/usr/bin/env bash
    set -euo pipefail
    BACKUP_PATH="{{path}}"
    if [ -z "${BACKUP_PATH}" ]; then
        echo "Error: backup path required." >&2
        echo "" >&2
        echo "  Usage: just self-hosted restore <backup-directory>" >&2
        echo "" >&2
        echo "  List available backups:" >&2
        echo "    just self-hosted list-backups" >&2
        exit 1
    fi

    # Trim trailing slash so concatenations are clean.
    BACKUP_PATH="${BACKUP_PATH%/}"
    MANIFEST_FILE="${BACKUP_PATH}/manifest.json"
    DB_FILE="${BACKUP_PATH}/db.sql.zst"
    DATA_FILE="${BACKUP_PATH}/data.tar.zst"

    HOST=$(hostname -s 2>/dev/null || echo "self-hosted")

    # Read database credentials.
    POSTGRES_ENV="{{postgres_env_file}}"
    POSTGRES_USER=$(grep -E '^POSTGRES_USER=' "${POSTGRES_ENV}" | cut -d= -f2-)
    POSTGRES_DB=$(grep -E '^POSTGRES_DB=' "${POSTGRES_ENV}" | cut -d= -f2-)

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Restore — ${HOST}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Source:    ${BACKUP_PATH}/"
    echo "  Database:  ${POSTGRES_DB} (user ${POSTGRES_USER})"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Pre-flight 1: manifest exists ─────────────────────────────────
    # Cheap to check first so a typo'd path fails immediately.
    if [ ! -f "${MANIFEST_FILE}" ]; then
        echo "Error: manifest.json not found at ${MANIFEST_FILE}." >&2
        echo "" >&2
        echo "  List manifested backups with:" >&2
        echo "    just self-hosted list-backups" >&2
        exit 1
    fi
    echo "Pre-flight 1/4: manifest located at ${MANIFEST_FILE}"

    # ── Pre-flight 2: doctor --strict ─────────────────────────────────
    # AC #15: destructive recipes refuse against unhealthy deployments.
    echo "Pre-flight 2/4: running doctor --strict..."
    if ! just self-hosted doctor --strict; then
        echo "" >&2
        echo "Error: doctor --strict failed. Refusing to restore against" >&2
        echo "an unhealthy deployment." >&2
        echo "" >&2
        echo "  Resolve the issues doctor reported, then re-run restore." >&2
        exit 1
    fi
    echo "  ✓ doctor --strict passed."
    echo ""

    # ── Pre-flight 3: verify_backup_compatibility ─────────────────────
    # AC #16: refuse cross-major-version jumps + schema-ahead backups.
    # Manifest is piped via stdin so we don't need to copy it into
    # the container; verify_backup_compatibility supports ``-`` as a
    # synonym for stdin.
    echo "Pre-flight 3/4: verifying backup compatibility..."
    if ! cat "${MANIFEST_FILE}" | \
        docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
            python manage.py verify_backup_compatibility --manifest -; then
        echo "" >&2
        echo "Error: backup is not compatible with this deployment." >&2
        echo "  See the verifier output above for the specific reason(s)." >&2
        exit 1
    fi
    echo "  ✓ Backup is compatible with this deployment."
    echo ""

    # ── Pre-flight 4: DB dump integrity ───────────────────────────────
    # Compatibility tells us the backup CAN be restored; integrity
    # tells us we're about to restore the artifact the manifest
    # described, not a tampered or truncated one.
    echo "Pre-flight 4/4: verifying DB dump integrity..."
    if ! command -v jq >/dev/null 2>&1; then
        echo "Error: 'jq' not found on host — install it (apt install jq)." >&2
        exit 1
    fi
    DB_EXPECTED_SHA=$(jq -r '.data.db_dump.checksum_sha256 // empty' "${MANIFEST_FILE}")
    DB_EXPECTED_SIZE=$(jq -r '.data.db_dump.size_bytes // empty' "${MANIFEST_FILE}")
    if [ -z "${DB_EXPECTED_SHA}" ] || [ "${DB_EXPECTED_SHA}" = "null" ]; then
        echo "Error: manifest is missing data.db_dump.checksum_sha256." >&2
        exit 1
    fi
    if ! [[ "${DB_EXPECTED_SHA}" =~ ^[0-9a-fA-F]{64}$ ]]; then
        echo "Error: manifest DB sha256 is not a 64-character hex digest." >&2
        exit 1
    fi
    if [ ! -f "${DB_FILE}" ]; then
        echo "Error: ${DB_FILE} missing from backup directory." >&2
        exit 1
    fi
    DB_ACTUAL_SIZE=$(stat -f%z "${DB_FILE}" 2>/dev/null || stat -c%s "${DB_FILE}")
    if [ "${DB_ACTUAL_SIZE}" != "${DB_EXPECTED_SIZE}" ]; then
        echo "Error: DB dump size mismatch (expected ${DB_EXPECTED_SIZE}, got ${DB_ACTUAL_SIZE})." >&2
        exit 1
    fi
    DB_ACTUAL_SHA=$(sha256sum "${DB_FILE}" | awk '{print $1}')
    DB_EXPECTED_SHA=$(printf '%s' "${DB_EXPECTED_SHA}" | tr '[:upper:]' '[:lower:]')
    if [ "${DB_ACTUAL_SHA}" != "${DB_EXPECTED_SHA}" ]; then
        echo "Error: DB dump sha256 mismatch." >&2
        echo "  expected: ${DB_EXPECTED_SHA}" >&2
        echo "  actual:   ${DB_ACTUAL_SHA}" >&2
        exit 1
    fi
    echo "  ✓ DB dump matches manifest (${DB_EXPECTED_SIZE} bytes, sha256 ${DB_EXPECTED_SHA:0:16}...)."
    echo ""

    # ── Operator confirmation ─────────────────────────────────────────
    # Restore is destructive: DB schema is dropped + recreated, the
    # data root is wiped + extracted. Make the operator type the
    # short hostname so an accidental ``just self-hosted restore
    # ...`` typed by mistake doesn't progress.
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ⚠ DESTRUCTIVE OPERATION"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  About to:"
    echo "    1. DROP and recreate the public schema in ${POSTGRES_DB}"
    echo "       (overwrites the current database)"
    echo "    2. Stream ${DB_FILE} through psql"
    echo "    3. Apply any migrations newer than the backup"
    echo "    4. WIPE DATA_STORAGE_ROOT and extract ${DATA_FILE}"
    echo ""
    echo "  Type the short hostname '${HOST}' to confirm, anything else aborts:"
    read -r CONFIRM
    if [ "${CONFIRM}" != "${HOST}" ]; then
        echo "Aborted by operator (no changes made)."
        exit 1
    fi
    echo ""

    # Resolve DATA_STORAGE_ROOT now that confirmation has passed.
    DATA_ROOT=$(docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        python -c "from django.conf import settings; print(settings.DATA_STORAGE_ROOT)" \
        2>/dev/null | tr -d '\r' | tail -1)
    if [ -z "${DATA_ROOT}" ]; then
        echo "Error: could not read DATA_STORAGE_ROOT from the web container." >&2
        exit 1
    fi

    # ── Step 1: Drop + recreate schema ────────────────────────────────
    # ``DROP SCHEMA public CASCADE`` is the standard idiom for
    # "wipe everything before importing a plain-SQL dump." Failing
    # to do this leaves orphan tables that conflict with the
    # incoming CREATE statements.
    echo "Step 1/5: Resetting public schema..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T postgres \
        psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
        -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;" >/dev/null
    echo "  ✓ Schema reset."
    echo ""

    # ── Step 2: Stream dump through psql ──────────────────────────────
    # zstd -dc decompresses to stdout; psql consumes from stdin.
    # ``-v ON_ERROR_STOP=1`` makes psql exit on the first error,
    # otherwise a borked dump silently leaves a half-imported db.
    echo "Step 2/5: Importing dump..."
    zstd -dc "${DB_FILE}" | \
        docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T postgres \
        psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null
    echo "  ✓ DB import complete."
    echo ""

    # ── Step 3: Apply migrations on top of restored state ─────────────
    # verify_backup_compatibility refused if the backup's migration
    # head is AHEAD of code; if code is ahead of the backup (the
    # normal case), migrate brings the schema forward without
    # touching data. Idempotent — re-runs safely.
    echo "Step 3/5: Applying any newer migrations..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        python manage.py check_migration_history
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        python manage.py migrate --noinput
    echo ""

    # ── Step 4: Wipe + extract data archive ───────────────────────────
    # ``find -mindepth 1 -delete`` clears the contents of
    # DATA_STORAGE_ROOT without removing the directory itself
    # (which the container expects to exist). Then tar extracts
    # back into it. Symmetric with the backup's ``tar -C <root> .``
    # so paths line up cleanly.
    echo "Step 4/5: Restoring data archive into ${DATA_ROOT}..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        bash -c "find '${DATA_ROOT}' -mindepth 1 -delete"
    cat "${DATA_FILE}" | \
        docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        tar --zstd -xf - -C "${DATA_ROOT}"
    echo "  ✓ Data archive extracted."
    echo ""

    # ── Step 5: Write the restore-test marker ─────────────────────────
    # Doctor's VB411 reads this file's mtime to compute restore-test
    # staleness (>90 days warns). The backup's tar may have included
    # a stale marker from a previous restore; touch updates the
    # mtime to now regardless.
    echo "Step 5/5: Recording restore drill marker..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        bash -c "touch '${DATA_ROOT}/.last-restore-test'"
    echo "  ✓ Marker written — VB411 will report OK after this restore."
    echo ""

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✓ Restore complete on ${HOST}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Recommended verification:"
    echo "    just self-hosted doctor"
    echo "    just self-hosted errors-since 5m"

# =============================================================================
# Versioned Upgrade (Phase 4 of ADR-2026-04-27)
# =============================================================================
#
# The ``upgrade`` recipe is the canonical operator-facing path for moving
# a self-hosted deployment from one Validibot version to another. It
# enforces the ADR section 9 lifecycle:
#
#   1. Pre-flight: doctor --strict          → refuse on unhealthy state (AC #15)
#   2. Pre-flight: clean working tree       → refuse to checkout over uncommitted edits
#   3. Pre-flight: target tag exists        → refuse to upgrade to a non-existent version
#   4. Pre-flight: validate upgrade path    → refuse cross-major-version jumps (AC #16)
#   5. Backup                               → manifested backup unless --no-backup
#   6. Git checkout target version          → switch the working tree to <target>
#   7. Build new images                     → docker compose build
#   8. Run migrations                       → Django migrate, idempotent
#   9. Restart services                     → docker compose up -d
#  10. Post-flight: doctor                  → verify upgrade landed cleanly
#  11. Post-flight: smoke-test              → end-to-end pipeline check
#  12. Write upgrade report                 → backups/upgrades/<target>/report.json
#
# Every step is naturally idempotent (git checkout, docker build,
# migrate, restart all no-op when the target state is already
# reached). Re-running ``upgrade --to <same-target>`` after a partial
# failure picks up where it left off without re-doing completed work
# — Sentry-style resume behaviour without explicit marker files.

# Versioned upgrade with backup + doctor + smoke-test gates (Phase 4).
# Usage: just self-hosted upgrade --to v0.9.0 [--no-backup]
upgrade *args:
    @just self-hosted _run-upgrade "{{args}}"

# Run the manifested upgrade orchestration.
#
# Parses ``--to <version>`` (required) and ``--no-backup`` (optional)
# from ``args``, walks the 12-step lifecycle, and records the outcome.
# The orchestration is shell because every step is a docker / git /
# just invocation; pulling logic out into a Django command would buy
# nothing and lose the operator-readable progress output.
[private]
_run-upgrade args:
    #!/usr/bin/env bash
    set -euo pipefail

    # ── Parse args ────────────────────────────────────────────────────
    # Accept ``--to v0.9.0`` and ``--no-backup`` in any order; reject
    # anything else with a helpful usage message rather than silently
    # ignoring it.
    TARGET=""
    NO_BACKUP=0
    # shellcheck disable=SC2086
    set -- {{args}}
    while [ $# -gt 0 ]; do
        case "$1" in
            --to)
                shift
                TARGET="${1:-}"
                ;;
            --to=*)
                TARGET="${1#--to=}"
                ;;
            --no-backup)
                NO_BACKUP=1
                ;;
            "")
                ;;
            *)
                echo "ERROR: unknown argument: $1" >&2
                echo "Usage: just self-hosted upgrade --to <version> [--no-backup]" >&2
                exit 1
                ;;
        esac
        shift
    done

    if [ -z "${TARGET}" ]; then
        echo "ERROR: --to <version> is required." >&2
        echo "" >&2
        echo "Usage: just self-hosted upgrade --to v0.9.0 [--no-backup]" >&2
        exit 1
    fi

    # Reject anything that doesn't look like a release tag. Validibot
    # publishes vX.Y.Z tags; freeform refs (branch names, SHAs) are
    # legitimate for development but the operator path should be a
    # named release. Rejecting non-vX.Y.Z up front avoids the
    # operator-confusion class of "I typed the wrong thing and now my
    # production is on a feature branch".
    if ! [[ "${TARGET}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]]; then
        echo "ERROR: --to value '${TARGET}' is not a vX.Y.Z release tag." >&2
        echo "" >&2
        echo "Pre-release suffixes (e.g. v0.9.0-rc1) are accepted; freeform" >&2
        echo "refs such as branch names or commit SHAs are not." >&2
        exit 1
    fi

    # ── Identity ──────────────────────────────────────────────────────
    HOST=$(hostname -s 2>/dev/null || echo "self-hosted")
    UPGRADE_DIR="backups/upgrades/${TARGET}"
    REPORT_FILE="${UPGRADE_DIR}/report.json"
    # ``completed.ok`` is the success sentinel — written only when both
    # post-flight checks (doctor + smoke-test) pass. ``report.json``
    # is unconditional status, including failed runs. Splitting the
    # two means a degraded upgrade (report present, marker absent) is
    # retryable — earlier we exited 0 on any report, which made
    # failed upgrades look successful and blocked retries.
    COMPLETED_MARKER="${UPGRADE_DIR}/completed.ok"
    mkdir -p "${UPGRADE_DIR}"
    STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    if [ -f "${COMPLETED_MARKER}" ]; then
        echo "Upgrade to ${TARGET} already complete — marker at ${COMPLETED_MARKER}."
        echo "Re-run ``just self-hosted doctor`` and ``just self-hosted smoke-test``"
        echo "if you want a fresh health check."
        exit 0
    fi
    if [ -f "${REPORT_FILE}" ]; then
        echo "Previous attempt to ${TARGET} did not complete cleanly — retrying."
        echo "(See ${REPORT_FILE} for the prior verdict.)"
        echo ""
    fi

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Upgrade — ${HOST}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Target:    ${TARGET}"
    echo "  Backup:    $([ ${NO_BACKUP} -eq 1 ] && echo 'SKIPPED (--no-backup)' || echo 'will run')"
    echo "  Report:    ${REPORT_FILE}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Pre-flight 1: doctor --strict ─────────────────────────────────
    # AC #15: destructive recipes refuse against unhealthy deployments.
    # An upgrade that starts from a half-broken state is much harder
    # to recover from than one refused at the door.
    echo "Pre-flight 1/4: running doctor --strict..."
    if ! just self-hosted doctor --strict; then
        echo "" >&2
        echo "Error: doctor --strict failed. Refusing to upgrade against" >&2
        echo "an unhealthy deployment." >&2
        echo "" >&2
        echo "  Resolve the issues doctor reported, then re-run:" >&2
        echo "    just self-hosted upgrade --to ${TARGET}" >&2
        exit 1
    fi
    echo "  ✓ doctor --strict passed."
    echo ""

    # ── Pre-flight 2: clean working tree ──────────────────────────────
    # ``git checkout`` would silently lose uncommitted edits to
    # versioned files. Refuse loudly rather than risk it.
    echo "Pre-flight 2/4: checking working tree..."
    if ! git diff --quiet HEAD 2>/dev/null; then
        echo "Error: working tree has uncommitted changes." >&2
        echo "" >&2
        echo "  Commit or stash them before upgrading:" >&2
        echo "    git status" >&2
        echo "    git stash" >&2
        exit 1
    fi
    echo "  ✓ Working tree is clean."
    echo ""

    # ── Pre-flight 3: target tag exists ───────────────────────────────
    # Fetch tags from origin first so a freshly-cloned repo or one
    # that hasn't synced lately can still find the latest releases.
    # An invalid tag here is operator-friendly to catch *before* the
    # backup runs (which is the slowest pre-flight step).
    echo "Pre-flight 3/4: verifying target tag exists..."
    git fetch --tags --quiet origin || true
    if ! git rev-parse --verify --quiet "${TARGET}^{commit}" >/dev/null; then
        echo "Error: tag '${TARGET}' not found in this repository." >&2
        echo "" >&2
        echo "  Available recent tags:" >&2
        git tag --list 'v*' --sort=-v:refname | head -5 | sed 's/^/    /' >&2
        echo "" >&2
        echo "  If your release is newer, pull from origin:" >&2
        echo "    git fetch --tags origin" >&2
        exit 1
    fi
    CURRENT_TAG=$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || echo "v0.0.0")
    echo "  ✓ Target ${TARGET} reachable (current: ${CURRENT_TAG})."
    echo ""

    # ── Pre-flight 4: validate upgrade path ───────────────────────────
    # AC #16: refuse cross-major-version jumps. Skipping a major
    # silently skips its migrations and contractual breaking-change
    # gates. The fix-pointer names the next intermediate stop the
    # operator should land on, derived from the available tag list.
    echo "Pre-flight 4/4: validating upgrade path..."
    CURRENT_MAJOR=$(echo "${CURRENT_TAG}" | sed -E 's/^v([0-9]+)\..*/\1/')
    TARGET_MAJOR=$(echo "${TARGET}" | sed -E 's/^v([0-9]+)\..*/\1/')
    if [ "${CURRENT_MAJOR}" -ne "${TARGET_MAJOR}" ]; then
        # Cross-major upgrade. The next stop after current is the
        # latest available release in the current major series.
        NEXT_STOP=$(git tag --list "v${CURRENT_MAJOR}.*" --sort=-v:refname | head -1)
        echo "Error: cannot upgrade directly from ${CURRENT_TAG} (major v${CURRENT_MAJOR})" >&2
        echo "       to ${TARGET} (major v${TARGET_MAJOR})." >&2
        echo "" >&2
        if [ -n "${NEXT_STOP}" ]; then
            echo "  Required intermediate stop: ${NEXT_STOP}." >&2
            echo "  Run:  just self-hosted upgrade --to ${NEXT_STOP}  first," >&2
            echo "  then retry the upgrade to ${TARGET}." >&2
        else
            echo "  Walk through each major's last release before crossing the boundary." >&2
        fi
        echo "" >&2
        echo "  See docs/operations/self-hosting/upgrades.md for the full path policy." >&2
        exit 1
    fi
    echo "  ✓ Path ${CURRENT_TAG} → ${TARGET} is within major v${CURRENT_MAJOR}."
    echo ""

    # ── Step 1/7: Backup ──────────────────────────────────────────────
    # Mandatory unless the operator passes --no-backup explicitly.
    # The manifested backup writes both the DB dump and the data
    # archive, so a botched migration can be rolled back via
    # ``just self-hosted restore``. ADR section 9: "rollback across
    # migrations is restore-from-backup, not reverse migrations."
    BACKUP_PATH=""
    if [ ${NO_BACKUP} -eq 0 ]; then
        echo "Step 1/7: Creating pre-upgrade backup..."
        # Capture the output to extract the backup path. Backup
        # prints "Backup root: backups/<id>/" near the end.
        TMP_BACKUP_LOG=$(mktemp)
        trap 'rm -f "$TMP_BACKUP_LOG"' EXIT
        just self-hosted backup | tee "${TMP_BACKUP_LOG}"
        BACKUP_PATH=$(awk '/Backup root:/ {print $NF; exit}' "${TMP_BACKUP_LOG}" | sed 's/\///' )
        # Trim any trailing slash for the report.
        BACKUP_PATH="${BACKUP_PATH%/}"
        echo "  ✓ Backup at ${BACKUP_PATH}/"
        echo ""
    else
        echo "Step 1/7: Backup SKIPPED (--no-backup)."
        echo "          ⚠ Restore from backup is not available if this upgrade fails."
        echo ""
    fi

    # ── Step 2/7: Git checkout target ─────────────────────────────────
    # Naturally idempotent: ``git checkout v0.9.0`` is a no-op when
    # HEAD is already at v0.9.0 (other than printing the detached-HEAD
    # advice line, which is fine).
    echo "Step 2/7: Checking out ${TARGET}..."
    git checkout --quiet "${TARGET}"
    echo "  ✓ Working tree at ${TARGET}."
    echo ""

    # ── Pin VALIDIBOT_VERSION + VALIDIBOT_REVISION post-checkout ──────
    # The default version- and revision-stamping path reads
    # pyproject.toml + ``git rev-parse --short HEAD`` at justfile-load
    # time. That works for everyday "deploy whatever's checked out"
    # flows, but the upgrade recipe is a different contract: the
    # operator named an exact release tag via ``--to``, and that's
    # what should appear on the resulting image and in backup
    # manifests / support bundles.
    #
    # Two values must be overridden together for the image labels to
    # be self-consistent:
    #
    #   - VALIDIBOT_VERSION → the operator-supplied tag (``${TARGET}``).
    #     The justfile constant ``{{validibot_version}}`` was captured
    #     at parse time BEFORE this recipe ran ``git checkout``, so
    #     without an override the build would stamp the OLD version.
    #
    #   - VALIDIBOT_REVISION → ``git rev-parse --short HEAD`` at this
    #     point in the recipe (now pointing at ``${TARGET}``'s commit).
    #     Same parse-time-capture problem: ``{{git_sha}}`` reflects
    #     the pre-checkout HEAD. Without this override the image
    #     would carry the correct release version label but the OLD
    #     commit revision label — confusing for forensics and what
    #     the second-pass review caught.
    #
    # ``_load-build-env`` already echoes ``${VALIDIBOT_VERSION:-...}``
    # and ``${VALIDIBOT_REVISION:-...}``, so exporting both values
    # here propagates through ``eval $(just self-hosted _load-build-env)``
    # into the docker compose build.
    export VALIDIBOT_VERSION="${TARGET}"
    export VALIDIBOT_REVISION="$(git rev-parse --short HEAD)"

    # ── Step 3/7: Build images ────────────────────────────────────────
    # Naturally idempotent (Docker layer caching). On a re-run after
    # a partial failure, this step is essentially free if no Dockerfile
    # or pyproject changes happened between attempts.
    echo "Step 3/7: Building images (pinned to ${VALIDIBOT_VERSION} @ ${VALIDIBOT_REVISION})..."
    eval "$(just self-hosted _load-build-env)"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} build
    echo "  ✓ Images built."
    echo ""

    # ── Step 4/7: Run migrations ──────────────────────────────────────
    # We bring up postgres + redis first if they aren't running, then
    # run migrate in the (still-old) web container. Django's migrate
    # is idempotent — re-running applies any unapplied migrations and
    # no-ops if everything is already current.
    echo "Step 4/7: Running migrations..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d postgres redis >/dev/null 2>&1 || true
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} run --rm \
        web python manage.py check_migration_history
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} run --rm \
        web python manage.py migrate --noinput
    echo "  ✓ Migrations applied."
    echo ""

    # ── Step 5/7: Restart services ────────────────────────────────────
    # ``docker compose up -d`` recreates containers whose images
    # changed (so web + worker pick up the new build) and leaves
    # already-current containers alone (postgres + redis).
    echo "Step 5/7: Restarting services..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d
    just self-hosted _wait-web
    echo "  ✓ Services restarted at ${TARGET}."
    echo ""

    # ── Step 6/7: Post-flight doctor ──────────────────────────────────
    # The post-flight doctor isn't --strict here; warnings on a
    # freshly-restarted deployment are common (cache cold, queue
    # heartbeat lag). Errors and fatals still cause the upgrade to
    # be reported as degraded but not as failed — the migration
    # already landed.
    echo "Step 6/7: Running post-upgrade doctor..."
    DOCTOR_PASS=1
    if ! just self-hosted doctor; then
        DOCTOR_PASS=0
        echo "  ⚠ Doctor reported issues. The upgrade landed but the deployment" >&2
        echo "    has unresolved findings — see output above and check logs." >&2
    fi
    echo ""

    # ── Step 7/7: Post-flight smoke-test ──────────────────────────────
    # End-to-end verification: a real validation must complete
    # successfully through the new code. This is the strongest signal
    # that the upgrade is safe to leave running.
    echo "Step 7/7: Running smoke-test..."
    SMOKE_PASS=1
    if ! just self-hosted smoke-test; then
        SMOKE_PASS=0
        echo "  ⚠ Smoke-test failed. The pipeline is broken under ${TARGET}." >&2
        echo "    Consider rolling back via ``just self-hosted restore ${BACKUP_PATH}``." >&2
    fi
    echo ""

    # ── Write upgrade report ──────────────────────────────────────────
    # jq is already a hard dep on self-hosted (the restore recipe
    # uses it), so we use it here unconditionally. Compute the
    # boolean values into shell variables first so the jq invocation
    # is straight argument substitution.
    if ! command -v jq >/dev/null 2>&1; then
        echo "Error: 'jq' not found on host — install it (apt install jq)." >&2
        exit 1
    fi
    COMPLETED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    SKIPPED_JSON=$(if [ ${NO_BACKUP} -eq 1 ]; then echo true; else echo false; fi)
    DOCTOR_JSON=$(if [ ${DOCTOR_PASS} -eq 1 ]; then echo true; else echo false; fi)
    SMOKE_JSON=$(if [ ${SMOKE_PASS} -eq 1 ]; then echo true; else echo false; fi)
    jq -n \
        --arg host "${HOST}" \
        --arg from "${CURRENT_TAG}" \
        --arg to "${TARGET}" \
        --arg started "${STARTED_AT}" \
        --arg completed "${COMPLETED_AT}" \
        --arg backup "${BACKUP_PATH}" \
        --argjson skipped "${SKIPPED_JSON}" \
        --argjson doctor "${DOCTOR_JSON}" \
        --argjson smoke "${SMOKE_JSON}" \
        '{
            schema_version: "validibot.upgrade.v1",
            host: $host,
            from_version: $from,
            to_version: $to,
            started_at: $started,
            completed_at: $completed,
            backup_path: $backup,
            backup_skipped: $skipped,
            doctor_pass: $doctor,
            smoke_test_pass: $smoke
        }' > "${REPORT_FILE}"

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    if [ ${DOCTOR_PASS} -eq 1 ] && [ ${SMOKE_PASS} -eq 1 ]; then
        echo "  ✓ Upgrade to ${TARGET} complete on ${HOST}"
    else
        echo "  ⚠ Upgrade to ${TARGET} landed with post-flight findings"
    fi
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Report:  ${REPORT_FILE}"
    if [ -n "${BACKUP_PATH}" ]; then
        echo "  Backup:  ${BACKUP_PATH}/"
    fi
    echo ""
    if [ ${DOCTOR_PASS} -eq 0 ] || [ ${SMOKE_PASS} -eq 0 ]; then
        echo "  Recommended next steps:"
        [ ${DOCTOR_PASS} -eq 0 ] && echo "    just self-hosted doctor                # re-run to see findings"
        [ ${SMOKE_PASS} -eq 0 ] && echo "    just self-hosted errors-since 5m       # recent service errors"
        if [ -n "${BACKUP_PATH}" ]; then
            echo "    just self-hosted restore ${BACKUP_PATH}  # roll back"
        fi
        echo ""
        echo "  This run is RETRYABLE — re-run ``just self-hosted upgrade --to ${TARGET}``"
        echo "  after fixing the issues above. The completed.ok marker is written"
        echo "  only when both post-flight checks pass."
        exit 1
    fi
    # Both doctor and smoke-test passed — write the completion sentinel
    # so future invocations short-circuit cleanly.
    : > "${COMPLETED_MARKER}"
    exit 0

# =============================================================================
# Support Bundle (Phase 6 of ADR-2026-04-27)
# =============================================================================
#
# Operators send a support bundle when they email support@validibot.com
# for help. The bundle contains everything support needs to diagnose
# the problem WITHOUT leaking secrets, signing keys, or raw user
# content. The bundle is a single zip file, deterministically named.
#
# Three sources of data go in:
#
#   1. App-side snapshot (collect_support_bundle Django command):
#      doctor JSON, redacted settings, migration head, validator
#      inventory, version info, outbound-call status. The Pydantic
#      ``validibot.support-bundle.v1`` schema pins the shape.
#
#   2. Host-side artefacts (this recipe captures):
#      - docker compose ps output (service status)
#      - recent web/worker container logs (last 200 lines each)
#      - disk usage (df -h)
#      - validator backend image inventory (with OCI labels)
#
#   3. Optional: a copy of the docker-compose.production.yml file
#      itself (no values, just structure).
#
# Redaction lives in the app-side command (``validibot.core.support_bundle``)
# — name-based on settings + value-shape defense in depth. The
# host-side artefacts (logs, ps output) are NOT scrubbed: they may
# contain operator-supplied URLs, request paths, etc., but the
# Validibot containers do not log secrets. Operators reviewing the
# bundle before sending can spot-check, and the support workflow
# documents the expectation.

# Generate a redacted support bundle zip for support tickets.
# Usage: just self-hosted collect-support-bundle [--output <path>]
collect-support-bundle *flags:
    @just self-hosted _run-collect-support-bundle "{{flags}}"

# Run the support bundle assembly.
#
# Walks both data sources in order and zips them into a deterministically-
# named archive at ``support-bundles/<timestamp>.zip``. The recipe
# warns if the deployment is currently down (``docker compose ps``
# empty) but still produces a bundle — a "deployment is broken"
# bundle is exactly the kind support most needs.
[private]
_run-collect-support-bundle flags:
    #!/usr/bin/env bash
    set -euo pipefail

    OUTPUT=""
    # shellcheck disable=SC2086
    set -- {{flags}}
    while [ $# -gt 0 ]; do
        case "$1" in
            --output)
                shift
                OUTPUT="${1:-}"
                ;;
            --output=*)
                OUTPUT="${1#--output=}"
                ;;
            "")
                ;;
            *)
                echo "ERROR: unknown flag: $1" >&2
                echo "Usage: just self-hosted collect-support-bundle [--output <path>]" >&2
                exit 1
                ;;
        esac
        shift
    done

    BUNDLE_ID=$(date -u +%Y%m%dT%H%M%SZ)
    HOST=$(hostname -s 2>/dev/null || echo "self-hosted")
    if [ -z "${OUTPUT}" ]; then
        OUTPUT="support-bundles/support-bundle-${HOST}-${BUNDLE_ID}.zip"
    fi
    mkdir -p "$(dirname "${OUTPUT}")"

    # Use a temp directory for assembly; the final zip is the only
    # artefact left behind on disk. ``trap`` ensures cleanup even on
    # error, so an interrupted bundle doesn't leave clutter.
    WORKDIR=$(mktemp -d)
    trap 'rm -rf "$WORKDIR"' EXIT

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Support bundle — ${HOST}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Bundle ID:  ${BUNDLE_ID}"
    echo "  Output:     ${OUTPUT}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Step 1: App-side snapshot ─────────────────────────────────────
    # The Django command produces a single JSON document with all the
    # app-side data. If the deployment is down (web container not
    # running), this step fails with a clear error and we still
    # produce a host-side-only bundle — that's exactly the case
    # support most needs to see.
    echo "Step 1/4: Capturing app-side snapshot (collect_support_bundle)..."
    if docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web \
        python manage.py collect_support_bundle 2>"${WORKDIR}/_app_snapshot_err.log" \
        > "${WORKDIR}/app-snapshot.json"; then
        echo "  ✓ App snapshot captured."
    else
        echo "  ⚠ App snapshot failed — web container down or unhealthy."
        echo "    The bundle will still include host-side artefacts."
        # Preserve the stderr in the bundle so support can see why.
        mv "${WORKDIR}/_app_snapshot_err.log" "${WORKDIR}/app-snapshot-error.log"
        # Drop the empty snapshot file rather than ship a malformed JSON.
        rm -f "${WORKDIR}/app-snapshot.json"
    fi
    rm -f "${WORKDIR}/_app_snapshot_err.log"
    echo ""

    # ── Step 2: Host-side artefacts ───────────────────────────────────
    # Container logs are piped through ``manage.py redact_text``
    # (running inside the web container) so credentials that may
    # have appeared in URLs, request payloads, or stack traces are
    # stripped before zipping. Without this, the app-side snapshot's
    # redaction would be undermined by a verbatim log dump.
    #
    # Service status, disk usage, and other operator-status output
    # are NOT piped through the redactor — they're structured by
    # docker / system commands and have no leak surface. Adding
    # redaction there would just slow things down.
    echo "Step 2/4: Capturing host-side artefacts..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps \
        > "${WORKDIR}/service-status.txt" 2>&1 || true

    # Redact each container's recent logs through the canonical
    # ``manage.py redact_text`` command if the web container is up,
    # OR the standalone stdlib script if the container is down.
    # Operators most often collect a support bundle WHEN the web
    # container is broken, so the standalone fallback is the load-
    # bearing path here, not an edge case. Earlier we fell through
    # to raw logs in that scenario, which broke the redacted-bundle
    # contract under exactly the conditions support bundles exist
    # to handle.
    #
    # Priority order:
    #   1. ``docker compose exec web python manage.py redact_text``
    #      — works when the deployment is healthy, uses the
    #      canonical redaction module
    #   2. ``python3 deploy/self-hosted/scripts/redact-text.py``
    #      — stdlib-only standalone script with the same patterns
    #      (a drift-test asserts they match), works regardless of
    #      container state
    #   3. Omit logs entirely, write the kit-shipped placeholder
    #      explaining how to capture and review logs by hand
    REDACTOR="deploy/self-hosted/scripts/redact-text.py"
    _capture_redacted_log() {
        local svc="$1" outfile="$2"
        # Try the canonical in-container redactor first.
        if docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} \
            logs --tail=200 "$svc" 2>&1 \
            | docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} \
                exec -T web python manage.py redact_text \
            > "$outfile" 2>/dev/null \
            && [ -s "$outfile" ]; then
            return 0
        fi
        # Fall back to the standalone stdlib redactor — the path
        # operators take when the web container is down (which is
        # often the very reason they're collecting a bundle).
        if [ -f "$REDACTOR" ] && command -v python3 >/dev/null 2>&1; then
            docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} \
                logs --tail=200 "$svc" 2>&1 \
                | python3 "$REDACTOR" > "$outfile" 2>/dev/null || true
            return 0
        fi
        # Neither redactor is available — omit logs and explain.
        if [ -f deploy/self-hosted/support-bundle-logs-omitted.txt ]; then
            cp deploy/self-hosted/support-bundle-logs-omitted.txt "$outfile"
        else
            echo "Logs omitted: redactor unavailable. See README." > "$outfile"
        fi
    }
    _capture_redacted_log web "${WORKDIR}/recent-web.log"
    _capture_redacted_log worker "${WORKDIR}/recent-worker.log"
    _capture_redacted_log postgres "${WORKDIR}/recent-postgres.log"

    df -h > "${WORKDIR}/disk-usage.txt" 2>&1 || true
    echo "  ✓ Host artefacts captured (logs piped through redact_text)."
    echo ""

    # ── Step 3: Validator backend inventory ───────────────────────────
    # Re-runs the inventory recipe (Phase 5) and captures its output.
    # This duplicates what an operator running ``validators`` directly
    # would see — but support staff get it without asking.
    echo "Step 3/4: Capturing validator backend inventory..."
    just self-hosted validators > "${WORKDIR}/validators.txt" 2>&1 || true
    echo "  ✓ Validator inventory captured."
    echo ""

    # ── Step 4: Versions + bundle manifest ────────────────────────────
    # A small ``versions.txt`` with the operator-perspective values:
    # Docker daemon version, host OS, etc. — things the Django
    # container can't see itself.
    echo "Step 4/4: Capturing versions + writing manifest..."
    {
        echo "host: ${HOST}"
        echo "bundle_id: ${BUNDLE_ID}"
        echo "captured_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo ""
        echo "[Docker]"
        docker version 2>/dev/null || echo "(docker version unavailable)"
        echo ""
        echo "[OS]"
        uname -a 2>/dev/null || true
        if [ -f /etc/os-release ]; then
            cat /etc/os-release
        fi
        echo ""
        echo "[just]"
        just --version 2>/dev/null || echo "(just version unavailable)"
    } > "${WORKDIR}/versions.txt"

    # README is shipped as a kit artefact at
    # ``deploy/self-hosted/support-bundle-README.txt`` rather than
    # embedded as a heredoc here, because just's recipe parser
    # chokes on shell text containing ``@`` and ``.`` (it tries to
    # interpret heredoc bodies as just syntax). The kit file is
    # easier to maintain anyway — operators can preview the README
    # without running the recipe.
    if [ -f deploy/self-hosted/support-bundle-README.txt ]; then
        cp deploy/self-hosted/support-bundle-README.txt "${WORKDIR}/README.txt"
    else
        echo "Validibot self-hosted support bundle. See validibot docs." \
            > "${WORKDIR}/README.txt"
    fi

    echo "  ✓ Manifest written."
    echo ""

    # ── Zip it up ─────────────────────────────────────────────────────
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
    echo ""
    echo "  Send to:"
    echo "    support@validibot.com (attach the .zip)"

# =============================================================================
# Validator inventory (Phase 5 of ADR-2026-04-27)
# =============================================================================
#
# ``just self-hosted validators`` lists the validator backend images
# present on this host's Docker daemon, with their OCI version labels
# and content digests. It's the operator-facing answer to "which
# validator versions are installed and what are their identities?"
#
# What this surface shows:
#   - Backend slug (e.g. energyplus, fmu) — read from the
#     ``io.validibot.validator-backend.slug`` OCI label.
#   - Backend image version — the human-readable
#     ``org.opencontainers.image.version`` label (e.g. EnergyPlus
#     25.2.0). This is the cleanup that landed earlier this session;
#     before that label existed, operators only saw cryptic image
#     IDs.
#   - Image content digest — sha256:... — the load-bearing identity
#     for trust verification (cosign, signed-digest pinning).
#   - Tag — the human-readable image tag (latest, sha-abc1234, etc.)
#   - Size + age — basic filesystem facts.
#
# What this surface does NOT do:
#   - It does not run the validators (use ``just self-hosted
#     smoke-test`` for end-to-end pipeline verification).
#   - It does not check whether the images are pinned in the live
#     Validibot deployment's ``Validator`` rows (doctor's VB712 /
#     VB713 cover that policy check).
#   - It does not pull or build images. Build with ``validator-build``
#     or ``validators-build-all``.

# Validator backend inventory — list local backend images with OCI version + digest.
validators:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Validator backends — local Docker daemon"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ``docker image ls`` filters by reference glob. The repository
    # convention ``validibot-validator-backend-<slug>`` is set by
    # ``just self-hosted validator-build``; deviating from it would
    # hide images from this listing, but no operator path produces
    # different names today.
    IMAGES=$(docker image ls \
        --filter 'reference=validibot-validator-backend-*' \
        --format '{{"{{.ID}}"}}|{{"{{.Repository}}"}}|{{"{{.Tag}}"}}|{{"{{.Size}}"}}|{{"{{.CreatedSince}}"}}' \
        2>/dev/null || true)

    if [ -z "${IMAGES}" ]; then
        echo "  No validator backend images found on this Docker daemon."
        echo ""
        echo "  Build them with:"
        echo "    just self-hosted validators-build-all"
        echo "    just self-hosted validator-build energyplus"
        echo "    just self-hosted validator-build fmu"
        echo "    just self-hosted validator-build portfolio_manager"
        exit 0
    fi

    # Tabular output. We ``docker image inspect`` once per row to
    # read the OCI label set; piping via ``--format`` keeps the
    # parse in Docker rather than fighting JSON in shell.
    printf "%-44s %-14s %-18s %-22s %-10s %s\n" \
        "REPOSITORY" "TAG" "BACKEND VERSION" "DIGEST" "SIZE" "AGE"
    printf "%-44s %-14s %-18s %-22s %-10s %s\n" \
        "$(printf '%.0s-' {1..44})" \
        "$(printf '%.0s-' {1..14})" \
        "$(printf '%.0s-' {1..18})" \
        "$(printf '%.0s-' {1..22})" \
        "$(printf '%.0s-' {1..10})" \
        "$(printf '%.0s-' {1..3})"

    while IFS='|' read -r ID REPO TAG SIZE AGE; do
        [ -z "${ID}" ] && continue
        # Read the OCI version label. ``-f`` returns empty string if
        # the label is missing, which we surface as "(unset)" so the
        # operator knows the image was built without the label work.
        BACKEND_VERSION=$(docker image inspect "${ID}" \
            --format '{{"{{ index .Config.Labels \"org.opencontainers.image.version\" }}"}}' \
            2>/dev/null || echo "")
        [ -z "${BACKEND_VERSION}" ] && BACKEND_VERSION="(unset)"
        # Truncate the ID to 22 chars so the column stays aligned.
        # Operators who want the full digest run ``docker image
        # inspect`` directly.
        SHORT_ID="${ID:0:22}"
        printf "%-44s %-14s %-18s %-22s %-10s %s\n" \
            "${REPO}" "${TAG}" "${BACKEND_VERSION}" "${SHORT_ID}" "${SIZE}" "${AGE}"
    done <<< "${IMAGES}"

    echo ""
    echo "Tip: backend version comes from org.opencontainers.image.version"
    echo "     (set when the image was built from validibot-validator-backends)."
    echo "     Use the digest for trust-critical verification, not the tag."

# =============================================================================
# Cleanup (Phase 5 of ADR-2026-04-27)
# =============================================================================
#
# Long-running self-hosted deployments accumulate artefacts that aren't
# part of any working set: stopped validator containers from runs that
# completed days ago, dangling images from previous builds, expired
# manifested backups past retention, old upgrade reports. Operators
# shouldn't need to know ``docker prune`` syntax or remember when their
# last cleanup ran.
#
# What this cleans:
#   - Stopped validator containers older than ${VALIDATOR_RETAIN_HOURS} (default 24h).
#     Identified by their ``io.validibot.validator-backend.slug`` OCI label.
#   - Dangling Docker images (Docker's "<none>" tagged layer leftovers).
#   - Manifested backups older than ${BACKUP_RETAIN_DAYS} (default 30d).
#     Determined from each backup's ``manifest.json::created_at``.
#   - Upgrade reports older than ${UPGRADE_REPORT_RETAIN_DAYS} (default 90d).
#
# What this DOES NOT clean:
#   - Validator backend IMAGES themselves — those persist across cleanups
#     because re-pulling / re-building them is expensive. Use
#     ``docker image prune`` directly when you genuinely want to reclaim
#     image storage.
#   - The current Postgres / Redis / storage volumes — those are part of
#     the working set. ``clean-all`` is the recipe for that.
#   - Ad-hoc ``backup-db`` ``.sql.gz`` dumps at the top of ``backups/`` —
#     those are operator-managed; we don't know your retention policy.
#
# Pattern adopted from Discourse's ``./launcher cleanup``: always show
# what would be deleted before deleting, even without ``--dry-run``.
# That makes the recipe safe to run on a schedule (cron / systemd
# timer) by passing ``--yes`` to skip the interactive confirm.

# Prune stale containers, dangling images, and expired backups (Phase 5).
# Usage: just self-hosted cleanup [--dry-run] [--yes]
cleanup *flags:
    @just self-hosted _run-cleanup "{{flags}}"

# Run the cleanup orchestration.
#
# Three retention scopes (validator containers, backups, upgrade
# reports) are each scanned, candidates listed, and an aggregate
# confirmation requested before any deletion. ``--dry-run`` lists
# candidates and exits 0 without touching anything; ``--yes`` skips
# the confirmation prompt for cron-friendly use.
[private]
_run-cleanup flags:
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
                echo "Usage: just self-hosted cleanup [--dry-run] [--yes]" >&2
                exit 1
                ;;
        esac
        shift
    done

    # Retention windows. Configurable via env so cron jobs can tune
    # them per-deployment without editing the recipe.
    VALIDATOR_RETAIN_HOURS="${VALIDATOR_RETAIN_HOURS:-24}"
    BACKUP_RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-30}"
    UPGRADE_REPORT_RETAIN_DAYS="${UPGRADE_REPORT_RETAIN_DAYS:-90}"

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Cleanup — $([ ${DRY_RUN} -eq 1 ] && echo 'DRY RUN' || echo 'live')"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Validator container retention: ${VALIDATOR_RETAIN_HOURS}h"
    echo "  Backup retention:              ${BACKUP_RETAIN_DAYS}d"
    echo "  Upgrade-report retention:      ${UPGRADE_REPORT_RETAIN_DAYS}d"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Scope 1: stopped validator containers ─────────────────────────
    # Filter by the validator-backend label so we don't accidentally
    # pick up other ``exited`` containers on the host. The cutoff
    # comes from a ``--filter "until=<duration>"`` which Docker
    # interprets as "older than".
    echo "Scope 1/3: Stopped validator containers older than ${VALIDATOR_RETAIN_HOURS}h..."
    VALIDATOR_CANDIDATES=$(docker container ls -a \
        --filter 'label=io.validibot.validator-backend.slug' \
        --filter 'status=exited' \
        --filter "until=${VALIDATOR_RETAIN_HOURS}h" \
        --format '{{"{{.ID}}"}}|{{"{{.Names}}"}}|{{"{{.Status}}"}}|{{"{{.RunningFor}}"}}' \
        2>/dev/null || true)
    if [ -z "${VALIDATOR_CANDIDATES}" ]; then
        echo "  (none)"
    else
        echo "${VALIDATOR_CANDIDATES}" | while IFS='|' read -r CID NAME STATUS AGE; do
            [ -z "${CID}" ] && continue
            printf "  %-13s %-30s %s (%s)\n" "${CID:0:12}" "${NAME}" "${STATUS}" "${AGE}"
        done
    fi
    echo ""

    # ── Scope 2: expired manifested backups ───────────────────────────
    # We walk ``backups/<id>/manifest.json``, read ``created_at``,
    # compare against ``now - BACKUP_RETAIN_DAYS``. Backups without
    # a parseable manifest are NOT deleted — that's a safety
    # invariant: cleanup only touches things we can clearly identify.
    echo "Scope 2/3: Manifested backups older than ${BACKUP_RETAIN_DAYS}d..."
    if ! command -v jq >/dev/null 2>&1; then
        echo "  ERROR: 'jq' not found on host — install it (apt install jq)." >&2
        exit 1
    fi
    BACKUP_CUTOFF_S=$(($(date -u +%s) - BACKUP_RETAIN_DAYS * 86400))
    BACKUP_CANDIDATES=""
    if [ -d backups ]; then
        while IFS= read -r MANIFEST; do
            [ -z "${MANIFEST}" ] && continue
            CREATED=$(jq -r '.created_at // empty' "${MANIFEST}" 2>/dev/null || true)
            [ -z "${CREATED}" ] && continue
            # ``date -d`` accepts ISO 8601; on macOS BSD ``date`` use -j -f.
            if CREATED_S=$(date -d "${CREATED}" +%s 2>/dev/null); then
                : # GNU
            elif CREATED_S=$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "${CREATED%.*Z}+0000" +%s 2>/dev/null); then
                : # BSD fallback
            else
                continue
            fi
            if [ "${CREATED_S}" -lt "${BACKUP_CUTOFF_S}" ]; then
                BACKUP_DIR=$(dirname "${MANIFEST}")
                SIZE=$(du -sh "${BACKUP_DIR}" 2>/dev/null | awk '{print $1}')
                BACKUP_CANDIDATES="${BACKUP_CANDIDATES}${BACKUP_DIR}|${CREATED}|${SIZE}\n"
            fi
        done < <(find backups -mindepth 2 -maxdepth 2 -name manifest.json 2>/dev/null | sort)
    fi
    if [ -z "${BACKUP_CANDIDATES}" ]; then
        echo "  (none)"
    else
        printf "${BACKUP_CANDIDATES}" | while IFS='|' read -r DIR CREATED SIZE; do
            [ -z "${DIR}" ] && continue
            printf "  %-44s  %s  (%s)\n" "${DIR}" "${CREATED}" "${SIZE}"
        done
    fi
    echo ""

    # ── Scope 3: expired upgrade reports ──────────────────────────────
    # Upgrade reports are tiny (~500 bytes each), so the retention
    # is much longer than the backup retention. We use mtime rather
    # than parsing the report's ``completed_at`` because the
    # filesystem timestamp is a sufficient proxy for "when was this
    # written" and avoids another jq pass.
    echo "Scope 3/3: Upgrade reports older than ${UPGRADE_REPORT_RETAIN_DAYS}d..."
    UPGRADE_CANDIDATES=""
    if [ -d backups/upgrades ]; then
        while IFS= read -r REPORT; do
            [ -z "${REPORT}" ] && continue
            REPORT_DIR=$(dirname "${REPORT}")
            UPGRADE_CANDIDATES="${UPGRADE_CANDIDATES}${REPORT_DIR}\n"
        done < <(find backups/upgrades -mindepth 2 -maxdepth 2 -name report.json -type f \
            -mtime "+${UPGRADE_REPORT_RETAIN_DAYS}" 2>/dev/null | sort)
    fi
    if [ -z "${UPGRADE_CANDIDATES}" ]; then
        echo "  (none)"
    else
        printf "${UPGRADE_CANDIDATES}" | while IFS= read -r DIR; do
            [ -z "${DIR}" ] && continue
            printf "  %s\n" "${DIR}"
        done
    fi
    echo ""

    # Dangling images aren't tied to a retention window — they're
    # always safe to remove because nothing references them. We
    # still preview before deleting so operators see the count.
    DANGLING_COUNT=$(docker image ls --filter 'dangling=true' --quiet 2>/dev/null | wc -l | tr -d ' ')
    echo "Bonus: ${DANGLING_COUNT} dangling image(s) ready to prune."
    echo ""

    # ── Decision point ────────────────────────────────────────────────
    # Always check whether anything would actually be deleted before
    # asking for confirmation. If everything is empty, we have nothing
    # to do and exit 0 cheerfully — useful for cron, where running
    # nightly with nothing to clean shouldn't surface as anything.
    HAS_WORK=0
    [ -n "${VALIDATOR_CANDIDATES}" ] && HAS_WORK=1
    [ -n "${BACKUP_CANDIDATES}" ] && HAS_WORK=1
    [ -n "${UPGRADE_CANDIDATES}" ] && HAS_WORK=1
    [ "${DANGLING_COUNT}" -gt 0 ] && HAS_WORK=1

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
    if [ -n "${VALIDATOR_CANDIDATES}" ]; then
        echo "${VALIDATOR_CANDIDATES}" | while IFS='|' read -r CID _ _ _; do
            [ -z "${CID}" ] && continue
            docker rm "${CID}" >/dev/null
        done
        echo "  ✓ Validator containers removed."
    fi

    if [ -n "${BACKUP_CANDIDATES}" ]; then
        printf "${BACKUP_CANDIDATES}" | while IFS='|' read -r DIR _ _; do
            [ -z "${DIR}" ] && continue
            rm -rf "${DIR}"
        done
        echo "  ✓ Expired backups removed."
    fi

    if [ -n "${UPGRADE_CANDIDATES}" ]; then
        printf "${UPGRADE_CANDIDATES}" | while IFS= read -r DIR; do
            [ -z "${DIR}" ] && continue
            rm -rf "${DIR}"
        done
        echo "  ✓ Old upgrade reports removed."
    fi

    if [ "${DANGLING_COUNT}" -gt 0 ]; then
        docker image prune -f >/dev/null
        echo "  ✓ Dangling images pruned."
    fi

    echo ""
    echo "Cleanup complete."

# =============================================================================
# Quick Reference
# =============================================================================
#
# Common operations:
#
#   just self-hosted bootstrap     # First-time install
#   just self-hosted deploy        # Deploy/update the application
#   just self-hosted status        # Check if services are running
#   just self-hosted logs          # View logs
#   just self-hosted backup-db     # Create database backup
#   just self-hosted manage "..."  # Run Django management command
#
# First-time setup:
#
#   1. cp .envs.example/.production/.self-hosted/.django .envs/.production/.self-hosted/.django
#   2. cp .envs.example/.production/.self-hosted/.postgres .envs/.production/.self-hosted/.postgres
#   3. Edit both files with your settings
#   4. just self-hosted check-env
#   5. just self-hosted bootstrap
#
# Updating:
#
#   just self-hosted update        # Full update with backup and migrations
#
# Troubleshooting:
#
#   just self-hosted health-check  # Check service status
#   just self-hosted errors        # View recent errors
#   just self-hosted resources     # Check resource usage
#
# =============================================================================
