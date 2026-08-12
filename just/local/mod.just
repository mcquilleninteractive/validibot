# =============================================================================
# Validibot - Local Development (Docker Compose)
# =============================================================================
#
# Commands for local development using Docker Compose. These run containers
# on your local machine for development and testing.
#
# This is NOT for production Docker Compose deployments - see the 'docker-compose'
# module for that.
#
# Prerequisites:
#   - Docker Desktop or Docker Engine installed
#   - docker-compose.local.yml in the project root
#
# Quick start:
#   just local up          # Start all containers
#   just local logs        # Follow logs
#   just local shell       # Open Django shell
#   just local down        # Stop containers
#
# Optional for Pro/Enterprise:
#   copy .envs.example/.local/.build to .envs/.local/.build before the first build
#
# =============================================================================

# Note: Shell setting is inherited from root justfile

# Docker Compose file for local development
local_compose := "docker-compose.local.yml"

# The .build file plays two roles — see ``.envs.example/.local/.build``
# for the full explanation:
#
# 1. ``--env-file`` for ``docker compose`` YAML interpolation of
#    ``${FOO}`` references (mostly Pro/Enterprise packaging build args).
# 2. ``source``d at the top of recipe bodies that need future shell-level
#    deployment knobs. This keeps the local recipe variants symmetric.
#
# Both are no-ops when the file is absent.
local_compose_env_args := `bash -lc 'if [ -f ".envs/.local/.build" ]; then printf -- "--env-file .envs/.local/.build"; fi'`

# =============================================================================
# Container Lifecycle
# =============================================================================

