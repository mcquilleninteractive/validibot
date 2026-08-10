# Troubleshooting

This page captures the common operator issues and how to diagnose them. The first thing to try for any issue is `just self-hosted doctor` — it covers most cases. If doctor doesn't surface the problem, the patterns below are next.

## "doctor told me there's a problem"

The doctor command's check IDs are documented at [doctor-check-ids.md](doctor-check-ids.md). Each ID has a stable meaning, a severity, and a suggested fix. Examples:

- `VB002 SECRET_KEY weak or missing` — set `DJANGO_SECRET_KEY` in `.envs/.production/.self-hosted/.django`.
- `VB008 API key digest key missing or coupled` — set `DJANGO_API_KEY_DIGEST_KEY` in `.envs/.production/.self-hosted/.django` to a separate generated value.
- `VB201 storage root not writable` — inspect the path doctor prints inside the `web` container; on the default stack it should be `/app/storage/private` backed by the `validibot_storage` Docker volume.
- `VB411 backups configured but no restore test recorded` — run a restore drill (see [restore.md](restore.md)).
- `VB320 Docker version below minimum` — upgrade Docker Engine.
- `VB322 rootful container engine` — supported only as an explicit
  compatibility posture; fresh installs default to rootless Docker.

`doctor --json` gives machine-readable output that's easy to grep or pipe.

## "the page won't load"

Check service status:

```bash
just self-hosted status
```

Expected services running: `web`, `worker`, `postgres`, `redis`, `scheduler`, optionally `caddy`, optionally `mcp`.

If a service isn't running:

```bash
just self-hosted logs <service-name>
```

Common causes:

- **`web` failing to start** — usually a missing required env setting. Doctor's VB001-VB099 range covers these. Check `.django` against `.envs.example/.production/.self-hosted/.django`.
- **`postgres` failing to start** — usually a permissions issue on the data volume. `docker compose logs postgres` will show the specific error.
- **`worker` running but not picking up tasks** — Redis unreachable. `just self-hosted health-check` will catch this.

## "I ran Compose before relocating Docker's data root"

The recommended DigitalOcean layout moves Docker's data root to `/srv/validibot/docker` before the first `docker compose` run. If you accidentally ran Compose while Docker still used `/var/lib/docker`, choose one recovery path before putting real data into the instance.

For a pre-bootstrap or disposable install, destroy the accidentally created volumes and restart from the corrected data root:

```bash
docker compose -f docker-compose.production.yml -p validibot down -v
systemctl --user stop docker
sudo install -d -o "$(id -un)" -g "$(id -gn)" /srv/validibot/docker
install -d -m 0700 ~/.config/docker
tee ~/.config/docker/daemon.json > /dev/null <<'EOF'
{
  "data-root": "/srv/validibot/docker"
}
EOF
systemctl --user start docker
docker info --format '{{.DockerRootDir}}'
```

The final command must print `/srv/validibot/docker` before you run `just self-hosted bootstrap` or `just self-hosted deploy` again.

If the instance already contains data you need to keep, do not run `down -v`. Take `just self-hosted backup` first, copy the backup off-host, then restore onto a fresh host prepared with the correct Docker data root. Manual migration of `/var/lib/docker/volumes/validibot_*` is possible only during a maintenance window with Docker stopped, and is easier to get wrong than a backup-and-restore drill.

## "validations are queueing but not running"

The worker is up but not processing tasks. Likely causes:

1. **Container-engine socket unreachable from worker container.** The worker
   dispatches advanced validators through the Docker API. Check
   `just self-hosted doctor` for `VB302` (API availability). Confirm
   `VALIDATOR_CONTAINER_SOCKET` names the deployment user's existing rootless
   socket, normally `$XDG_RUNTIME_DIR/docker.sock`.
2. **Validator image not built or present.** Run `just self-hosted validators`. If an image is missing, build it with `just self-hosted validator-build <name>` or trigger the relevant validator-image deployment path.
3. **Validator manifest missing.** Less common; usually shows up in worker logs as a `ValidatorNotFound` error.
4. **Storage permissions wrong.** The worker can't materialise the per-attempt workspace. Check `VB201`.

## "validator backend container exited but no result"

The advanced-validator container exited but no `output.json` appeared, or the orchestrator marked the run as `ERROR`. Use the implicit-sentinel table from [validator-images.md](validator-images.md) to interpret:

| State | Likely cause |
|---|---|
| `output.json` absent + container exit code ≠ 0 | OOM, segfault, image pull error, callback timeout. Check `docker logs <container>` and `dmesg` for OOM. |
| `output.json` absent + container exit code 0 | Backend bug; the container completed without writing the envelope. Filed as a bug against the validator backend. |
| `output.json` present but unparseable | Backend bug; envelope schema mismatch. Filed as a bug against the validator backend. |

The orchestrator distinguishes these via the run's status: `RuntimeError` vs `SystemError`. The audit log records which.

## "Pro features show as locked even though Pro is installed"

Pro features unlock by import-time feature registration. If they're not registering:

