"""Constants and validation shared by the MCP OIDC client bootstrap.

The initial integration supports two predefined public OAuth clients: Claude
Desktop / Claude Code and ChatGPT. The management command and settings layer
both import from here so each client's canonical shape lives in one place.
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
# ChatGPT generates an opaque callback_id for each app integration and shows
# the complete redirect URI in its app-management page. An administrator must
# copy that URI into settings; Validibot neither invents nor discovers the ID.
# This public client always uses authorization code + PKCE and has no secret.

CHATGPT_OIDC_CLIENT_ID = "validibot-chatgpt"
CHATGPT_OIDC_CLIENT_NAME = "ChatGPT"
CHATGPT_OIDC_REDIRECT_URI_PREFIX = "https://chatgpt.com/connector/oauth/"
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


def validate_chatgpt_redirect_uri(value: str) -> str:
    """Validate one app-specific callback URI issued by ChatGPT.

    New ChatGPT app integrations use
    ``https://chatgpt.com/connector/oauth/{callback_id}``. The callback ID is
    opaque, so this check validates the exact origin and one non-empty path
    segment without guessing the identifier's internal format.
    """

    cleaned = value.strip()
    callback_id = cleaned.removeprefix(CHATGPT_OIDC_REDIRECT_URI_PREFIX)
    if (
        not callback_id
        or callback_id == cleaned
        or "/" in callback_id
        or "?" in callback_id
        or "#" in callback_id
    ):
        msg = (
            "IDP_OIDC_CHATGPT_REDIRECT_URIS entries must match "
            f"{CHATGPT_OIDC_REDIRECT_URI_PREFIX}{{callback_id}} exactly. "
            "Copy the complete callback URL from the ChatGPT app-management "
            "page; legacy or hand-built callback URLs are not supported."
        )
        raise ValueError(msg)
    return cleaned


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
