"""Validate the web-only MCP production boundary before service deployment.

The web and worker use the same image and secret, but only the web service
mounts MCP. This command gives deployment automation a named, quoting-safe way
to validate the web configuration before Cloud Run revisions are changed.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.core.features import CommercialFeature
from validibot.core.features import is_feature_enabled
from validibot.mcp_server.configuration import validate_production_mcp_configuration


class Command(BaseCommand):
    """Fail when an activated web MCP surface has unsafe configuration."""

    help = "Validate the activated web MCP production configuration."

    def add_arguments(self, parser) -> None:
        """Accept the runtime role whose configuration is being checked."""

        parser.add_argument(
            "--role",
            choices=["web", "worker"],
            default="web",
            help="Runtime role to validate; MCP is mounted only by web.",
        )

    def handle(self, *args, **options) -> None:
        """Validate active web MCP settings and skip non-MCP roles."""

        role = str(options["role"])
        if role != "web":
            self.stdout.write(
                "MCP configuration is not applicable to the worker role.",
            )
            return
        if not is_feature_enabled(CommercialFeature.MCP_SERVER):
            self.stdout.write("MCP is not activated; no MCP configuration is required.")
            return

        try:
            validate_production_mcp_configuration()
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("MCP web configuration is valid."))
