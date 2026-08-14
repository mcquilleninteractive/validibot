# Validibot GCP Deployment Configuration (for Just commands)
# ==============================================================================
# Copy this file to .envs/.production/.google-cloud/.just
# Source it before running just commands: source .envs/.production/.google-cloud/.just
#
# WHERE THIS FILE SITS AMONG THE .envs/.../google-cloud/ FILES
# ------------------------------------------------------------
# Three files configure a GCP deployment. They differ by WHO reads them and
# WHEN, and that is what decides where any given variable belongs:
#
#   .just    You `source` it into your shell before running `just`. It is the
#            host-side context for every `just gcp` recipe. It contains
#            non-secret deployment coordinates and is never uploaded.
#   .django  Uploaded as the stage's `django-env` Secret Manager secret and
#            consumed by Django web, worker, scheduler, migration, and other
#            application jobs. All runtime configuration belongs there,
#            including embedded MCP OAuth, x402, billing, and audit settings.
#   .build   Optional host-side deploy knobs read only by recipes that
#            explicitly document them. It is not a runtime env file and must
#            not contain application secrets.
#
# Rule of thumb:
#   - host-side GCP command context -> here (.just)
#   - Django/application runtime   -> .django
#   - explicitly documented local deploy knob -> .build
#
# (Mechanically: `just` recipes run on the host, not in containers; these are
# deploy-time, not runtime, settings. Only .django is uploaded to Secret
# Manager for runtime use, so host-sourced values like these stay out of it.)

# GCP Project Configuration
# ==============================================================================
# Your GCP project ID (find it in the GCP Console)
export GCP_PROJECT_ID="your-gcp-project-id"

# Region for Cloud Run, Cloud SQL, etc. (deploy-time, used by `just gcp`).
# Common options: us-central1, europe-west1, australia-southeast1.
# The runtime app also needs its cloud-resource region in .django today because
# many management/job paths mount only the Django secret file. Use the same
# deployment region for both host commands and runtime cloud resources.
export GCP_REGION="your-gcp-region"

# Application name prefix for all GCP resources
# All GCP resource names (Cloud Run services, Cloud SQL instances, buckets,
# service accounts, etc.) are derived from this prefix. For example, with
# GCP_APP_NAME="myapp", resources will be named myapp-web, myapp-db, etc.
# Default: "validibot"
export GCP_APP_NAME="your-gcp-app-name"

# Timezone for Cloud Scheduler cron jobs
# See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
export GCP_SCHEDULER_TIMEZONE="UTC"

# Cloud Run request timeout (for long-running validator jobs)
# Format: seconds with 's' suffix, max 3600s (1 hour)
export GCP_CLOUD_RUN_TIMEOUT="3600s"

# Commercial Layers (optional)
# ==============================================================================
# Set to "1" to bundle the commercial pro + cloud packages into Docker
# builds. Requires the corresponding sibling directories to exist and is
# only meaningful if you have a license for them. When unset or "0",
# `just gcp build` produces a community-only image.
# export GCP_BUILD_VALIDIBOT_CLOUD="1"
