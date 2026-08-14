# Running Validibot with Docker

Validibot runs entirely in Docker containers for local development — Django, a
Celery worker, a scheduler, Postgres, Redis, and a local mail catcher. You don't
need Python, Postgres, or Node installed on your machine; Docker handles
everything.

This page gets you from a fresh machine to a running app in about ten minutes.

## Before you start

You need three things installed:

| Tool | What it's for | Get it |
| ---- | ------------- | ------ |
| **Docker Desktop** (Mac/Windows) or **Docker Engine** / **Podman** (Linux) | Runs all the containers. Make sure it's actually started before you continue. | <https://docs.docker.com/get-docker/> |
| **just** | The command runner. Every shortcut below (`just local up`) comes from it. | <https://just.systems/> |
| **git** | To clone the repository. | <https://git-scm.com/downloads> |

You'll also want **4 GB of RAM free for Docker (8 GB recommended)** and these
ports available: **8000** (web), **8025** (mail). `just local up` checks these
two for you and stops with a friendly message if something else is using them.

> **What is `just`?** It's a small command runner (think "Make, but friendlier").
> Validibot's commands live in a `justfile` at the repo root, so you run short,
> memorable commands like `just local up` instead of long `docker compose`
> invocations. Install it once (`brew install just`, or see
> <https://just.systems/>); the [How the `just` commands work](#how-the-just-commands-work)
> section below explains what they do under the hood.

## Step 1 — Get the code

```bash
git clone https://github.com/mcquilleninteractive/validibot.git
cd validibot
```

Run every command below from inside this `validibot` folder.

## Step 2 — Create your env files

These hold your local settings and secrets. Copy them from the examples:

```bash
mkdir -p .envs/.local
cp .envs.example/.local/.django   .envs/.local/.django
cp .envs.example/.local/.postgres .envs/.local/.postgres
```

> ⚠️ The `.envs/` folder holds real secrets and is gitignored. Never commit it.
> See [Environment Configuration](deployment/environment-configuration.md) for
> details. (Pro/Enterprise users also copy `.build` — see [Going further](#going-further).)

## Step 3 — Set the three required values

Open `.envs/.local/.django` and replace the three `!!!SET...!!!` placeholders.
**The app will not start in local development without all three** — the local
settings raise an error if the secret key or MFA key is missing.

| Variable | What it is | Generate it with |
| -------- | ---------- | ---------------- |
| `DJANGO_SECRET_KEY` | Django signing key | `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DJANGO_MFA_ENCRYPTION_KEY` | Fernet key that encrypts MFA secrets. Must be a valid Fernet key, not just any random string. | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SUPERUSER_PASSWORD` | Your admin login password | Pick a strong one — this is how you'll sign in |

> No local Python with `cryptography`? Generate the Fernet key with Docker, which
> you already have:
>
> ```bash
> docker run --rm python:3.13-slim sh -c "pip install -q cryptography && \
>   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
> ```

**`.envs/.local/.postgres` needs no edits** — its defaults
(`validibot` / `validibot`) work as-is for local development.

## Step 4 — Build and start

```bash
just local up
```

…which runs:

```bash
docker compose -f docker-compose.local.yml up -d
```

**The first run builds the images and downloads base layers — expect a few
minutes.** Later starts are fast. (Need to rebuild after changing dependencies or
a Dockerfile? Use `just local build`.)

On first start the web container automatically applies database migrations and
runs `setup_validibot`, which configures site settings, roles, the built-in
validators, **and creates your admin user** from the `SUPERUSER_*` values. You
don't need to run anything else.

## Step 5 — Open the app

Go to **http://localhost:8000** and sign in:

- **Username:** `admin` (the `SUPERUSER_USERNAME` default)
- **Password:** the `SUPERUSER_PASSWORD` you set in step 3

That's it — you're running Validibot. 🎉

## Did it work?

Quick checks once the stack is up:

```bash
just local ps                          # all services should be "Up"
curl http://localhost:8000/health/     # should return OK
just local manage "check_validibot"    # confirms setup is correct
```

- **App:** http://localhost:8000
- **Captured emails:** http://localhost:8025 (Mailpit — every email the app
  "sends" locally lands here instead of a real inbox)

## How the `just` commands work

You don't need this to get running, but it helps to know what `just` is doing.

The repo root has a `justfile` that acts as an orchestrator. It pulls in
shared helpers (`import 'just/common.just'`) and wires up each command group as a
module — `mod local 'just/local'` is what gives you `just local <command>`.
There are sibling groups for the other workflows: `just local-pro`,
`just local-cloud`, `just self-hosted`, `just gcp`. Run plain `just` to see the
menu, or `just --list` for everything.

For local development, the recipes in `just/local/mod.just` are thin wrappers
around Docker Compose, always run from the repo root so relative paths resolve:

| Command | What it actually runs |
| ------- | --------------------- |
| `just local up` | Pre-checks ports 8000/8025, then `docker compose -f docker-compose.local.yml up -d` (builds automatically on first run). |
| `just local build` | `docker compose … up -d --build` — rebuild after dependency or Dockerfile changes. |
| `just local rebuild` | Same, but also wipes and repopulates the venv volume (use after dependency upgrades). |
| `just local down` | Stop containers; data is kept. |
| `just local logs` | Follow logs from all services. |
| `just local ps` | Container status. |
| `just local migrate` | Run `manage.py migrate` in the web container. |
| `just local manage "<cmd>"` | Run any `manage.py` command (e.g. `createsuperuser`). |
| `just local clean` | Stop and **delete all data** (volumes removed). |

One nicety: if `.envs/.local/.build` exists, the recipes automatically pass it as
`--env-file`; if it doesn't, they skip it. So community users never have to think
about that file. Full reference: [Justfile Guide](deployment/justfile-guide.md).

## What's running

| Service | What it does |
| ------- | ------------ |
| `web` | The Django app (dev server on 8000, your code hot-reloads). |
| `worker` | Celery worker for background jobs; also launches advanced validator containers via the Docker socket. |
| `scheduler` | Celery Beat — periodic jobs like cleanup and data expiry. |
| `postgres` | The database. |
| `redis` | Task queue for Celery. |
| `mailpit` | Captures local email at http://localhost:8025. |

Entrypoint and start scripts live in `compose/local/django/` and wait for
Postgres before launching.

## Troubleshooting first-run issues

**"Cannot connect to the Docker daemon"** — Docker Desktop isn't running. Start
it, wait for it to settle, then retry.

**"Port 8000 / 8025 is in use"** — `just local up` detected a leftover process
and stopped. Kill the process it names, then re-run `just local up`.

**The web container exits right after starting** — almost always a missing or
invalid value from step 3. Check `just local logs web`; a missing
`DJANGO_SECRET_KEY` or `DJANGO_MFA_ENCRYPTION_KEY` (or an MFA key that isn't a
valid Fernet key) raises a clear error at startup.

**First build is very slow** — normal the first time; it's cached afterward.
Watch progress with `just local logs`.

**The page loads but looks unstyled** — prebuilt CSS/JS ships in the repo, so this
is rare. You only need Node if you're *editing* SCSS/TypeScript: run
`npm install && npm run build` on the host to regenerate the assets.

**Start completely clean** — `just local clean` removes containers *and data*,
then `just local up` rebuilds from scratch. See also
[Reset an Environment](how-to/reset-an-environment.md).

## Advanced validators (optional — skip for your first run)

**You don't need these to get started.** The built-in validators (JSON Schema,
XML Schema, Tabular, etc.) run inside the Django process and work the moment the
stack is up.

Isolated validators — **EnergyPlus**, **FMU**, **SHACL**, **Schematron**,
**Portfolio Manager**, and **PDF** — run as separate sibling containers that
the `worker` launches on demand. The matching image must already exist on your Docker host.
They live in a separate repo and build with one command — no registry, login,
or push needed for local use:

```bash
git clone https://github.com/mcquilleninteractive/validibot-validator-backends.git
cd validibot-validator-backends
just build-all          # or build one: just build energyplus
```

This produces images named `validibot-validator-backend-<slug>:latest` (image
slugs: `energyplus`, `fmu`, `shacl`, `schematron`, `portfolio-manager`, `pdf`).
The Portfolio Manager source directory and `just build` argument use
`portfolio_manager`; its OCI/GCP/Docker image slug uses the normalized
hyphenated form. PDF uses `just build pdf`. The worker finds each image **by
name automatically** — there's nothing to configure. By default these
containers run with **no network access** for safety
(they exchange files through a shared storage volume); uncomment
`VALIDATOR_NETWORK` in the compose file only if a validator genuinely needs the
internet.

The worker keeps the full storage volume, but each validator container receives
only one execution attempt's read-only input directory and writable output
directory. Retries use a new workspace, so a failed attempt's files cannot
satisfy or be overwritten by the retry.

> ⚠️ Only run validator backend images you build and control yourself — they
> execute with access to your validation data.

For registry-based deployment (build-and-push to GCP Artifact Registry, etc.) and
per-backend details, see the `validibot-validator-backends` README and
[Execution Backends](overview/execution_backends.md).

## Going further

- **Pro / Enterprise, the MCP server, signed credentials:** these are opt-in and
  documented in [Run Validibot Locally](deployment/deploy-local.md) (copy
  `.envs.example/.local/.build`, set `VALIDIBOT_COMMERCIAL_PACKAGE`, and use
  `just local-pro up`).
- **Production-style stack** (Gunicorn, no code mount, for parity testing):
  `docker-compose.production.yml` via `just self-hosted bootstrap`. See
  [Deploy with Docker Compose](deployment/deploy-docker-compose.md).
- **VS Code test runner:** the repo ships `.vscode/.env` pointing pytest at the
  Docker Postgres. If the Testing panel hangs, confirm the interpreter is
  `.venv/bin/python` and that Postgres is up (`just local up`).

> **Note on `local-cloud`:** if you see `just local-cloud ...` recipes, those drive
> the separate hosted-Cloud workflow, not the self-hosted path — you can ignore
> them.

## Where things live

- `compose/local/django/Dockerfile`: base image for local dev (includes dev extras).
- `compose/production/django/Dockerfile`: base image for production (no dev extras).
- `docker-compose.local.yml`: local dev (runserver, code mounted) with web + worker + scheduler + postgres + redis + mailpit.
- `docker-compose.production.yml`: production-like (gunicorn, no code mount).
- `just/local/mod.just`: the local development recipes (`up`, `down`, `build`, `logs`, …).
- `compose/local/django/entrypoint.sh` and `start.sh`: wait for DB, fix Docker socket permissions, run migrations, first-run setup, start dev server on 8000.
- `compose/production/django/entrypoint.sh` and `start.sh`: wait for DB, fix Docker socket permissions if mounted, collectstatic, skip setup until migrations exist, then start Gunicorn on 8000.
