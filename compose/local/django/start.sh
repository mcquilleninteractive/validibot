#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

# Only the main web container runs migrations
# Other containers (worker, scheduler) skip this to avoid race conditions
python manage.py check_migration_history
python manage.py migrate --noinput

# Run the complete first-install sequence only when its versioned completion
# marker is absent. A partial prior attempt remains retryable because the
# initializer writes its marker only after every data concern succeeds.
python manage.py initialize_validibot --if-needed

# Sync system validators on every local startup to keep the development
# catalogue current. ``--allow-drift`` is the LOCAL stance: developers
# naturally edit semantic config while iterating, whereas production keeps
# strict drift detection.
python manage.py sync_validators --allow-drift

# Keep code-backed local data current on every startup. These repeat once on a
# fresh database after the complete initializer, which is harmless and keeps
# the normal development refresh path simple and explicit.
echo "Syncing local help pages..."
python manage.py sync_help

echo "Ensuring local EnergyPlus weather resources..."
python manage.py seed_weather_files

# Exercise the same composed ASGI application used in production.
exec uvicorn config.asgi:application \
  --host 0.0.0.0 \
  --port 8000 \
  --reload \
  --reload-include '*.html'
