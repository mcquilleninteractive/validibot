# =============================================================================
# Validibot Cloud — Local Development
# =============================================================================
#
# Run the cloud version locally via Docker Compose.
#
# Usage:
#   just local-cloud up            # Start cloud containers (detached)
#   just local-cloud up --build    # Rebuild and start
#   just local-cloud down          # Stop cloud containers
#
# Note: `just local down` only stops the community-only stack (docker-compose.local.yml).
# The cloud stack includes cloud billing and tenancy services that aren't in
# the local compose file, so use `just local-cloud down` to stop them.
#
# =============================================================================

cloud_compose := "../validibot-cloud/docker-compose.cloud.yml"

# Helper to construct DATABASE_URL from individual POSTGRES_* env vars.
# The entrypoint sets this at container start, but `docker compose exec` bypasses it.
# Duplicated from local.just because just modules have isolated scope.
_db_url := 'DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"'

# Start cloud containers locally
#
# Usage:
#   just local-cloud up                 # web on http://localhost:8000
#   just local-cloud up --port 8080     # web on http://localhost:8080 (-p works too)
#   just local-cloud up --build         # rebuild image + refresh the venv volume
#
# The web container automatically applies migrations, initializes or refreshes
# the system validator catalog, syncs help pages, and seeds bundled EnergyPlus
# weather resources before it becomes healthy. No separate setup commands are
# required after ``up``. Test-only users and workflows are still provisioned by
# the E2E recipes, not by normal application startup.
#
# The --port / -p flag remaps only the host side of the web container's port
# (the container still serves on 8000). It is consumed by this recipe and not
# passed through to `docker compose`, which has no --port flag of its own.
#
# When --build is passed, the stack is stopped and the venv volume is removed
# first so that `uv sync` installs dependencies from the updated lockfile.
# Without this, the persisted volume can serve a stale venv that
# is missing newly added packages.
[no-cd]
up *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # Pull an optional host-port override (-p / --port / --port=N) out of the
    # args before the rest flow through to `docker compose up`, which has no
    # --port flag of its own. WEB_PORT is read by the web service's ``ports``
    # mapping in docker-compose.cloud.yml; the container always serves on 8000.
    read -ra _parsed <<< "{{args}}" || true
    args=""
    port_set=0
    skip_next=0
    for ((i = 0; i < ${#_parsed[@]}; i++)); do
        if [[ "$skip_next" == "1" ]]; then
            skip_next=0
            continue
        fi
        case "${_parsed[$i]}" in
            -p|--port)
                export WEB_PORT="${_parsed[$((i + 1))]:-}"
                port_set=1
                skip_next=1
                ;;
            -p=*|--port=*)
                export WEB_PORT="${_parsed[$i]#*=}"
                port_set=1
                ;;
            *)
                args="${args:+$args }${_parsed[$i]}"
                ;;
        esac
    done
    if [[ "$port_set" == "1" ]]; then
        if [[ ! "${WEB_PORT:-}" =~ ^[0-9]+$ ]]; then
            echo "Error: --port expects a port number, e.g. 'just local-cloud up --port 8080'." >&2
            exit 1
        fi
        echo "Mapping web to http://localhost:${WEB_PORT} (container still serves on 8000)."
    fi
    if [[ "$args" == *"--build"* ]]; then
        docker compose -f "{{cloud_compose}}" down
        docker volume rm validibot_validibot_local_venv 2>/dev/null || true
    fi
    docker compose -f "{{cloud_compose}}" up -d $args

# Stop all cloud containers.
#
# Tears down every service defined in docker-compose.cloud.yml — web, worker,
# postgres, redis, mailpit, etc. Volumes are preserved so data persists
# across restarts; use `just local-cloud clean` for a full reset.
# Run a Django management command in the cloud web container
# (mirrors `just local manage` — the exec bypasses the entrypoint, so
# DATABASE_URL is assembled from the POSTGRES_* vars via _db_url).
# Usage: just local-cloud manage createsuperuser
# Usage: just local-cloud manage "changepassword admin"
[no-cd]
manage +command:
    docker compose -f {{cloud_compose}} exec web bash -c '{{_db_url}} python manage.py {{command}}'

[no-cd]
down:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose -f "{{cloud_compose}}" down

