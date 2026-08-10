# Paid pilot onboarding checklist

This is the operator-facing checklist for taking a paid pilot from "we just bought" to "we have a working, supportable Validibot install we trust." It is what we walk through together during the install-review session that comes with every paid tier.

**Audience:** customer's operator, on the phone with us during the first install review.
**Time budget:** 60–90 minutes.
**Outcome:** every box checked, with the artefacts archived in the customer's documentation.

## Before the call

Customer needs to have:

- [ ] A Linux VM provisioned (DigitalOcean, AWS EC2, Hetzner, on-prem — anything that runs Docker)
- [ ] A domain name with DNS pointed at the VM (`validibot.example.com`)
- [ ] SSH access to the VM
- [ ] The Validibot Pro package URL + license token from the purchase email
- [ ] `git`, `docker`, `docker compose`, and `just` installed on the VM (or willing to install during the call)

If the customer is using DigitalOcean specifically, also pre-read the [DigitalOcean provider guide](providers/digitalocean.md). Other providers, the [overview](overview.md) is the entry point.

## Step 1 — Install (15 minutes)

- [ ] Clone the repo to `/srv/validibot/repo` (or wherever the customer's convention puts deployments)
- [ ] Copy env templates and edit:
  ```bash
  mkdir -p .envs/.production
  cp -r .envs.example/.production/.self-hosted/ .envs/.production/.self-hosted/
  $EDITOR .envs/.production/.self-hosted/.django  # SITE_URL, DJANGO_ALLOWED_HOSTS, secrets
  $EDITOR .envs/.production/.self-hosted/.postgres  # POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
  ```
- [ ] If Pro: also edit `.envs/.production/.self-hosted/.build` to set `VALIDIBOT_COMMERCIAL_PACKAGE` and `VALIDIBOT_COMMERCIAL_NETRC` from the purchase email
- [ ] Run `just self-hosted check-env` — exits 0
- [ ] Run `just self-hosted bootstrap`
- [ ] Visit `https://<their-site-url>/` in a browser — login page loads

## Step 2 — DigitalOcean resource review (5 minutes; skip if other provider)

If the customer is on DigitalOcean, walk through the provider-specific checklist together — most of these have a one-time setup cost and pay back many times over:

- [ ] Droplet size matches workload (paid pilot baseline: 4 vCPU / 8-16 GB / 200 GB volume mounted at `/srv/validibot`)
- [ ] Docker data root is on the mounted Volume (`docker info --format '{{ .DockerRootDir }}'` prints `/srv/validibot/docker`)
- [ ] DigitalOcean Cloud Firewall configured (`22` from operator IPs only, `80`/`443` from anywhere, everything else denied)
- [ ] DigitalOcean automatic Droplet backups enabled only as OS-level insurance; they do not include attached Volumes, so Validibot backups plus off-host replication are still required
- [ ] DigitalOcean monitoring agent installed (free, gives Validibot doctor's `--provider digitalocean` checks something to consume)
- [ ] DNS A-record at the customer's DNS provider points to a Reserved IP assigned to the Droplet, or directly to the Droplet public IP for evaluation installs

For other providers, replace these with the equivalent: VPC firewall rules, infrastructure-level snapshots, host-OS metric collection, DNS record.

## Step 3 — Doctor (5 minutes)

The doctor pre-flight catches configuration problems before they bite during real work.

- [ ] `just self-hosted doctor` — every check is `OK`, `INFO`, `WARN`, or `SKIPPED`. Zero `ERROR` or `FATAL`.
- [ ] If `--provider digitalocean` is appropriate: `just self-hosted doctor --provider digitalocean` also clean
- [ ] Walk through any `WARN`-level findings together. Some are expected (e.g. VB411 "no restore drill recorded" — Step 5 below clears this)
- [ ] Save the JSON output: `just self-hosted doctor --json > install-doctor-baseline.json`. Commit this to the customer's runbook so they have a healthy baseline to compare future doctor runs against.

## Step 4 — Backup (5 minutes)

Take a baseline backup. This is the rollback insurance for everything that follows.

- [ ] `just self-hosted backup` — completes, prints a backup root path
- [ ] Inspect the bundle: `ls -la backups/<id>/` — should show `manifest.json`, `db.sql.zst`, `data.tar.zst`, `checksums.sha256`
- [ ] `(cd backups/<id> && sha256sum -c checksums.sha256)` — all three OK
- [ ] **Off-host copy**: rsync, restic, or upload to S3/GCS — pick whichever fits the customer's existing backup story. Confirm with `ls` on the off-host destination.

## Step 5 — Restore drill (15-20 minutes)

A backup that hasn't been restored isn't proven to work. The restore drill is the highest-leverage step in this whole checklist.

- [ ] Spin up a temporary restore environment — easiest is a second Droplet (cheap, disposable). Could also be a fresh local Compose stack on the operator's laptop.
- [ ] Copy `backups/<id>/` from production to the restore environment
- [ ] On the restore environment: clone Validibot, copy env templates, edit (using the production values is fine for a drill), `just self-hosted bootstrap`
- [ ] `just self-hosted restore backups/<id>` — confirms with the hostname, walks the four pre-flight gates, completes
- [ ] On the restore environment: `just self-hosted doctor` — VB411 should now report OK (the restore wrote `.last-restore-test`)
- [ ] On the restore environment: `just self-hosted smoke-test` — passes
- [ ] Tear down the restore environment

This step is what proves the backup-and-restore *cycle* works. Without it, the customer has backup files of unknown utility.

## Step 6 — Demo workflow (5 minutes)

The smoke test confirms the pipeline works end-to-end against a built-in JSON Schema validator. Walk the operator through what just happened:

- [ ] `just self-hosted smoke-test` on the production Droplet — passes
- [ ] Open the Validibot UI, navigate to the smoke-test demo workflow (`Smoke Test JSON Schema [Demo]`)
- [ ] Inspect the most recent run — show how the JSON output, the validator inventory, and the run history all hang together

## Step 7 — Customer's own workflow (15-30 minutes)

The customer brought Validibot for a reason. Now is when they prove it solves their actual problem.

- [ ] Customer creates their first real workflow (or imports one from a template)
- [ ] Customer uploads a representative submission
- [ ] Run completes — the validator chain works as expected
- [ ] If the customer is on Pro and uses advanced validators (EnergyPlus, FMU): build the validator backend image with `just self-hosted validator-build energyplus`, then re-run

This is the hands-on portion. Time-box it; if the customer's workflow is unusually complex, schedule a follow-up rather than burning the whole call on it.

## Step 8 — Evidence export (5 minutes; Pro only)

If the customer has Pro signing enabled, walk through how a signed credential is generated and verified.

- [ ] Run a workflow that includes a "signed credential" step
- [ ] Show the customer where the signed credential lives (in the run's evidence bundle)
- [ ] Verify it externally: `curl https://<site>/.well-known/jwks.json` returns the deployment's public key
- [ ] Show how a downstream consumer verifies the credential against that JWKS endpoint

For community deployments, skip this step.

## Step 9 — Support bundle dry-run (5 minutes)

Make sure the customer knows how to send us a support bundle *before* they need to.

- [ ] `just self-hosted collect-support-bundle` — produces a zip
- [ ] Inspect: `unzip -l support-bundles/support-bundle-*.zip` — shows `app-snapshot.json`, logs, etc.
- [ ] Review the README inside the zip together so the customer sees the redaction story
- [ ] Confirm the customer knows the support email + SLA tier — see [support-bundle.md](support-bundle.md#the-support-workflow-as-a-contract)

## After the call

Each customer's runbook should include:

- [ ] The path to their Validibot deployment + how to SSH in
- [ ] The path to their offsite backup destination
- [ ] The healthy baseline `doctor --json` output (from Step 3)
- [ ] Their cron / systemd schedule for `just self-hosted backup` and `just self-hosted cleanup`
- [ ] The support email + SLA reminder (with their tier)
- [ ] Names + contact details of the operators trained on the deployment
- [ ] Quarterly restore-drill calendar invite (to repeat Step 5)

Send the customer a copy of all this — partly so they can hold it, partly so the next operator at their company doesn't start from scratch when this one moves teams.

## See also

- [Overview](overview.md) — what self-hosted Validibot is
- [Install](install.md) — step-by-step first install
- [DigitalOcean provider guide](providers/digitalocean.md) — provider-specific setup
- [Backups](backups.md) — what gets captured + retention
- [Restore](restore.md) — restore drill walkthrough
- [Support bundle](support-bundle.md) — how the support workflow uses the bundle
- [Doctor check IDs](doctor-check-ids.md) — what each warning means + fix hints
