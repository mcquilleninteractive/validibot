# Just Command Runner Guide

Validibot uses [Just](https://just.systems/man/en/) as its command runner for managing local development and deployments across multiple platforms. Just is similar to Make but with a cleaner syntax, better error messages, and modern features like modules and imports.

## Why Just?

We chose Just over alternatives (Make, shell scripts, Python scripts) because:

- **Simplicity**: Clean, readable syntax without Make's arcane rules
- **Modules**: Native support for organizing commands by platform/concern
- **Cross-platform**: Works on macOS, Linux, and Windows
- **No dependencies**: Single binary with no runtime requirements
- **Tab completion**: Built-in shell completion for discoverability
- **Dry-run mode**: Preview commands before executing them

## Installation

```bash
# macOS
brew install just

# Linux (via Homebrew)
brew install just

# Linux (via cargo)
cargo install just

# Enable tab completion (add to ~/.zshrc or ~/.bashrc)
eval "$(just --completions zsh)"  # or bash/fish
```

## File Structure

Our justfile setup uses a modular architecture to support multiple deployment platforms:

```
justfile              # Root orchestrator
just/
├── common.just       # Shared variables and helpers
├── local/
│   └── mod.just      # Community-only local dev (just local ...)
├── local-pro/
│   └── mod.just      # Community + validibot-pro local dev
├── local-cloud/
│   └── mod.just      # Community + pro + cloud local dev
├── gcp/
│   ├── mod.just      # Google Cloud Platform deployment
│   └── django/
│       └── mod.just  # Django-only GCP ops (just gcp django ...)
├── mcp/
│   └── mod.just      # MCP server build/deploy/secrets/logs/test
│                     # (also re-exposed under gcp via `mod mcp '../mcp'`
│                     #  in just/gcp/mod.just, so "just gcp mcp ..." works)
├── aws/
│   └── mod.just      # AWS deployment (stub - not implemented)
└── self-hosted/
    └── mod.just      # Self-hosted Docker Compose on a VM
```

### Root Justfile

The root `justfile` serves as the orchestrator. It:

- Sets global configuration (shell, dotenv settings)
- **Imports** shared helpers (merged into root namespace)
- Declares **modules** for each deployment target (namespaced)

### Modules and submodules

Every platform-specific group of commands is a module. Deployment
targets with multiple sub-areas (like GCP, which hosts the Django web
service plus optionally the MCP server plus per-service secret
management) use **submodules** for clean grouping:

```bash
just gcp deploy-all prod        # Umbrella: web + worker + scheduler + MCP
just gcp secrets prod           # Umbrella: pushes .django AND .mcp
just gcp django secrets prod    # Surgical: only .django → django-env
just gcp mcp deploy prod        # Surgical: only rebuild/redeploy MCP
```

The same mcp module is also mounted at the top level so `just mcp ...`
works as an alias for `just gcp mcp ...` — useful for target-agnostic
commands like `just mcp test` (local pytest).

### Imports vs Modules

Two mechanisms for organizing recipes:

| Feature | Imports | Modules |
|---------|---------|---------|
| Syntax | `import 'path.just'` | `mod name 'path'` |
| Access | Direct: `just recipe` | Namespaced: `just module recipe` |
| Use case | Shared helpers | Platform-specific commands |

Currently only `just/common.just` is imported at the root. All user-
facing command groups (`local`, `local-pro`, `local-cloud`, `gcp`,
`mcp`, `self-hosted`, `aws`) are modules so the target is explicit
in every invocation.

## Quick Reference

### Local Development

These commands are imported directly (no prefix needed):

```bash
# Container lifecycle
just local up              # Start all local containers
just local down            # Stop containers
just local build           # Rebuild and restart
just local logs            # Follow all container logs
just local ps              # Show container status
just local restart         # Stop then start
just local clean           # Stop and remove volumes (deletes data!)

# Django commands
just local shell           # Bash shell in Django container
just local migrate         # Run database migrations
just local manage "cmd"    # Run any manage.py command

# Testing
just local test            # Run tests locally
just local test -k "name"  # Run specific tests
just local test-integration # Run integration tests with Docker
```

### Google Cloud Platform

GCP commands require environment variables for your project configuration. Before running any `just gcp` command:

```bash
# Option 1: Source your config file (recommended)
source .envs/.production/.google-cloud/.just

# Option 2: Set variables manually
export GCP_PROJECT_ID="your-project-id"
export GCP_REGION="us-central1"
```

Copy `.envs.example/.production/.google-cloud/.just` to `.envs/.production/.google-cloud/.just` and fill in your values. This file is gitignored so your project details stay private.

Commands are prefixed with `gcp`:

```bash
# List all GCP commands
just -f just/gcp/mod.just --list

# Deployment (web + worker)
just gcp deploy prod         # Hotfix path: web only
just gcp deploy-worker prod  # Hotfix path: worker only
just gcp deploy-all prod     # Full: web + worker + scheduler + MCP (if enabled)

# Operations
just gcp logs prod           # View logs
just gcp logs-follow prod    # Stream logs
just gcp status prod         # Check service status
just gcp status-all          # Status of all stages
just gcp open prod           # Open in browser

# Database & Django
just gcp migrate prod                  # Migrate and initialize if needed
just gcp setup-data prod               # Explicitly refresh all initialized data
just gcp management-cmd prod "shell"   # Run any command via a temp Cloud Run Job

# Secrets — umbrella and surgical paths
just gcp secrets prod                  # Umbrella: .django AND .mcp (when MCP enabled)
just gcp django secrets prod           # Surgical: only .django → django-env
just gcp mcp secrets prod              # Surgical: only .mcp → mcp-env

# Infrastructure
just gcp init-stage dev                # Create web/worker SAs + Cloud SQL, etc.
just gcp scheduler-setup dev           # Create scheduled jobs
just gcp lb-setup prod "example.com"   # Set up HTTPS load balancer

# Maintenance
just gcp mode-maintenance dev          # Isolate runtime/work, start/keep DB ready
just gcp mode-status dev               # Report LIVE/MAINTENANCE/PARKED/transition
just gcp deploy-maintenance dev         # Deploy latest but remain offline
just gcp init-stage-maintenance dev     # Reconcile infra but remain offline
just gcp maintenance-management-cmd dev "check_validibot --strict"
just gcp mode-parked dev               # Take the stage offline and stop DB
just gcp mode-live dev                 # Ensure DB ready, then safely resume
```

`deploy`, `deploy-worker`, and `deploy-all` deliberately require a complete
`LIVE` stage: database ready, normal public runtime ingress, running queues,
and every managed scheduler job enabled. `mode-maintenance` is the fail-closed
entry point: it forces all services to internal ingress and zero minimums,
pauses scheduler/queue work, and starts or keeps Cloud SQL RUNNABLE for
consecutive operator tasks.
`deploy-maintenance`, maintenance management commands, and validator operations
leave the database running; their EXIT traps restore runtime isolation without
changing database state. Use `mode-parked` as the sole explicit SQL stop
after all maintenance work is complete. Database starts and stops are
asynchronous and poll both instance state and active provider operations, so
slow provider maintenance cannot outlive the local CLI wait; the default
30-minute deadline can be overridden with
`GCP_SQL_TRANSITION_TIMEOUT_SECONDS`. Runtime cleanup is best-effort across
individual Cloud Run, scheduler, and queue operations: it skips resources
already isolated, records failures, and attempts every remaining safeguard
before returning non-zero. Optional MCP revisions are deployed with
`VALIDIBOT_MCP_ENABLED=false`; `mode-live` makes web reachable first and
then restores the configured MCP value so its normal startup license check can
succeed without weakening the gate.

#### MCP server commands

When the deployment runs the MCP server (`ENABLE_MCP_SERVER=true` in
the stage's `.build` file), it has its own Cloud Run service and
lifecycle. The commands are submodule-scoped under `gcp mcp`:

```bash
just gcp mcp setup prod      # First-time: create MCP SA + IAM bindings
just mcp build-local         # Build local MCP image; never pushes
just gcp mcp build-push      # Build + push MCP image to Artifact Registry
just gcp mcp deploy prod     # Deploy MCP image to Cloud Run
just gcp mcp secrets prod    # Upload .mcp → mcp-env Secret Manager secret
just gcp mcp lb-add prod mcp.yourdomain.com  # Wire MCP into the LB
just gcp mcp logs prod       # Tail MCP service logs
just gcp mcp status prod     # MCP service URL + revision info
just gcp mcp test            # Run MCP pytest suite locally (no GCP calls)
```

Same module is also reachable as `just mcp ...` at the top level;
`just mcp check`, `just mcp test`, and `just mcp test-e2e` are the natural
entry points for local verification. `just mcp build` remains a compatibility
alias for the historical build-and-push behavior; use `build-local` or
`build-push` in new automation so publication is explicit.

### Self-hosted Docker Compose

Commands are prefixed with `self-hosted`:

```bash
# List all self-hosted commands
just -f just/self-hosted/mod.just --list

# Deployment
just self-hosted bootstrap    # First-time self-host install
just self-hosted deploy       # Build and start all services
just self-hosted upgrade --to v0.9.0  # Versioned upgrade with backup

# Operations
just self-hosted status       # Check container status
just self-hosted logs         # Follow logs
just self-hosted health-check # Verify services are running
just self-hosted doctor       # Full diagnostic
just self-hosted smoke-test   # End-to-end demo workflow

# Backup and restore
just self-hosted migrate              # Run migrations
just self-hosted backup               # Manifested app backup
just self-hosted list-backups         # Show available backups
just self-hosted restore backups/<id> # Restore manifested backup

# Maintenance
just self-hosted maintenance-on       # Enter maintenance mode
just self-hosted maintenance-off      # Exit maintenance mode
```

### AWS (Stub)

AWS support is planned but not yet implemented. Commands show helpful guidance:

```bash
just aws deploy prod    # Shows implementation guidance
```

## Stages

All platforms support three deployment stages:

| Stage | Purpose | Resource Naming |
|-------|---------|-----------------|
| `dev` | Development and testing | `*-dev` suffix |
| `staging` | Pre-production testing | `*-staging` suffix |
| `prod` | Production | No suffix |

Example resource names:

- Dev: `$GCP_APP_NAME-web-dev`, `$GCP_APP_NAME-db-dev`
- Prod: `$GCP_APP_NAME-web`, `$GCP_APP_NAME-db`

## Common Patterns

### Deploying Updates

```bash
# GCP: full-stack deploy (web + worker + scheduler + MCP when enabled)
just gcp deploy-all dev
just gcp verify-deployment-quick dev

# After testing on dev, promote to prod
just gcp deploy-all prod

# Hotfix path: web-only deploy, skips worker and MCP
just gcp deploy prod

# Self-hosted Docker Compose: versioned upgrade
just self-hosted upgrade --to v0.9.0
```

Migrations run automatically as part of `deploy-all` and `deploy`
(gated by `GCP_SKIP_MIGRATE=1` for hotfixes with no schema changes).

### Viewing Logs

```bash
# GCP: Last 50 log entries
just gcp logs prod

# GCP: Stream logs in real-time
just gcp logs-follow prod

# Docker Compose: Follow container logs
just self-hosted logs
```

### Running Django Commands

```bash
# Local development
just local manage "createsuperuser"
just local manage "shell_plus"

# GCP (creates temporary Cloud Run Job)
just gcp management-cmd prod "createsuperuser"

# Docker Compose
just self-hosted manage "createsuperuser"
```

### Checking Status

```bash
# GCP: All stages at once
just gcp status-all

# GCP: Specific stage
just gcp status dev

# Docker Compose
just self-hosted health-check
```

## Tips & Tricks

### Preview Commands

See what a command will do without executing:

```bash
just --show gcp deploy
just --dry-run gcp deploy prod
```

### List All Commands

```bash
just --list              # Root commands
just -f just/gcp/mod.just --list          # GCP commands
just -f just/self-hosted/mod.just --list  # Self-hosted Docker Compose commands
```

### Run from Anywhere

Just automatically finds the justfile in parent directories:

```bash
cd validibot/core/
just local up  # Still works!
```

### Environment Variables

Commands that need environment variables:

```bash
# GCP: Connect to Cloud SQL
DATABASE_PASSWORD='secret' just gcp local-to-gcp-shell dev

# Testing: E2E tests
E2E_TEST_API_URL=https://... just gcp test-e2e
```

## Adding a New Platform

To add support for a new cloud platform:

1. Create `just/<platform>/mod.just`
2. Add `mod <platform> 'just/<platform>'` to root justfile
3. Follow the structure of `just/gcp/mod.just`
4. Implement the core commands: `deploy`, `migrate`, `logs`, `status`

See `just/aws/mod.just` for a template with detailed implementation notes.

## Migrating from Old Commands

If you have scripts or muscle memory from the old flat justfile:

| Old Command | New Command |
|-------------|-------------|
| `just gcp-deploy dev` | `just gcp deploy dev` |
| `just gcp-logs prod` | `just gcp logs prod` |
| `just gcp-status-all` | `just gcp status-all` |
| `just gcp-migrate dev` | `just gcp migrate dev` |
| `just gcp-secrets prod` | `just gcp secrets prod` |

The new structure uses a space instead of a hyphen after the platform name.

## Troubleshooting

### "Module not found" Error

Ensure you're running Just version 1.31.0 or later:

```bash
just --version
```

### "Recipe not found" Error

Check that you're using the correct prefix:

```bash
# Wrong: deployment commands need platform prefix
just deploy prod

# Correct: use the platform prefix
just gcp deploy prod
```

### Tab Completion Not Working

Ensure you've added the completion to your shell profile:

```bash
# Add to ~/.zshrc
eval "$(just --completions zsh)"

# Then reload
source ~/.zshrc
```
