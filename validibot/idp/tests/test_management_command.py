"""Tests for the Validibot OIDC client bootstrap management command."""

from __future__ import annotations

from io import StringIO

from allauth.idp.oidc.models import Client as OIDCClient
from django.core.management import call_command
from django.test import TestCase
from django.test import override_settings

from validibot.idp.constants import CHATGPT_OIDC_CLIENT_ID
from validibot.idp.constants import CLAUDE_OIDC_CLIENT_ID

TEST_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
TEST_SCOPES = (
    "openid",
    "profile",
    "email",
    "validibot:mcp",
)
TEST_CHATGPT_REDIRECT_URI = "https://chatgpt.com/connector/oauth/test-callback"


@override_settings(
    IDP_OIDC_CLAUDE_CLIENT_ID=CLAUDE_OIDC_CLIENT_ID,
    IDP_OIDC_CLAUDE_CLIENT_NAME="Claude Desktop",
    IDP_OIDC_CLAUDE_REDIRECT_URIS=TEST_REDIRECT_URIS,
    IDP_OIDC_CLAUDE_SCOPES=TEST_SCOPES,
    IDP_OIDC_CLAUDE_GRANT_TYPES=("authorization_code", "refresh_token"),
    IDP_OIDC_CLAUDE_RESPONSE_TYPES=("code",),
    IDP_OIDC_CLAUDE_SKIP_CONSENT=False,
    IDP_OIDC_CHATGPT_CLIENT_ID=CHATGPT_OIDC_CLIENT_ID,
    IDP_OIDC_CHATGPT_CLIENT_NAME="ChatGPT",
    IDP_OIDC_CHATGPT_REDIRECT_URIS=(TEST_CHATGPT_REDIRECT_URI,),
    IDP_OIDC_CHATGPT_SCOPES=TEST_SCOPES,
    IDP_OIDC_CHATGPT_GRANT_TYPES=("authorization_code", "refresh_token"),
    IDP_OIDC_CHATGPT_RESPONSE_TYPES=("code",),
    IDP_OIDC_CHATGPT_SKIP_CONSENT=False,
)
class EnsureOIDCClientsCommandTests(TestCase):
    """Verify the Claude client bootstrap stays deterministic and idempotent."""

    def test_command_creates_expected_public_client(self) -> None:
        """The first run should create the Claude public client with MCP scopes."""

        stdout = StringIO()

        call_command("ensure_oidc_clients", stdout=stdout)

        client = OIDCClient.objects.get(id=CLAUDE_OIDC_CLIENT_ID)
        self.assertEqual(client.name, "Claude Desktop")
        self.assertEqual(client.type, OIDCClient.Type.PUBLIC)
        self.assertFalse(client.skip_consent)
        self.assertEqual(tuple(client.get_redirect_uris()), TEST_REDIRECT_URIS)
        self.assertEqual(tuple(client.get_scopes()), TEST_SCOPES)
        self.assertEqual(tuple(client.get_default_scopes()), TEST_SCOPES)
        self.assertEqual(
            tuple(client.get_grant_types()),
            ("authorization_code", "refresh_token"),
        )
        self.assertEqual(tuple(client.get_response_types()), ("code",))
        self.assertIn("Created OIDC client", stdout.getvalue())

    def test_command_updates_existing_client_without_creating_duplicates(self) -> None:
        """A rerun should reconcile drift in place instead of creating a new row."""

        client = OIDCClient.objects.create(
            id=CLAUDE_OIDC_CLIENT_ID,
            name="Old Claude Client",
            type=OIDCClient.Type.CONFIDENTIAL,
            skip_consent=True,
        )
        client.set_redirect_uris(["https://example.com/old-callback"])
        client.set_scopes(["openid"])
        client.set_default_scopes(["openid"])
        client.set_grant_types([OIDCClient.GrantType.AUTHORIZATION_CODE])
        client.set_response_types(["token"])
        client.save()

        stdout = StringIO()
        call_command("ensure_oidc_clients", stdout=stdout)

        client.refresh_from_db()
        self.assertEqual(OIDCClient.objects.filter(id=CLAUDE_OIDC_CLIENT_ID).count(), 1)
        self.assertEqual(client.name, "Claude Desktop")
        self.assertEqual(client.type, OIDCClient.Type.PUBLIC)
        self.assertFalse(client.skip_consent)
        self.assertEqual(tuple(client.get_redirect_uris()), TEST_REDIRECT_URIS)
        self.assertEqual(tuple(client.get_scopes()), TEST_SCOPES)
        self.assertEqual(tuple(client.get_default_scopes()), TEST_SCOPES)
        self.assertEqual(
            tuple(client.get_grant_types()),
            ("authorization_code", "refresh_token"),
        )
        self.assertEqual(tuple(client.get_response_types()), ("code",))
        self.assertIn("Updated OIDC client", stdout.getvalue())

    def test_command_creates_chatgpt_as_public_pkce_client(self) -> None:
        """ChatGPT must use the configured callback without a client secret."""

        call_command("ensure_oidc_clients")

        client = OIDCClient.objects.get(id=CHATGPT_OIDC_CLIENT_ID)
        self.assertEqual(client.name, "ChatGPT")
        self.assertEqual(client.type, OIDCClient.Type.PUBLIC)
        self.assertFalse(client.skip_consent)
        self.assertEqual(client.get_redirect_uris(), [TEST_CHATGPT_REDIRECT_URI])
        self.assertEqual(tuple(client.get_scopes()), TEST_SCOPES)
        self.assertFalse(client.check_secret("any-secret"))

    def test_command_is_noop_when_client_is_already_current(self) -> None:
        """A third run should report success without rewriting the same client."""

        call_command("ensure_oidc_clients")
        stdout = StringIO()

        call_command("ensure_oidc_clients", stdout=stdout)

        self.assertEqual(OIDCClient.objects.filter(id=CLAUDE_OIDC_CLIENT_ID).count(), 1)
        self.assertIn("already up to date", stdout.getvalue())
