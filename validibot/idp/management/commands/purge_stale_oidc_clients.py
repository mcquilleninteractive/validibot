"""Remove inactive dynamically registered OAuth clients after retention."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

from allauth.idp.oidc.models import Client as OIDCClient
from allauth.idp.oidc.models import Token as OIDCToken
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models import QuerySet


class Command(BaseCommand):
    """Purge old DCR clients only when they have no unexpired OAuth token."""

    help = (
        "Delete inactive dynamically registered OIDC clients after the configured "
        "retention period. Predefined and CIMD clients are never selected."
    )

    def add_arguments(self, parser: Any) -> None:
        """Expose a dry-run and an operator-controlled retention override."""

        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the number of clients without deleting them.",
        )
        parser.add_argument(
            "--retention-days",
            type=int,
            default=None,
            help="Override IDP_OIDC_DCR_INACTIVE_CLIENT_RETENTION_DAYS.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Delete only expired, abandoned DCR registrations."""

        del args
        retention_days = options["retention_days"]
        if retention_days is None:
            retention_days = int(
                settings.IDP_OIDC_DCR_INACTIVE_CLIENT_RETENTION_DAYS,
            )
        if retention_days < 1:
            raise CommandError("retention-days must be at least 1.")

        clients = self._stale_clients(retention_days=retention_days)
        count = clients.count()
        if options["dry_run"]:
            self.stdout.write(f"Would delete {count} inactive DCR client(s).")
            return

        clients.delete()
        message = f"Deleted {count} inactive DCR client(s)."
        self.stdout.write(self.style.SUCCESS(message))

    @staticmethod
    def _stale_clients(*, retention_days: int) -> QuerySet[OIDCClient]:
        """Select old DCR clients without any still-usable token."""

        now = timezone.now()
        cutoff = now - timedelta(days=retention_days)
        live_tokens = OIDCToken.objects.filter(client_id=OuterRef("pk")).filter(
            Q(expires_at__gt=now) | Q(expires_at__isnull=True),
        )
        return OIDCClient.objects.filter(
            data__dcr=True,
            created_at__lt=cutoff,
        ).filter(
            ~Exists(live_tokens),
        )
