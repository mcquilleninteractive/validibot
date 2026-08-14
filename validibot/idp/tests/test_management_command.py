"""Tests for the Validibot OIDC client bootstrap management command."""

from __future__ import annotations

from io import StringIO

from allauth.idp.oidc.models import Client as OIDCClient
from django.core.management import call_command
from django.core.management.base import CommandError
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
    """Verify both predefined client registrations and safe configuration."""

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
        """ChatGPT must use its generated callback without a client secret."""

        call_command("ensure_oidc_clients")

        client = OIDCClient.objects.get(id=CHATGPT_OIDC_CLIENT_ID)
        self.assertEqual(client.name, "ChatGPT")
        self.assertEqual(client.type, OIDCClient.Type.PUBLIC)
        self.assertFalse(client.skip_consent)
        self.assertEqual(client.get_redirect_uris(), [TEST_CHATGPT_REDIRECT_URI])
        self.assertEqual(tuple(client.get_scopes()), TEST_SCOPES)
        self.assertFalse(client.check_secret("any-secret"))

    @override_settings(IDP_OIDC_CHATGPT_REDIRECT_URIS=())
    def test_command_skips_chatgpt_when_callback_is_not_configured(self) -> None:
        """A deployment without ChatGPT setup must still bootstrap cleanly."""

        stdout = StringIO()

        call_command("ensure_oidc_clients", stdout=stdout)

        self.assertTrue(OIDCClient.objects.filter(id=CLAUDE_OIDC_CLIENT_ID).exists())
        self.assertFalse(
            OIDCClient.objects.filter(id=CHATGPT_OIDC_CLIENT_ID).exists(),
        )
        self.assertIn("Skipped ChatGPT OIDC client", stdout.getvalue())
        self.assertIn("IDP_OIDC_CHATGPT_REDIRECT_URIS", stdout.getvalue())

    @override_settings(IDP_OIDC_CHATGPT_REDIRECT_URIS=())
    def test_missing_callback_does_not_delete_an_existing_client(self) -> None:
        """Omitting optional config must never become an implicit deletion."""

        client = OIDCClient.objects.create(
            id=CHATGPT_OIDC_CLIENT_ID,
            name="Existing ChatGPT client",
            type=OIDCClient.Type.PUBLIC,
        )
        client.set_redirect_uris([TEST_CHATGPT_REDIRECT_URI])
        client.save()
        stdout = StringIO()

        call_command("ensure_oidc_clients", stdout=stdout)

        client.refresh_from_db()
        self.assertEqual(client.name, "Existing ChatGPT client")
        self.assertEqual(client.get_redirect_uris(), [TEST_CHATGPT_REDIRECT_URI])
        self.assertIn("left unchanged", stdout.getvalue())

    def test_command_rejects_non_current_chatgpt_callback_shapes(self) -> None:
        """Invalid or legacy callbacks must fail before any client is changed."""

        invalid_redirect_uris = (
            "https://chatgpt.com/connector_platform_oauth_redirect",
            "https://chatgpt.com/connector/oauth/",
            "https://chatgpt.com/connector/oauth/callback/nested",
            "https://chatgpt.com/connector/oauth/callback?unexpected=true",
            "https://chatgpt.com/connector/oauth/callback#unexpected",
            "http://chatgpt.com/connector/oauth/callback",
            "https://example.com/connector/oauth/callback",
        )

        for redirect_uri in invalid_redirect_uris:
            with (
                self.subTest(redirect_uri=redirect_uri),
                override_settings(
                    IDP_OIDC_CHATGPT_REDIRECT_URIS=(redirect_uri,),
                ),
                self.assertRaisesMessage(
                    CommandError,
                    "https://chatgpt.com/connector/oauth/{callback_id}",
                ),
            ):
                call_command("ensure_oidc_clients")

        self.assertFalse(OIDCClient.objects.exists())

    def test_command_is_noop_when_client_is_already_current(self) -> None:
        """A third run should report success without rewriting the same client."""

        call_command("ensure_oidc_clients")
        stdout = StringIO()

        call_command("ensure_oidc_clients", stdout=stdout)

        self.assertEqual(OIDCClient.objects.filter(id=CLAUDE_OIDC_CLIENT_ID).count(), 1)
        self.assertIn("already up to date", stdout.getvalue())
