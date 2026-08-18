"""Security policy for OAuth clients registered dynamically for MCP.

Desktop MCP clients have no prior relationship with a Validibot deployment, so
current Codex and Claude clients use Dynamic Client Registration (DCR).
django-allauth owns the protocol machinery; this module narrows its generic
client model to the public authorization-code + PKCE shape that the embedded
MCP server supports.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError

from validibot.idp.constants import MCP_OIDC_SCOPE

if TYPE_CHECKING:
    from allauth.idp.oidc.models import Client as OIDCClient

_PUBLIC_CLIENT_SCOPES = ("openid", "profile", "email", MCP_OIDC_SCOPE)
_ALLOWED_GRANT_TYPES = {"authorization_code", "refresh_token"}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def validate_dynamic_client_registration(
    *,
    client: OIDCClient,
    client_metadata: dict[str, Any],
) -> None:
    """Validate and normalize one unauthenticated RFC 7591 registration.

    DCR is retained for deployed Codex and Claude clients that do not yet use
    CIMD.  The registered client is always public, always consent-gated, and
    can request only the scopes understood by Validibot's MCP authorization
    server.
    """

    max_metadata_bytes = max(
        1,
        int(getattr(settings, "IDP_OIDC_DCR_MAX_METADATA_BYTES", 16_384)),
    )
    encoded_metadata = json.dumps(
        client_metadata,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded_metadata) > max_metadata_bytes:
        raise ValidationError("Client metadata is too large.")

    _validate_public_client_shape(client=client)
    if client_metadata.get("token_endpoint_auth_method") != "none":
        raise ValidationError(
            "Desktop MCP clients must register as public clients using "
            "token_endpoint_auth_method 'none'.",
        )

    application_type = client_metadata.get("application_type")
    if application_type not in (None, "native", "web"):
        raise ValidationError("application_type must be 'native' or 'web'.")

    _apply_server_owned_client_policy(client)


def _validate_public_client_shape(*, client: OIDCClient) -> None:
    """Reject OAuth capabilities outside Validibot's desktop MCP contract."""

    if client.type != client.Type.PUBLIC:
        raise ValidationError("Only public OAuth clients may register automatically.")

    client.name = client.name.strip()
    if not client.name:
        raise ValidationError("client_name is required.")

    grant_types = set(client.get_grant_types())
    if "authorization_code" not in grant_types or not grant_types.issubset(
        _ALLOWED_GRANT_TYPES
    ):
        raise ValidationError(
            "Only authorization_code with optional refresh_token is supported.",
        )
    if client.get_response_types() != ["code"]:
        raise ValidationError("Only response_type 'code' is supported.")

    requested_scopes = set(client.get_scopes())
    if not requested_scopes.issubset(_PUBLIC_CLIENT_SCOPES):
        raise ValidationError("The registration requested an unsupported scope.")

    redirect_uris = client.get_redirect_uris()
    max_redirect_uris = max(
        1,
        int(getattr(settings, "IDP_OIDC_DCR_MAX_REDIRECT_URIS", 8)),
    )
    if len(redirect_uris) > max_redirect_uris:
        raise ValidationError("The registration supplied too many redirect URIs.")
    if len(set(redirect_uris)) != len(redirect_uris):
        raise ValidationError("Redirect URIs must be unique.")
    for redirect_uri in redirect_uris:
        _validate_redirect_uri(redirect_uri)


def _validate_redirect_uri(uri: str) -> None:
    """Allow HTTPS callbacks on trusted hosts or native loopback callbacks."""

    max_uri_length = max(
        1,
        int(getattr(settings, "IDP_OIDC_DCR_MAX_REDIRECT_URI_LENGTH", 2_048)),
    )
    if len(uri) > max_uri_length:
        raise ValidationError("A redirect URI is too long.")
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("A redirect URI is invalid.") from exc

    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or "*" in uri
        or not parsed.path.startswith("/")
    ):
        raise ValidationError("A redirect URI is invalid.")

    hostname = parsed.hostname.lower().rstrip(".")
    if parsed.scheme == "http":
        if hostname not in _LOOPBACK_HOSTS:
            raise ValidationError("HTTP redirect URIs must use a loopback host.")
        return

    allowed_https_hosts = {
        str(host).strip().lower().rstrip(".")
        for host in getattr(settings, "IDP_OIDC_DCR_HTTPS_REDIRECT_HOSTS", ())
        if str(host).strip()
    }
    if (
        parsed.scheme != "https"
        or hostname not in allowed_https_hosts
        or port not in (None, 443)
    ):
        raise ValidationError(
            "HTTPS redirect URIs must use an approved MCP client host.",
        )


def _apply_server_owned_client_policy(client: OIDCClient) -> None:
    """Apply invariant fields after client-supplied metadata is validated."""

    client.type = client.Type.PUBLIC
    client.skip_consent = False
    client.set_scopes(list(_PUBLIC_CLIENT_SCOPES))
    client.set_default_scopes([MCP_OIDC_SCOPE])
