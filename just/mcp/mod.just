# =============================================================================
# MCP Server — GCP Cloud Run Deployment
# =============================================================================
#
# Recipes for building, deploying, and managing the Validibot MCP server
# on Google Cloud Run.
#
# The MCP server is a standalone FastMCP application with zero Django
# dependency. It proxies tool calls from AI agents (Claude Desktop,
# Cursor, etc.) to the Validibot REST API.
#
# Lives in the community repo (this repo) so self-hosted Pro operators
# can deploy the same image to their own GCP project. Every operator-
# specific value is driven through environment variables:
#
#   GCP_PROJECT_ID   — the GCP project to deploy into
#   GCP_REGION       — the region for Cloud Run, Artifact Registry, etc.
#   GCP_APP_NAME     — resource name prefix (service accounts, services)
#   VALIDIBOT_MCP_API_BASE_URL
#                    — URL of the Validibot Django API the MCP proxies to
#
# Source the config once per shell:
#
#   source .envs/.production/.google-cloud/.just
#
# The recipe names mirror the main ``just gcp`` module where it makes
# sense (build, deploy, setup, secrets, logs, status) so operators only
# have to learn one vocabulary.
#
# QUICK REFERENCE
# ===============
#
# First-time setup:
#   just mcp setup prod
#   just mcp build-push
#   just mcp deploy prod
#   just mcp lb-add prod mcp.your-domain.example
#
# Subsequent deploys:
#   just mcp build-push
#   just mcp deploy prod
#
# =============================================================================


# Every recipe runs from the repo root so relative paths (``mcp/``,
# ``.envs/...``, git commands) resolve the same way whether the user
# invoked ``just mcp <recipe>`` from the repo root or from a subdirectory.
set working-directory := '../..'


# ── Variables ────────────────────────────────────────────────────────────

# Loaded from .envs/.production/.google-cloud/.just (sourced by the user).
gcp_project := env_var_or_default("GCP_PROJECT_ID", "")
gcp_region := env_var_or_default("GCP_REGION", "us-central1")
app_name := env_var_or_default("GCP_APP_NAME", "validibot")

# MCP image path in Artifact Registry. Shares the repository with the
# main Django image but uses a distinct image name (validibot-mcp).
mcp_image := gcp_region + "-docker.pkg.dev/" + gcp_project + "/" + app_name + "/" + app_name + "-mcp"

# Git SHA for image tagging — same pattern as the main gcp module.
# Images are tagged with the short SHA so a running revision is always
# traceable back to a specific commit.
git_sha := `git rev-parse --short HEAD`

# Note: the public MCP URLs are deliberately NOT justfile variables. They are
# read at recipe runtime — typically from the stage's ``.build`` file — so
# self-hosters must supply them explicitly rather than inheriting defaults that
# happen to point at another operator's services. See
# ``_require-mcp-api-base-url`` below and the template at
# ``.envs.example/.production/.google-cloud/.build``.


# ── Environment check ────────────────────────────────────────────────────

# Fail fast when the shared public MCP URLs are missing. Used by the deploy
# recipe so self-hosted operators who forgot to set these env vars get a loud
# error instead of silently shipping an MCP container with wrong API routing,
# OAuth audience, or protected-resource metadata.
#
# The value can be supplied three ways (checked in this order):
#   1. inline on the command line
#   2. the user's shell environment
#   3. ``.envs/<stage>/.google-cloud/.build`` if present — see
#      ``.envs.example/.production/.google-cloud/.build`` for the
#      template. ``set -a`` around the source exports the variable
#      so this check and the child ``gcloud run deploy`` both see it.
[private]
_require-mcp-api-base-url:
    #!/usr/bin/env bash
    if [ -z "${VALIDIBOT_MCP_API_BASE_URL:-}" ]; then
        echo "Error: VALIDIBOT_MCP_API_BASE_URL is not set."
        echo ""
        echo "The MCP server needs the URL of the Validibot Django API"
        echo "it should forward tool calls to. There is no default"
        echo "because shipping a default could accidentally proxy your"
        echo "users' traffic to a different operator's service."
        echo ""
        echo "Set it via one of:"
        echo ""
        echo "  1. Inline:"
        echo "       VALIDIBOT_MCP_API_BASE_URL=https://app.yourco.com just gcp mcp deploy prod"
        echo ""
        echo "  2. Your shell:"
        echo "       export VALIDIBOT_MCP_API_BASE_URL=https://app.yourco.com"
        echo ""
        echo "  3. (Recommended) .envs/<stage>/.google-cloud/.build"
        echo "     (template: .envs.example/.production/.google-cloud/.build)"
        exit 1
    fi
    if [ -z "${VALIDIBOT_MCP_BASE_URL:-}" ]; then
        echo "Error: VALIDIBOT_MCP_BASE_URL is not set."
        echo ""
        echo "The MCP server needs its own public URL for OAuth resource"
        echo "metadata, redirect URI derivation, and token audience checks."
        echo "There is no hosted default."
        echo ""
        echo "Set it in .envs/<stage>/.google-cloud/.build, for example:"
        echo "  VALIDIBOT_MCP_BASE_URL=https://mcp.yourco.com"
        exit 1
    fi

