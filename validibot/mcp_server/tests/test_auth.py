"""Integration tests for embedded MCP OAuth bearer verification.

The MCP endpoint must verify JWT cryptography and allauth's revocation state,
then bind the token to one active user, one exact RFC 8707 resource, and the
``validibot:mcp`` scope. These tests exercise all of those checks together.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING

import jwt
import pytest
from allauth.idp.oidc.models import Client as OIDCClient
from allauth.idp.oidc.models import Token as OIDCToken
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import NoEncryption
from cryptography.hazmat.primitives.serialization import PrivateFormat
from django.utils import timezone

from validibot.mcp_server.auth import ValidibotTokenVerifier
from validibot.mcp_server.auth import _verification_key
from validibot.users.tests.factories import UserFactory

if TYPE_CHECKING:
    from validibot.users.models import User

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.anyio]

RESOURCE = "https://app.validibot.com/mcp"
ISSUER = "https://app.validibot.com"


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio because Django's ASGI integration is asyncio-based."""

    return "asyncio"


@pytest.fixture
def signing_key() -> str:
    """Return a throwaway RSA key so auth tests never use project secrets."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode()


def _issue_token(
    *,
    signing_key: str,
    user: User,
    resource: str = RESOURCE,
    client_id: str = "validibot-chatgpt-test",
    subject: str | None = None,
    issuer: str = ISSUER,
    scopes: tuple[str, ...] = ("openid", "validibot:mcp"),
    use_claim: str = "access",
) -> tuple[str, OIDCToken]:
    """Create matching signed JWT and allauth token records."""

    client = OIDCClient.objects.create(
        id=client_id,
        name="ChatGPT",
        type=OIDCClient.Type.PUBLIC,
    )
    now = int(time.time())
    raw_token = jwt.encode(
        {
            "aud": [resource],
            "client_id": client.id,
            "exp": now + 3600,
            "iat": now,
            "iss": issuer,
            "scope": " ".join(scopes),
            "sub": subject or str(user.pk),
            "token_use": use_claim,
        },
        signing_key,
        algorithm="RS256",
    )
    record = OIDCToken(
        type=OIDCToken.Type.ACCESS_TOKEN,
        client=client,
        user=user,
        expires_at=timezone.now() + timezone.timedelta(hours=1),
    )
    record.set_value(raw_token)
    record.set_scopes(list(scopes))
    record.set_resources([resource])
    record.save()
    return raw_token, record


async def test_verifier_accepts_exact_active_allauth_token(
    settings,
    signing_key: str,
) -> None:
    """A signed, stored, scoped token should become an MCP principal."""

    settings.SITE_URL = ISSUER
    settings.IDP_OIDC_PRIVATE_KEY = signing_key
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE
    user = await _create_user()
    raw_token, _record = await _sync_issue(signing_key=signing_key, user=user)

    verified = await ValidibotTokenVerifier().verify_token(raw_token)

    assert verified is not None
    assert verified.resource == RESOURCE
    assert verified.subject == str(user.pk)
    assert verified.claims == {"iss": ISSUER, "user_id": user.pk}


async def test_verifier_rejects_wrong_resource_and_revoked_record(
    settings,
    signing_key: str,
) -> None:
    """JWT validity must not bypass audience binding or database revocation."""

    settings.SITE_URL = ISSUER
    settings.IDP_OIDC_PRIVATE_KEY = signing_key
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE
    user = await _create_user()
    wrong_token, _wrong_record = await _sync_issue(
        signing_key=signing_key,
        user=user,
        resource="https://example.invalid/mcp",
    )

    assert await ValidibotTokenVerifier().verify_token(wrong_token) is None

    raw_token, record = await _sync_issue(
        signing_key=signing_key,
        user=user,
        client_id="validibot-chatgpt-revoked",
    )
    await record.adelete()

    assert await ValidibotTokenVerifier().verify_token(raw_token) is None


async def test_verifier_rejects_subject_that_disagrees_with_token_record(
    settings,
    signing_key: str,
) -> None:
    """A signed token must not switch away from its allauth owning user."""

    settings.SITE_URL = ISSUER
    settings.IDP_OIDC_PRIVATE_KEY = signing_key
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE
    user = await _create_user()
    raw_token, _record = await _sync_issue(
        signing_key=signing_key,
        user=user,
        subject=str(user.pk + 1),
    )

    assert await ValidibotTokenVerifier().verify_token(raw_token) is None


async def test_verifier_rejects_missing_scope_and_wrong_token_use(
    settings,
    signing_key: str,
) -> None:
    """A stored JWT must still be an MCP-scoped access token, not an ID token."""

    settings.SITE_URL = ISSUER
    settings.IDP_OIDC_PRIVATE_KEY = signing_key
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE
    user = await _create_user()
    unscoped, _record = await _sync_issue(
        signing_key=signing_key,
        user=user,
        client_id="validibot-chatgpt-unscoped",
        scopes=("openid",),
    )
    wrong_use, _wrong_use_record = await _sync_issue(
        signing_key=signing_key,
        user=user,
        client_id="validibot-chatgpt-id-token",
        use_claim="id",
    )

    assert await ValidibotTokenVerifier().verify_token(unscoped) is None
    assert await ValidibotTokenVerifier().verify_token(wrong_use) is None


async def test_verifier_rejects_wrong_issuer_and_inactive_user(
    settings,
    signing_key: str,
) -> None:
    """A signed token cannot cross issuers or outlive its local user account."""

    settings.SITE_URL = ISSUER
    settings.IDP_OIDC_PRIVATE_KEY = signing_key
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE
    user = await _create_user()
    wrong_issuer, _record = await _sync_issue(
        signing_key=signing_key,
        user=user,
        client_id="validibot-chatgpt-wrong-issuer",
        issuer="https://issuer.example.invalid",
    )
    active_token, _active_record = await _sync_issue(
        signing_key=signing_key,
        user=user,
        client_id="validibot-chatgpt-inactive-user",
    )

    assert await ValidibotTokenVerifier().verify_token(wrong_issuer) is None
    user.is_active = False
    await user.asave(update_fields=["is_active"])
    assert await ValidibotTokenVerifier().verify_token(active_token) is None


async def test_verifier_rejects_expired_database_record(
    settings,
    signing_key: str,
) -> None:
    """Database expiry must revoke a JWT even while its signed exp is future."""

    settings.SITE_URL = ISSUER
    settings.IDP_OIDC_PRIVATE_KEY = signing_key
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = RESOURCE
    user = await _create_user()
    raw_token, record = await _sync_issue(
        signing_key=signing_key,
        user=user,
        client_id="validibot-chatgpt-expired-record",
    )
    record.expires_at = timezone.now() - timedelta(seconds=1)
    await record.asave(update_fields=["expires_at"])

    assert await ValidibotTokenVerifier().verify_token(raw_token) is None


async def test_verification_key_is_parsed_once_per_configured_value(
    signing_key: str,
) -> None:
    """Repeated bearer checks must not repeat expensive private-key parsing."""

    _verification_key.cache_clear()

    first = _verification_key(signing_key)
    second = _verification_key(signing_key)

    assert first is second
    assert _verification_key.cache_info().hits == 1


async def _sync_issue(
    *,
    signing_key: str,
    user: User,
    resource: str = RESOURCE,
    client_id: str = "validibot-chatgpt-test",
    subject: str | None = None,
    issuer: str = ISSUER,
    scopes: tuple[str, ...] = ("openid", "validibot:mcp"),
    use_claim: str = "access",
) -> tuple[str, OIDCToken]:
    """Run the ORM-heavy token fixture in Django's synchronous thread."""

    from asgiref.sync import sync_to_async

    def issue() -> tuple[str, OIDCToken]:
        return _issue_token(
            signing_key=signing_key,
            user=user,
            resource=resource,
            client_id=client_id,
            subject=subject,
            issuer=issuer,
            scopes=scopes,
            use_claim=use_claim,
        )

    return await sync_to_async(issue, thread_sensitive=True)()


async def _create_user() -> User:
    """Create a factory user without running synchronous ORM on the event loop."""

    from asgiref.sync import sync_to_async

    return await sync_to_async(UserFactory, thread_sensitive=True)()