# Stop the complete cloud stack and delete every local data volume.
#
# This is the required reset path after an intentionally incompatible migration
# rewrite. It removes PostgreSQL, Redis, uploaded artifacts, backups, and the
# shared Python environment so the next ``up --build`` starts from a genuinely
# empty deployment.
# WARNING: this permanently deletes all local-cloud data.
[no-cd]
clean:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose -f "{{cloud_compose}}" down -v --remove-orphans

# Rebuild and start containers, clearing the venv volume
#
# The venv lives in a named Docker volume so it persists across image
# rebuilds.  This recipe removes the volume first, guaranteeing that
# `uv sync` repopulates it from the fresh image (critical after
# dependency upgrades like validibot-shared).
#
# Usage: just local-cloud rebuild              # cached rebuild + fresh venv
#        just local-cloud rebuild --no-cache   # full clean rebuild (slow)
#        just local-cloud rebuild worker       # rebuild specific service
[no-cd]
rebuild *args:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose -f "{{cloud_compose}}" down
    docker volume rm validibot_validibot_local_venv 2>/dev/null || true
    docker compose -f "{{cloud_compose}}" build {{args}}
    docker compose -f "{{cloud_compose}}" up -d

# Run EnergyPlus E2E tests — one command does everything
#
# This is the single-command entry point for running complete end-to-end
# EnergyPlus tests.  It ensures the stack is running, provisions test
# data, checks for the EnergyPlus Docker image, and runs the tests.
#
# Usage: just local-cloud e2e-tests
#        just local-cloud e2e-tests -k test_passing   # run specific test
[no-cd]
e2e-tests *args:
    #!/usr/bin/env bash
    set -euo pipefail

    compose="{{cloud_compose}}"

    # --- 1. Ensure Docker Compose stack is running ---
    running=$(docker compose -f "$compose" ps --status running -q 2>/dev/null | wc -l | tr -d ' ')
    if [ "$running" -lt 3 ]; then
        echo "Docker Compose stack is not fully running ($running containers)."
        echo "Starting with: docker compose -f $compose up -d --build"
        echo ""
        docker compose -f "$compose" up -d --build
        echo ""
        echo "Waiting for containers to become healthy..."
        sleep 10
    fi

    # --- 2. Check EnergyPlus validator image exists ---
    if ! docker image inspect validibot-validator-backend-energyplus:latest >/dev/null 2>&1; then
        echo ""
        echo "Error: EnergyPlus validator image not found."
        echo "Build it first: (in ../validibot-validator-backends) just build energyplus"
        echo ""
        exit 1
    fi

    # --- 3. Run prerequisites (idempotent) ---
    echo "Ensuring complete application-data initialization..."
    docker compose -f "$compose" exec -T web \
        bash -c '{{_db_url}} python manage.py initialize_validibot' >/dev/null 2>&1

    # --- 4. Provision E2E workflow data ---
    echo "Provisioning E2E workflow data..."
    env_output=$(docker compose -f "$compose" exec -T web \
        bash -c '{{_db_url}} python manage.py setup_e2e_workflows --export-env' 2>/dev/null)
    if [ -z "$env_output" ]; then
        echo ""
        echo "Error: Could not provision E2E workflow data."
        echo "Check container logs: docker compose -f $compose logs web"
        echo ""
        exit 1
    fi

    # The community command owns workflow fixtures; the cloud companion adds
    # the trial/profile/credit state required by fail-closed hosted policies.
    docker compose -f "$compose" exec -T web \
        bash -c '{{_db_url}} python manage.py setup_e2e_cloud' >/dev/null

    # Filter out non-env lines (e.g. Django startup logs) and eval only KEY=VALUE lines
    eval "$(echo "$env_output" | grep -E '^[A-Z][A-Z0-9_]*=.+$')"
    export FULLSTACK_API_TOKEN FULLSTACK_ORG_SLUG E2E_ENERGYPLUS_TEMPLATE_WORKFLOW_ID

    # --- 5. Run E2E tests ---
    echo ""
    echo "Running EnergyPlus E2E tests against: ${FULLSTACK_API_URL:-http://localhost:8000/api/v1}"
    echo "Workflow ID: ${E2E_ENERGYPLUS_TEMPLATE_WORKFLOW_ID}"
    echo "Note: Each test runs a real EnergyPlus simulation (~2-5 min per test)"
    echo ""
    uv run --group dev pytest tests/tests_e2e/test_energyplus_template.py {{args}} -v --log-cli-level=INFO
