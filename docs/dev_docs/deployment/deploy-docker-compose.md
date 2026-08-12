# Deploy with Docker Compose

This is the main self-hosted production target for Validibot. Use it when you want to run Validibot on a VPS, a single cloud VM, an on-prem server, or any host you control with Docker.

For most self-hosted customers, this is the best production path.

## When to choose this target

Choose Docker Compose if you want:

- A production-style deployment on infrastructure you control
- A simpler alternative to Kubernetes
- A good fit for DigitalOcean, Hetzner, EC2, or on-prem servers
- A deployment that can stay online for real users behind a reverse proxy

Choose [Run Validibot Locally](deploy-local.md) instead if you only want to evaluate the product on your laptop.

## What this target runs

The Docker Compose production stack uses `docker-compose.production.yml` and the `just self-hosted ...` commands.

It runs:

- `web` with Gunicorn and `UvicornWorker` (including `/mcp` on Pro)
- `worker` for background jobs and validator execution
- `scheduler` for periodic tasks
- `postgres`
- `redis`

One optional service lives behind a Compose profile and stays off unless
you opt in:

- `caddy` — a bundled reverse proxy that auto-issues a Let's Encrypt
  certificate. Enable by setting `COMPOSE_PROFILES=caddy` before
  `just self-hosted deploy`. Off by default because most operators
  already run nginx, Traefik, Cloudflare Tunnel, or a hosting-provider
  load balancer. See [Reverse Proxy Setup](reverse-proxy.md) for the
  full set of options.

## First-time install

1. Create the production env directory:

   ```bash
	   mkdir -p .envs/.production/.self-hosted
   ```

2. Copy the env templates:

   ```bash
   cp .envs.example/.production/.self-hosted/.django .envs/.production/.self-hosted/.django
   cp .envs.example/.production/.self-hosted/.postgres .envs/.production/.self-hosted/.postgres
   ```

	   Also copy the `.build` file — it holds commercial-package
	   installation vars (Pro / Enterprise). Safe to copy for any deployment; all vars
   have sensible defaults when left empty.

   ```bash
   cp .envs.example/.production/.self-hosted/.build .envs/.production/.self-hosted/.build
   ```

3. Edit both files and replace the placeholder values.

   Make sure you set:

	   - `DJANGO_SECRET_KEY`
	   - `DJANGO_API_KEY_DIGEST_KEY`
	   - `DJANGO_ALLOWED_HOSTS`
	   - `SITE_URL`
	   - `DJANGO_MFA_ENCRYPTION_KEY`
	   - `WORKER_API_KEY`
	   - `POSTGRES_PASSWORD`
	   - `SUPERUSER_PASSWORD`

   If you are installing a commercial package, edit `.envs/.production/.self-hosted/.build` too:

   ```bash
   VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
   VALIDIBOT_COMMERCIAL_NETRC=/absolute/path/to/commercial.netrc
   ```

   Use `validibot-enterprise==<version>` instead if you purchased Enterprise.
   You can also use a quoted exact wheel URL on `pypi.validibot.com` that
   includes `#sha256=<hash>` instead of a package name and version.

   Put the `pypi.validibot.com` login and package key in the referenced
   mode-0600 netrc. Compose mounts the credential only into the package-install
   step as a BuildKit secret; it does not become a build argument or image
   layer.

   Then point Django at the Pro-activating settings module by setting
   `DJANGO_SETTINGS_MODULE` in your `.envs/.production/.self-hosted/.django`:

   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production_pro
   ```

   That settings module adds `validibot_pro` to `INSTALLED_APPS`, which
   is what Django needs in order to import the package and run its
   license-registration hook. Do not edit `config/settings/base.py`
   directly — that makes future upgrades harder; the dedicated
   settings module is the supported path.

	   MCP requires no additional service setup. Its public implementation is
	   embedded in Django, and the Pro package activates `/mcp` through the
	   `mcp_server` feature. Configure the OAuth audience and client callback in
	   `.django`; do not create a `.mcp` file or shared service key.

4. Validate the env files and bootstrap the deployment:

   ```bash
   just self-hosted check-env
   just self-hosted bootstrap
   ```

`bootstrap` is the recommended first-run command. It:

- validates the env files (`check-env`)
- builds and starts the stack
- waits for the web container to come up
- applies migrations
- runs `setup_validibot` to seed roles and the superuser
- runs `ensure_oidc_clients` to register the OIDC clients (needed if you
  enable MCP)
- runs `check_validibot` as a final sanity check

## Enable signed credentials on Docker Compose

If you purchased Pro or Enterprise and want signed credentials, the simplest
self-hosted option is the local file signing backend.

Create a private signing key on the host:

```bash
mkdir -p .envs/.production/.self-hosted/keys
openssl ecparam -name prime256v1 -genkey -noout \
  -out .envs/.production/.self-hosted/keys/credential-signing.pem
