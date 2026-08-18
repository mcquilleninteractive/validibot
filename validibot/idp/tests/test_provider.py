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
from django.utils import timezone

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
    IDP_OIDC_RATE_LIMITS=False,
    IDP_OIDC_REGISTRATION_REQUESTS_PER_IP_PER_MINUTE=0,
    IDP_OIDC_ENDPOINT_GLOBAL_REQUESTS_PER_MINUTE=0,
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
        self.assertEqual(
            payload["registration_endpoint"],
            f"{TEST_SITE_URL}/identity/o/api/clients",
        )
        self.assertTrue(payload["authorization_response_iss_parameter_supported"])
        self.assertNotIn("client_id_metadata_document_supported", payload)

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
        self.assertEqual(
            oauth_payload["registration_endpoint"],
            oidc_payload["registration_endpoint"],
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
        self.assertContains(response, TEST_REDIRECT_URI)

    def test_dcr_registers_codex_as_a_consent_gated_public_client(self) -> None:
        """Codex should obtain a client ID without an administrator or secret."""

        redirect_uri = "http://127.0.0.1:43123/callback/codex-test"
        response = self.client.post(
            reverse("idp:oidc:client_registration"),
            data=json.dumps(
                {
                    "client_name": "Codex",
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "application_type": "native",
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertNotIn("client_secret", payload)
        self.assertEqual(payload["token_endpoint_auth_method"], "none")
        self.assertEqual(payload["scope"], TEST_SCOPE)
        oidc_client = OIDCClient.objects.get(id=payload["client_id"])
        self.assertEqual(oidc_client.type, OIDCClient.Type.PUBLIC)
        self.assertFalse(oidc_client.skip_consent)
        self.assertEqual(oidc_client.get_default_scopes(), ["validibot:mcp"])
        self.assertTrue((oidc_client.data or {}).get("dcr"))

        consent = self.client.get(
            reverse("idp:oidc:authorization"),
            {
                "response_type": "code",
                "client_id": oidc_client.id,
                "redirect_uri": redirect_uri,
                "scope": TEST_SCOPE,
                "state": "state-123",
                "code_challenge": self._code_challenge(TEST_CODE_VERIFIER),
                "code_challenge_method": "S256",
                "resource": TEST_MCP_AUDIENCE,
            },
        )
        self.assertEqual(consent.status_code, 200)
        self.assertContains(consent, "application running on this computer")

    def test_dcr_client_completes_consent_and_pkce_token_exchange(self) -> None:
        """A newly registered desktop client must complete the whole OAuth flow."""

        registration = self.client.post(
            reverse("idp:oidc:client_registration"),
            data=json.dumps(
                {
                    "client_name": "Codex end-to-end",
                    "redirect_uris": [TEST_REDIRECT_URI],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(registration.status_code, 201)
        client_id = registration.json()["client_id"]

        authorization = self.client.get(
            reverse("idp:oidc:authorization"),
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": TEST_REDIRECT_URI,
                "scope": TEST_SCOPE,
                "state": "state-123",
                "code_challenge": self._code_challenge(TEST_CODE_VERIFIER),
                "code_challenge_method": "S256",
                "resource": TEST_MCP_AUDIENCE,
            },
        )
        self.assertEqual(authorization.status_code, 200)
        signed_request = authorization.context["form"].initial["request"]

        consent = self.client.post(
            reverse("idp:oidc:authorization"),
            {
                "request": signed_request,
                "scopes": TEST_SCOPE.split(),
                "action": "grant",
            },
        )
        self.assertEqual(consent.status_code, 302)
        callback = parse_qs(urlparse(consent["Location"]).query)
        self.assertEqual(callback["iss"], [TEST_SITE_URL])

        token = self.client.post(
            reverse("idp:oidc:token"),
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": callback["code"][0],
                "redirect_uri": TEST_REDIRECT_URI,
                "code_verifier": TEST_CODE_VERIFIER,
                "resource": TEST_MCP_AUDIENCE,
            },
        )
        self.assertEqual(token.status_code, 200)
        payload = token.json()
        decoded = self._decode_jwt_payload(payload["access_token"])
        self.assertEqual(decoded["aud"], [TEST_MCP_AUDIENCE])
        self.assertEqual(decoded["scope"], TEST_SCOPE)
        self.assertIn("refresh_token", payload)

    def test_dcr_registers_claude_hosted_callback(self) -> None:
        """Claude Desktop should register its hosted callback without a secret."""

        response = self.client.post(
            reverse("idp:oidc:client_registration"),
            data=json.dumps(
                {
                    "client_name": "Claude",
                    "redirect_uris": [TEST_REDIRECT_URI],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "scope": "validibot:mcp",
                    "application_type": "web",
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        oidc_client = OIDCClient.objects.get(id=response.json()["client_id"])
        self.assertEqual(oidc_client.get_redirect_uris(), [TEST_REDIRECT_URI])
        self.assertEqual(oidc_client.get_scopes(), TEST_SCOPE.split())

    def test_dcr_accepts_localhost_with_an_ephemeral_port(self) -> None:
        """Native clients must be able to choose a safe callback port at runtime."""

        redirect_uri = "http://localhost:54321/oauth/callback"
        response = self.client.post(
            reverse("idp:oidc:client_registration"),
            data=json.dumps(
                {
                    "client_name": "Claude Code",
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "application_type": "native",
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        client = OIDCClient.objects.get(id=response.json()["client_id"])
        self.assertEqual(client.get_redirect_uris(), [redirect_uri])

    def test_dcr_rejects_untrusted_redirect_hosts(self) -> None:
        """An unauthenticated registration must not create an open redirect."""

        response = self.client.post(
            reverse("idp:oidc:client_registration"),
            data=json.dumps(
                {
                    "client_name": "Impostor",
                    "redirect_uris": ["https://attacker.example/callback"],
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")
        self.assertFalse(OIDCClient.objects.filter(name="Impostor").exists())

    def test_dcr_rejects_non_loopback_http_callbacks(self) -> None:
        """A native callback must never send an authorization code over cleartext."""

        response = self.client.post(
            reverse("idp:oidc:client_registration"),
            data=json.dumps(
                {
                    "client_name": "Cleartext collector",
                    "redirect_uris": ["http://attacker.example/callback"],
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")
        self.assertFalse(
            OIDCClient.objects.filter(name="Cleartext collector").exists(),
        )

    def test_dcr_rejects_confidential_desktop_clients(self) -> None:
        """Automatic registration must never mint a reusable client secret."""

        response = self.client.post(
            reverse("idp:oidc:client_registration"),
            data=json.dumps(
                {
                    "client_name": "Secret collector",
                    "redirect_uris": [TEST_REDIRECT_URI],
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "client_secret_basic",
                },
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")
        self.assertFalse(OIDCClient.objects.filter(name="Secret collector").exists())

    def test_authorization_response_includes_issuer_identifier(self) -> None:
        """RFC 9207 should bind the callback to Validibot's exact issuer."""

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
                "resource": TEST_MCP_AUDIENCE,
            },
        )

        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertEqual(query["iss"], [TEST_SITE_URL])

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
        self.assertEqual(decoded["exp"] - decoded["iat"], 900)
        refresh_record = OIDCToken.objects.lookup(
            OIDCToken.Type.REFRESH_TOKEN,
            payload["refresh_token"],
        )
        self.assertIsNotNone(refresh_record)
        assert refresh_record is not None
        self.assertIsNotNone(refresh_record.expires_at)
        assert refresh_record.expires_at is not None
        remaining_seconds = (refresh_record.expires_at - timezone.now()).total_seconds()
        self.assertGreater(remaining_seconds, 2_591_900)
        self.assertLessEqual(remaining_seconds, 2_592_000)

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
