"""Integration tests for the OAuth/OIDC provider used by MCP clients."""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from urllib.parse import parse_qs
from urllib.parse import urlparse

from allauth.account.models import EmailAddress
from allauth.idp.oidc.models import Client as OIDCClient
from allauth.idp.oidc.models import Token as OIDCToken
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import NoEncryption
from cryptography.hazmat.primitives.serialization import PrivateFormat
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse

from validibot.users.tests.factories import UserFactory

if TYPE_CHECKING:
    from validibot.users.models import User


@lru_cache(maxsize=1)
def _generate_test_private_key() -> str:
    """Generate a throwaway RSA private key for OIDC signing in tests.

    Cached so every test in the module shares the same key without
    paying the generation cost more than once per process.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("ascii")


TEST_OIDC_PRIVATE_KEY = _generate_test_private_key()
TEST_SITE_URL = "https://app.validibot.com"
TEST_MCP_AUDIENCE = "https://app.validibot.com/mcp"
TEST_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
TEST_SCOPE = "openid profile email validibot:mcp"
TEST_CODE_VERIFIER = "pkce-verifier-for-validibot-tests-0123456789"


@override_settings(
    ROOT_URLCONF="validibot.idp.tests.test_urls",
    SITE_URL=TEST_SITE_URL,
    IDP_OIDC_PRIVATE_KEY=TEST_OIDC_PRIVATE_KEY,
    IDP_OIDC_MCP_RESOURCE_AUDIENCE=TEST_MCP_AUDIENCE,
)
class ValidibotOIDCProviderTests(TestCase):
    """Verify the Validibot OIDC issuer surface behaves as required for MCP.

    These tests cover: canonical-issuer metadata derived from ``SITE_URL``,
    the branded consent page, PKCE enforcement for public clients, and
    JWT access tokens carrying the MCP audience claim. The same surface
    serves both self-hosted Pro deployments and the hosted cloud offering.
    """

    def setUp(self) -> None:
        """Create a logged-in user with a verified primary email address."""

        super().setUp()
        self.user = cast("User", UserFactory())
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )
        self.client.force_login(self.user)

    def test_openid_configuration_uses_canonical_issuer(self) -> None:
        """OIDC discovery should advertise the canonical SITE_URL issuer."""

        response = self.client.get("/.well-known/openid-configuration")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["issuer"], TEST_SITE_URL)
        self.assertEqual(
            payload["authorization_endpoint"],
            f"{TEST_SITE_URL}/identity/o/authorize",
        )
        self.assertEqual(
            payload["token_endpoint"],
            f"{TEST_SITE_URL}/identity/o/api/token",
        )
        self.assertIn("validibot:mcp", payload["scopes_supported"])
        self.assertEqual(payload["code_challenge_methods_supported"], ["S256"])

    def test_oauth_authorization_server_metadata_is_derived_from_oidc(self) -> None:
        """RFC 8414 metadata should mirror the OIDC discovery core endpoints."""

        oidc_response = self.client.get("/.well-known/openid-configuration")
        oauth_response = self.client.get("/.well-known/oauth-authorization-server")

        self.assertEqual(oidc_response.status_code, 200)
        self.assertEqual(oauth_response.status_code, 200)

        oidc_payload = oidc_response.json()
        oauth_payload = oauth_response.json()

        self.assertEqual(oauth_payload["issuer"], oidc_payload["issuer"])
        self.assertEqual(
            oauth_payload["authorization_endpoint"],
            oidc_payload["authorization_endpoint"],
        )
        self.assertEqual(
            oauth_payload["token_endpoint"],
            oidc_payload["token_endpoint"],
        )
        self.assertEqual(oauth_payload["jwks_uri"], oidc_payload["jwks_uri"])
        self.assertIn("authorization_code", oauth_payload["grant_types_supported"])
        self.assertIn("refresh_token", oauth_payload["grant_types_supported"])
        self.assertEqual(
            oauth_payload["code_challenge_methods_supported"],
            ["S256"],
        )

    def test_authorization_page_uses_branded_mcp_copy(self) -> None:
        """The consent page should explain the AI-assistant access boundary."""

        oidc_client = self._create_public_client(skip_consent=False)
        response = self.client.get(
            reverse("idp:oidc:authorization"),
            {
                "response_type": "code",
                "client_id": oidc_client.id,
                "redirect_uri": TEST_REDIRECT_URI,
                "scope": TEST_SCOPE,
                "state": "state-123",
                "code_challenge": self._code_challenge(TEST_CODE_VERIFIER),
                "code_challenge_method": "S256",
                "resource": TEST_MCP_AUDIENCE,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "connect to your Validibot account")
        self.assertContains(response, "Validibot MCP server")
        self.assertContains(response, "does not grant general administration access")

    def test_public_client_token_exchange_requires_pkce_verifier(self) -> None:
        """A public client must provide a code verifier during token exchange."""

        oidc_client = self._create_public_client(skip_consent=True)
        authorization_code = self._authorization_code_for(oidc_client)

        response = self.client.post(
            reverse("idp:oidc:token"),
            {
                "grant_type": "authorization_code",
                "client_id": oidc_client.id,
                "code": authorization_code,
                "redirect_uri": TEST_REDIRECT_URI,
                "resource": TEST_MCP_AUDIENCE,
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["error"], "invalid_request")

    def test_jwt_access_token_includes_mcp_audience_and_scope(self) -> None:
        """Successful code exchange should emit a JWT scoped to the MCP resource."""

        oidc_client = self._create_public_client(skip_consent=True)
        payload = self._exchange_authorization_code(oidc_client)
        access_token = payload["access_token"]
        decoded = self._decode_jwt_payload(access_token)

        self.assertEqual(decoded["iss"], TEST_SITE_URL)
        self.assertEqual(decoded["aud"], [TEST_MCP_AUDIENCE])
        self.assertEqual(decoded["scope"], TEST_SCOPE)
        self.assertEqual(decoded["token_use"], "access")
        self.assertIn("refresh_token", payload)

    def test_refresh_preserves_resource_scope_and_rotates_token(self) -> None:
        """Refresh must retain MCP binding and replace the reusable credential."""

        oidc_client = self._create_public_client(skip_consent=True)
        initial = self._exchange_authorization_code(oidc_client)

        response = self.client.post(
            reverse("idp:oidc:token"),
            {
                "grant_type": "refresh_token",
                "client_id": oidc_client.id,
                "refresh_token": initial["refresh_token"],
            },
        )

        self.assertEqual(response.status_code, 200)
        refreshed = response.json()
        decoded = self._decode_jwt_payload(refreshed["access_token"])
        self.assertEqual(decoded["aud"], [TEST_MCP_AUDIENCE])
        self.assertEqual(decoded["scope"], TEST_SCOPE)
        self.assertNotEqual(refreshed["refresh_token"], initial["refresh_token"])
        self.assertIsNone(
            OIDCToken.objects.lookup(
                OIDCToken.Type.REFRESH_TOKEN,
                initial["refresh_token"],
            ),
        )

    def test_revocation_endpoint_invalidates_stored_access_token(self) -> None:
        """The standard allauth endpoint must make bearer revocation immediate."""

        oidc_client = self._create_public_client(skip_consent=True)
        payload = self._exchange_authorization_code(oidc_client)

        response = self.client.post(
            reverse("idp:oidc:revoke"),
            {
                "client_id": oidc_client.id,
                "token": payload["access_token"],
                "token_type_hint": "access_token",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            OIDCToken.objects.lookup(
                OIDCToken.Type.ACCESS_TOKEN,
                payload["access_token"],
            ),
        )

    def test_authorization_rejects_any_other_resource(self) -> None:
        """OAuth must not mint an MCP token for a different audience."""

        oidc_client = self._create_public_client(skip_consent=True)
        response = self.client.get(
            reverse("idp:oidc:authorization"),
            {
                "response_type": "code",
                "client_id": oidc_client.id,
                "redirect_uri": TEST_REDIRECT_URI,
                "scope": TEST_SCOPE,
                "state": "state-123",
                "code_challenge": self._code_challenge(TEST_CODE_VERIFIER),
                "code_challenge_method": "S256",
                "resource": "https://attacker.example/mcp",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "wants to connect to your Validibot account")

    def _create_public_client(self, *, skip_consent: bool) -> OIDCClient:
        """Create the Claude-style public OIDC client used by the MCP flow."""

        oidc_client = OIDCClient.objects.create(
            name="Claude Desktop",
            type=OIDCClient.Type.PUBLIC,
            skip_consent=skip_consent,
        )
        oidc_client.set_grant_types(
            [
                OIDCClient.GrantType.AUTHORIZATION_CODE,
                OIDCClient.GrantType.REFRESH_TOKEN,
            ],
        )
        oidc_client.set_response_types(["code"])
        oidc_client.set_redirect_uris([TEST_REDIRECT_URI])
        oidc_client.set_scopes(["openid", "profile", "email", "validibot:mcp"])
        oidc_client.set_default_scopes(["openid", "profile", "email", "validibot:mcp"])
        oidc_client.save()
        return oidc_client

    def _authorization_code_for(self, oidc_client: OIDCClient) -> str:
        """Run the authorization step and return the resulting code value."""

        response = self.client.get(
            reverse("idp:oidc:authorization"),
            {
                "response_type": "code",
                "client_id": oidc_client.id,
                "redirect_uri": TEST_REDIRECT_URI,
                "scope": TEST_SCOPE,
                "state": "state-123",
                "code_challenge": self._code_challenge(TEST_CODE_VERIFIER),
                "code_challenge_method": "S256",
                "resource": TEST_MCP_AUDIENCE,
            },
        )
        self.assertEqual(response.status_code, 302)

        parsed = urlparse(response["Location"])
        query = parse_qs(parsed.query)
        codes = query.get("code")
        self.assertIsNotNone(codes)
        assert codes is not None
        return codes[0]

    def _exchange_authorization_code(
        self,
        oidc_client: OIDCClient,
    ) -> dict[str, Any]:
        """Exchange a fresh PKCE authorization code through allauth's endpoint."""

        authorization_code = self._authorization_code_for(oidc_client)
        response = self.client.post(
            reverse("idp:oidc:token"),
            {
                "grant_type": "authorization_code",
                "client_id": oidc_client.id,
                "code": authorization_code,
                "redirect_uri": TEST_REDIRECT_URI,
                "code_verifier": TEST_CODE_VERIFIER,
                "resource": TEST_MCP_AUDIENCE,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _code_challenge(self, verifier: str) -> str:
        """Build the S256 PKCE challenge for a known verifier string."""

        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _decode_jwt_payload(self, token: str) -> dict[str, Any]:
        """Decode a JWT payload without verifying it.

        The provider behavior under test is the emitted claim set, not the
        JOSE verification library. Using a minimal decoder keeps the test
        independent of an extra runtime dependency.
        """

        parts = token.split(".")
        self.assertEqual(len(parts), 3)
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
