# Self-Hosted Validibot — Overview

This is the entry point for operators running Validibot on their own
infrastructure. If you're a Validibot developer rather than an
operator, you probably want `docs/dev_docs/` instead.

## What "self-hosted" means

A self-hosted deployment is Validibot running on a single Linux VM
that **you** control — typically on DigitalOcean, AWS EC2, Hetzner,
or your own on-prem hardware. You own the host, the data, the
backups, the upgrade cadence, and the support escalation path.

Self-hosted is one of three deployment targets Validibot supports:

| Target | Substrate | Audience | Driver |
|---|---|---|---|
| **local** | Docker Compose on your laptop | a single developer | `just local <cmd>` |
| **self-hosted** | Docker Compose on a VM | customers running their own copy | `just self-hosted <cmd>` |
| **GCP** | Cloud Run, Cloud SQL, Cloud Tasks | Validibot's hosted offering | `just gcp <cmd>` |

The same Validibot codebase serves all three. The differences are in
the substrate (where containers actually run) and the operational
expectations (developer testing vs production data custody vs cloud
SaaS).

## What's on the VM

```text
your VM
├── Docker Engine + Compose plugin
├── /srv/validibot/ (recommended mounted block-storage volume)
│   ├── repo/          Validibot checkout and operator backups
│   └── docker/        Docker data root, including named volumes
└── A Validibot Compose stack:
    ├── web        Django ASGI application (Gunicorn + UvicornWorker; MCP on Pro)
    ├── worker     Celery worker (background tasks, validators)
    ├── scheduler  Celery Beat (periodic tasks)
    ├── postgres   Database
    ├── redis      Task queue broker
    └── caddy      (optional, opt-in) reverse proxy with auto-TLS
```

Validation jobs spawn additional short-lived Docker containers
("advanced validators" — EnergyPlus, FMU, etc.) via the worker's
access to the host Docker socket. These are run-scoped and clean up
after themselves.

## Recommended VM sizing

| Use case | CPU | Memory | Disk | Notes |
|---|---:|---:|---:|---|
| Evaluation | 2 vCPU | 4 GB | 50 GB | Small demo workflows |
| Small team | 4 vCPU | 8-16 GB | 200 GB SSD | EnergyPlus/FMU light use |
| Heavy simulation | 8+ vCPU | 32 GB | 500 GB+ SSD | More validator concurrency |

For paid pilots we recommend the "small team" size with a separate
block-storage volume mounted at `/srv/validibot`, and Docker configured
with `/srv/validibot/docker` as its data root before the first Compose
run. That keeps Docker named volumes, validation evidence, and app
backups off the disposable boot disk. See the DigitalOcean tutorial
under `providers/` for one provider's specifics.

## What outbound calls happen by default

Self-hosted Validibot is **telemetry-off by default**. No product
analytics. No license phone-home. No usage reporting. The default
install makes only these outbound calls:

- container image pulls during install/upgrade (the canonical public GHCR
  packages, or an operator-configured private registry);
- email delivery if you configure an email provider;
- Let's Encrypt ACME challenges if you enable the bundled Caddy proxy;
- nothing else.

Sentry error reporting and other diagnostics can be opted in per
section 8 of `.envs/.production/.self-hosted/.django`. None of them
are required for the application to validate, sign, back up, restore,
or export evidence.

## Two editions: community and Pro

| | Community | Pro |
|---|---|---|
| Cost | Free (AGPL) | Paid license |
| Validators | Built-in (JSON, XML, basic, AI) | + advanced (EnergyPlus, FMU, custom) |
| Workflows | Yes | + teams, guests, signed credentials |
| MCP server | No | Yes |
| Install path | Public Docker images | Build locally from a private wheel |
| Support | Community | Email support included |

**Activating Pro is a settings module switch + a package install.**
Buy a license at https://validibot.com, receive a private package
URL and a Pro wheel reference. Set those in your `.build` env file,
flip `DJANGO_SETTINGS_MODULE` to `config.settings.production_pro` in
`.django`, and run `just self-hosted deploy`. Existing data, users,
workflows, and runs are preserved across the upgrade. Pro migrations
are additive — they add tables but never reshape community tables.

Keep the private package URL and wheel reference out of source control, shell
history, and logs. Valid package access is required for installation and
upgrades; renewal and credential-rotation details are provided with the
commercial license. Optional services confirm their feature availability
against the local Validibot deployment during startup.

## First install (one-page summary)

This is the minimum sequence after the host has Docker, the Compose
plugin, `just`, a non-root operator user, and durable storage prepared.
The bootstrap scripts under `deploy/self-hosted/scripts/` are still
stubs, so use the DigitalOcean tutorial for exact current host-prep
commands or perform the equivalent steps on your own provider.

