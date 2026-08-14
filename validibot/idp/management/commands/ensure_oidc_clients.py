"""Ensure predefined public OIDC clients have the expected configuration.

This command manages two OIDC clients needed for the MCP OAuth flow:

1. **Claude Desktop public client** — used by Claude Desktop / Claude Code
   to authenticate end users via the standard OAuth 2.1 PKCE flow.

2. **ChatGPT public client** — created when its app-specific
   ``https://chatgpt.com/connector/oauth/{callback_id}`` URI has been
   configured. It authenticates directly against Django using PKCE.

Both clients are intentionally idempotent: the command creates them if missing,
updates them if configuration has drifted, and is safe to run on every deploy
after migrations. An absent ChatGPT callback is a supported state: the command
reports that it skipped ChatGPT and continues managing Claude.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from allauth.idp.oidc.models import Client as OIDCClient
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.idp.constants import validate_chatgpt_redirect_uri

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class ManagedOIDCClient:
    """Describe one OIDC client registration managed by deployment automation.

    The command compares the live Django row against this dataclass and only
    writes changes when drift is detected. That keeps the command safe to run
    on every deploy while still correcting configuration changes such as new
    redirect URIs or consent behavior.
    """

    client_id: str
    name: str
    redirect_uris: tuple[str, ...]
    scopes: tuple[str, ...]
    default_scopes: tuple[str, ...]
    grant_types: tuple[str, ...]
    response_types: tuple[str, ...]
    skip_consent: bool


class Command(BaseCommand):
    """Create or update the predefined public clients needed for MCP OAuth."""

    help = "Create or update the predefined public clients for the MCP OAuth flow."

    def handle(self, *args: Any, **options: Any) -> None:
        """Reconcile each configured client without duplicating rows."""

        del args, options
        for definition in self._get_managed_clients():
            self._upsert_client(definition)

    def _get_managed_clients(self) -> list[ManagedOIDCClient]:
        """Return all managed client definitions from Django settings."""

        clients = [
            # Public client for Claude Desktop / Claude Code end-user auth.
            ManagedOIDCClient(
                client_id=settings.IDP_OIDC_CLAUDE_CLIENT_ID,
                name=settings.IDP_OIDC_CLAUDE_CLIENT_NAME,
                redirect_uris=tuple(settings.IDP_OIDC_CLAUDE_REDIRECT_URIS),
                scopes=tuple(settings.IDP_OIDC_CLAUDE_SCOPES),
                default_scopes=tuple(settings.IDP_OIDC_CLAUDE_SCOPES),
                grant_types=tuple(settings.IDP_OIDC_CLAUDE_GRANT_TYPES),
                response_types=tuple(settings.IDP_OIDC_CLAUDE_RESPONSE_TYPES),
                skip_consent=settings.IDP_OIDC_CLAUDE_SKIP_CONSENT,
            ),
        ]

        chatgpt_redirect_uris = tuple(
            getattr(
                settings,
                "IDP_OIDC_CHATGPT_REDIRECT_URIS",
                (),
            ),
        )
        if chatgpt_redirect_uris:
            try:
                chatgpt_redirect_uris = tuple(
                    validate_chatgpt_redirect_uri(uri) for uri in chatgpt_redirect_uris
                )
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            clients.append(
                ManagedOIDCClient(
                    client_id=settings.IDP_OIDC_CHATGPT_CLIENT_ID,
                    name=settings.IDP_OIDC_CHATGPT_CLIENT_NAME,
                    redirect_uris=chatgpt_redirect_uris,
                    scopes=tuple(settings.IDP_OIDC_CHATGPT_SCOPES),
                    default_scopes=tuple(settings.IDP_OIDC_CHATGPT_SCOPES),
                    grant_types=tuple(settings.IDP_OIDC_CHATGPT_GRANT_TYPES),
                    response_types=tuple(
                        settings.IDP_OIDC_CHATGPT_RESPONSE_TYPES,
                    ),
                    skip_consent=settings.IDP_OIDC_CHATGPT_SKIP_CONSENT,
                ),
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Skipped ChatGPT OIDC client: "
                    "IDP_OIDC_CHATGPT_REDIRECT_URIS is not configured. "
                    "Copy the app-specific callback URL from ChatGPT before "
                    "enabling this integration. Any existing database "
                    "registration is left unchanged.",
                ),
            )

        return clients

    def _upsert_client(self, definition: ManagedOIDCClient) -> None:
        """Create or reconcile a single managed OIDC client row."""

        client, created = OIDCClient.objects.get_or_create(
            id=definition.client_id,
            defaults={
                "name": definition.name,
                "type": OIDCClient.Type.PUBLIC,
                "skip_consent": definition.skip_consent,
            },
        )

        changed = created
        changed |= self._set_attr(client, "name", definition.name)
        changed |= self._set_attr(client, "type", OIDCClient.Type.PUBLIC)
        changed |= self._set_attr(client, "skip_consent", definition.skip_consent)

        changed |= self._set_text_values(
            client.get_redirect_uris,
            client.set_redirect_uris,
            definition.redirect_uris,
        )
        changed |= self._set_text_values(
            client.get_scopes,
            client.set_scopes,
            definition.scopes,
        )
        changed |= self._set_text_values(
            client.get_default_scopes,
            client.set_default_scopes,
            definition.default_scopes,
        )
        changed |= self._set_text_values(
            client.get_grant_types,
            client.set_grant_types,
            definition.grant_types,
        )
        changed |= self._set_text_values(
            client.get_response_types,
            client.set_response_types,
            definition.response_types,
        )

        if changed:
            client.full_clean()
            client.save()

        client_id = definition.client_id
        if created:
            msg = f"Created OIDC client '{client_id}' (public)."
        elif changed:
            msg = f"Updated OIDC client '{client_id}' (public)."
        else:
            msg = f"OIDC client '{client_id}' is already up to date."
        self.stdout.write(self.style.SUCCESS(msg))

    @staticmethod
    def _set_attr(client: OIDCClient, field_name: str, desired_value: object) -> bool:
        """Update a simple model field when the current value has drifted."""

        if getattr(client, field_name) == desired_value:
            return False
        setattr(client, field_name, desired_value)
        return True

    @staticmethod
    def _set_text_values(
        getter: Callable[[], list[str]],
        setter: Callable[[list[str]], None],
        desired_values: tuple[str, ...],
    ) -> bool:
        """Update newline-backed OIDC fields through the model helper methods."""

        if tuple(getter()) == desired_values:
            return False
        setter(list(desired_values))
        return True
