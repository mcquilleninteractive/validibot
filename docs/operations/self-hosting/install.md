# Installing Validibot (self-hosted)

This is the substrate-generic install guide. It works on any Linux host with Docker. Provider-specific tutorials (DigitalOcean, AWS EC2, Hetzner, on-prem) sit on top of this — see `providers/`.

If you're evaluating Validibot on your laptop, [Run Validibot Locally](../../dev_docs/deployment/deploy-local.md) is faster.

## Prerequisites

A Linux VM with:

- Ubuntu LTS, Debian stable, or another Docker-friendly distribution;
- Docker Engine + Compose plugin (Docker 24+ recommended; Docker 27+ if available);
- 4+ vCPU, 8+ GB RAM, 50+ GB disk for evaluation; more for production (see sizing guide in `overview.md`);
- a public IP and DNS record if you want HTTPS;
- root or sudo access for the initial bootstrap.

The `bootstrap-host` helper is still a stub. For current installs, prepare the host manually or follow the [DigitalOcean provider guide](providers/digitalocean.md) for exact commands, then run the `just self-hosted bootstrap` step from the prepared checkout.

## Recommended VM sizing

| Use case | CPU | Memory | Disk | Notes |
|---|---:|---:|---:|---|
| Evaluation | 2 vCPU | 4 GB | 50 GB | Small demo workflows |
| Small team | 4 vCPU | 8-16 GB | 200 GB SSD | EnergyPlus/FMU light use |
| Heavy simulation | 8+ vCPU | 32 GB | 500 GB+ SSD | More worker/validator concurrency |

For paid pilots we recommend the "small team" size with a separate block-storage volume mounted at `/srv/validibot`, and Docker configured with `/srv/validibot/docker` as its data root before the first Compose run. That keeps Docker named volumes, validation evidence, and app backups off the disposable boot disk.

## Install steps

### 1. Prepare the host

On a fresh VM, complete these host-prep steps before cloning the repo:

- install Docker Engine and the Compose plugin;
- install `just`;
- create a non-root `validibot` operator user and install rootless Docker for
  that user;
- mount durable storage at `/srv/validibot` for production installs;
- configure Docker's data root to `/srv/validibot/docker` before the first Compose run if you are using that mounted volume layout.

The helper scripts in `deploy/self-hosted/scripts/` are reserved for this role, but they are still stubs. Do not rely on them for production host preparation yet.

### 2. Clone the repo (or extract a release tarball)

```bash
git clone https://github.com/mcquilleninteractive/validibot.git
cd validibot
```

For air-gapped or release-tarball installs, extract the tarball and `cd` into the resulting directory. The tarball bundles `docker-compose.production.yml`, the `deploy/self-hosted/` directory, the `just/self-hosted/` module, and a copy of the env templates.

### 3. Copy and edit env files

```bash
mkdir -p .envs/.production
cp -r .envs.example/.production/.self-hosted/ .envs/.production/.self-hosted/
$EDITOR .envs/.production/.self-hosted/.django
```

The `.django` file has eight grouped sections you'll need to customise:

1. **Required** — `SITE_URL`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_SECRET_KEY`, `DJANGO_API_KEY_DIGEST_KEY`, `DJANGO_MFA_ENCRYPTION_KEY`, `WORKER_API_KEY`;
2. **URLs/security** — `DJANGO_CSRF_TRUSTED_ORIGINS`, secure cookies, HSTS;
3. **Database/cache** — usually defaults;
4. **Storage** — default Compose uses `/app/storage/private` backed by the `validibot_storage` Docker named volume;
5. **Email** — `DJANGO_EMAIL_BACKEND`, sender addresses;
6. **Validators** — runner selection, image policy;
7. **Pro/signing** — empty for community, set when activating Pro;
8. **Optional telemetry** — off by default.

Generate secrets:

```bash
# DJANGO_SECRET_KEY
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# DJANGO_API_KEY_DIGEST_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"

