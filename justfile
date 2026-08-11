# =============================================================================
# Validibot Justfile
# =============================================================================
#
# Just is a modern command runner (like Make, but better).
# Install: brew install just
# Docs: https://just.systems/man/en/
#
# ARCHITECTURE
# ============
#
# This project uses a modular justfile structure to support multiple deployment
# platforms. Commands are organized as follows:
#
#   justfile              <- You are here (root orchestrator)
#   just/
#   ├── common.just       <- Shared variables and helpers
#   ├── local.just        <- Local Docker development
#   ├── gcp/
#   │   └── mod.just      <- Google Cloud Platform deployment
#   ├── aws/
#   │   └── mod.just      <- AWS deployment (stub)
#   └── self-hosted/
#       └── mod.just      <- Self-hosted deployment (Docker Compose on a VM)
#
# USAGE
# =====
#
# Local Docker development (commands namespaced by flavour):
#   just local up              # Community-only stack
#   just local-pro up          # Community + validibot-pro
#   just local-cloud up        # Community + validibot-pro + validibot-cloud
#   just local down            # Stop containers (same pattern for each flavour)
#   just local logs            # View logs
#   just local test            # Run tests
#
# Platform-specific deployment (prefixed with platform name):
#   just gcp deploy prod          # Deploy to GCP production
#   just gcp logs dev             # View GCP dev logs
#   just aws deploy prod          # Deploy to AWS (not yet implemented)
#   just self-hosted deploy       # Deploy a self-hosted instance (single VM)
#
# Platform modules use namespaced commands to avoid conflicts and make it
# clear which platform you're operating on.
#
# TIPS
# ====
#   - Tab completion: Add to ~/.zshrc: eval "$(just --completions zsh)"
#   - Run from subdirectory: just will find this justfile automatically
#   - See what a command does: just --show <command>
#   - Dry run: just --dry-run <command>
#   - List all commands: just --list
#   - List module commands: just -f just/gcp/mod.just --list
#
# ADDING A NEW PLATFORM
# =====================
#
# To add support for a new cloud platform:
#
#   1. Create just/<platform>/mod.just
#   2. Add: mod <platform> 'just/<platform>'  (below)
#   3. Follow the structure of just/gcp/mod.just
#   4. Aim for command parity where it makes sense
#
# See just/aws/mod.just for a template with implementation notes.
#
# =============================================================================

# =============================================================================
# Settings
# =============================================================================

# Load .env file if present (optional, for local dev)
set dotenv-load := false

# Use bash for shell commands (more predictable than sh)
set shell := ["bash", "-cu"]

# =============================================================================
# Imports
# =============================================================================
#
# Imported files merge their recipes into the root namespace.
# These are used for commands you want to run without a prefix.
#
# Prefix with ? to make optional (won't error if file doesn't exist).
# =============================================================================

# Shared configuration and helper functions
import 'just/common.just'

# =============================================================================
# Modules
# =============================================================================
#
# Modules create namespaced command groups, invoked with: just <module> <command>
# This keeps platform-specific commands organized and avoids conflicts.
#
# Use: mod <name> '<path>'
# Access: just <name> <recipe>
#
# =============================================================================

# Local Docker development — community-only stack (no commercial add-ons).
# Usage: just local <command>
# Examples:
#   just local up
#   just local up --build
#   just local down
#   just local logs
mod local 'just/local'

# Google Cloud Platform deployment
# Usage: just gcp <command>
# Examples:
#   just gcp deploy prod
#   just gcp logs dev
#   just gcp status-all
mod gcp 'just/gcp'

# MCP server — standalone FastMCP image operations (build, deploy,
# secrets, logs, tests).
#
# Historical entry point. Prefer ``just gcp mcp <command>`` for GCP
# work — that grammar is symmetric with ``just gcp django <command>``
# and scopes MCP operations under their deploy target. Both paths
# reach the same module; neither deprecates the other.
#
# The test recipes (``just mcp test``, ``just mcp test-e2e``) are
# genuinely target-agnostic and stay naturally accessed via this
# top-level mount.
#
# Usage:
#   just mcp test                        # local pytest + ruff on mcp/
#   just mcp deploy prod                 # same as ``just gcp mcp deploy prod``
mod mcp 'just/mcp'

