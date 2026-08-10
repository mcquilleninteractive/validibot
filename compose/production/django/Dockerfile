# =============================================================================
# Validibot Production Dockerfile (multi-stage)
#
# Three-stage build:
#   Stage 0 (extras):  Empty default overlay for optional extra packages
#   Stage 1 (builder): Compiles Python C extensions
#   Stage 2 (runtime): Slim image with only what's needed to serve Django
#
# Two optional knobs let a deployment layer extra Python packages on
# top of the base install without branching the Dockerfile:
#   EXTRA_UV_EXTRAS         — extras to pass to ``uv sync`` (build arg)
#   --build-context extras  — overlay directory of sdists to install
# Both default to no-op; leaving them unset produces the standard
# community image.
# =============================================================================


# =============================================================================
# Stage 0: Empty default for the ``extras`` named context
#
# ``FROM scratch`` provides an empty filesystem that the builder stage
# copies from by default. A build can override this with:
#
#   docker build --build-context extras=./some/dir ...
#
# rebinding the stage's content to any host directory. The builder's
# ``COPY --from=extras / /tmp/extras/`` reads from the root of whatever
# ``extras`` resolves to — empty with the default, populated with the
# override. Installing an override's subdirectories happens in the
# builder stage below.
#
# Docs: https://docs.docker.com/reference/cli/docker/buildx/build/#build-context
# =============================================================================
FROM scratch AS extras


# =============================================================================
# Stage 1: Builder
#
# Purpose: Compile Python packages that have C extensions (psycopg, lxml,
# cryptography, pillow, grpcio, argon2-cffi, etc.). The resulting .venv
# is copied to the runtime stage; everything else is discarded.
# =============================================================================
# Base image pinned by digest (OSSF Scorecard Pinned-Dependencies); Dependabot's
# docker ecosystem bumps the digest+tag together so patches still flow.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=1 \
    UV_CACHE_DIR=/tmp/uv-cache

ARG APP_HOME=/app
ARG VALIDIBOT_COMMERCIAL_PACKAGE=""

# Extra uv extras passed through to ``uv sync`` (space-separated, or
# empty). Used by cloud/enterprise builds to pull in provider-specific
# dependency groups (e.g. ``cloud`` enables the Stripe extra), and by the
# self-hosted compose file (docker-compose.production.yml), which injects
# ``docker-runner`` so the worker can dispatch advanced validators through
# the mounted Docker socket. GCP builds (just gcp) use the cloud-run runner
# and omit ``docker-runner``.
ARG EXTRA_UV_EXTRAS=""

# Optional overlay packages are read from the ``extras`` named context
# (see Stage 0 above and the COPY --from=extras step further down).
# Each subdirectory is installed with ``uv pip install --no-deps`` —
# its declared dependencies are expected to already be satisfied by
# the main ``uv sync`` above.
WORKDIR ${APP_HOME}

# Build tools — only needed here, not in the final image.
#   build-essential: C/C++ compiler for Python extensions
#   libpq-dev:      PostgreSQL headers for psycopg[c]
#   gettext:        Django translation compilation (compilemessages)
#   git:            Needed for django-celery-beat installed from Git
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gettext \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files first for Docker layer caching.
# Changes to application code won't invalidate the dependency layer.
COPY pyproject.toml uv.lock ${APP_HOME}/
COPY --chmod=755 compose/common/install-commercial-package.sh /usr/local/bin/install-commercial-package

# Install production dependencies (no dev extras).
#   --frozen: Use the lockfile exactly as resolved
#   --no-install-project: Don't install validibot itself (it's a Django
#     project run from the working directory, not a package)
#
# EXTRA_UV_EXTRAS is a space-separated list of pyproject.toml extras
# to include. Empty → no --extra flags; "foo" → --extra foo;
# "foo bar" → --extra foo --extra bar.
RUN --mount=type=secret,id=validibot_commercial_netrc,target=/root/.netrc,required=false,mode=0600 \
    EXTRA_FLAGS=""; \
    for extra in ${EXTRA_UV_EXTRAS}; do EXTRA_FLAGS="${EXTRA_FLAGS} --extra ${extra}"; done; \
    uv sync --no-dev --no-install-project --frozen ${EXTRA_FLAGS} \
    && install-commercial-package ${APP_HOME}/.venv/bin/python \
    && rm -rf /tmp/uv-cache

