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

# Base image pinned by digest (OSSF Scorecard Pinned-Dependencies); Dependabot's
# docker ecosystem bumps the digest+tag together so patches still flow.
FROM python:3.13-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6 AS base

WORKDIR /app

# uv for fast dependency resolution (matches the cloud image).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy the full package and install. The package is small; no layer
# caching win from splitting pyproject.toml and src/.
COPY pyproject.toml .
COPY src/ src/
RUN uv pip install --system --no-cache-dir .

ENV PORT=8080

# Uvicorn serves the ASGI app. The startup lifespan runs the license
# gate before accepting traffic.
CMD ["uvicorn", "validibot_mcp.server:app", "--host", "0.0.0.0", "--port", "8080"]
