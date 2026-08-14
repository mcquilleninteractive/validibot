"""Initialize every code-backed data concern required by a fresh installation.

Migrations create the database structure; this command creates the application
data that the running system expects after those migrations.  Keeping the
orchestration here gives local Docker, self-hosted deployments, and managed
cloud deployments one shared definition of "initialized".

The command is idempotent.  ``--if-needed`` is intended for deployment startup
paths: it skips the complete sequence once the role catalogue proves that
first-install setup has already completed.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

from validibot.core.site_settings import get_site_settings

INITIALIZATION_STATE_KEY = "application_initialization_version"
INITIALIZATION_VERSION = 1


class Command(BaseCommand):
    """Coordinate all application-data initialization after migrations."""

    help = (
        "Initialize site data, validators and Step I/O, help content, and "
        "bundled validator resources"
    )

    def add_arguments(self, parser):
        """Add the deployment-safe first-install guard."""
        parser.add_argument(
            "--if-needed",
            action="store_true",
            help=(
                "Skip initialization when the role catalogue shows that "
                "first-install setup has already completed."
            ),
        )

    def handle(self, *args, **options):
        """Run the complete initialization sequence in dependency order."""
        if options["if_needed"] and self._is_initialized():
            self.stdout.write(
                "Application data is already initialized; skipping first-install setup."
            )
            return

        self.stdout.write("Initializing Validibot application data...")

        # setup_validibot creates the site, schedules, permissions, roles,
        # validators and their Step I/O definitions, workspaces, and actions.
        call_command("setup_validibot", "--noinput", stdout=self.stdout)

        # Help pages and bundled validator files are deliberately separate
        # management concerns, but they are still required application data on
        # a fresh installation.  Operators should never have to remember them.
        call_command("sync_help", stdout=self.stdout)
        call_command("seed_weather_files", "--strict", stdout=self.stdout)

        # Write the marker last. If any preceding command fails, a retry sees
        # the older value and safely reruns the idempotent sequence instead of
        # mistaking a partial installation for a complete one.
        self._mark_initialized()
        self.stdout.write(self.style.SUCCESS("Validibot initialization complete."))

    def _is_initialized(self) -> bool:
        """Return whether the complete versioned initialization marker exists."""
        site_settings = get_site_settings()
        return (
            site_settings.data.get(INITIALIZATION_STATE_KEY) == INITIALIZATION_VERSION
        )

    def _mark_initialized(self) -> None:
        """Persist completion without disturbing unrelated site settings."""
        site_settings = get_site_settings()
        site_settings.data = {
            **site_settings.data,
            INITIALIZATION_STATE_KEY: INITIALIZATION_VERSION,
        }
        site_settings.save(update_fields=["data", "modified"])
