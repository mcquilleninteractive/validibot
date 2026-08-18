"""Tests for bounded lifecycle management of dynamic OAuth clients.

DCR is an unauthenticated database-writing endpoint. These tests prove cleanup
is limited to old DCR records with no usable tokens, leaving active connections
and deployment-managed clients untouched.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from allauth.idp.oidc.models import Client as OIDCClient
from allauth.idp.oidc.models import Token as OIDCToken
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone


class PurgeStaleOIDCClientsTests(TestCase):
    """Verify cleanup cannot invalidate a live or predefined OAuth client."""

    def test_dry_run_reports_without_deleting_stale_client(self) -> None:
        """Operators must be able to inspect cleanup impact before mutation."""

        client = self._client(name="Abandoned", dcr=True, age_days=31)
        stdout = StringIO()

        call_command(
            "purge_stale_oidc_clients",
            "--dry-run",
            "--retention-days=30",
            stdout=stdout,
        )

        self.assertTrue(OIDCClient.objects.filter(pk=client.pk).exists())
        self.assertIn("Would delete 1 inactive DCR client(s).", stdout.getvalue())

    def test_stale_dcr_client_without_live_token_is_deleted(self) -> None:
        """An abandoned registration should not consume storage forever."""

        client = self._client(name="Abandoned", dcr=True, age_days=31)

        call_command("purge_stale_oidc_clients", "--retention-days=30")

        self.assertFalse(OIDCClient.objects.filter(pk=client.pk).exists())

    def test_unexpired_refresh_token_preserves_old_dcr_client(self) -> None:
        """Cleanup must not break a desktop connection that can still refresh."""

        client = self._client(name="Active desktop", dcr=True, age_days=31)
        token = OIDCToken(
            client=client,
            type=OIDCToken.Type.REFRESH_TOKEN,
            expires_at=timezone.now() + timedelta(days=1),
        )
        token.set_value("active-refresh-token")
        token.save()

        call_command("purge_stale_oidc_clients", "--retention-days=30")

        self.assertTrue(OIDCClient.objects.filter(pk=client.pk).exists())

    def test_legacy_token_without_expiry_preserves_old_dcr_client(self) -> None:
        """Cleanup must preserve an older token whose lifetime is unbounded."""

        client = self._client(name="Legacy desktop", dcr=True, age_days=31)
        token = OIDCToken(
            client=client,
            type=OIDCToken.Type.REFRESH_TOKEN,
            expires_at=None,
        )
        token.set_value("legacy-refresh-token")
        token.save()

        call_command("purge_stale_oidc_clients", "--retention-days=30")

        self.assertTrue(OIDCClient.objects.filter(pk=client.pk).exists())

    def test_predefined_client_is_never_selected(self) -> None:
        """Deployment-managed OAuth clients must survive regardless of age."""

        client = self._client(name="Managed client", dcr=False, age_days=365)

        call_command("purge_stale_oidc_clients", "--retention-days=30")

        self.assertTrue(OIDCClient.objects.filter(pk=client.pk).exists())

    @staticmethod
    def _client(*, name: str, dcr: bool, age_days: int) -> OIDCClient:
        """Create one public client with controlled protocol provenance and age."""

        client = OIDCClient.objects.create(
            name=name,
            type=OIDCClient.Type.PUBLIC,
            data={"dcr": True} if dcr else None,
        )
        OIDCClient.objects.filter(pk=client.pk).update(
            created_at=timezone.now() - timedelta(days=age_days),
        )
        client.refresh_from_db()
        return client
