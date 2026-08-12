# Validibot MCP Server — Production image.
#
# Standalone FastMCP server with no Django dependency. Communicates with
# the Validibot REST API at VALIDIBOT_API_BASE_URL. Used by every
# production-style deployment target — Cloud Run (``just mcp build``),
# docker-compose production, and local-pro dev.
#
# Build context: ``mcp/`` (the sibling Python project at mcp/ in this repo).
# Callers pass the ``mcp/`` directory as the build context, so COPY
# paths below are relative to mcp/.
#
# Required env vars at runtime (see .envs.example/.production/.docker-compose/.mcp):
#   VALIDIBOT_API_BASE_URL  — URL of the Validibot Django API
#   VALIDIBOT_MCP_BASE_URL  — public URL of this MCP server
#   VALIDIBOT_OAUTH_AUTHORIZATION_SERVER_URL — upstream OAuth issuer
#   VALIDIBOT_MCP_ENABLED   — global kill switch (default true)
#   VALIDIBOT_MCP_SERVICE_KEY — shared secret for MCP→Django service auth
#     (only used in local dev; Cloud Run uses OIDC identity tokens)

# The uv helper and Python runtime are both pinned by digest. Dependabot updates
# the readable tag and reviewed digest together; builds never consume ``latest``.
FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /app

# Install the exact production dependency graph recorded in the MCP lockfile.
# The project source is copied separately into the runtime image, matching the
# main Django image's source-run layout and avoiding an isolated package build.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && rm -rf /tmp/uv-cache


FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid validibot --create-home \
        --home-dir /home/validibot --shell /usr/sbin/nologin validibot

COPY --from=builder --chown=validibot:validibot /app/.venv /app/.venv
COPY --chown=validibot:validibot src/ /app/src/

USER validibot:validibot

# Uvicorn serves the ASGI app. The startup lifespan runs the license
# gate before accepting traffic.
CMD ["uvicorn", "validibot_mcp.server:app", "--host", "0.0.0.0", "--port", "8080"]
