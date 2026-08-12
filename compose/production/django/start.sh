#!/bin/bash
# Exit on any command failure.
set -o errexit
# Treat failures in pipelines as errors.
set -o pipefail
# Error on unset variables to catch misconfigurations early.
set -o nounset

# Note: Environment variables are loaded by the entrypoint script (/entrypoint)

# ── Database migrations are NOT run here ────────────────────────────
# Migrations run as a dedicated Cloud Run Job BEFORE new instances
# start receiving traffic (see `just gcp deploy`). This prevents:
#   - Multiple instances racing to apply the same migration
#   - Wasted DB connections from concurrent SELECT ... FOR UPDATE locks
#   - Unclear error attribution (migration failure vs app failure)
#
# For Docker Compose deployments, migrations run via `just local migrate`
# before starting the stack.
#
# If you need to run migrations manually:
#   GCP:   just gcp migrate <stage>
#   Local: just local migrate
# ────────────────────────────────────────────────────────────────────

# Collect static assets for serving.
python manage.py collectstatic --noinput

# Setup tasks only run on the web service.
#
# Docker Compose bootstraps the schema via `just docker-compose bootstrap`
# before asking the application to seed default data. GCP applies migrations
# as a dedicated job before new instances receive traffic.
#
# This script therefore only runs setup/sync when the relevant tables already
# exist. On a brand-new database with no migrations, it emits a clear message
# and lets the container keep serving so operators can run the bootstrap
# commands instead of crashing the web process on startup.
if [ "${APP_ROLE:-web}" = "web" ]; then
  if python manage.py shell -c "from django.db import connection; from validibot.users.models import Role; exit(0 if Role._meta.db_table in connection.introspection.table_names() else 1)" >/dev/null 2>&1; then
    # Managed deployment paths initialize before service cutover. This guarded
    # call is a safe fallback for production runtimes that start the web service
    # directly after applying migrations, and retries partial initialization.
    python manage.py initialize_validibot --if-needed

    # Strictly verify system validator configuration on every production start.
    python manage.py sync_validators
  else
    echo "Database schema not ready yet; skipping initialization and validator sync."
    echo "Run 'just docker-compose migrate' and 'just docker-compose setup-data' after the stack starts."
  fi
fi

# Gunicorn configuration
# WEB_CONCURRENCY: Number of worker processes (default: 4)
# GUNICORN_TIMEOUT_SECONDS: Worker timeout in seconds (default: 3600 for long validations)
WEB_CONCURRENCY="${WEB_CONCURRENCY:-4}"
GUNICORN_TIMEOUT_SECONDS="${GUNICORN_TIMEOUT_SECONDS:-3600}"

# Serve the composed Django + MCP ASGI application. Gunicorn remains the
# process manager while Uvicorn workers provide the ASGI protocol server.
exec gunicorn config.asgi:application \
  --bind 0.0.0.0:8000 \
  --worker-class uvicorn_worker.UvicornWorker \
  --workers "${WEB_CONCURRENCY}" \
  --timeout "${GUNICORN_TIMEOUT_SECONDS}"
