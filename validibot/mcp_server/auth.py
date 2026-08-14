"""OAuth bearer-token verification for the embedded MCP resource server."""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import jwt
from allauth.idp.oidc.models import Token as OIDCToken
from asgiref.sync import sync_to_async
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from django.conf import settings
from mcp.server.auth.provider import AccessToken

from validibot.mcp_server.constants import MCP_REQUIRED_SCOPE


class ValidibotTokenVerifier:
    """Verify allauth JWTs, token revocation state, scope, and exact resource.

    Signature verification alone is insufficient because an otherwise valid
    JWT may have been revoked. The database lookup uses allauth's stored token
    hash and also supplies the canonical local user identity to tool handlers.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return official-SDK access information for a valid MCP token."""

        resource = get_mcp_resource_url()
        claims = self._decode_claims(token=token, resource=resource)
        if claims is None:
            return None
        record = await self._get_active_token(token)
        if record is None or record.user_id is None or record.client_id is None:
            return None
        scopes = record.get_scopes()
        if MCP_REQUIRED_SCOPE not in scopes:
            return None
        if record.get_resources() != [resource]:
            return None
        if str(claims.get("sub") or "") != str(record.user_id):
            return None
        if str(claims.get("client_id") or "") != str(record.client_id):
            return None
        claim_scopes = str(claims.get("scope") or "").split()
        if set(claim_scopes) != set(scopes):
            return None
        expires_at = int(claims["exp"])
        return AccessToken(
            token=token,
            client_id=str(record.client_id),
            scopes=scopes,
            expires_at=expires_at,
            resource=resource,
            subject=str(claims["sub"]),
            claims={
                "iss": str(claims["iss"]),
                "user_id": record.user_id,
            },
        )

    @staticmethod
    def _decode_claims(*, token: str, resource: str) -> dict[str, Any] | None:
        """Verify JWT cryptography and claims without accepting broad audiences."""

        private_key = getattr(settings, "IDP_OIDC_PRIVATE_KEY", "")
        issuer = str(getattr(settings, "SITE_URL", "")).rstrip("/")
        if not private_key or not issuer:
            return None
        try:
            verification_key = _verification_key(private_key)
            claims = jwt.decode(
                token,
                key=verification_key,
                algorithms=["RS256"],
                audience=resource,
                issuer=issuer,
                options={
                    "require": [
                        "aud",
                        "client_id",
                        "exp",
                        "iat",
                        "iss",
                        "scope",
                        "sub",
                        "token_use",
                    ],
                },
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            return None
        if claims.get("token_use") != "access":
            return None
        if claims.get("aud") not in (resource, [resource]):
            return None
        if int(claims.get("exp", 0)) <= int(time.time()):
            return None
        return claims

    @staticmethod
    @sync_to_async(thread_sensitive=True)
    def _get_active_token(token: str) -> OIDCToken | None:
        """Resolve a non-expired, non-revoked allauth access-token record."""

        record = OIDCToken.objects.select_related("user", "client").lookup(
            OIDCToken.Type.ACCESS_TOKEN,
            token,
        )
        if record is None:
            return None
        if record.user is None or not record.user.is_active:
            return None
        return record


def get_mcp_resource_url() -> str:
    """Return the exact RFC 8707 resource identifier for this deployment."""

    configured = str(
        getattr(settings, "IDP_OIDC_MCP_RESOURCE_AUDIENCE", ""),
    ).rstrip("/")
    if configured:
        return configured
    return f"{str(settings.SITE_URL).rstrip('/')}/mcp"


@lru_cache(maxsize=4)
def _verification_key(private_key: str) -> RSAPublicKey:
    """Parse each configured signing key once per process and key rotation."""

    signing_key = load_pem_private_key(private_key.encode(), password=None)
    if not isinstance(signing_key, RSAPrivateKey):
        raise TypeError("The configured OIDC signing key is not RSA")
    return signing_key.public_key()
