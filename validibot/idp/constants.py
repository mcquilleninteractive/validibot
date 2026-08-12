"""Constants shared by the OIDC provider and public client bootstrap.

These values define the default Claude Desktop / Claude Code client
registration for the MCP OAuth flow. The management command and
settings layer both import from here so the canonical client shape lives in
one place.
"""

from __future__ import annotations

MCP_OIDC_SCOPE = "validibot:mcp"

CLAUDE_OIDC_CLIENT_ID = "validibot-claude-desktop"
CLAUDE_OIDC_CLIENT_NAME = "Claude Desktop"
CLAUDE_OIDC_SCOPES = (
    "openid",
    "profile",
    "email",
    MCP_OIDC_SCOPE,
)
CLAUDE_OIDC_GRANT_TYPES = (
    "authorization_code",
    "refresh_token",
)
CLAUDE_OIDC_RESPONSE_TYPES = ("code",)
OIDC_TOKEN_ENDPOINT_AUTH_METHODS = (
    "none",
    "client_secret_post",
)
OIDC_CODE_CHALLENGE_METHODS = ("S256",)
CLAUDE_OIDC_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


# ── ChatGPT predefined public client ──────────────────────────────────
# The exact callback URI is installation-specific and therefore supplied in
# settings after the plugin builder shows it. This public client always uses
# authorization code + PKCE and has no secret.

CHATGPT_OIDC_CLIENT_ID = "validibot-chatgpt"
CHATGPT_OIDC_CLIENT_NAME = "ChatGPT"
CHATGPT_OIDC_SCOPES = (
    "openid",
    "profile",
    "email",
    MCP_OIDC_SCOPE,
)
CHATGPT_OIDC_GRANT_TYPES = (
    "authorization_code",
    "refresh_token",
)
CHATGPT_OIDC_RESPONSE_TYPES = ("code",)


def normalize_oidc_values(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Return a stable deduplicated tuple of non-empty OIDC config values."""

    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        normalized.append(cleaned)
        seen.add(cleaned)
    return tuple(normalized)