# Fail fast when GCP_PROJECT_ID is missing so every recipe has the same
# error surface regardless of which one the user invoked first.
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

# Cross-target deploy toggle. Shell snippet that every MCP-touching
# recipe `eval`s at its top: if ENABLE_MCP_SERVER is not set to a truthy
# value the recipe body returns early with a helpful message, so
# higher-level "deploy everything" scripts can safely invoke any of
# these recipes on stacks that do not run MCP. The same variable gates
# the Docker Compose profile.
_mcp_enabled_gate := '''
if ! [[ "${ENABLE_MCP_SERVER:-false}" =~ ^(true|True|TRUE|1|yes|Yes|YES)$ ]]; then
    echo "MCP build/deploy skipped: ENABLE_MCP_SERVER is not set."
    echo ""
    echo "To build and deploy the MCP server, run:"
    echo ""
    echo "  ENABLE_MCP_SERVER=true just mcp <recipe>"
    echo ""
    echo "MCP is a Pro feature; the standalone FastMCP container"
    echo "is only deployed when the flag is explicitly set."
    exit 0
fi
'''


# ── Build ─────────────────────────────────────────────────────────────────
# Local builds and registry publication are separate operations. Deployment
# uses ``build-push``; ``build`` remains as a compatibility alias so existing
# operator scripts keep working.
#
# Build context is ``mcp/`` (the sibling FastMCP project); the Dockerfile
# lives at ``compose/production/mcp/Dockerfile`` so it shares a directory
# with the other production Dockerfiles (django, postgres).

build-local:
    #!/usr/bin/env bash
    set -euo pipefail
    {{_mcp_enabled_gate}}

    # ── Dirty-repo guard ────────────────────────────────────────────
    # Images are tagged with git SHA. A dirty tree would make the tag
    # misleading about what code actually got deployed.
    if [ "${GCP_ALLOW_DIRTY:-0}" != "1" ]; then
        if [ -n "$(git status --porcelain -- mcp/ compose/production/mcp/)" ]; then
            echo "Error: Uncommitted changes in mcp/ or compose/production/mcp/."
            echo ""
            echo "The MCP image will be tagged {{git_sha}} but won't match"
            echo "that commit. Commit or stash first, or set GCP_ALLOW_DIRTY=1."
            exit 1
        fi
    fi

    echo "Building local MCP image: validibot-mcp:{{git_sha}}"

    # --platform linux/amd64 matches Cloud Run's runtime even when
    # building on Apple Silicon. --provenance=false avoids multi-platform
    # manifest issues with the simpler single-arch push.
    docker build --platform linux/amd64 --provenance=false \
        -f compose/production/mcp/Dockerfile \
        -t validibot-mcp:{{git_sha}} \
        -t validibot-mcp:latest \
        mcp/

    echo "Local MCP image ready: validibot-mcp:{{git_sha}}"

# Build registry-tagged image bytes and push them to Artifact Registry.
build-push: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    {{_mcp_enabled_gate}}

    if [ "${GCP_ALLOW_DIRTY:-0}" != "1" ]; then
        if [ -n "$(git status --porcelain -- mcp/ compose/production/mcp/)" ]; then
            echo "Error: Uncommitted changes in mcp/ or compose/production/mcp/."
            echo "Commit or stash first, or set GCP_ALLOW_DIRTY=1."
            exit 1
        fi
    fi

    echo "Building MCP image for Artifact Registry: {{mcp_image}}:{{git_sha}}"
    docker build --platform linux/amd64 --provenance=false \
        -f compose/production/mcp/Dockerfile \
        -t {{mcp_image}}:{{git_sha}} \
        -t {{mcp_image}}:latest \
        mcp/

    echo "Pushing MCP image to Artifact Registry..."
    docker push {{mcp_image}}:{{git_sha}}
    docker push {{mcp_image}}:latest

    echo ""
    echo "MCP image ready: {{mcp_image}}:{{git_sha}}"