# Amazon Web Services deployment (stub - not yet implemented)
# Usage: just aws <command>
# Status: Commands show "not implemented" message with implementation guidance
mod aws 'just/aws'

# Self-hosted deployment (Docker Compose on a single VM)
#
# This is the customer-operated target — the same substrate as
# ``just local`` but deployed to a customer's VM (DigitalOcean, AWS EC2,
# Hetzner, on-prem, etc.) for production use. Self-hosted is single-stage
# per VM (one VM = one stage); recipes do not take a stage argument.
#
# Usage: just self-hosted <command>
# Examples:
#   just self-hosted deploy
#   just self-hosted doctor
#   just self-hosted backup
#   just self-hosted health-check
mod self-hosted 'just/self-hosted'

# Pro version local development (community + validibot-pro, no cloud layer)
# Usage: just local-pro up
# Usage: just local-pro up --build
# Usage: ENABLE_MCP_SERVER=true just local-pro up   # include MCP container
mod local-pro 'just/local-pro'

# Cloud version local development (layers validibot-cloud on local stack)
# Usage: just local-cloud up
# Usage: just local-cloud up --build
mod local-cloud 'just/local-cloud'

# =============================================================================
# Default Command
# =============================================================================

# List all available commands (this is the default when you just run 'just')
default:
    @echo ""
    @echo "Validibot Command Runner"
    @echo "========================"
    @echo ""
    @echo "Local Docker (pick the flavour you need):"
    @echo "    just local <command>        # Community only"
    @echo "    just local-pro <command>    # Community + validibot-pro"
    @echo "    just local-cloud <command>  # Community + pro + cloud"
    @echo ""
    @echo "Each local flavour supports: up, up --build, down, rebuild, logs, ..."
    @echo ""
    @echo "Platform Modules:"
    @echo "    just gcp <command>             # Google Cloud Platform"
    @echo "    just gcp django <command>      # Django-only GCP ops (e.g. secrets)"
    @echo "    just gcp mcp <command>         # MCP-only GCP ops (secrets, deploy, ...)"
    @echo "    just mcp <command>             # MCP operations (alias; also: local tests)"
    @echo "    just aws <command>             # AWS (not implemented)"
    @echo "    just self-hosted <command>     # Self-hosted (Docker Compose on a VM)"
    @echo ""
    @echo "Examples:"
    @echo "    just local up             # Start community dev stack"
    @echo "    just local-pro up         # Start community + pro"
    @echo "    just gcp deploy prod      # Deploy to GCP production"
    @echo "    just -f just/gcp/mod.just --list   # List all GCP commands"
    @echo ""
    @echo "Run 'just --list' for full command list"
    @echo "Run 'just -f just/<module>/mod.just --list' for module command lists"
    @echo ""

# =============================================================================
# Testing
# =============================================================================
#
# One venv, three fidelity tiers. Community is the baseline; the pro and cloud
# tiers activate the commercial packages via their settings module and run
# those repos' suites. They degrade gracefully (skip, not fail) when the
# commercial source/packages are absent (e.g. a community-only checkout).
#
#   just test                # community suite + open-core isolation guard
#   just test PATHS...        # scoped community run (paths, -k, -x, ... passed through)
#   just test-pro             # validibot-pro's own suite
#   just test-pro PATHS...    # those paths under config.settings.test_pro (Pro active)
#   just test-cloud           # validibot-cloud's + validibot-pro's suites
#   just test-cloud PATHS...  # those paths under validibot_cloud.settings.test
#   just setup-test-env       # make .venv able to run all three tiers

# Community test suite (config.settings.test). Extra pytest args pass through.
test *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    source ./set-env.sh >/dev/null 2>&1 || true
    exec .venv/bin/pytest {{args}}

# Verify both Python lockfiles without changing them.
lock-check:
    uv lock --check
    cd mcp && uv lock --check

# Lint Django application Python; MCP owns an independent stricter lint recipe.
lint:
    uv run --frozen --group dev ruff check --exclude '*.md' .

