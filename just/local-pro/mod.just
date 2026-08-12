# =============================================================================
# Validibot community + Pro — local Docker Compose
# =============================================================================
#
# Run the community stack locally with validibot-pro editable-installed
# from ../validibot-pro. Use this when you want Pro features (teams,
# guests, MCP) without the full cloud layer on top.
#
# Layered on docker-compose.local.yml via Compose's multi-file merge:
#   docker compose -f docker-compose.local.yml \
#                  -f docker-compose.local-pro.yml \
#                  up -d
#
# Usage:
#   just local-pro up                            # start community + pro
#   just local-pro up --build                    # rebuild + start
#   just local-pro down                          # stop
#   just local-pro rebuild                       # full clean rebuild
#
# If you want the cloud layer (billing, tenancy, onboarding), use
# ``just local-cloud up`` instead — that runs docker-compose.cloud.yml
# from the validibot-cloud repo.
#
# =============================================================================

# Base compose + overlay. Paths are relative to the community repo root.
base_compose := "docker-compose.local.yml"
overlay_compose := "docker-compose.local-pro.yml"

# The same _db_url helper used by just/local.just — duplicated here
# because just modules have isolated scope (can't reference parent recipes).
_db_url := 'DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"'

# Read the base module's optional .envs/.local/.build file if present,
# so VALIDIBOT_COMMERCIAL_PACKAGE-style build args still flow through.
# We don't use them for local-pro (we editable-install from the sibling
# directory instead of a private PyPI), but the base compose's build args
# would fail if the file were missing.
compose_env_args := `bash -lc 'if [ -f ".envs/.local/.build" ]; then printf -- "--env-file .envs/.local/.build"; fi'`


# Start community + pro stack (detached).
#
# Stops the stack and rebuilds the venv volume when --build is passed so new
# dependencies from pro (authlib, cryptography) are picked up cleanly — mirrors
# the behaviour of ``just local-cloud up --build``.
[no-cd]
up *args:
    #!/usr/bin/env bash
    set -euo pipefail
    args="{{args}}"
    if [[ "$args" == *"--build"* ]]; then
        docker compose {{compose_env_args}} \
            -f "{{base_compose}}" \
            -f "{{overlay_compose}}" \
            down
        docker volume rm validibot_validibot_local_venv 2>/dev/null || true
    fi
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        up -d $args


# Stop the community + pro stack.
[no-cd]
down:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        down


# Rebuild and start, clearing the venv volume first.
#
# The venv lives in a named volume that persists across rebuilds; when
# pro's deps change we need to flush it so uv pip install sees the fresh
# image layer.
[no-cd]
rebuild *args:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        down
    docker volume rm validibot_validibot_local_venv 2>/dev/null || true
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        build {{args}}
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        up -d