# Compatibility alias. Prefer build-local or build-push in new automation.
build: build-push


# ── Setup (one-time) ──────────────────────────────────────────────────────
# Creates the GCP resources needed before the first deployment:
#   - Dedicated service account with minimal IAM
#   - Artifact Registry repository (if not already created by ``just gcp init-stage``)
#   - Docker auth for the registry
#
# Idempotent — safe to run multiple times.

[arg('stage', pattern='dev|staging|prod')]
setup stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    {{_mcp_enabled_gate}}
    APP_NAME="{{app_name}}"

    echo "============================================="
    echo "MCP Server — Setup for: {{stage}}"
    echo "============================================="
    echo ""

    # ── Service account ─────────────────────────────────────────────
    # Intentionally minimal. The MCP service only needs:
    #   - secretmanager.secretAccessor for mcp-env
    #   - run.invoker on the Django service for OIDC identity tokens
    # No database, no GCS, no KMS. Compromising the container yields
    # only the OAuth client secret — no lateral movement.
    if [ "{{stage}}" = "prod" ]; then
        SA_NAME="${APP_NAME}-mcp-prod"
    else
        SA_NAME="${APP_NAME}-mcp-{{stage}}"
    fi
    SA_EMAIL="$SA_NAME@{{gcp_project}}.iam.gserviceaccount.com"

    echo "1. Creating service account: $SA_NAME"
    if gcloud iam service-accounts describe "$SA_EMAIL" \
        --project={{gcp_project}} &>/dev/null; then
        echo "   Already exists"
    else
        gcloud iam service-accounts create "$SA_NAME" \
            --display-name="Validibot {{stage}} MCP Server" \
            --project={{gcp_project}}
        echo "   Created"
    fi
    echo ""

    if [ "{{stage}}" = "prod" ]; then
        SECRET_NAME="mcp-env"
    else
        SECRET_NAME="mcp-env-{{stage}}"
    fi
    if gcloud secrets describe "$SECRET_NAME" \
        --project={{gcp_project}} &>/dev/null; then
        echo "   Granting roles/secretmanager.secretAccessor on $SECRET_NAME..."
        gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
            --member="serviceAccount:$SA_EMAIL" \
            --role="roles/secretmanager.secretAccessor" \
            --project={{gcp_project}} \
            --quiet >/dev/null
    else
        echo "   Secret $SECRET_NAME does not exist yet; 'mcp secrets' will bind it."
    fi

    # Older setup versions granted secretAccessor at project scope. Remove
    # that known unconditional binding only after the exact-secret binding is
    # present (or let ``mcp secrets`` do both when creating a new secret).
    PROJECT_POLICY=$(gcloud projects get-iam-policy {{gcp_project}} --format=json)
    if jq -e --arg member "serviceAccount:$SA_EMAIL" '
        .bindings[]?
        | select(.role == "roles/secretmanager.secretAccessor")
        | select((.condition // null) == null)
        | .members[]?
        | select(. == $member)
    ' <<< "$PROJECT_POLICY" >/dev/null; then
        if gcloud secrets describe "$SECRET_NAME" \
            --project={{gcp_project}} &>/dev/null; then
            echo "   Removing legacy project-wide secretAccessor binding..."
            gcloud projects remove-iam-policy-binding {{gcp_project}} \
                --member="serviceAccount:$SA_EMAIL" \
                --role="roles/secretmanager.secretAccessor" \
                --condition=None \
                --quiet >/dev/null
        else
            echo "   Keeping legacy binding until $SECRET_NAME can be created and bound."
        fi
    fi

    # run.invoker on the Django web service lets the MCP server fetch
    # Cloud Run OIDC identity tokens that Django accepts for
    # service-to-service authentication on /api/v1/mcp/*.
    if [ "{{stage}}" = "prod" ]; then
        DJANGO_SERVICE="${APP_NAME}-web"
    else
        DJANGO_SERVICE="${APP_NAME}-web-{{stage}}"
    fi
    echo "   Granting roles/run.invoker on $DJANGO_SERVICE..."
    gcloud run services add-iam-policy-binding "$DJANGO_SERVICE" \
        --member="serviceAccount:$SA_EMAIL" \
        --role="roles/run.invoker" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --quiet &>/dev/null || true

    echo "   IAM roles: secretmanager.secretAccessor, run.invoker ($DJANGO_SERVICE)"
    echo ""

    # ── Artifact Registry ───────────────────────────────────────────
    # ``just gcp init-stage`` normally creates this; double-check so
    # the MCP service can also be set up stand-alone if needed.
    REPO_NAME="${APP_NAME}"

    echo "2. Checking Artifact Registry repository: $REPO_NAME"
    if gcloud artifacts repositories describe "$REPO_NAME" \
        --location={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null; then
        echo "   Already exists"
    else
        gcloud artifacts repositories create "$REPO_NAME" \
            --repository-format=docker \
            --location={{gcp_region}} \
            --project={{gcp_project}}
        echo "   Created"
    fi
    echo ""

    echo "3. Configuring Docker authentication for Artifact Registry"
    gcloud auth configure-docker {{gcp_region}}-docker.pkg.dev --quiet
    echo "   Done"
    echo ""

    echo "============================================="
    echo "Setup complete for {{stage}}"
    echo "============================================="
    echo ""
    echo "Next steps:"
    echo "  1. Create .envs/.production/.google-cloud/.mcp with secrets"
    echo "  2. just mcp secrets {{stage}}"
    echo "  3. just mcp build-push"
    echo "  4. just mcp deploy {{stage}}"


# ── Secrets ──────────────────────────────────────────────────────────────
# Upload MCP server environment secrets to Secret Manager.
#
# This recipe exists as a **surgical alternative** to ``just gcp secrets``,
# which uploads both ``.django`` and ``.mcp`` in one step. Reach for
# ``just mcp secrets`` when you specifically want to push only the MCP
# env file — typically because you're rotating the OAuth client secret
# or an x402 credential without changing anything Django-side. Using
# ``just gcp secrets`` in that case would generate a new, noise-only
# version of django-env in Secret Manager even though the content
# hadn't changed, which muddles audit history.
#
# For the common case of "upload every secret that might have changed
# before a deploy," use ``just gcp secrets <stage>`` instead — it
# delegates here internally when ENABLE_MCP_SERVER is set.
#
# The MCP env file (.envs/<stage>/.google-cloud/.mcp) holds the
# confidential OAuth client secret and other per-deployment values. Non-
# secret config (API base URL, MCP enabled flag) is set directly on the
# Cloud Run service in the deploy recipe because it's not sensitive and
# changing it without a new revision wouldn't help anyway.
#
# Secret naming:
#   prod    → mcp-env
#   dev     → mcp-env-dev
#   staging → mcp-env-staging
#
# After a successful upload, retention keeps the new version, one rollback
# version, and any version still referenced by a current Cloud Run service or
# job. Gated by ENABLE_MCP_SERVER so community-only operators who run
# ``just gcp secrets`` on an MCP-disabled stack get a silent skip
# rather than an error about a missing ``.mcp`` file.

[arg('stage', pattern='dev|staging|prod')]
secrets stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    {{_mcp_enabled_gate}}

    if [ "{{stage}}" = "prod" ]; then
        SECRET_NAME="mcp-env"
        ENV_FILE=".envs/.production/.google-cloud/.mcp"
    else
        SECRET_NAME="mcp-env-{{stage}}"
        ENV_FILE=".envs/.{{stage}}/.google-cloud/.mcp"
    fi

    if [ ! -f "$ENV_FILE" ]; then
        echo "Error: $ENV_FILE not found"
        echo ""
        echo "Create the MCP env file with:"
        echo "  mkdir -p $(dirname "$ENV_FILE")"
        echo "  cat > $ENV_FILE << 'EOF'"
        echo "VALIDIBOT_OAUTH_CLIENT_SECRET=<your-generated-secret>"
        echo "VALIDIBOT_LOG_LEVEL=INFO"
        echo "EOF"
        exit 1
    fi

    if ! gcloud secrets describe "$SECRET_NAME" --project={{gcp_project}} &>/dev/null; then
        echo "Creating new secret: $SECRET_NAME"
        gcloud secrets create "$SECRET_NAME" \
            --replication-policy=user-managed \
            --locations={{gcp_region}} \
            --project={{gcp_project}}
    fi

    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        SA_NAME="${APP_NAME}-mcp-prod"
    else
        SA_NAME="${APP_NAME}-mcp-{{stage}}"
    fi
    SA_EMAIL="$SA_NAME@{{gcp_project}}.iam.gserviceaccount.com"
    echo "Restricting MCP secret access to $SECRET_NAME..."
    gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
        --member="serviceAccount:$SA_EMAIL" \
        --role="roles/secretmanager.secretAccessor" \
        --project={{gcp_project}} \
        --quiet >/dev/null

    PROJECT_POLICY=$(gcloud projects get-iam-policy {{gcp_project}} --format=json)
    if jq -e --arg member "serviceAccount:$SA_EMAIL" '
        .bindings[]?
        | select(.role == "roles/secretmanager.secretAccessor")
        | select((.condition // null) == null)
        | .members[]?
        | select(. == $member)
    ' <<< "$PROJECT_POLICY" >/dev/null; then
        gcloud projects remove-iam-policy-binding {{gcp_project}} \
            --member="serviceAccount:$SA_EMAIL" \
            --role="roles/secretmanager.secretAccessor" \
            --condition=None \
            --quiet >/dev/null
    fi

    command -v jq >/dev/null 2>&1 || {
        echo "Error: jq is required to validate the uploaded secret version."
        exit 1
    }

    echo "Uploading MCP secrets from $ENV_FILE to $SECRET_NAME..."
    NEW_VERSION_JSON="$(
        gcloud secrets versions add "$SECRET_NAME" \
            --data-file="$ENV_FILE" \
            --project={{gcp_project}} \
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

    echo "Secret $SECRET_NAME updated; bounded version history applied."


# ── Deploy ────────────────────────────────────────────────────────────────
# Deploys the MCP server to Cloud Run.
#
# Key differences from the main Django deployment:
#   - No --add-cloudsql-instances (no database)
#   - No VPC egress (no private network access needed)
#   - Secrets mounted from mcp-env Secret Manager secret
#   - Public config (API URL and MCP enabled flag) via --set-env-vars
#   - Lower resource limits (lightweight proxy, not a full Django app)
#
# Ingress mode is chosen automatically: if an NEG exists for the service
# (i.e. ``just mcp lb-add`` has already been run), ingress is locked to
# internal-and-cloud-load-balancing; otherwise direct *.run.app access
# is allowed for bootstrap.

[arg('stage', pattern='dev|staging|prod')]
deploy stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    {{_mcp_enabled_gate}}
    APP_NAME="{{app_name}}"

    # Source the stage's .build file (if present) to pick up deploy-time
    # knobs like VALIDIBOT_MCP_API_BASE_URL, VALIDIBOT_MCP_BASE_URL, and the
    # MCP enabled flag.
    # ``set -a`` exports every variable the file defines so the
    # ``gcloud run deploy`` invocation below inherits them. Absent file →
    # no-op (inline env vars still work).
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

    # Fail loudly if the shared public URLs still aren't set.
    just gcp mcp _require-mcp-api-base-url

    # Re-read variables now that .build has been sourced. The MCP server no
    # longer handles x402 — anonymous agent payments moved to validibot-cloud's
    # Django, which reads ALL x402 config from its own .django secret. So no
    # x402 values are stamped onto the MCP service.
    API_BASE_URL="${VALIDIBOT_MCP_API_BASE_URL}"
    MCP_BASE_URL="${VALIDIBOT_MCP_BASE_URL}"
    MCP_ENABLED="${VALIDIBOT_MCP_ENABLED:-true}"
    if [ "${GCP_DEPLOY_MAINTENANCE:-0}" = "1" ]; then
        # A maintenance revision must become ready while the Django issuer/API
        # is intentionally internal and Cloud SQL is about to stop. Disabling
        # MCP skips its startup license request and makes every tool return 503;
        # mode-live restores the configured value after web is online.
        MCP_ENABLED=false
    fi
    SET_ENV_VARS="VALIDIBOT_API_BASE_URL=${API_BASE_URL},VALIDIBOT_MCP_BASE_URL=${MCP_BASE_URL},VALIDIBOT_MCP_ENABLED=${MCP_ENABLED}"

    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-mcp"
        SA="${APP_NAME}-mcp-prod@{{gcp_project}}.iam.gserviceaccount.com"
        SECRET="mcp-env"
        MIN_INSTANCES=0
        MAX_INSTANCES=4
    else
        SERVICE="${APP_NAME}-mcp-{{stage}}"
        SA="${APP_NAME}-mcp-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        SECRET="mcp-env-{{stage}}"
        MIN_INSTANCES=0
        MAX_INSTANCES=2
    fi

    if [ "{{stage}}" = "prod" ]; then
        NEG_NAME="${APP_NAME}-mcp-neg"
    else
        NEG_NAME="${APP_NAME}-mcp-neg-{{stage}}"
    fi

    if gcloud compute network-endpoint-groups describe "$NEG_NAME" \
        --region={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null 2>&1; then
        INGRESS="internal-and-cloud-load-balancing"
    else
        INGRESS="all"
    fi
    if [ "${GCP_DEPLOY_MAINTENANCE:-0}" = "1" ]; then
        INGRESS="internal"
        MIN_INSTANCES=0
        echo "   Maintenance-safe deploy: forcing ingress=internal and min-instances=0"
    fi

    echo "Deploying $SERVICE to Cloud Run ({{stage}}, ingress: $INGRESS)..."
    gcloud run deploy "$SERVICE" \
        --image {{mcp_image}}:{{git_sha}} \
        --region {{gcp_region}} \
        --port 8080 \
        --service-account "$SA" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --set-env-vars "$SET_ENV_VARS" \
        --min-instances $MIN_INSTANCES \
        --max-instances $MAX_INSTANCES \
        --memory 512Mi \
        --cpu 1 \
        --timeout 300s \
        --execution-environment gen2 \
        --allow-unauthenticated \
        --ingress "$INGRESS" \
        --project {{gcp_project}}

    echo ""
    echo "MCP service deployed to {{stage}} (ingress: $INGRESS)"

    URL=$(gcloud run services describe "$SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null || echo "unknown")
    echo "URL: $URL"


# ── Load Balancer Integration ─────────────────────────────────────────────
# Adds the MCP Cloud Run service to the EXISTING load balancer created
# by ``just gcp lb-setup``. Requires:
#   - The main load balancer must already exist
#   - The MCP Cloud Run service must be deployed
#   - DNS A record for the MCP domain pointing to the LB's static IP
#
# Operates in five steps so each section is inspectable and replayable:
#   1. Serverless NEG pointing to the MCP Cloud Run service
#   2. Backend service wrapping that NEG
#   3. SSL certificate expanded to cover the MCP domain
#   4. Host rule added to the URL map
#   5. Ingress locked to load-balancer-only on the Cloud Run service

[arg('stage', pattern='dev|staging|prod')]
lb-add stage domain: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    echo "============================================="
    echo "Adding MCP to Load Balancer: {{stage}}"
    echo "Domain: {{domain}}"
    echo "============================================="
    echo ""

    if [ "{{stage}}" = "prod" ]; then
        MCP_SERVICE="${APP_NAME}-mcp"
        URL_MAP_NAME="${APP_NAME}-url-map"
        CERT_NAME="${APP_NAME}-cert"
        PROXY_NAME="${APP_NAME}-https-proxy"
        SUFFIX=""
    else
        MCP_SERVICE="${APP_NAME}-mcp-{{stage}}"
        URL_MAP_NAME="${APP_NAME}-url-map-{{stage}}"
        CERT_NAME="${APP_NAME}-cert-{{stage}}"
        PROXY_NAME="${APP_NAME}-https-proxy-{{stage}}"
        SUFFIX="-{{stage}}"
    fi

    MCP_NEG_NAME="${APP_NAME}-mcp-neg${SUFFIX}"
    MCP_BACKEND_NAME="${APP_NAME}-mcp-backend${SUFFIX}"

    echo "0. Checking prerequisites..."

    if ! gcloud run services describe "$MCP_SERVICE" \
        --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        echo "   Error: MCP Cloud Run service '$MCP_SERVICE' not found."
        echo "   Deploy it first: just mcp deploy {{stage}}"
        exit 1
    fi
    echo "   MCP service: found"

    if ! gcloud compute url-maps describe "$URL_MAP_NAME" \
        --project={{gcp_project}} &>/dev/null; then
        echo "   Error: URL map '$URL_MAP_NAME' not found."
        echo "   Set up the main load balancer first: just gcp lb-setup"
        exit 1
    fi
    echo "   URL map: found"
    echo ""

    echo "1. Checking MCP Network Endpoint Group ($MCP_NEG_NAME)..."
    if gcloud compute network-endpoint-groups describe "$MCP_NEG_NAME" \
        --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        echo "   Already exists"
    else
        gcloud compute network-endpoint-groups create "$MCP_NEG_NAME" \
            --region={{gcp_region}} \
            --network-endpoint-type=serverless \
            --cloud-run-service="$MCP_SERVICE" \
            --project={{gcp_project}}
        echo "   Created (pointing to $MCP_SERVICE)"
    fi
    echo ""

    echo "2. Checking MCP Backend Service ($MCP_BACKEND_NAME)..."
    if ! gcloud compute backend-services describe "$MCP_BACKEND_NAME" \
        --global --project={{gcp_project}} &>/dev/null; then
        gcloud compute backend-services create "$MCP_BACKEND_NAME" \
            --global \
            --protocol=HTTP \
            --project={{gcp_project}}
        echo "   Created"
    else
        echo "   Already exists"
    fi

    if gcloud compute backend-services describe "$MCP_BACKEND_NAME" \
        --global --project={{gcp_project}} \
        --format="value(backends[].group)" 2>/dev/null | grep -q "/$MCP_NEG_NAME$"; then
        echo "   NEG already attached"
    else
        gcloud compute backend-services add-backend "$MCP_BACKEND_NAME" \
            --global \
            --network-endpoint-group="$MCP_NEG_NAME" \
            --network-endpoint-group-region={{gcp_region}} \
            --project={{gcp_project}}
        echo "   Attached NEG to backend"
    fi
    echo ""

    # Google-managed certificates are immutable — can't add domains to
    # an existing cert. Instead we create a new certificate covering
    # ALL domains currently served by the proxy plus the new MCP domain,
    # then swap the proxy over. The proxy may already have MULTIPLE
    # certificates attached, so we collect domains from every one to
    # avoid accidentally dropping coverage for existing hostnames.
    echo "3. Updating SSL certificate..."

    EXISTING_CERTS=$(gcloud compute target-https-proxies describe "$PROXY_NAME" \
        --project={{gcp_project}} \
        --format="value(sslCertificates)" 2>/dev/null || echo "")

    ALL_EXISTING_DOMAINS=""
    EXISTING_CERT_NAMES=""
    for CERT_URL in $EXISTING_CERTS; do
        CERT_BASENAME=$(basename "$CERT_URL")
        EXISTING_CERT_NAMES="$EXISTING_CERT_NAMES $CERT_BASENAME"
        CERT_DOMAINS=$(gcloud compute ssl-certificates describe "$CERT_BASENAME" \
            --global --project={{gcp_project}} \
            --format="value(managed.domains)" 2>/dev/null || echo "")
        if [ -n "$CERT_DOMAINS" ]; then
            if [ -n "$ALL_EXISTING_DOMAINS" ]; then
                ALL_EXISTING_DOMAINS="$ALL_EXISTING_DOMAINS;$CERT_DOMAINS"
            else
                ALL_EXISTING_DOMAINS="$CERT_DOMAINS"
            fi
        fi
    done

    UNIQUE_DOMAINS=$(echo "$ALL_EXISTING_DOMAINS" | tr ';' '\n' | sort -u | tr '\n' ',' | sed 's/,$//')

    if echo "$UNIQUE_DOMAINS" | grep -q "{{domain}}"; then
        echo "   {{domain}} already covered by existing certificate(s)"
    else
        if [ -n "$UNIQUE_DOMAINS" ]; then
            ALL_DOMAINS_CSV="${UNIQUE_DOMAINS},{{domain}}"
        else
            ALL_DOMAINS_CSV="{{domain}}"
        fi

        NEW_CERT_NAME="${CERT_NAME}-$(date +%Y%m%d%H%M%S)"
        echo "   Creating new certificate: $NEW_CERT_NAME"
        echo "   Domains: $ALL_DOMAINS_CSV"

        gcloud compute ssl-certificates create "$NEW_CERT_NAME" \
            --domains="$ALL_DOMAINS_CSV" \
            --global \
            --project={{gcp_project}}

        echo "   Updating HTTPS proxy to use new certificate..."
        gcloud compute target-https-proxies update "$PROXY_NAME" \
            --ssl-certificates="$NEW_CERT_NAME" \
            --project={{gcp_project}}

        echo "   Certificate updated (covers all domains in a single cert)."
        echo "   Previous certificates ($EXISTING_CERT_NAMES ) can be deleted"
        echo "   after the new one is ACTIVE (check with:"
        echo "     gcloud compute ssl-certificates describe $NEW_CERT_NAME --global)"
    fi
    echo ""

    echo "4. Adding host rule to URL map..."

    if gcloud compute url-maps describe "$URL_MAP_NAME" \
        --project={{gcp_project}} \
        --format="yaml(hostRules)" 2>/dev/null | grep -q "{{domain}}"; then
        echo "   Host rule for {{domain}} already exists"
    else
        gcloud compute url-maps add-path-matcher "$URL_MAP_NAME" \
            --path-matcher-name="mcp-matcher${SUFFIX}" \
            --default-service="$MCP_BACKEND_NAME" \
            --new-hosts="{{domain}}" \
            --project={{gcp_project}}
        echo "   Added: {{domain}} → $MCP_BACKEND_NAME"
    fi
    echo ""

    echo "5. Restricting MCP service ingress to load balancer only..."
    gcloud run services update "$MCP_SERVICE" \
        --ingress internal-and-cloud-load-balancing \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --quiet
    echo "   Done (direct *.run.app access blocked)"
    echo ""

    if [ "{{stage}}" = "prod" ]; then
        IP_NAME="${APP_NAME}-ip"
    else
        IP_NAME="${APP_NAME}-ip${SUFFIX}"
    fi
    IP_ADDRESS=$(gcloud compute addresses describe "$IP_NAME" \
        --global --project={{gcp_project}} \
        --format="value(address)" 2>/dev/null || echo "unknown")

    echo "============================================="
    echo "Load Balancer Integration Complete"
    echo "============================================="
    echo ""
    echo "ACTION REQUIRED: Add DNS record"
    echo ""
    echo "  Type:  A"
    echo "  Name:  {{domain}}"
    echo "  Value: $IP_ADDRESS"
    echo "  TTL:   1 hour"
    echo ""
    echo "The SSL certificate may take 15-60 minutes to provision"
    echo "after DNS propagates."


# ── Logs ──────────────────────────────────────────────────────────────────

[arg('stage', pattern='dev|staging|prod')]
logs stage: _require-gcp-config
    #!/usr/bin/env bash
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-mcp"
    else
        SERVICE="${APP_NAME}-mcp-{{stage}}"
    fi

    gcloud run services logs read "$SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --limit=50


# ── Status ────────────────────────────────────────────────────────────────

[arg('stage', pattern='dev|staging|prod')]
status stage: _require-gcp-config
    #!/usr/bin/env bash
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-mcp"
    else
        SERVICE="${APP_NAME}-mcp-{{stage}}"
    fi

    echo "MCP Service: $SERVICE ({{stage}})"
    echo "============================================="
    gcloud run services describe "$SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="table(
            status.url:label=URL,
            status.conditions[0].status:label=READY,
            spec.template.spec.containers[0].image:label=IMAGE,
            spec.template.metadata.annotations.'autoscaling.knative.dev/minScale':label=MIN,
            spec.template.metadata.annotations.'autoscaling.knative.dev/maxScale':label=MAX,
            spec.template.spec.containers[0].resources.limits.memory:label=MEMORY,
            spec.template.spec.serviceAccountName:label=SERVICE_ACCOUNT
        )" 2>/dev/null || echo "Service not found. Run: just mcp deploy {{stage}}"


# ── Testing ──────────────────────────────────────────────────────────────
# Unit/integration tests run locally with mocked APIs. E2E tests hit the
# live deployed service with a real API token.

lock-check:
    cd mcp && uv lock --check

lint:
    cd mcp && uv run --frozen --extra dev ruff check src/ tests/

format-check:
    cd mcp && uv run --frozen --extra dev ruff format --check src/ tests/

format:
    cd mcp && uv run --frozen --extra dev ruff format src/ tests/
    cd mcp && uv run --frozen --extra dev ruff check --fix src/ tests/

test:
    #!/usr/bin/env bash
    set -euo pipefail
    cd mcp
    uv run --frozen --extra dev pytest -xvs tests/

check: lock-check format-check lint test

# Run E2E tests against the live MCP server.
# Requires .envs/.local/.test with VALIDIBOT_E2E_TOKEN and VALIDIBOT_E2E_ORG_SLUG.
test-e2e:
    #!/usr/bin/env bash
    set -euo pipefail

    if [ ! -f .envs/.local/.test ]; then
        echo "ERROR: .envs/.local/.test not found"
        echo ""
        echo "Create it with:"
        echo "  VALIDIBOT_E2E_TOKEN=your-api-key"
        echo "  VALIDIBOT_E2E_ORG_SLUG=your-org-slug"
        exit 1
    fi

    set -a
    source .envs/.local/.test
    set +a

    cd mcp
    uv run --frozen --extra dev pytest tests/test_e2e.py -xvs
