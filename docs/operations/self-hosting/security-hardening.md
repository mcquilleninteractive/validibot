# Security Hardening

This page is the practical checklist for hardening a self-hosted Validibot install. It captures the recommended security model for customer-operated deployments.

If you're a risk-averse customer (energy modeling consultancy, research lab, utility reviewer), running through this list is part of going from "deployed" to "production-ready."

## The security model

The default self-hosted profile assumes:

- **customer controls the VM** — Validibot does not require root or kernel-level privileges beyond what Docker needs;
- **customer controls DNS and TLS** — bring your own certificates or use the bundled Caddy profile;
- **customer controls backups** — Validibot ships the recipes but you own where they go;
- **Validibot containers are trusted** application code;
- **validator containers are semi-trusted** and isolated per run (see [Validator Images](validator-images.md));
- **operators choose validator backends they know and understand** — the
  self-hosted product is not a public arbitrary-container execution service;
- **the deployment runs on a network the customer controls**;
- **users may upload untrusted files** — the launch contract validates them at the boundary;
- **admins may install additional validator images** — once self-service registration ships, those go through tier-2 hardening;
- **outbound internet may be restricted** — Validibot does not phone home by default.

## Recommended hardening

### 1. Run behind HTTPS

Use Caddy (bundled) or your own reverse proxy. The kit's Caddyfile uses `SITE_URL` to provision Let's Encrypt certificates on startup.

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

If you bring your own proxy:

- forward to the `web` container on port 8000;
- set `DJANGO_SECURE_PROXY_SSL_HEADER` appropriately in `.django`;
- set `DJANGO_CSRF_TRUSTED_ORIGINS` to include your public origin;
- set `DJANGO_SECURE_SSL_REDIRECT=true`;
- enable HSTS via `DJANGO_SECURE_HSTS_*` settings.

When MCP is enabled, give it a distinct HTTPS origin such as
`https://mcp.example.com`. The production Compose file publishes MCP only on
`127.0.0.1:8001`; an external host proxy forwards that loopback port, while the
bundled Caddy service reaches `mcp:8080` over the private Compose network.
Never publish the MCP HTTP port directly to an untrusted network. Keep
`VALIDIBOT_MCP_BASE_URL` identical in `.django` and `.mcp` so OAuth issuance,
redirects, and audience verification describe the same TLS-protected resource.

### 2. Use strong generated secrets

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

The doctor command (`VB001`-`VB008`) checks for missing, development-default,
or coupled secrets. `DJANGO_API_KEY_DIGEST_KEY` must be different from
`DJANGO_SECRET_KEY`.

### 3. Keep Postgres and Redis private to the Compose network

The default `docker-compose.production.yml` only exposes the `web` container's port (and Caddy's 80/443 if enabled). Postgres and Redis are accessible only inside the Compose network.

If you bind Postgres to the host (e.g. for an external admin tool), bind to `127.0.0.1`, not `0.0.0.0`. Use a SSH tunnel for remote admin access.

### 4. Pin Validibot and validator images by version

Production docs recommend exact-version tags or digest pins, not `latest`.

```bash
# .envs/.production/.self-hosted/.build
VALIDIBOT_IMAGE_TAG=0.8.0          # exact version
VALIDATOR_BACKEND_IMAGE_POLICY=digest   # or signed-digest after configuring cosign verification
```

The doctor command warns if `latest` is used in a self-hosted deployment.

### 5. Run-scoped validator mounts

Validator backends get only their own per-run input/output directories, not the global storage root. See [Validator Images](validator-images.md) for the full layout.

This is a **default**, not a configurable hardening — the old global mount has been removed entirely. The negative-control isolation test in CI would fail if it were re-introduced.

### 6. Disable validator network access by default

Validator containers run with Docker's `network_mode="none"` by default. The
current implementation does not grant network from a validator manifest. An
operator can attach all advanced validators to a named network by explicitly
setting `VALIDATOR_NETWORK`; leave it unset unless a known backend genuinely
needs network access.

Per-validator network capabilities and allowlists are future work. Until that
policy exists, network access is a deployment-wide operator choice.

### 7. Keep the rootless Docker default