# Start all local Docker containers in detached mode
# This brings up PostgreSQL, Redis, Django, and any other services defined
# in docker-compose.local.yml
#
# [no-cd] keeps the recipe running from the project root (Just's default
# for mod recipes is the module's own directory). Without it, the relative
# {{local_compose}} path would resolve to just/local/docker-compose.local.yml
# and fail. The same applies to every recipe in this file that touches
# {{local_compose}} or relative paths like .envs/ — see local-pro/mod.just
# and local-cloud/mod.just for the same pattern.
[no-cd]
up:
    #!/usr/bin/env bash
    # Check for port conflicts before starting
    conflicts=()
    for port in 8000 8025; do
        pid=$(lsof -ti :$port 2>/dev/null)
        if [ -n "$pid" ]; then
            process=$(ps -p $pid -o comm= 2>/dev/null)
            conflicts+=("  Port $port is in use by '$process' (PID $pid)")
        fi
    done
    if [ ${#conflicts[@]} -gt 0 ]; then
        echo ""
        echo "⚠️  Port conflict detected:"
        for c in "${conflicts[@]}"; do
            echo "$c"
        done
        echo ""
        echo "This is often caused by a leftover process from a previous session."
        echo "To fix: kill the process(es) above, then re-run 'just local up'."
        echo ""
        exit 1
    fi
    docker compose {{local_compose_env_args}} -f {{local_compose}} up -d

# Stop all local Docker containers
# Containers are stopped but not removed - data persists in volumes
[no-cd]
down:
    docker compose {{local_compose_env_args}} -f {{local_compose}} down

# Rebuild and start all containers
# Use this after changing Dockerfile, requirements, or dependencies
# Usage: just local build           # detached (background)
# Usage: just local build --attach  # attached (shows logs, Ctrl+C to stop)
[no-cd]
build *args:
    docker compose {{local_compose_env_args}} -f {{local_compose}} up {{ if args == "--attach" { "" } else { "-d" } }} --build

# Rebuild and start containers, clearing the venv volume
#
# The venv lives in a named Docker volume so it persists across image
# rebuilds.  This recipe removes the volume first, guaranteeing that
# `uv sync` repopulates it from the fresh image (critical after
# dependency upgrades like validibot-shared).
#
# Usage: just local rebuild              # cached rebuild + fresh venv
#        just local rebuild --no-cache   # full clean rebuild (slow)
#        just local rebuild worker       # rebuild specific service
[no-cd]
rebuild *args:
    docker compose {{local_compose_env_args}} -f {{local_compose}} down
    -docker volume rm validibot_validibot_local_venv
    docker compose {{local_compose_env_args}} -f {{local_compose}} build {{args}}
    docker compose {{local_compose_env_args}} -f {{local_compose}} up -d

# Follow logs from all containers (Ctrl+C to exit)
# Shows interleaved logs from all services
[no-cd]
logs:
    docker compose {{local_compose_env_args}} -f {{local_compose}} logs -f

# Show status of all containers
[no-cd]
ps:
    docker compose {{local_compose_env_args}} -f {{local_compose}} ps

# Restart all containers (stop then start)
restart: down up

# Stop containers and remove volumes (clean slate)
# WARNING: This deletes all database data! Use for clean slate only.
[no-cd]
clean:
    docker compose {{local_compose_env_args}} -f {{local_compose}} down -v --remove-orphans

# =============================================================================
# Django Commands in Container
# =============================================================================

# Helper to construct DATABASE_URL from individual POSTGRES_* env vars
# The entrypoint sets this at container start, but `docker compose exec` bypasses it
_db_url := 'DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"'

# Open a bash shell in the Django container
# Useful for running arbitrary commands, debugging, or exploring
[no-cd]
shell:
    docker compose {{local_compose_env_args}} -f {{local_compose}} exec web bash -c '{{_db_url}} exec bash'

# Open a Django shell in the container
# Usage: just local djshell
[no-cd]
djshell:
    docker compose {{local_compose_env_args}} -f {{local_compose}} exec web bash -c '{{_db_url}} python manage.py shell'

# Run Django migrations in the local container
# Applies any pending database migrations
[no-cd]
migrate:
    docker compose {{local_compose_env_args}} -f {{local_compose}} exec web bash -c '{{_db_url}} python manage.py check_migration_history && {{_db_url}} python manage.py migrate'

# Run a Django management command in the container
# Usage: just local manage createsuperuser
# Usage: just local manage "shell_plus --ipython"
# Usage: just local manage seed_weather_files
[no-cd]
manage +command:
    docker compose {{local_compose_env_args}} -f {{local_compose}} exec web bash -c '{{_db_url}} python manage.py {{command}}'

# =============================================================================
# Testing
# =============================================================================

# Run tests locally (no GCP/cloud dependencies)
# Uses local database and mocked services
# Usage: just local test
# Usage: just local test -k "test_name"    # Run specific test
# Usage: just local test --collect-only    # See what tests would run
[no-cd]
test *args:
    uv run pytest {{args}} --log-cli-level=INFO

# Run integration tests end-to-end (starts/stops isolated Postgres + mailpit)
# Uses a separate Docker Compose project name to avoid affecting dev containers.
# Prereqs: Docker Compose available; BUILD_DJANGO_IMAGE=1 to force image rebuild
[no-cd]
test-integration *args:
    #!/usr/bin/env bash
    set -euo pipefail
    PROJECT="validibot-test"
    COMPOSE="docker compose {{local_compose_env_args}} -f {{local_compose}} -f compose/local/docker-compose.integration.yml -p $PROJECT"

    cleanup() {
        echo "Cleaning up test containers..."
        $COMPOSE down -v 2>/dev/null || true
    }
    trap cleanup EXIT

    echo "Starting integration dependencies (postgres, mailpit)..."
    echo "Ensuring web image (with Chromium & chromedriver) exists..."
    IMAGE_NAME="${PROJECT}-web"
    if [ "${BUILD_DJANGO_IMAGE:-0}" -eq 1 ] || ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
        $COMPOSE build web
    else
        echo "Reusing existing $IMAGE_NAME image (set BUILD_DJANGO_IMAGE=1 to force rebuild)"
    fi

    $COMPOSE up -d postgres mailpit
    echo "Running integration tests..."
    $COMPOSE run --rm \
        -e DJANGO_SETTINGS_MODULE=config.settings.test \
        web \
        uv run --group dev pytest {{ if args == "" { "tests/tests_integration/" } else { args } }} -v --log-cli-level=INFO

# Run E2E stress tests against a running Docker Compose environment
# Requires: `just local up` running. Test data (user, org, workflow, API token) is
# auto-provisioned via the setup_fullstack_test_data management command.
# See: docs/dev_docs/how-to/run-e2e-tests.md
#
# Usage: just local test-e2e
[no-cd]
test-e2e *args:
    #!/usr/bin/env bash
    set -euo pipefail

    # Auto-provision test data if env vars aren't already set
    if [ -z "${FULLSTACK_API_TOKEN:-}" ]; then
        echo "Provisioning test data via management command..."
        env_output=$(docker compose {{local_compose_env_args}} -f {{local_compose}} exec -T web \
            bash -c '{{_db_url}} python manage.py setup_fullstack_test_data --export-env' 2>/dev/null)
        if [ -z "$env_output" ]; then
            echo ""
            echo "Error: Could not provision test data."
            echo "Make sure 'just local up' is running and the app has been set up."
            echo ""
            exit 1
        fi
        eval "$(echo "$env_output")"
        export FULLSTACK_API_TOKEN FULLSTACK_ORG_SLUG FULLSTACK_WORKFLOW_ID
    fi

    echo "Running E2E stress tests against: ${FULLSTACK_API_URL:-http://localhost:8000/api/v1}"
    uv run --group dev pytest tests/tests_e2e/test_stress.py {{args}} -v --log-cli-level=INFO

# Run EnergyPlus E2E tests against a running Docker Compose environment
# Requires: Docker Compose stack running (`just local up` or `just local-cloud up --build`)
# AND the EnergyPlus Docker image available.
# The EnergyPlus simulation takes ~2-5 minutes per test case.
#
# Usage: just local test-e2e-energyplus
[no-cd]
test-e2e-energyplus *args:
    #!/usr/bin/env bash
    set -euo pipefail

    # Check EnergyPlus validator image exists
    if ! docker image inspect validibot-validator-backend-energyplus:latest >/dev/null 2>&1; then
        echo ""
        echo "Error: EnergyPlus validator image not found."
        echo "Build it first: (in ../validibot-validator-backends) just build energyplus"
        echo ""
        exit 1
    fi

    # Auto-provision E2E workflow data if env vars aren't already set
    if [ -z "${E2E_ENERGYPLUS_TEMPLATE_WORKFLOW_ID:-}" ]; then
        echo "Provisioning E2E workflow data via management command..."
        env_output=$(docker compose {{local_compose_env_args}} -f {{local_compose}} exec -T web \
            bash -c '{{_db_url}} python manage.py setup_e2e_workflows --export-env' 2>/dev/null)
        if [ -z "$env_output" ]; then
            echo ""
            echo "Error: Could not provision E2E workflow data."
            echo "Make sure Docker Compose is running ('just local up' or 'just local-cloud up --build')"
            echo "and check the web container logs for an automatic setup failure."
            echo ""
            exit 1
        fi
        eval "$(echo "$env_output")"
        export FULLSTACK_API_TOKEN FULLSTACK_ORG_SLUG E2E_ENERGYPLUS_TEMPLATE_WORKFLOW_ID
    fi

    echo "Running EnergyPlus E2E tests against: ${FULLSTACK_API_URL:-http://localhost:8000/api/v1}"
    echo "Workflow ID: ${E2E_ENERGYPLUS_TEMPLATE_WORKFLOW_ID}"
    echo "Note: Each test runs a real EnergyPlus simulation (~2-5 min per test)"
    echo ""
    uv run --group dev pytest tests/tests_e2e/test_energyplus_template.py {{args}} -v --log-cli-level=INFO

# =============================================================================
# Documentation
# =============================================================================

# Build developer documentation with Zensical
# Zensical lives in the opt-in `docs` group (not installed by default), so we
# pull it on demand here with `--group docs`.
[no-cd]
docs-build:
    uv run --group docs zensical build -f mkdocs.dev.yml --clean

# Serve developer documentation locally with Zensical (port 9000)
# For contributors and maintainers. `--group docs` pulls the opt-in docs group.
[no-cd]
docs-dev:
    -lsof -ti :9000 | xargs kill 2>/dev/null
    uv run --group docs zensical serve -f mkdocs.dev.yml -a localhost:9000