chmod 600 .envs/.production/.self-hosted/keys/credential-signing.pem
```

Then add this to `.envs/.production/.self-hosted/.django`:

```bash
SIGNING_KEY_PATH=/run/validibot-keys/credential-signing.pem
CREDENTIAL_ISSUER_URL=https://validibot.example.com
```

The production compose file mounts `.envs/.production/.self-hosted/keys`
into the web and worker containers at `/run/validibot-keys`.

After deployment and migrations, register the key's public half:

```bash
just self-hosted manage \
  "register_signing_key --local-private-key /run/validibot-keys/credential-signing.pem"
just self-hosted manage "signing_key_status"
```

Registration stores only the public JWK. If you rotate later, register the new
PEM first, confirm its `kid` appears in `/.well-known/jwks.json`, then change
`SIGNING_KEY_PATH` and redeploy. Old public JWKs remain published so existing
credentials continue to verify.

## Verify the deployment

After bootstrap completes:

```bash
just self-hosted status
just self-hosted health-check
just self-hosted doctor                # full doctor diagnostic
```

At this point the app is running on port `8000` on the host. For a real deployment, put it behind a reverse proxy before exposing it publicly.

## Reverse proxy and TLS

Validibot does not ship with an always-on proxy container by default. That keeps the stack compatible with self-hosters who already have Caddy, Traefik, nginx, or Cloudflare Tunnel in place.

Use one of these guides next:

- [Reverse Proxy Setup](reverse-proxy.md)
- [Self-Hosting on DigitalOcean](https://github.com/mcquilleninteractive/validibot/blob/main/docs/operations/self-hosting/providers/digitalocean.md)

## Updates and day-two operations

Routine operations use the same `just self-hosted ...` namespace:

```bash
just self-hosted deploy
just self-hosted upgrade --to v0.9.0
just self-hosted logs
just self-hosted backup
just self-hosted list-backups
just self-hosted restore backups/<id>
```

`deploy` is for starting or rebuilding the currently checked-out stack. `upgrade --to <version>` is the safer day-two path because it takes a manifested backup and runs pre-flight and post-flight checks around the migration.

## Security and isolation notes

There are a few important production details to understand:

- The worker is the only service that gets Docker socket access for advanced validator execution.
- The reverse proxy should terminate TLS and keep internal services private.
- Secrets belong in `.envs/`, never in the repo.
- Advanced validator images should be images you built and control yourself.

For the operator responsibilities and safe-default expectations, read [Docker Compose Deployment Responsibility](docker-compose-responsibility.md).

## Good fits for this target

Docker Compose is a good fit when:

- you want to self-host on one machine
- you are comfortable managing OS updates and backups
- you do not need GCP-specific infrastructure

It is also the easiest target to run on AWS today, because the AWS-specific deployment automation is not implemented yet.

## Related guides

- [Run Validibot Locally](deploy-local.md)
- [Environment Configuration](environment-configuration.md)
- [Justfile Guide](justfile-guide.md)
- [Reverse Proxy Setup](reverse-proxy.md)
- [Self-Hosting on DigitalOcean](https://github.com/mcquilleninteractive/validibot/blob/main/docs/operations/self-hosting/providers/digitalocean.md)