# Verify Python formatting without changing files.
format-check:
    uv run --frozen --group dev ruff format --check --exclude '*.md' .

# Enforce the ratcheting production-code mypy baseline used by CI.
typecheck:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    source ./set-env.sh >/dev/null 2>&1 || true
    exec uv run --frozen --group dev python scripts/check_mypy_baseline.py

# Verify frontend types, tests, production bundles, and bundle freshness.
frontend-check:
    #!/usr/bin/env bash
    set -euo pipefail
    # The build writes normal generated targets. Stale targets remain visible
    # for the developer to review and commit.
    npm ci
    npm run typecheck
    npm test -- --passWithNoTests=false
    npm run build
    CHANGES="$(git status --porcelain -- validibot/static/css validibot/static/js)"
    if [[ -n "$CHANGES" ]]; then
        echo "Error: Committed frontend bundles are stale. Review the rebuilt files:"
        echo "$CHANGES"
        exit 1
    fi

# Audit the exact locked runtime sets for Django/self-hosting and MCP.
audit:
    #!/usr/bin/env bash
    set -euo pipefail
    AUDIT_DIR="$(mktemp -d)"
    trap 'rm -rf "$AUDIT_DIR"' EXIT

    uv export --frozen --extra docker-runner \
        --no-emit-project \
        --no-emit-local \
        --quiet \
        --format requirements-txt \
        --output-file "$AUDIT_DIR/validibot.txt"
    uvx --from pip-audit==2.10.1 pip-audit \
        --requirement "$AUDIT_DIR/validibot.txt" \
        --require-hashes \
        --disable-pip \
        --strict

    (
        cd mcp
        uv export --frozen \
            --no-emit-project \
            --quiet \
            --format requirements-txt \
            --output-file "$AUDIT_DIR/mcp.txt"
    )
    uvx --from pip-audit==2.10.1 pip-audit \
        --requirement "$AUDIT_DIR/mcp.txt" \
        --require-hashes \
        --disable-pip \
        --strict

# Run the complete local integration gate.
check: lock-check format-check lint typecheck test frontend-check
    just mcp check

# Require the exact main-branch commit to have a successful CI workflow.
_require-release-ci:
    #!/usr/bin/env bash
    set -euo pipefail

    gh auth status >/dev/null
    REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
    HEAD_SHA="$(git rev-parse HEAD)"
    RUN_INFO="$(
        gh run list \
            --repo "$REPO" \
            --workflow ci.yml \
            --branch main \
            --commit "$HEAD_SHA" \
            --event push \
            --limit 1 \
            --json databaseId,status,conclusion,url \
            --jq 'if length == 0 then "" else .[0] | "\(.databaseId)|\(.status)|\(.conclusion // "")|\(.url)" end'
    )"

    if [[ -z "$RUN_INFO" ]]; then
        echo "Error: No main-branch CI run exists for $HEAD_SHA."
        echo "Push main and wait for CI before releasing."
        exit 1
    fi

    IFS='|' read -r RUN_ID RUN_STATUS RUN_CONCLUSION RUN_URL <<< "$RUN_INFO"
    if [[ "$RUN_STATUS" != "completed" ]]; then
        echo "Waiting for CI run $RUN_ID to finish: $RUN_URL"
        gh run watch "$RUN_ID" --repo "$REPO" --exit-status
    elif [[ "$RUN_CONCLUSION" != "success" ]]; then
        echo "Error: CI did not succeed for $HEAD_SHA: $RUN_URL"
        exit 1
    fi

    echo "CI succeeded for $HEAD_SHA: $RUN_URL"

# Run every local and remote release prerequisite without creating a tag.
release-check: check audit _require-release-ci

# Community + validibot-pro. No args: run Pro's own suite (its settings). With
# args: run those paths under config.settings.test_pro (Pro in INSTALLED_APPS).
test-pro *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if ! .venv/bin/python -c "import validibot_pro" >/dev/null 2>&1; then
        echo "skip: validibot_pro is not installed — skipping the Pro tier."
        echo "      (commercial license required, then: just setup-test-env)"
        exit 0
    fi
    source ./set-env.sh >/dev/null 2>&1 || true
    if [ -n "{{args}}" ]; then
        exec .venv/bin/pytest --ds=config.settings.test_pro {{args}}
    fi
    ( cd ../validibot-pro && "{{justfile_directory()}}/.venv/bin/pytest" )