```bash
# On the prepared VM as the operator user:
cd /srv/validibot/repo

# 1. Copy and edit env files
mkdir -p .envs/.production/.self-hosted
cp .envs.example/.production/.self-hosted/.django \
   .envs/.production/.self-hosted/.django
cp .envs.example/.production/.self-hosted/.postgres \
   .envs/.production/.self-hosted/.postgres
cp .envs.example/.production/.self-hosted/.build \
   .envs/.production/.self-hosted/.build
$EDITOR .envs/.production/.self-hosted/.django  # set SITE_URL, secrets, etc.

# 2. Validate config and DNS
just self-hosted check-env
just self-hosted check-dns        # verify SITE_URL resolves to this VM

# 3. Bring up the stack
just self-hosted bootstrap   # builds, migrates, creates superuser, registers OIDC clients

# 4. Verify
just self-hosted health-check
just self-hosted doctor
just self-hosted smoke-test
```

## Day-to-day operations

```bash
just -f just/self-hosted/mod.just --list  # see all recipes
just self-hosted status        # are services running?
just self-hosted logs          # follow logs from all services
just self-hosted health-check  # quick service health
just self-hosted doctor        # full diagnostic
just self-hosted smoke-test    # end-to-end demo workflow
just self-hosted backup        # manifested app backup
just self-hosted list-backups  # show available backups
just self-hosted restore backups/<backup-id>
just self-hosted upgrade --to v0.9.0
just self-hosted collect-support-bundle
```

## Reverse proxy: bring your own, or use bundled Caddy

The Compose stack does not run a reverse proxy by default. Most
operators already have one (nginx, Traefik, Cloudflare Tunnel, hosting
provider load balancer) and would resent another being installed.

If you don't have one, the kit ships an opt-in Caddy service with
automatic Let's Encrypt TLS:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

Confirm DNS first:

```bash
just self-hosted check-dns
```

If you bring your own proxy, configure it to forward to the `web`
container on port 8000 and set `DJANGO_SECURE_PROXY_SSL_HEADER` plus
`DJANGO_CSRF_TRUSTED_ORIGINS` appropriately in your `.django` file.

On Pro, MCP is served by the same web container at `<SITE_URL>/mcp`. No second
hostname, port, container, or proxy rule is required. Ensure the proxy preserves
the original HTTPS scheme and host for both ordinary Django routes and `/mcp`.

## Provider quickstarts

The canonical deployment is "single Linux VM with Docker Compose."
Provider-specific tutorials map that generic shape to real
infrastructure:

- [DigitalOcean](providers/digitalocean.md) — primary supported provider
  tutorial and the canonical worked example.
- AWS EC2, Hetzner, on-prem — substrate-generic install instructions
  apply. Provider tutorials may follow on customer demand. (No
  comparable self-hosted product writes per-provider reference docs;
  Validibot follows the same pattern.)

## Support

For paid Pro support, generate a redacted support bundle and email it
to support@validibot.com:

```bash
just self-hosted collect-support-bundle
```

The bundle includes versions, doctor output, recent logs, migration
state, and validator manifests. It excludes secrets, signing keys,
API tokens, and raw submission contents.

## Known remaining gaps

Most operator lifecycle recipes now do real work. The remaining gaps to
account for during a paid pilot are:

- the pre-`just` bootstrap helper scripts are still stubs, so host prep
  is manual or provider-guide driven;
- self-hosted S3-compatible object storage is documented as a future
  option but is not yet an operator-supported path;
- using an external managed Postgres service requires careful env and
  TLS configuration, and the bundled Compose Postgres service remains
  the default supported path.

High-value implementation follow-ups:

- replace the DigitalOcean guide's manual Docker-install/data-root block
  with an idempotent one-shot installer script;
- add a DigitalOcean doctor check that verifies Docker's data root is
  under the expected mounted volume path;
- add doctor findings for database configuration mode, especially
  `DATABASE_URL` pointing off-host while the bundled local Postgres
  service is still running.

## Detailed guides

The fuller operator documentation:

- **[Install](install.md)** — substrate-generic install steps that work on any Linux + Docker host.
- **[Configuration](configuration.md)** — env file reference, the eight grouped sections of `.django`, settings module switching, deployment profiles.
- **[Backups](backups.md)** — backup architecture, manifest schema, off-host recommendations.
- **[Restore](restore.md)** — restore drills, component selection, recovery patterns.
- **[Upgrades](upgrades.md)** — upgrade lifecycle, pre-flight checks, strict upgrade-path enforcement, in-flight run handling.
- **[Validator Images](validator-images.md)** — what's installed, run-scoped isolation, image pinning, cleanup, future trust tiers.
- **[Security Hardening](security-hardening.md)** — the recommended hardening checklist.
- **[Support Bundle](support-bundle.md)** — what's in the redacted bundle, what's excluded, support workflow contract.
- **[Troubleshooting](troubleshooting.md)** — common issues and how to diagnose them.
- **[Release Notes Policy](release-notes-policy.md)** — what every release announces.
- **[Operator Recipes](operator-recipes.md)** — full reference for `just self-hosted` recipes.
- **[Doctor Check IDs](doctor-check-ids.md)** — every check ID and its fix.
