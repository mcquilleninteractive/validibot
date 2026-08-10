# Self-Hosting Validibot on DigitalOcean

This is the canonical end-to-end tutorial for getting a production-ready Validibot instance running on a DigitalOcean Droplet. It is the single source of truth for DigitalOcean — every other doc that mentions DO links here rather than repeating instructions.

By the end of this guide you'll have:

- A Validibot instance reachable at your own domain with a valid Let's Encrypt TLS certificate.
- Docker's data root on a separate block-storage volume, so Validibot's Docker named volumes (Postgres, uploads, evidence, Redis, Caddy certs) survive Droplet rebuilds.
- A DigitalOcean Cloud Firewall locking inbound traffic down to SSH (your IP only) plus 80/443.
- A documented backup, restore-drill, and upgrade workflow.
- A clear path to activate Pro (signed credentials, teams, guests, MCP, advanced analytics) if and when you license it.

The same instance can run the community edition (free, AGPL) or be upgraded to Pro by changing two env values and re-running `just self-hosted deploy`. Existing data is preserved across the switch.

## Prerequisites

- A DigitalOcean account with billing set up.
- A registered domain you can add an A-record to (e.g. `validibot.example.org`). You don't need to host DNS at DigitalOcean — any registrar works.
- An SSH keypair already uploaded to DigitalOcean. Password auth is disabled throughout this guide.
- A laptop with `git`, `ssh`, and (optionally) `doctl` installed.
- Optional: a Validibot Pro license. Community works without one — see [Step 14](#step-14-optional-activate-pro).

If you'd rather evaluate locally before committing to a Droplet, [Run Validibot Locally](../../../dev_docs/deployment/deploy-local.md) takes about ten minutes.

## Sizing and cost

The right Droplet size depends almost entirely on which validators you'll run. The built-in validators (JSON Schema, XML Schema, Basic, AI) run in the Django process and use little memory. Advanced validators (EnergyPlus, FMU, custom containers) spawn short-lived sibling containers that can each consume 2-4 GB during a simulation.

### Memory budget for the base stack

| Component | Idle | Peak |
|---|---|---|
| Django (Gunicorn, 2 workers) | ~150-200 MB | ~400 MB |
| Celery worker | ~150 MB | ~300 MB |
| Celery beat (scheduler) | ~80 MB | ~100 MB |
| PostgreSQL | ~100 MB | ~300 MB |
| Redis | ~50 MB | ~100 MB |
| Caddy (if bundled) | ~20 MB | ~50 MB |
| OS + Docker overhead | ~300 MB | ~400 MB |
| **Total** | **~850 MB** | **~1.65 GB** |

An EnergyPlus container layered on top of this needs another 2-4 GB. A 2 GB Droplet running the base stack has roughly 350 MB headroom — not enough for even a small EnergyPlus simulation, so it'll OOM-kill other services when one runs.

### Recommended Droplet sizes

The prices below are planning examples, not quotes. DigitalOcean changes plan prices and backup options over time, so confirm the final monthly number in the Droplet create screen before quoting a customer.

| Use case | Droplet | Monthly | Notes |
|---|---|---|---|
| Built-in validators only | 2 GB / 1 vCPU | $12 | JSON, XML, Basic, AI — no EnergyPlus or FMU |
| Occasional advanced validators | 4 GB / 2 vCPU | $24 | Add swap; may queue during heavy use |
| **Paid pilot (recommended)** | **8 GB / 4 vCPU** | **$48** | **Regular advanced validator usage** |
| High-volume production | 16 GB / 8 vCPU | $96 | Multiple concurrent advanced validations |

For paid pilots we strongly recommend the 8 GB tier plus a **200 GB block-storage volume mounted at `/srv/validibot`**. This guide moves Docker's data root to `/srv/validibot/docker` before the first `docker compose up`, so Validibot's named volumes live on the block volume instead of the disposable boot disk.

### Total monthly cost

| Configuration | Components | Monthly |
|---|---|---|
| Minimal (built-in validators) | 2 GB Droplet | $12 |
| Small (occasional advanced) | 4 GB Droplet + swap | $24 |
| **Recommended (paid pilot)** | **8 GB Droplet + 200 GB volume** | **$48 + $20** |
| Production (managed DB) | 8 GB Droplet + 200 GB volume + Managed Postgres | $83+ |

Optional add-ons (covered in [Step 13](#step-13-optional-enhancements)): Managed PostgreSQL, Spaces, and Load Balancers. Confirm current pricing in the DigitalOcean control panel before quoting these to a customer.

## Architecture

What DigitalOcean owns:

- VM provisioning, networking, block storage, snapshots, optional Managed Postgres, Cloud Firewall.

What you (the operator) own:

- Compose configuration, app secrets, migrations, validation data, evidence bundles, application-level backups, restore tests, upgrades, and the support escalation path.

What's on the Droplet:

```text
Droplet (Ubuntu 24.04 LTS)
├── Docker Engine + Compose plugin
├── /srv/validibot/           (mounted block-storage volume)
│   ├── repo/                 (this repository, cloned)
│   │   └── backups/          (manifested application-level backups)
│   └── docker/               (Docker data-root)
│       └── volumes/          (Postgres, uploads/evidence, Redis, Caddy data)
└── A Validibot Compose stack:
    ├── web        Django web application (Gunicorn)
    ├── worker     Celery worker (background tasks, validators)
    ├── scheduler  Celery Beat (periodic tasks)
    ├── postgres   Database
    ├── redis      Task queue broker
    ├── caddy      (opt-in profile) reverse proxy with auto-TLS
    └── mcp        (Pro feature, opt-in) FastMCP server
```

Advanced validators (EnergyPlus, FMU) run as short-lived sibling containers spawned by the worker via the host Docker socket. They clean up after themselves.

---

## Step 1: Create the Droplet

Ubuntu 24.04 LTS x64, in your customer's preferred region, with SSH-key login only.

### Using the DigitalOcean control panel

1. **Create → Droplets**.
2. **Choose an image:** OS → **Ubuntu 24.04 LTS x64**. This guide installs Docker in [Step 6](#step-6-bootstrap-the-host) so the base OS and package setup are explicit. DigitalOcean's Docker 1-Click image is useful for quick experiments, but its base OS can lag the Ubuntu LTS baseline in this guide.
3. **Choose a plan:** see the [sizing table](#recommended-droplet-sizes). Paid-pilot baseline is **Basic $48/mo (8 GB / 4 vCPU)**.
4. **Datacenter region:** close to your users (latency) and your operator team (admin convenience).
5. **Authentication:** select your SSH key. Never enable password auth.
6. **Hostname:** something descriptive like `validibot-prod`.
7. **Create Droplet** and note the public IPv4 address.

### Using doctl

```bash
# Install doctl: https://docs.digitalocean.com/reference/doctl/how-to/install/

doctl compute droplet create validibot-prod \
  --image ubuntu-24-04-x64 \
  --size s-4vcpu-8gb \
  --region syd1 \
  --ssh-keys $(doctl compute ssh-key list --format ID --no-header | head -1) \
  --enable-monitoring \
  --wait
```

Adjust `--size` and `--region` to match your sizing decision. Pick the SSH key explicitly if your account has more than one key; the command above uses the first key only as a compact example. `--enable-monitoring` installs the DigitalOcean Monitoring agent — useful for graphs and alerts, optional if you'd rather keep telemetry off.

## Step 2: Attach block-storage volume

For paid pilots and anything in production, Docker's data root should live on a separate block-storage volume — not the boot disk. Validibot's Compose file uses Docker named volumes for Postgres, uploaded files, validation evidence, Redis, and Caddy certs. Moving Docker's data root before the first deploy puts all of those named volumes on the attached DigitalOcean Volume.

1. **Create → Volumes**.
2. Same region as the Droplet.
3. **Size:** 200 GB for a paid pilot. Bump to 500 GB+ for heavy simulation workloads.
4. **Choose a Droplet to attach to:** the one you just created.
5. **Configuration:** manually format and mount.
6. **Filesystem:** ext4.
7. **Mount path:** `/srv/validibot`.

DigitalOcean's UI generates the exact device path for the volume, usually under `/dev/disk/by-id/scsi-0DO_Volume_<name>`. Format only a brand-new empty volume. If you are reattaching a volume that already contains Validibot data, **do not run `mkfs`**.

For a new volume, the shape should look like this:

```bash
sudo mkfs.ext4 -F /dev/disk/by-id/scsi-0DO_Volume_<volume-name>
sudo mkdir -p /srv/validibot
echo '/dev/disk/by-id/scsi-0DO_Volume_<volume-name> /srv/validibot ext4 defaults,nofail,discard,noatime 0 2' \
  | sudo tee -a /etc/fstab
sudo mount -a
```

Confirm the mount before doing anything else:

```bash
findmnt /srv/validibot
df -h /srv/validibot
# Should show your DigitalOcean Volume mounted at /srv/validibot
```

The bootstrap step ([Step 6](#step-6-bootstrap-the-host)) creates `repo/` and `docker/` under this mount with the right ownership.

## Step 3: Configure DNS

For quick evaluations, add an A-record at your DNS provider pointing your chosen hostname (e.g. `validibot.example.org`) at the Droplet's public IPv4 address. TTL 3600 is fine.

For paid pilots or production, allocate a **DigitalOcean Reserved IP** in the same datacenter, assign it to the Droplet, and point DNS at the Reserved IP instead. Reserved IPs can be reassigned to a replacement Droplet in the same datacenter, which makes rebuild and restore drills less dependent on DNS propagation.

You can host DNS at DigitalOcean (**Networking → Domains**) or at any other registrar — Validibot doesn't care.

Verify propagation from your laptop:

```bash
dig +short validibot.example.org
# Should print the Droplet public IP or the Reserved IP you assigned
```

After [Step 6](#step-6-bootstrap-the-host), you'll re-verify from the Droplet itself with `just self-hosted check-dns`. Doing that *before* enabling TLS prevents Caddy from burning Let's Encrypt rate-limit attempts against bad DNS.

One caveat: `check-dns` compares DNS to the Droplet's detected outbound public IPv4. If DNS points at a Reserved IP and outbound traffic still uses the Droplet's primary public IPv4, `check-dns` can false-fail. In that case, verify the Reserved IP assignment in the DigitalOcean control panel and with `dig`, then continue.

## Step 4: Configure DigitalOcean Cloud Firewall

DigitalOcean Droplets ship with UFW installed but disabled. Do not enable UFW for this deployment: **Docker bypasses UFW by default**, which silently exposes container ports you thought you'd blocked. Use a DigitalOcean Cloud Firewall instead — it filters at the network edge before traffic reaches the Droplet.

1. **Networking → Firewalls → Create Firewall**.
2. Name: `validibot-prod`.
3. **Inbound rules:**
   - **SSH (TCP 22)** — *Sources:* your operator IP/CIDR only. Don't allow `0.0.0.0/0` here.
   - **HTTP (TCP 80)** — *Sources:* all IPv4 + IPv6. Caddy needs port 80 for Let's Encrypt ACME challenges.
   - **HTTPS (TCP 443)** — *Sources:* all IPv4 + IPv6.
   - Everything else: omit (default-deny).
4. **Outbound rules:** allow all (the default). If you create the firewall through the API or later remove defaults, remember that DigitalOcean Cloud Firewalls also filter egress: no outbound rules means no outbound traffic.
5. **Apply to Droplets:** select `validibot-prod`.
6. **Create Firewall**.

If you enable IPv6 on the Droplet, add the matching AAAA DNS record and keep the IPv6 HTTP/HTTPS firewall sources. If you do not plan to serve IPv6, leave IPv6 disabled on the Droplet.

## Step 5: SSH hardening and non-root user

SSH to the Droplet as root for the last time:

```bash
ssh root@<droplet-ip>
```

### Create the non-root user

```bash
adduser validibot
usermod -aG sudo validibot

# Copy your SSH key from root to the new user
rsync --archive --chown=validibot:validibot ~/.ssh /home/validibot
```

### Harden sshd

Edit `/etc/ssh/sshd_config` and ensure:

```text
PasswordAuthentication no
PermitRootLogin prohibit-password
```

Restart SSH:

```bash
systemctl restart ssh
```

If your image uses `sshd` as the service name, run `systemctl restart sshd` instead.

### Install Fail2Ban (defence in depth)

```bash
apt update && apt install -y fail2ban
systemctl enable --now fail2ban
```

Test that the new user works *before* logging out of the root session:

```bash
# From a SECOND terminal on your laptop:
ssh validibot@<droplet-ip>
```

Once that works, exit the root session.

## Step 6: Bootstrap the host

There's a Validibot helper that will eventually install Docker, validate the DigitalOcean mount, move Docker's data root onto `/srv/validibot/docker`, install `just`, and set up the repo directory.

> **Status note:** the bootstrap scripts (`bootstrap-host`, `bootstrap-digitalocean`) currently print "not yet implemented." The fallback steps below are what they will eventually automate. Once the scripts ship, the fallback section will be removed from this guide.

### When the scripts are implemented (target state)

```bash
ssh validibot@<droplet-ip>
sudo git clone https://github.com/mcquilleninteractive/validibot.git /srv/validibot/repo
sudo chown -R validibot:validibot /srv/validibot/repo
cd /srv/validibot/repo
./deploy/self-hosted/scripts/bootstrap-digitalocean --data-root /srv/validibot
```

That script will detect the Droplet (via the metadata service), validate the volume mount, install `just`, and prep the directory tree.

### Today (manual fallback)

```bash
ssh validibot@<droplet-ip>

# Base host tools used by the operator recipes.
sudo apt update
sudo apt install -y ca-certificates curl gnupg git jq zstd dnsutils

# Install Docker Engine + Compose plugin from Docker's apt repository.
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
  docker-compose-plugin docker-ce-rootless-extras \
  uidmap dbus-user-session libcap2-bin

# Fresh installs use Docker's rootless daemon under the validibot operator
# account. Disable the unused rootful daemon before any Compose state exists.
sudo systemctl disable --now docker.socket docker.service
sudo loginctl enable-linger validibot

# Put rootless Docker's named volumes on the attached DigitalOcean Volume
# before the first docker compose run.
sudo install -d -o validibot -g validibot /srv/validibot/docker
install -d -m 0700 ~/.config/docker
tee ~/.config/docker/daemon.json > /dev/null <<'EOF'
{
  "data-root": "/srv/validibot/docker"
}
EOF

dockerd-rootless-setuptool.sh install
systemctl --user enable --now docker
docker context use rootless

# The bundled Caddy profile publishes 80/443. Grant only rootlesskit the
# ability to bind privileged ports, then restart the user daemon.
sudo setcap cap_net_bind_service=ep "$(command -v rootlesskit)"
systemctl --user restart docker

docker info --format '{{.DockerRootDir}}'
# Should print: /srv/validibot/docker
docker info --format '{{json .SecurityOptions}}'
# Must include: name=rootless
docker info --format '{{.CgroupVersion}} {{.CgroupDriver}}'
# Must print: 2 systemd (validator resource limits depend on cgroup delegation)
test -S "${XDG_RUNTIME_DIR}/docker.sock"

# Install just (used to drive all subsequent operations). This pins the
# version instead of using GitHub's moving "latest" redirect.
JUST_VERSION=1.36.0
curl -sSL "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
  | sudo tar -xz -C /usr/local/bin just

# Clone the repo into the volume so the working tree survives Droplet rebuilds
sudo git clone https://github.com/mcquilleninteractive/validibot.git /srv/validibot/repo
sudo chown -R validibot:validibot /srv/validibot/repo

cd /srv/validibot/repo
```

Do not run `docker compose` until the data-root check prints
`/srv/validibot/docker`, `SecurityOptions` includes `name=rootless`, the cgroup
check prints `2 systemd`, and the per-user socket exists. If Docker creates
named volumes under the boot disk first, you need to migrate them deliberately
before relying on Droplet rebuild survival. See [Troubleshooting → I ran
Compose before relocating Docker's data
root](../troubleshooting.md#i-ran-compose-before-relocating-dockers-data-root).

Existing installations may retain rootful Docker during a planned migration.
Set `VALIDATOR_CONTAINER_SOCKET=/var/run/docker.sock` explicitly in `.build`
and keep using the rootful Docker context; this is a compatibility posture, not
the fresh-install default.

## Step 7: Configure environment files

Validibot uses four env files for self-hosted deployments — all under `.envs/.production/.self-hosted/`:

| File | Purpose |
|---|---|
| `.django` | Django runtime config — secrets, allowed hosts, email, storage paths, MFA key. |
| `.postgres` | Postgres credentials. |
| `.build` | Build/host config — rootless socket, MCP host port, image tags, optional exact Pro package reference. |
| `.mcp` | MCP runtime and OAuth settings (used only when the Pro MCP service is enabled). |

Copy the templates and edit them:

```bash
mkdir -p .envs/.production
cp -r .envs.example/.production/.self-hosted/ .envs/.production/.self-hosted/

$EDITOR .envs/.production/.self-hosted/.django
$EDITOR .envs/.production/.self-hosted/.postgres
$EDITOR .envs/.production/.self-hosted/.build
```

The `.django` file is organised into eight sections. The required settings for a fresh install:

```bash
# Section 1: required
SITE_URL=https://validibot.example.org
DJANGO_ALLOWED_HOSTS=validibot.example.org
DJANGO_SECRET_KEY=<see below>
DJANGO_API_KEY_DIGEST_KEY=<see below>
DJANGO_MFA_ENCRYPTION_KEY=<see below>
WORKER_API_KEY=<see below>

# Section 2: URLs/security
DJANGO_CSRF_TRUSTED_ORIGINS=https://validibot.example.org
DJANGO_SECURE_SSL_REDIRECT=true
DJANGO_ADMIN_URL=<random-admin-path>/
MFA_TOTP_ISSUER="Acme Validibot"

# Section 4: storage
# Do not set DATA_STORAGE_ROOT for the default Compose deployment.
# docker-compose.production.yml sets it to /app/storage/private inside
# the container, backed by the validibot_storage Docker named volume.

# Section 5: email — configure your provider (Mailgun, SES, SMTP relay)
DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
DJANGO_DEFAULT_FROM_EMAIL=validibot@example.org
# ... your SMTP creds ...

# Initial admin
SUPERUSER_USERNAME=admin
SUPERUSER_EMAIL=admin@example.org
SUPERUSER_PASSWORD=<strong password>
```

Generate the core Django secrets:

```bash
# DJANGO_SECRET_KEY
docker run --rm python:3.13-alpine python -c \
  "from secrets import token_urlsafe; print(token_urlsafe(50))"

# DJANGO_API_KEY_DIGEST_KEY
docker run --rm python:3.13-alpine python -c \
  "from secrets import token_urlsafe; print(token_urlsafe(32))"

# DJANGO_MFA_ENCRYPTION_KEY (must be Fernet-format)
docker run --rm python:3.13-alpine sh -c \
  "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

Generate the remaining secrets:

```bash
# WORKER_API_KEY
docker run --rm python:3.13-alpine python -c \
  "from secrets import token_urlsafe; print(token_urlsafe(32))"

# DJANGO_ADMIN_URL
docker run --rm python:3.13-alpine python -c \
  "from secrets import token_urlsafe; print(token_urlsafe(16) + '/')"
```

And in `.postgres`, set a strong password:

```bash
POSTGRES_PASSWORD=<32+ random chars>
```

Validate before going further:

```bash
just self-hosted check-env
just self-hosted check-dns   # verifies SITE_URL before TLS
```

`check-env` reports missing required vars and unchanged placeholders. `check-dns` is your last chance to catch a DNS typo before Let's Encrypt rate-limits you. If you pointed DNS at a Reserved IP, remember the Reserved IP caveat in [Step 3](#step-3-configure-dns).

## Step 8: Start the stack

First, bootstrap the app without the public Caddy profile — useful sanity check that the app boots and migrations succeed.

```bash
just self-hosted bootstrap
```

`bootstrap` builds the image, runs migrations, creates the superuser (from `SUPERUSER_*` vars), registers OIDC clients, seeds default validators and roles, and starts the stack.

Then enable the bundled Caddy reverse proxy with auto-TLS:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

Caddy requests a Let's Encrypt certificate for `SITE_URL` on first boot. If DNS resolves correctly (verified by Step 7) and ports 80/443 are open (Step 4), you'll have a valid cert within a few seconds.

### If you already run a reverse proxy

Don't enable the `caddy` profile. Configure your existing proxy (nginx, Traefik, Cloudflare Tunnel, hosting-provider load balancer) to forward to the `web` container on port 8000, and keep the following in `.django`:

```bash
DJANGO_SECURE_SSL_REDIRECT=true
DJANGO_SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
DJANGO_CSRF_TRUSTED_ORIGINS=https://validibot.example.org
```

## Step 9: Verify the deployment

```bash
just self-hosted status         # all containers Up + healthy?
just self-hosted health-check   # quick HTTP health probe
just self-hosted doctor         # full diagnostic
just self-hosted doctor --provider digitalocean   # + DO-specific checks
just self-hosted smoke-test     # end-to-end demo workflow
```

Treat any `ERROR` or `FATAL` result as a launch blocker. Warnings may be acceptable during an evaluation, but write them down in the final install report so the operator knows what remains.

From your laptop:

```bash
curl --fail --proto '=https' --tlsv1.2 --show-error --silent --head https://validibot.example.org/health/
# HTTP/2 200
# server: Caddy
# ...
```

And in the browser at `https://validibot.example.org`: you should see the Validibot login screen with a valid certificate. Log in with the superuser credentials from `.django`.

### What to check before declaring success

- All containers are `running` and `healthy` in `just self-hosted status`.
- HTTPS resolves with a valid cert (`curl --fail --proto '=https' ... /health/` succeeds and the browser shows no certificate warnings).
- The doctor command reports `OK`, `INFO`, `WARN`, or `SKIPPED` only — zero `ERROR` or `FATAL`.
- The DO-specific overlay (`--provider digitalocean`) confirms DNS and the volume mount, reports on the monitoring agent, and prints a reminder to verify your Cloud Firewall rules. The Droplet can't introspect its own Cloud Firewall without a DO API token (which the security model deliberately keeps off the production server), so verify firewall posture from your workstation with `doctl compute firewall list` or the DO control panel.
- You can log in as the superuser and create a test workflow.

## Step 10: Configure automated backups

Use the manifested application backup recipe. It captures:

- `db.sql.zst` — a plain SQL Postgres dump.
- `data.tar.zst` — the full `DATA_STORAGE_ROOT` archive, including uploads, submissions, evidence, and validator resources.
- `manifest.json` — Validibot version, migration head, Postgres version, file sizes, and checksums.
- `checksums.sha256` — a sidecar you can verify without parsing JSON.

```bash
cd /srv/validibot/repo
just self-hosted backup
just self-hosted list-backups
```

The default backup directory is `/srv/validibot/repo/backups/`, which is on the attached volume because the repo lives under `/srv/validibot`. Schedule it nightly:

```bash
(crontab -l 2>/dev/null; echo "0 2 * * * cd /srv/validibot/repo && /usr/local/bin/just self-hosted backup >> backups/backup.log 2>&1") | crontab -
```

Local backups on the same volume are not enough. For off-host encrypted backups, install [restic](https://restic.net/) and point it at `/srv/validibot/repo/backups/`, sending the repository to S3, B2, GCS, Azure, SFTP, or DigitalOcean Spaces. The Validibot [Backups doc](../backups.md) covers retention and restore-test patterns in more depth.

### What DigitalOcean snapshots are good for

DigitalOcean **Droplet snapshots** and **automatic backups** are infrastructure-level — they roll the whole VM back to a point in time. They're useful for catastrophic recovery (the Droplet itself is corrupted), but they **do not replace** application-level backups, because:

- They can't produce a `manifest.json` with migration state and checksums.
- They can't pass through Validibot's restore compatibility check.
- They can't be partially restored (e.g. just the database).
- DigitalOcean automatic Droplet backups do **not** include attached Volumes. In this guide, the real application state lives on the `/srv/validibot` Volume.
- Volume snapshots are crash-consistent, not application-consistent. They do not coordinate with Postgres, Docker, or the filesystem to flush every in-memory write.

Enable DigitalOcean automatic Droplet backups if you want OS-level insurance, but treat them as additional to, not a substitute for, `just self-hosted backup`. If you also take a DigitalOcean Volume snapshot, first run a Validibot backup, schedule a maintenance window, stop the stack with `just self-hosted down`, run `sync`, take the Volume snapshot, then bring the stack back up.

Backup output structure:

```text
/srv/validibot/repo/backups/
  2026-04-27T120000Z/
    manifest.json
    db.sql.zst
    data.tar.zst
    checksums.sha256
```

## Step 11: Perform a restore drill

Backups you haven't restored are wishes, not backups. Before you call the install production-ready, restore one onto a clean test Droplet and verify it boots.

1. On the production Droplet: `cd /srv/validibot/repo && just self-hosted backup`.
2. Create a second Droplet and Volume in the same region.
3. Follow Steps 1-8 of this guide to get a fresh Validibot stack running on the test Droplet.
4. Copy the chosen backup directory to the test Droplet, preserving the directory shape:

   ```bash
   rsync -a backups/20260427T120000Z/ \
     validibot@<test-droplet-ip>:/srv/validibot/repo/backups/20260427T120000Z/
   ```

5. On the test Droplet:

   ```bash
   cd /srv/validibot/repo
   just self-hosted restore backups/20260427T120000Z
   just self-hosted doctor
   just self-hosted smoke-test
   ```

Doctor warns (check `VB411`) if a backup has never been restored. Make sure that warning is gone before you treat the install as production-ready.

## Step 12: Upgrade workflow

Upgrades follow a fixed sequence: doctor pre-flight, clean working tree, target tag check, manifested backup, checkout, rebuild, migrate, restart, doctor, smoke-test.

```bash
cd /srv/validibot/repo

# See available releases
git fetch --tags origin
git tag --list 'v*' --sort=-v:refname | head

# Upgrade to an exact release tag. This creates a manifested backup first.
just self-hosted upgrade --to v0.9.0

just self-hosted doctor
just self-hosted smoke-test
```

Do not use `just self-hosted update`; it is deprecated and exits with an error. Use `--no-backup` only if you have already taken and verified a backup for this exact upgrade window.

## Step 13: Optional enhancements

### Managed PostgreSQL

For production workloads at scale, offload Postgres to DigitalOcean's managed database service. Benefits: point-in-time backups, failover options, easier scaling, and Postgres patches handled for you.

Treat this as an advanced configuration today. The stock Compose file still defines a local `postgres` service and `web` has a `depends_on` relationship with it. Do **not** remove the local `postgres` service unless you also ship and test a Compose override that removes that dependency.

1. **Databases → Create Database Cluster** → PostgreSQL. Choose the same major version as the bundled Validibot Postgres image unless the newer version has been tested for this Validibot release.
2. Same region and VPC as the Droplet. Add a standby node if the customer needs HA.
3. **Trusted sources:** add the Droplet or its tag so only the application host can connect.
4. Download the cluster CA certificate into `.envs/.production/.self-hosted/keys/do-postgres-ca.crt`.
5. Edit `.envs/.production/.self-hosted/.postgres` so the entrypoint waits for the managed cluster:

   ```bash
   POSTGRES_HOST=your-cluster.db.ondigitalocean.com
   POSTGRES_PORT=25060
   POSTGRES_DB=validibot
   POSTGRES_USER=validibot
   POSTGRES_PASSWORD=<from the DO control panel>
   ```

6. Add a full `DATABASE_URL` to `.envs/.production/.self-hosted/.django`. URL-encode special characters in the password.

   ```bash
   DATABASE_URL=postgres://validibot:<url-encoded-password>@your-cluster.db.ondigitalocean.com:25060/validibot?sslmode=verify-full&sslrootcert=/run/validibot-keys/do-postgres-ca.crt
   ```

   Do not use `POSTGRES_OPTIONS`; the current production entrypoint ignores it when constructing `DATABASE_URL`.

7. Leave the local `postgres` service running unless you have a tested override. It is unused by Django when `DATABASE_URL` points to the managed cluster, but it satisfies the current Compose dependency.
8. `just self-hosted deploy`, then `just self-hosted doctor`.

### DigitalOcean Spaces (object storage)

DigitalOcean Spaces is S3-compatible, but Validibot's self-hosted S3 path is not operator-supported yet. There is partial S3 plumbing in settings, but `s3://` URI routing and end-to-end self-hosted Spaces operation are not ready for a customer install. For now:

- Stay on the default local volume storage (with the volume mounted at `/srv/validibot`, you're already on durable storage).
- Or use the GCS-backed deployment path if you genuinely need object storage today.

Do **not** set `DATA_STORAGE_BACKEND=s3` in a current Validibot deployment.

### DigitalOcean Monitoring agent

If you didn't pass `--enable-monitoring` at Droplet creation, install the agent now:

```bash
curl -sSL https://repos.insights.digitalocean.com/install.sh | sudo bash
```

The agent reports CPU, memory, disk, and network metrics back to DigitalOcean for the **Graphs** tab and alert policies. It's free. Validibot's `doctor --provider digitalocean` check (VB912) reports whether it's installed.

### Advanced validators (EnergyPlus, FMU, custom)

Advanced validators run as short-lived sibling containers spawned by the worker via the host Docker socket — already configured in `docker-compose.production.yml`. To use them:

1. **Build the validator images on the Droplet.** Self-hosted operators
   currently build advanced validators from a sibling checkout of
   `validibot-validator-backends`; there is no public Validibot image
   registry to pull from. Clone the validator-backends repo next to your
   `validibot/` checkout and build the images you need:

   ```bash
   git clone https://github.com/mcquilleninteractive/validibot-validator-backends.git
   cd /path/to/validibot
   just self-hosted validator-build energyplus
   just self-hosted validator-build fmu
   ```

   Each recipe produces an image tagged `validibot-validator-backend-<name>:<git_sha>`
   plus a `:latest` alias on the Droplet's local Docker daemon. The
   first build is slow (downloading EnergyPlus / FMU runtime
   dependencies); rebuilds are incremental.

2. **Network isolation:** by default validator containers run with no network access. If a validator legitimately needs to fetch external resources, set `VALIDATOR_NETWORK` in `.django`.

See [Validator Images](../validator-images.md) for image-naming conventions, building custom validators, and isolation details.

## Step 14: (optional) Activate Pro

If you've bought a Pro license, you'll have received a package login, key, and
exact package reference. Activation is two config changes plus a redeploy — no
data migration, no separate install:

1. Edit `.envs/.production/.self-hosted/.build`:

   ```bash
   VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
   VALIDIBOT_COMMERCIAL_NETRC=/home/validibot/.config/validibot/commercial.netrc
   ```

   Store the credentials from the purchase email in that mode-0600 file:

   ```netrc
   machine pypi.validibot.com
     login <email>
     password <package-key>
   ```

   ```bash
   chmod 600 /home/validibot/.config/validibot/commercial.netrc
   ```

   Compose exposes it only to the package-install build step as a BuildKit
   secret; it is not copied into the image.

2. Edit `.envs/.production/.self-hosted/.django`:

   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production_pro
   ```

3. Redeploy:

   ```bash
   just self-hosted deploy
   just self-hosted doctor
   ```

Pro migrations are additive — they create new tables (teams, guests, signed credentials, MCP, advanced analytics) but never reshape community tables. Existing data, users, workflows, and runs are preserved.

Treat the package key as a credential: keep the netrc out of logs, support
bundles, and source control. Valid access is required when installing or
upgrading the commercial package; the license email explains renewal and
credential rotation.

## Troubleshooting

### A container won't start

```bash
just self-hosted status              # which container is unhealthy?
just self-hosted logs-service web    # what does it say?
just self-hosted logs-service postgres
```

Most common causes:

- **Database connection refused:** `POSTGRES_*` env vars don't match, or Postgres hasn't finished initialising yet. Wait 10 seconds and retry. If using Managed Postgres, confirm trusted-source rules include the Droplet.
- **Docker-compatible socket unavailable:** confirm
  `systemctl --user status docker`, verify
  `test -S "$XDG_RUNTIME_DIR/docker.sock"`, and check that `.build` selects the
  same socket. `just self-hosted doctor --verbose` should report `VB322` as
  rootless. The worker intentionally sees that mount as
  `/var/run/docker.sock` internally.
- **Permission denied on `/app/storage/private`:** confirm Docker's root directory is `/srv/validibot/docker`, then inspect the storage volume from the web container: `docker compose -f docker-compose.production.yml -p validibot exec web ls -ld /app/storage /app/storage/private`. If ownership is wrong, repair it inside the container as root: `docker compose -f docker-compose.production.yml -p validibot exec -u root web chown -R django:django /app/storage`.

### TLS / Let's Encrypt errors

```bash
just self-hosted logs-service caddy
```

Common causes:

- **DNS not propagated:** `dig +short validibot.example.org` from the Droplet should return the Droplet's IP. If not, wait and retry.
- **Port 80 blocked:** Cloud Firewall is missing the HTTP inbound rule. Caddy needs port 80 for the ACME HTTP-01 challenge even if you only serve HTTPS afterwards.
- **Rate limited:** Let's Encrypt rate-limits failed attempts. Wait an hour. Don't keep retrying with broken DNS — that's exactly what `check-dns` is for.

### Database connection refused (Managed Postgres)

```bash
# From the Droplet, try connecting directly:
psql "postgresql://validibot:PASSWORD@your-cluster.db.ondigitalocean.com:25060/validibot?sslmode=verify-full&sslrootcert=/path/to/do-postgres-ca.crt"
```

If this fails: trusted sources don't include the Droplet, the password is wrong, the CA certificate path is wrong, or SSL verification is misconfigured.

### Out of memory

```bash
free -h
docker stats --no-stream
```

Quick fix (buys you headroom for one advanced validator run):

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Long-term fix: resize the Droplet up a tier, or move PostgreSQL to Managed Postgres to free ~300 MB.

## Final install report

When you've completed the core install, capture these for your team's wiki so you (or whoever's on call next) can answer "what's running where?" in seconds:

- Droplet size, region, and IP.
- Reserved IP, if used.
- Volume size and mount point.
- Docker root directory (`docker info --format '{{.DockerRootDir}}'`).
- Validibot version (`git rev-parse HEAD` in `/srv/validibot/repo`) and image digests.
- DNS hostname.
- Backup destination (local path + off-host bucket / restic repo).
- Latest `doctor --json` output.
- Latest smoke-test report.
- Restore-drill date and outcome.
- Pro license details (if applicable) and entitlement expiry.

## See also

- [Self-Hosting Overview](../overview.md) — concepts, two editions, day-to-day operations
- [Install](../install.md) — substrate-generic install steps (AWS EC2, Hetzner, on-prem)
- [Backups](../backups.md) — backup architecture and off-host patterns
- [Restore](../restore.md) — restore drills and the snapshot-vs-backup distinction
- [Security Hardening](../security-hardening.md) — full hardening checklist
- [Pilot Onboarding](../pilot-onboarding.md) — checklist for joint customer install reviews
- [Operator Recipes](../operator-recipes.md) — full `just self-hosted` recipe reference
- [Doctor Check IDs](../doctor-check-ids.md) — every diagnostic check ID and its fix