# Community + pro + cloud. No args: run Cloud's then Pro's suites under their
# settings. With args: run those paths under validibot_cloud.settings.test.
test-cloud *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    if ! .venv/bin/python -c "import validibot_cloud" >/dev/null 2>&1; then
        echo "skip: validibot_cloud is not installed — skipping the Cloud tier."
        echo "      (commercial, then: just setup-test-env)"
        exit 0
    fi
    source ./set-env.sh >/dev/null 2>&1 || true
    if [ -n "{{args}}" ]; then
        exec .venv/bin/pytest --ds=validibot_cloud.settings.test {{args}}
    fi
    .venv/bin/pytest --ds=validibot_cloud.settings.test ../validibot-cloud/validibot_cloud
    if .venv/bin/python -c "import validibot_pro" >/dev/null 2>&1; then
        ( cd ../validibot-pro && "{{justfile_directory()}}/.venv/bin/pytest" )
    fi

# Prepare the single dev venv to run all three tiers (community + pro + cloud).
# Safe to re-run; reconciles .venv to the lockfile + cloud extra, then editable-
# installs the commercial packages if their sibling repos are present.
setup-test-env:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    echo "-> Syncing community deps + cloud extra (stripe) into .venv"
    uv sync --extra cloud
    echo "-> Editable-installing commercial packages (skipped if absent)"
    for pkg in ../validibot-pro ../validibot-cloud; do
        if [ -d "$pkg" ]; then
            uv pip install --no-deps -e "$pkg"
        else
            echo "   (skip: $pkg not present)"
        fi
    done
    echo "OK: just test | just test-pro | just test-cloud"

# =============================================================================
# Cross-Platform Commands
# =============================================================================
#
# These recipes work with any platform by taking a platform argument.
# They're convenience wrappers that delegate to the appropriate module.
#
# Note: For most operations, prefer using the module directly:
#   just gcp deploy prod    (instead of: just deploy gcp prod)
#
# =============================================================================

# Show deployment status for a platform and stage
# Usage: just status gcp prod | just status self-hosted
[no-cd]
platform-status platform stage="":
    #!/usr/bin/env bash
    case "{{platform}}" in
        gcp)
            if [ -z "{{stage}}" ]; then
                just gcp status-all
            else
                just gcp status {{stage}}
            fi
            ;;
        self-hosted)
            just self-hosted status
            ;;
        aws)
            just aws status {{stage}}
            ;;
        *)
            echo "Unknown platform: {{platform}}"
            echo "Supported: gcp, aws, self-hosted"
            exit 1
            ;;
    esac

# =============================================================================
# Release
# =============================================================================
#
# Cuts a signed-tag release for the Validibot Django app. CI verifies
# the tag against the signer allowlist on protected main, builds one
# source bundle, generates CycloneDX SBOMs and checksums, attests the
# source archive, and creates an immutable GitHub release.
#
# Operator verification (after clone): see RELEASING.md.

