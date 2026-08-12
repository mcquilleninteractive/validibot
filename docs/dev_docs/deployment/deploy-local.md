# Run Validibot Locally

This is the fastest way to try Validibot on your own machine. It is the right place to start if you just bought a self-hosting license, want to evaluate the product locally, or need a private sandbox before moving to a server.

Most first-time users should start here.

## When to choose this target

Choose the local target if you want:

- A quick single-machine install for evaluation or development
- The shortest path from clone to running app
- A safe place to learn the product before exposing it on a network

Choose [Deploy with Docker Compose](deploy-docker-compose.md) instead if you want a long-lived server or a deployment that other people can access over the network.

## What this target runs

The local stack uses `docker-compose.local.yml` and the `just local` commands:

- `web` running Django with local code mounted in
- `worker` for background jobs
- `scheduler` for periodic tasks
- `postgres` for the database
- `redis` for the task queue
- `mailpit` for local email capture

On first start, the local web container applies migrations and runs `setup_validibot` automatically.

## Prerequisites

Before you start, make sure you have:

- Docker Desktop or Docker Engine installed
- [git](https://git-scm.com/downloads) installed
- [just](https://just.systems/) installed
- At least 4 GB of RAM available to Docker (8 GB recommended)

## Quick start

```bash
git clone https://github.com/mcquilleninteractive/validibot.git
cd validibot

mkdir -p .envs/.local
cp .envs.example/.local/.django .envs/.local/.django
cp .envs.example/.local/.postgres .envs/.local/.postgres

# Also copy the optional commercial-package build config. Safe to copy for
# community-only use — every variable has a sensible default.
cp .envs.example/.local/.build .envs/.local/.build

# Now set the three required values in .envs/.local/.django (see below),
# then start the stack:
just local up
```

### Set the required values

Open `.envs/.local/.django` and replace the three `!!!SET...!!!` placeholders.
**Local settings raise an error and the app will not start** if the secret key or
MFA key is missing:

| Variable | What it is | Generate it with |
| -------- | ---------- | ---------------- |
| `DJANGO_SECRET_KEY` | Django signing key | `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DJANGO_MFA_ENCRYPTION_KEY` | Fernet key that encrypts MFA secrets — must be a valid Fernet key | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SUPERUSER_PASSWORD` | Your admin login password | choose a strong value |

`.envs/.local/.postgres` works as-is — no changes needed for local development.

> No local Python with `cryptography`? Generate the Fernet key with Docker:
> `docker run --rm python:3.13-slim sh -c "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"`

Once the containers are up:

- Open `http://localhost:8000`
- Sign in with the admin credentials from `.envs/.local/.django`
- Use `http://localhost:8025` to inspect locally captured emails

## If you purchased Pro or Enterprise

Local Docker builds can optionally bake a commercial package into the image. Do that before your first `just local up`, or run `just local build` afterwards to rebuild the stack.

If you already copied `.envs.example/.local/.build` in the Quick start above, just edit `.envs/.local/.build`. Otherwise:

```bash
cp .envs.example/.local/.build .envs/.local/.build
```

Then edit `.envs/.local/.build` and set:

```bash
VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
VALIDIBOT_COMMERCIAL_NETRC=/absolute/path/to/commercial.netrc
```

Use `validibot-enterprise==<version>` instead of `validibot-pro==<version>` if
you purchased Enterprise. You can also use a quoted exact wheel URL on
`pypi.validibot.com` that includes `#sha256=<hash>` instead of a package name
and version.

The netrc must contain the `pypi.validibot.com` login and package key and have
mode 0600. Compose mounts it only into the package-install build step.

Then point Django at the Pro-activating settings module by setting
`DJANGO_SETTINGS_MODULE` in `.envs/.local/.django`:

```bash
DJANGO_SETTINGS_MODULE=config.settings.local_pro
```

That's all — the settings module adds `validibot_pro` to
`INSTALLED_APPS`, which is what Django needs in order to import the
package and run its license-registration hook. Do not patch
`config/settings/base.py` directly (that makes upgrades messier);
the dedicated settings module is the supported path.

Enterprise will follow the same pattern when its settings module
lands (a `config.settings.local_enterprise` that appends both
`validibot_pro` and `validibot_enterprise`). That module doesn't
exist yet — today the supported tiers for local development are
community (`local`) and Pro (`local_pro`).

## Include the MCP server

MCP is embedded in the normal Django ASGI application. Its implementation is
public Community code, but `config.asgi` mounts `/mcp` only when the installed
commercial package registers the `mcp_server` feature.

After selecting `config.settings.local_pro` as described above, restart the Pro
stack:

```bash
just local-pro up --build
```

The endpoint is `http://localhost:8000/mcp`, on the same web port as Django.
There is no `.mcp` env file, Compose profile, service key, or second container.
The Community `just local up` stack leaves the route unmounted.

For OAuth and official-client test instructions, see [MCP Server](../mcp/index.md).

## Enable signed credentials locally

If you want to test the signed credential action locally, generate a small
local signing key and point `SIGNING_KEY_PATH` at it.

Create the key on the host:

```bash
mkdir -p .envs/.local/keys
openssl ecparam -name prime256v1 -genkey -noout \
  -out .envs/.local/keys/credential-signing.pem
chmod 600 .envs/.local/keys/credential-signing.pem
```

Then add this to `.envs/.local/.django`:

```bash
SIGNING_KEY_PATH=/run/validibot-keys/credential-signing.pem
CREDENTIAL_ISSUER_URL=http://localhost:8000
```

After updating the env file, rebuild or restart the stack:

```bash
just local build
just local up
```

After migrations have run, register the key's public half once. Registration
stores no private material and is idempotent:

```bash
python manage.py register_signing_key \
  --local-private-key .envs/.local/keys/credential-signing.pem
python manage.py signing_key_status
```

## Verify the install

Run these checks after the stack starts:

```bash
just local ps
curl http://localhost:8000/health/
just local manage "check_validibot"
```

If you want more detail while the app is starting, use:

```bash
just local logs
```

## Common local commands

```bash
just local up
just local down
just local build
just local logs
just local migrate
just local manage "check_validibot"
just local manage "createsuperuser"
```

See [Justfile Guide](justfile-guide.md) for the full command reference.

## Advanced validators locally

Built-in validators (JSON Schema, XML Schema, Tabular, and so on) work as soon as the local stack is running. Isolated validators such as EnergyPlus, FMU, SHACL, Schematron, and Portfolio Manager run as sibling containers that the worker launches on demand, so you also need the relevant validator image available on the Docker host. If the image is missing, only that advanced validation fails — the rest of the app keeps working, and the `just` doctor and test recipes tell you which image to build.

These images live in a separate repo and build with one command — no registry, login, or push needed for local use:

```bash
git clone https://github.com/mcquilleninteractive/validibot-validator-backends.git
cd validibot-validator-backends
just build-all          # or build one: just build energyplus
```

This produces images named `validibot-validator-backend-<slug>:latest` (image slugs: `energyplus`, `fmu`, `shacl`, `schematron`, `portfolio-manager`). Build the Portfolio Manager source with `just build portfolio_manager`; its image name is normalized to the hyphenated slug. The worker finds each one by that name automatically — there's nothing to configure.

For consistency with the production stack, only the local `worker` service gets Docker socket access. The `web` and `scheduler` containers do not need it.

For more detail — per-backend notes, the container security model, and registry-based deployment — see:

- [Docker Setup](../docker.md) (the "Advanced validators" section)
- [Execution Backends](../overview/execution_backends.md)

## A note on `local-cloud`

You may notice `just local-cloud ...` recipes in the justfile. Those drive a separate development workflow for the hosted Validibot Cloud product and are not used for self-hosting — ignore them.

## Where to go next

Once you are comfortable running locally:

- Move to [Deploy with Docker Compose](deploy-docker-compose.md) for a single-host production deployment
- Move to [Deploy to GCP](deploy-gcp.md) if you want a managed cloud deployment
- Read [Environment Configuration](environment-configuration.md) for the env file structure
