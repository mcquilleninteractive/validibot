# =============================================================================
# Django — GCP-specific secrets + ops
# =============================================================================
#
# Submodule for operations that target *only* the Django web/worker
# services on GCP. Invoked as ``just gcp django <recipe> <stage>``.
#
# Both ``just gcp secrets prod`` and ``just gcp django secrets prod`` reach
# this single uploader. The Django secret now configures every application
# surface, including the embedded MCP endpoint.
# =============================================================================


# Recipes run from the repo root so relative paths (.envs/...) resolve
# the same whether the user invoked from root or a subdirectory.
set working-directory := '../../..'


# ── Variables ────────────────────────────────────────────────────────────

# Loaded from .envs/<stage>/.google-cloud/.just (sourced by the user).
gcp_project := env_var_or_default("GCP_PROJECT_ID", "")
gcp_region := env_var_or_default("GCP_REGION", "us-central1")


# ── Environment check ────────────────────────────────────────────────────
# Duplicated from just/gcp/mod.just because just modules have isolated
# scope and can't reference recipes from parent or sibling modules
# (see memory:Justfile Best Practices). Keeping the check identical
# is more important than deduplicating — the message operators see
# should be the same regardless of which recipe they invoked.

[private]
_require-gcp-config:
    #!/usr/bin/env bash
    if [ -z "${GCP_PROJECT_ID:-}" ]; then
        echo "Error: GCP_PROJECT_ID is not set."
        echo ""
        echo "Source the GCP config first:"
        echo ""
        echo "  source .envs/.production/.google-cloud/.just"
        exit 1
    fi


# ── Secrets ──────────────────────────────────────────────────────────────
# Upload the Django env file to Secret Manager (only Django — the
# umbrella ``just gcp secrets`` covers both Django and MCP).
#
# Secret naming:
#   prod    → django-env
#   dev     → django-env-dev
#   staging → django-env-staging
#
# Use this when you're rotating a Django-only value and want Secret
# Manager's version history to reflect a Django-only change. After upload,
# retention keeps the new version, one rollback version, and any version still
# referenced by a current Cloud Run service or job.

[arg('stage', pattern='dev|staging|prod')]
secrets stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail

    if [ "{{stage}}" = "prod" ]; then
        SECRET_NAME="django-env"
        ENV_FILE=".envs/.production/.google-cloud/.django"
    else
        SECRET_NAME="django-env-{{stage}}"
        ENV_FILE=".envs/.{{stage}}/.google-cloud/.django"
    fi

    # Each stage must have its own env file. Never copy from another
    # stage — a prod secret in the dev env file can corrupt dev data
    # or worse, leak prod credentials into dev process env.
    if [ ! -f "$ENV_FILE" ]; then
        echo "Error: $ENV_FILE not found"
        echo ""
        echo "Create the environment file manually:"
        echo "  mkdir -p $(dirname "$ENV_FILE")"
        echo "  # Create $ENV_FILE with stage-specific values"
        echo ""
        echo "See .envs.example/ for the required variables."
        echo "IMPORTANT: Do NOT copy from another stage's secrets."
        exit 1
    fi

    if ! gcloud secrets describe "$SECRET_NAME" --project={{gcp_project}} &>/dev/null; then
        echo "Creating new secret: $SECRET_NAME"
        gcloud secrets create "$SECRET_NAME" \
            --replication-policy="user-managed" \
            --locations="{{gcp_region}}" \
            --project={{gcp_project}}
    fi

    command -v jq >/dev/null 2>&1 || {
        echo "Error: jq is required to validate the uploaded secret version."
        exit 1
    }

    echo "Uploading secrets from $ENV_FILE to $SECRET_NAME..."
    NEW_VERSION_JSON="$(
        gcloud secrets versions add "$SECRET_NAME" \
            --data-file="$ENV_FILE" \
            --project {{gcp_project}} \
            --format=json
    )"
    NEW_VERSION="$(
        jq -r '.name // "" | split("/") | last' <<< "$NEW_VERSION_JSON"
    )"
    if [[ ! "$NEW_VERSION" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: upload succeeded, but its version could not be verified."
        echo "No old secret versions were changed."
        exit 1
    fi

    echo "Secret $SECRET_NAME version $NEW_VERSION established."
    bash ops/gcp/prune-secret-versions.sh \
        --project={{gcp_project}} \
        --secret="$SECRET_NAME" \
        --new-version="$NEW_VERSION" \
        --keep="${GCP_SECRET_VERSIONS_TO_KEEP:-2}" \
        --mode=apply

    echo ""
    echo "Secret $SECRET_NAME updated; bounded version history applied."
    echo "Run 'just gcp deploy-all {{stage}}' to apply changes."