The default Docker daemon runs as root, which means its API socket carries
root-equivalent host authority. Rootless Docker runs the daemon and validator
containers inside an unprivileged user's namespace. That meaningfully reduces
the consequence of a worker or container-runtime escape.

Fresh self-hosted installs select the deployment user's standard rootless
socket, `${XDG_RUNTIME_DIR:-/run/user/1000}/docker.sock`. Rootless remains
defence in depth rather than a complete sandbox: operators run known backends
on their own networks, while uploaded files can still be hostile.

To select a rootless Docker daemon:

1. Install and start rootless Docker for the user that owns the Validibot
   deployment, following Docker's
   [rootless-mode guide](https://docs.docker.com/engine/security/rootless/).
   Enable user-service lingering if the daemon must survive logout.
2. Find that user's numeric UID with `id -u`. Confirm its socket exists, for
   example `/run/user/1000/docker.sock`.
3. Confirm `.envs/.production/.self-hosted/.build` resolves the host socket:

   ```dotenv
   VALIDATOR_CONTAINER_SOCKET=${XDG_RUNTIME_DIR:-/run/user/1000}/docker.sock
   ```

   If the deployment user is not UID 1000 and `XDG_RUNTIME_DIR` is unavailable,
   set the exact `/run/user/<uid>/docker.sock` path instead.

4. Run `just self-hosted deploy`, then `just self-hosted doctor --verbose`.
   `VB320` reports the engine version and `VB322` must report a rootless
   engine.
5. Confirm `docker info --format '{{.CgroupVersion}} {{.CgroupDriver}}'`
   reports cgroup v2 with the systemd driver. Rootless Docker can enforce the
   validator CPU, memory, and process limits only when cgroup v2 delegation is
   available; do not treat a host that silently ignores those limits as
   hardened.

The Compose mount keeps `/var/run/docker.sock` as the path *inside* the worker,
so the application needs no special rootless code. Only the host-side socket
path changes.

Existing rootful installations remain supported, but must make that
compatibility choice explicit with
`VALIDATOR_CONTAINER_SOCKET=/var/run/docker.sock`. `check-env` reports the
rootful selection, and doctor check `VB322` records the effective daemon mode.

Rootless networking can behave differently around privileged ports, firewall
rules, and source-IP preservation. Before using the bundled Caddy profile,
verify that ports 80/443 bind correctly and that application logs retain the
client address you expect. An external rootful reverse proxy forwarding to the
web service is a valid alternative.

Rootless Podman exposes a Docker-compatible socket, commonly
`/run/user/<uid>/podman/podman.sock`. It can be selected with the same setting,
but remains an experimental compatibility path until the full advanced
validator acceptance suite passes against the exact Podman release in use.
See Podman's
[system service documentation](https://docs.podman.io/en/stable/markdown/podman-system-service.1.html).

After changing engines, run the real EnergyPlus acceptance path:

```bash
uv run pytest \
  tests/tests_integration/test_docker_compose_execution.py \
  -v -k test_energyplus_execution_via_docker
```

The test requires the EnergyPlus backend image and skips when it is absent.

### 8. Back up database and data storage off-host

The `backups/` directory is on the same VM by default, under the repo checkout. On the recommended DigitalOcean layout that means `/srv/validibot/repo/backups/`, which is durable only because the repo lives on the attached Volume. For production, copy backups off-host. Options: rsync to another machine, restic to S3-compatible storage, cloud provider snapshots (as a complement, not a replacement). See [Backups](backups.md).

### 9. Test restore quarterly

A backup that has never been restored is not considered valid. The doctor command warns if no restore-test marker is recorded (VB411). See [Restore](restore.md) for the drill.

### 10. Keep telemetry off unless explicitly desired

Self-hosted Validibot is **telemetry-off by default**. No product analytics. No license phone-home. No usage reporting.

Allowed outbound calls by default:

- container image pulls during install/upgrade;
- email delivery if configured;
- Let's Encrypt ACME challenges if Caddy profile is enabled.

Sentry error reporting and other diagnostics can be opted in via `.envs/.production/.self-hosted/.django`. None are required.

## What outbound calls happen

The doctor command on self-hosted reports which outbound calls are enabled, so operators can audit. Section by section:

| Outbound call | Default | Purpose |
|---|---|---|
| Container image pulls | enabled | Install and upgrade |
| Email delivery | configured | Outbound app email if you set an SMTP backend |
| Let's Encrypt ACME | enabled if Caddy profile is on | TLS certs |
| Sentry error reporting | off | Optional opt-in for diagnostics |
| PostHog product analytics | off | Not enabled in self-hosted |
| Pro license phone-home | off | Package-index credential is the entitlement gate, not a runtime call |
| x402 public agent registry | off | Cloud-only feature |

## Hardened operating posture

There is not currently a single `self-hosted-hardened` switch that applies
these controls automatically. Risk-averse operators should explicitly:

- use `VALIDATOR_BACKEND_IMAGE_POLICY=digest`, or `signed-digest` after
  configuring cosign verification;
- leave `VALIDATOR_NETWORK` unset;
- keep the rootless socket default and confirm `VB322`;
- keep telemetry integrations unset;
- allow only operator-reviewed validator images.

## Filesystem permissions

The default Compose stack stores app data in Docker named volumes, not in a hand-managed `/srv/validibot/data` tree. If you set up Docker yourself, verify the host paths and container storage boundary:

| Path | Owner | Mode |
|---|---|---|
| `/srv/validibot/` | `root:root` | `755` |
| `/srv/validibot/repo/` | `validibot:validibot` | `755` |
| `/srv/validibot/docker/` | Docker-managed | do not recursively chown |
| `/srv/validibot/repo/.envs/.production/.self-hosted/` | `validibot:validibot` | `700` |
| `/app/storage/private` inside `web` / `worker` | app container user | writable by Django |

Doctor's `VB201` check verifies the in-container data root is writable by the app and not by root only. If it fails on the default Compose stack, inspect the `validibot_storage` Docker volume and the container user rather than recursively changing ownership of the whole Docker data root.

## Network architecture

Recommended firewall configuration:

| Port | Source | Purpose |
|---|---|---|
| `22/tcp` | operator IP ranges only | SSH |
| `80/tcp` | internet | HTTP (redirects to HTTPS) |
| `443/tcp` | internet | HTTPS |
| `5432/tcp` | none (or 127.0.0.1 only) | Postgres |
| `6379/tcp` | none | Redis |
| `5555/tcp` | none | Flower (if enabled) |
| `8000/tcp` | none (proxied via Caddy/your proxy) | Web |
| `8001/tcp` | loopback only (proxied through TLS) | MCP, when enabled |

DigitalOcean's Cloud Firewall is documented in [providers/digitalocean.md](providers/digitalocean.md) with these rules. Other providers: configure equivalently.

## Audit log

Validibot writes audit events for trust-relevant actions:

- workflow access denied;
- workflow execution denied;
- launch rejected by file-type contract;
- launch rejected by step incompatibility;
- validator sandbox policy violation;
- evidence bundle exported;
- evidence bundle export omitted raw content due to retention policy.

Self-hosted operators can query the audit log via the admin UI or the database directly.

## Incident response

If you suspect compromise:

1. Generate a support bundle: `just self-hosted collect-support-bundle`. The bundle is redacted (no secrets, no raw submission contents).
2. Email support@validibot.com with the bundle attached. Pro Team gets 24-hour response; Research/Studio and Organization tiers get 4-hour response.
3. If the issue is severe, take the instance offline (`just self-hosted down`) and preserve the data/database for forensics.
4. Restore from the most recent uncompromised backup (see [Restore](restore.md)).

The support bundle is the trust contract: if a customer can't trust that sending it preserves their data custody, they won't send it. See [Support Bundle](support-bundle.md) for what's included and what's redacted.

## See also

- [Install](install.md) — initial setup
- [Validator Images](validator-images.md) — run-scoped isolation
- [Backups](backups.md) — off-host backup recommendation
- [Restore](restore.md) — quarterly drill
- [Support Bundle](support-bundle.md) — what's redacted
- [Doctor Check IDs](doctor-check-ids.md) — security-relevant checks
- [Trust Architecture (developer-facing)](../../dev_docs/overview/trust-architecture.md)