1. Check that `validibot_pro` is in `INSTALLED_APPS`. The settings module is `config.settings.production_pro` for Pro; `config.settings.production` is community-only.
2. Check that the `validibot-pro` package is installed in the image. `docker compose exec web pip show validibot-pro` should show the version.
3. Check that the build pulled the wheel successfully. `docker compose logs web` during startup will mention package registration.
4. Run `just self-hosted doctor` — `VB070` family covers Pro feature registration.

If you bought a license but haven't applied it yet, see [install.md § Activating Pro](install.md#activating-pro).

## "I made a config change and the stack won't come back up"

Most likely you broke an env file. Check:

1. `just self-hosted check-env` — does it parse?
2. `just self-hosted doctor` — does it identify the missing/invalid setting?

If the config change was meant to add a new feature, check the corresponding operator guide first. External managed Postgres is supported only with careful TLS settings; self-hosted S3-compatible object storage is not yet an operator-supported path. Component-selective restore is not implemented, so roll env files back from your config-management system or another copy of `.envs/.production/.self-hosted/`. See [restore.md](restore.md).

## "the upgrade failed half-way through"

The `upgrade` recipe is idempotent. After fixing the underlying issue (network, disk space, whatever), re-run:

```bash
just self-hosted upgrade --to v0.9.0
```

It picks up at the failed step. Each step prints `starting / done / skipped (already done)`.

If you can't recover at the failed step (e.g. a destructive migration corrupted state):

```bash
just self-hosted down
just self-hosted restore backups/<pre-upgrade-timestamp>
$EDITOR .envs/.production/.self-hosted/.build  # set VALIDIBOT_IMAGE_TAG back to the old version
just self-hosted deploy
```

The upgrade recipe prints the backup path before proceeding so you know which to restore. See [upgrades.md](upgrades.md).

## "the dashboard is slow"

Most likely culprits:

1. **Postgres is undersized.** Check `docker stats` while the dashboard loads. If Postgres is hitting CPU or memory limits, scale up the VM or move Postgres external.
2. **Redis is full.** Less common; Redis is mostly used for the Celery broker.
3. **Storage is full.** Check `df -h /srv/validibot`. Validation runs and evidence accumulate; `just self-hosted cleanup` should be on a cron.
4. **Caddy is slow on TLS handshakes.** Check the Caddy logs; sometimes Let's Encrypt rate-limits during certificate renewal.

The doctor `VB100` family covers database health and `VB200` covers storage.

## "errors are appearing in the log"

For a quick scan of recent errors:

```bash
just self-hosted errors-since 1h
just self-hosted errors-since 24h
```

This greps the last N units of `docker compose logs` for ERROR / EXCEPTION / TRACEBACK across all services. Pattern adopted from GitLab's `gitlab-ctl tail`.

If you're chasing a specific error, use the underlying Compose tooling:

```bash
docker compose logs web --since 1h | grep -i error
docker compose logs worker --since 24h | grep -A 20 "Traceback"
```

## "I lost the .envs files"

Two paths:

1. **Restore from config management or an off-host copy.** Validibot application backups do not include secret env files, so use the system where you keep `.envs/.production/.self-hosted/`:

   ```bash
   rsync -a off-host:/path/to/.self-hosted/ .envs/.production/.self-hosted/
   ```

2. **Recreate from templates.** Copy `.envs.example/.production/.self-hosted/` to `.envs/.production/.self-hosted/` and re-edit each file. Generate fresh secrets with the commands from [install.md](install.md). **You will need to re-encrypt user MFA secrets** — the new `DJANGO_MFA_ENCRYPTION_KEY` won't decrypt the old ones. Plan to ask all MFA users to re-enroll, or restore from a backup with the correct key.

This is one of the reasons to keep backups off-host.

## "I need to migrate to a bigger VM"

```bash
# On the source host:
just self-hosted backup

# Copy the backup directory to the new host:
scp -r backups/<timestamp> validibot@new-host:/srv/validibot/repo/backups/

# On the new host (after install steps):
just self-hosted restore backups/<timestamp>
just self-hosted doctor
just self-hosted smoke-test

# Update DNS to point at the new host
```

See [restore.md](restore.md) § Restoring on a different host for the full flow.

## "I need to send a support ticket"

```bash
just self-hosted collect-support-bundle
```

Email the resulting zip to support@validibot.com. The bundle is redacted — no secrets, no signing keys, no submission contents. See [support-bundle.md](support-bundle.md) for what's in it.

Without the bundle, response time is 1 week. With the bundle, response time is 24 hours (Pro Team) or 4 hours (Research/Studio, Organization).

## When to escalate

- the doctor command shows `FATAL` findings;
- evidence bundles can't be exported (the trust contract is failing);
- validator backends consistently fail with `SystemError` (platform issue, not user data issue);
- you can't restore from a backup;
- you suspect compromise (unexpected outbound calls, unfamiliar processes, mysterious database changes).

For compromise specifically: take the instance offline (`just self-hosted down`), preserve the data and database for forensics, and email support immediately.

## See also

- [Doctor Check IDs](doctor-check-ids.md) — every check ID and its fix
- [Install](install.md) — initial setup
- [Configuration](configuration.md) — env file reference
- [Backups](backups.md) and [Restore](restore.md) — recovery paths
- [Upgrades](upgrades.md) — upgrade lifecycle and rollback
- [Support Bundle](support-bundle.md) — what to send to support
