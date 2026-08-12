# `compose/` — container build files

This directory holds the Dockerfiles and entrypoint scripts that Docker Compose
uses to build Validibot's images. (Scripts shared across builds, like
`install-commercial-package.sh`, live in `common/`.)

The layout often raises a question: **why does `local/` only contain `django/`,
while `production/` contains `django/`, `mcp/`, and `postgres/`?**

## The rule

A subfolder exists here only when that environment needs its **own build
recipe** for that service — not one folder per service that happens to run in
that environment. Services that run from an off-the-shelf image (Redis, Mailpit,
Caddy) have no folder at all; the compose file just pulls them by tag.

That leaves three custom-built images, and only one of them actually differs
between local and production:

| Image | `local` / `local-pro` stack builds from | `production` (self-hosted) builds from |
| --- | --- | --- |
| **django** | `local/django/Dockerfile` | `production/django/Dockerfile` |
| **postgres** | `production/postgres/Dockerfile` | `production/postgres/Dockerfile` |
| **mcp** | `production/mcp/Dockerfile` | `production/mcp/Dockerfile` |

So `local/` needs no `postgres/` or `mcp/` folder — the local compose files
simply point at the production Dockerfiles.

## Why each lands where it does

**`django/` is duplicated** because the dev and production builds are genuinely
different recipes. The local image (`local/django/Dockerfile`) is single-stage,
installs dev dependencies and the `docker-runner` extra, adds tooling like
Chromium and a devcontainer user, and runs an entrypoint that fixes the mounted
Docker-socket permissions. The production image
(`production/django/Dockerfile`) is multi-stage, ships no dev dependencies, runs
Gunicorn, collects static files, and layers commercial code through a build-time
overlay. You can't collapse the two, so each environment owns one.

**`postgres/` has a single Dockerfile** because it's a trivial version pin
(`FROM postgres:17-alpine`). Its only job is to keep local and CI on the same
major version as production. The same file is correct everywhere, so duplicating
it under `local/` would just be two identical files to keep in sync. (Real
production on managed Postgres — e.g. Cloud SQL — doesn't use this image at all;
only Compose-based stacks do.)

MCP has no separate Dockerfile. The official SDK's ASGI application is mounted
inside Django at `/mcp`, so local Pro, Cloud, and self-hosted Pro all exercise
the same web image and startup command. Community builds contain the public
implementation but do not mount the route without the `mcp_server` feature.

## One thing to watch

Because the local and CI stacks build their database from
`production/postgres/Dockerfile`, changing that file — say, bumping the Postgres
major version — also changes your local and CI databases. That's intentional (it
keeps environments aligned), but worth remembering before you edit it.