# Release a new version: signs the tag, pushes, CI publishes the GitHub release.
# Usage: just release 0.4.0
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate version format.
    if [[ ! "{{VERSION}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "✗ Version must be in format X.Y.Z (e.g., 0.4.0). Got: {{VERSION}}"
        exit 1
    fi

    # Refuse if working tree is dirty.
    if [[ -n $(git status --porcelain) ]]; then
        echo "✗ Working tree has uncommitted changes. Commit or stash first."
        git status --short
        exit 1
    fi

    # Refuse if not on main.
    BRANCH=$(git branch --show-current)
    if [[ "$BRANCH" != "main" ]]; then
        echo "✗ Not on main branch (currently on '$BRANCH')."
        echo "  Releases are cut from main only. Switch with: git checkout main"
        exit 1
    fi

    # Refuse if tag already exists locally or remotely.
    TAG="v{{VERSION}}"
    if git rev-parse "$TAG" >/dev/null 2>&1; then
        echo "✗ Tag $TAG already exists locally."
        exit 1
    fi
    if git ls-remote --tags origin "refs/tags/$TAG" | grep -q "$TAG"; then
        echo "✗ Tag $TAG already exists on origin."
        exit 1
    fi

    # Confirm we're up-to-date with origin.
    git fetch origin main
    if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
        echo "✗ Local main is not in sync with origin/main."
        echo "  Run: git pull"
        exit 1
    fi

    # Verify the cross-repo dependency on validibot-shared is at the
    # latest published version. Catches the "I forgot to bump
    # validibot-shared after publishing a new shared release"
    # failure mode — easy to miss, hard to debug after the release
    # is out (deployed code uses an older shared schema).
    #
    # Override with VALIDIBOT_RELEASE_ALLOW_STALE_SHARED=1 for
    # emergencies (e.g. PyPI is down, or you intentionally want to
    # pin to an older shared release).
    if [[ "${VALIDIBOT_RELEASE_ALLOW_STALE_SHARED:-0}" != "1" ]]; then
        SHARED_PINNED=$(grep -E '"validibot-shared==' pyproject.toml | head -1 | sed -E 's/.*"validibot-shared==([^"]+)".*/\1/')
        if [[ -z "$SHARED_PINNED" ]]; then
            echo "⚠ Could not detect validibot-shared pin in pyproject.toml; skipping freshness check."
        else
            SHARED_LATEST=$(curl -s --max-time 10 https://pypi.org/pypi/validibot-shared/json 2>/dev/null | jq -r '.info.version' 2>/dev/null)
            if [[ -z "$SHARED_LATEST" ]] || [[ "$SHARED_LATEST" == "null" ]]; then
                echo "⚠ Could not query PyPI for latest validibot-shared. Currently pinned: $SHARED_PINNED."
                echo "  Press Enter to continue anyway, Ctrl+C to abort..."
                read -r
            elif [[ "$SHARED_PINNED" != "$SHARED_LATEST" ]]; then
                echo "✗ validibot-shared is pinned to $SHARED_PINNED but latest on PyPI is $SHARED_LATEST."
                echo ""
                echo "  Update pyproject.toml so the line reads:"
                echo "      \"validibot-shared==$SHARED_LATEST\","
                echo ""
                echo "  Then commit + push, and re-run: just release {{VERSION}}"
                echo ""
                echo "  Override (emergencies only): VALIDIBOT_RELEASE_ALLOW_STALE_SHARED=1 just release {{VERSION}}"
                exit 1
            else
                echo "✓ validibot-shared is at latest ($SHARED_LATEST)"
            fi
        fi
    fi

    echo ""
    echo "Running the full release gate..."
    just release-check

    if [[ -n $(git status --porcelain) ]]; then
        echo "✗ Release checks changed the working tree. Review and commit those changes first."
        git status --short
        exit 1
    fi

    echo ""
    echo "About to sign and push tag $TAG."
    echo "Press Enter to continue, Ctrl+C to abort..."
    read -r

    # Sign the tag, then verify it locally against the allowlist from
    # protected origin/main before anything leaves this machine. Git
    # configuration is command-scoped so the release helper does not
    # alter the operator's global or repository signing settings.
    TRUSTED_SIGNERS=$(mktemp)
    trap 'rm -f "$TRUSTED_SIGNERS"' EXIT
    git show origin/main:.allowed_signers > "$TRUSTED_SIGNERS"
    git tag -s "$TAG" -m "$TAG"
    if ! git \
        -c gpg.format=ssh \
        -c gpg.ssh.allowedSignersFile="$TRUSTED_SIGNERS" \
        verify-tag "$TAG"; then
        echo "✗ Local signature verification failed. Tag was not pushed."
        echo "  Inspect or remove the local tag before retrying: $TAG"
        exit 1
    fi

    git push origin "$TAG"

    echo ""
    echo "✓ Pushed $TAG"
    echo "  CI will independently verify, attest, and publish the release."
    echo "  Monitor: gh run watch"