# DJANGO_MFA_ENCRYPTION_KEY (must be Fernet-format)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# WORKER_API_KEY
openssl rand -base64 48
```

Also edit `.postgres` and `.build` from the same template. The `.build`
template selects the deployment user's rootless Docker socket. If you retain a
rootful daemon for compatibility, opt in explicitly with
`VALIDATOR_CONTAINER_SOCKET=/var/run/docker.sock` and review the host-authority
tradeoff in [Security Hardening](security-hardening.md).

### 4. Validate config and DNS

```bash
just self-hosted check-env
just self-hosted check-dns       # verify SITE_URL resolves to this VM
```

### 5. Bring up the stack

```bash
just self-hosted bootstrap      # builds, migrates, creates superuser, registers OIDC clients
```

### 6. Verify

```bash
just self-hosted health-check   # quick service health
just self-hosted doctor         # full diagnostic
just self-hosted smoke-test     # end-to-end demo workflow
```

The doctor command is target-aware and emits a stable JSON schema (`validibot.doctor.v1`). Pass `--json` for machine-readable output, `--strict` to fail CI on warnings.

## Day-to-day operations

Once installed:

```bash
just -f just/self-hosted/mod.just --list  # all recipes
just self-hosted status           # are services running?
just self-hosted logs             # follow logs from all services
just self-hosted health-check     # quick service health
just self-hosted doctor           # full diagnostic
just self-hosted backup           # full application backup
just self-hosted upgrade --to v0.9.0   # versioned upgrade
just self-hosted cleanup          # remove stale containers/images/backups
```

See [operator-recipes.md](operator-recipes.md) for the full reference.

## Reverse proxy: bring your own, or use bundled Caddy

The Compose stack does not run a reverse proxy by default. Most operators already have one (nginx, Traefik, Cloudflare Tunnel, hosting provider load balancer) and would resent another being installed.

If you don't have one, the kit ships an opt-in Caddy service with automatic Let's Encrypt TLS:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

Confirm DNS first:

```bash
just self-hosted check-dns
```

If you bring your own proxy, configure it to forward to the `web` container on port 8000 and set `DJANGO_SECURE_PROXY_SSL_HEADER` plus `DJANGO_CSRF_TRUSTED_ORIGINS` appropriately in your `.django` file.

On Pro, MCP is available at `<SITE_URL>/mcp` through the same proxy and web
container. No distinct hostname, port, or proxy backend is required.

## Provider quickstarts

The canonical deployment is "single Linux VM with Docker Compose." Provider-specific tutorials map that generic shape to real infrastructure:

- [DigitalOcean](providers/digitalocean.md) — primary supported provider tutorial.
- AWS EC2, Hetzner, on-prem — substrate-generic install instructions apply. Provider tutorials may follow on customer demand.

## Activating Pro

If you've bought a Pro license:

1. Edit `.envs/.production/.self-hosted/.build`:

   ```bash
   VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
   VALIDIBOT_COMMERCIAL_NETRC=/home/validibot/.config/validibot/commercial.netrc
   ```

   Create that netrc from the credentials in the purchase email and restrict
   it to the deployment user:

   ```netrc
   machine pypi.validibot.com
     login <email>
     password <package-key>
   ```

   ```bash
   chmod 600 /home/validibot/.config/validibot/commercial.netrc
   ```

   Compose mounts the file only into the commercial package installation step;
   credentials do not become build arguments or image metadata.

2. Edit `.envs/.production/.self-hosted/.django`:

   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production_pro
   ```

3. Re-run deploy:

   ```bash
   just self-hosted deploy
   just self-hosted doctor
   ```

That's it. Existing data, users, workflows, and runs are preserved across the upgrade. Pro migrations are additive — they add tables (teams, guests, signed credentials, MCP, advanced analytics) but never reshape community tables. The package-index credential is the entitlement gate; there is no runtime license phone-home.

## Known remaining gaps

Most lifecycle recipes now do real work. The pre-`just` bootstrap helper scripts remain stubs, self-hosted S3-compatible object storage is not yet an operator-supported path, and external managed Postgres requires the careful TLS configuration described in the provider guide.

## See also

- [Self-Hosting Overview](overview.md)
- [Configuration](configuration.md) — env file reference
- [Backups](backups.md) — backup architecture and policy
- [Restore](restore.md) — restore drill workflow
- [Upgrades](upgrades.md) — upgrade lifecycle
- [Security Hardening](security-hardening.md) — recommended hardening
- [Support Bundle](support-bundle.md) — what's in it, what's redacted
- [DigitalOcean Provider Guide](providers/digitalocean.md)