# Install any overlay packages pulled in via the ``extras`` named
# context. Install order is explicit so that packages which register
# feature flags (validibot-pro, validibot-enterprise) land before
# anything that depends on those flags at import time. Extra packages
# that don't appear in the explicit list are installed last in
# alphabetical order.
COPY --from=extras / /tmp/extras/
RUN set -e; \
    INSTALLED=""; \
    for pkg_name in validibot-pro validibot-enterprise; do \
        pkg_dir="/tmp/extras/${pkg_name}"; \
        if [ -d "$pkg_dir" ]; then \
            echo "Installing extra commercial package: ${pkg_name}..."; \
            uv pip install --no-deps "$pkg_dir"; \
            INSTALLED="${INSTALLED} ${pkg_name}"; \
        fi; \
    done; \
    # Install anything else that happens to be there (future-proofing
    # for additional commercial layers we haven't named above).
    if [ -d /tmp/extras ]; then \
        for pkg_dir in $(find /tmp/extras -mindepth 1 -maxdepth 1 -type d | sort); do \
            pkg_name=$(basename "$pkg_dir"); \
            case " ${INSTALLED} " in \
                *" ${pkg_name} "*) continue ;; \
            esac; \
            echo "Installing extra commercial package (generic): ${pkg_name}..."; \
            uv pip install --no-deps "$pkg_dir"; \
        done; \
    fi; \
    rm -rf /tmp/extras


# =============================================================================
# Stage 2: Runtime
#
# Purpose: Minimal image that runs the Django application. No compilers,
# no dev headers, no git — just the Python runtime, compiled extensions
# (from the builder), and the application code.
# =============================================================================
# Runtime base pinned by digest (OSSF Scorecard Pinned-Dependencies); kept fresh
# by Dependabot's docker ecosystem updates.
FROM python:3.13-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG APP_HOME=/app
ARG VALIDIBOT_VERSION=""
ARG VALIDIBOT_REVISION=""
ARG VALIDIBOT_SOURCE="https://github.com/mcquilleninteractive/validibot"

ENV VALIDIBOT_VERSION="${VALIDIBOT_VERSION}"
LABEL org.opencontainers.image.title="Validibot" \
      org.opencontainers.image.version="${VALIDIBOT_VERSION}" \
      org.opencontainers.image.revision="${VALIDIBOT_REVISION}" \
      org.opencontainers.image.source="${VALIDIBOT_SOURCE}"

WORKDIR ${APP_HOME}

# Create the django user BEFORE copying files so we can use --chown on COPY.
# This avoids a separate `chown -R` layer that would duplicate all file data.
# The docker group is for Docker socket access when running validator
# containers locally (entrypoint.sh adjusts the GID to match the host).
# We also make django own /app (WORKDIR creates it as root) and pre-create
# the staticfiles directory so collectstatic can write to it at startup.
RUN addgroup --system django \
  && adduser --system --ingroup django django \
  && (getent group docker || groupadd docker) \
  && usermod -aG docker django \
  && chown django:django ${APP_HOME} \
  && mkdir -p ${APP_HOME}/staticfiles && chown django:django ${APP_HOME}/staticfiles

# Runtime-only system packages — no compilers, no headers.
#   libpq5:            PostgreSQL client library (psycopg runtime dependency)
#   postgresql-client: pg_isready command (Docker Compose DB wait in entrypoint)
#   curl:              Health checks in Docker Compose deployments
#   gosu:              Privilege drop from root to django in entrypoint.sh
#   zstd:              Compression for ``just self-hosted backup`` /
#                      ``restore`` data archives. ``tar --zstd`` invokes
#                      the ``zstd`` binary externally; without it the
#                      backup/restore recipes fail at the tar step.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    curl \
    gosu \
    zstd \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtual environment from the builder.
# This contains all compiled C extensions (psycopg, lxml, etc.) without
# needing the compilers that built them.
COPY --from=builder --chown=django:django ${APP_HOME}/.venv ${APP_HOME}/.venv

# Copy the application code, owned by django user.
# Using --chown avoids a separate RUN chown layer that would duplicate data.
COPY --chown=django:django . ${APP_HOME}/

# Copy entrypoint/start scripts (these run as root initially for Docker
# socket permissions, then drop to django via gosu).
COPY compose/production/django/entrypoint.sh /entrypoint
COPY compose/production/django/start.sh /start
RUN chmod +x /entrypoint /start

# Put the venv's bin directory first so `python`, `gunicorn`, etc.
# resolve to the venv versions.
ENV PATH="${APP_HOME}/.venv/bin:${PATH}"

# Entrypoint runs as root to fix Docker socket permissions (stat, chgrp,
# groupmod), then drops to the django user via gosu. See entrypoint.sh.
USER root

ENTRYPOINT ["/entrypoint"]
CMD ["/start"]
