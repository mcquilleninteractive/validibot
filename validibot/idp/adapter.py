"""OIDC adapter customizations for the Validibot authorization server."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import urlsplit

from allauth.idp.oidc.adapter import DefaultOIDCAdapter
from allauth.idp.oidc.models import Token as OIDCToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from validibot.idp.constants import MCP_OIDC_SCOPE
from validibot.idp.registration import validate_dynamic_client_registration

if TYPE_CHECKING:
    from collections.abc import Iterable

    from allauth.idp.oidc.models import Client as OIDCClient
    from django.contrib.auth.base_user import AbstractBaseUser

_SERVER_ENDPOINT_KEYS = (
    "authorization_endpoint",
    "device_authorization_endpoint",
    "end_session_endpoint",
    "jwks_uri",
    "registration_endpoint",
    "revocation_endpoint",
    "token_endpoint",
    "userinfo_endpoint",
)


class ValidibotOIDCAdapter(DefaultOIDCAdapter):
    """Customize the issuer, scope label, and MCP resource policy.

    This adapter sits at the boundary between django-allauth's generic OIDC
    provider and Validibot's MCP-specific needs. django-allauth carries RFC
    8707 resources through the authorization and token flows and derives JWT
    audiences from them; this adapter only restricts which resource is valid.

    Lives in the community repo so self-hosted Pro deployments can issue
    tokens that their MCP server will accept. Cloud overrides the MCP
    audience value via its own settings.
    """

    scope_display = {
        **DefaultOIDCAdapter.scope_display,
        MCP_OIDC_SCOPE: _("Use Validibot workflows through the MCP server"),
    }

    def get_issuer(self) -> str:
        """Return the canonical issuer URL for this deployment.

        Using ``SITE_URL`` keeps the issuer stable behind reverse proxies
        and in tests where the request host is ``testserver`` instead of
        the public hostname.
        """

        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        if site_url:
            return site_url
        return super().get_issuer()

    def validate_resource_uris(self, *, uris: list[str], **kwargs: Any) -> None:
        """Allow omission or the one exact MCP resource identifier.

        allauth represents an omitted resource as an empty list. That is
        necessary on refresh, where it restores the resource grant retained on
        the refresh token. Every explicitly supplied resource remains exact.
        """

        expected = str(settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE).rstrip("/")
        if uris and uris != [expected]:
            raise ValidationError(
                _("The requested OAuth resource is not available."),
                code="invalid_target",
            )

    def validate_client_registration(
        self,
        *,
        client: OIDCClient,
        client_metadata: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Restrict DCR to consent-gated public desktop MCP clients."""

        validate_dynamic_client_registration(
            client=client,
            client_metadata=client_metadata,
        )

    def populate_access_token(
        self,
        access_token: dict[str, Any],
        *,
        client: OIDCClient,
        scopes: Iterable[str],
        user: AbstractBaseUser,
        **kwargs: Any,
    ) -> None:
        """Preserve the exact retained resource on a refresh-token grant.

        RFC 8707 lets a refresh request omit ``resource`` to inherit its
        original grant. The JWT is encoded before allauth copies that retained
        resource into the replacement database rows, so derive the claim from
        the still-active allauth refresh-token record at this adapter boundary.
        """

        super().populate_access_token(
            access_token,
            client=client,
            scopes=scopes,
            user=user,
            **kwargs,
        )
        if "aud" in access_token or self.request is None:
            return
        raw_refresh_token = self.request.POST.get("refresh_token")
        if not raw_refresh_token:
            return
        refresh_token = OIDCToken.objects.filter(
            client=client,
            user=user,
        ).lookup(
            OIDCToken.Type.REFRESH_TOKEN,
            raw_refresh_token,
        )
        expected = str(settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE).rstrip("/")
        if refresh_token and refresh_token.get_resources() == [expected]:
            access_token["aud"] = [expected]

    def populate_server_metadata(self, data: dict[str, Any]) -> None:
        """Customize allauth's discovery metadata for the public MCP origin.

        allauth owns the discovery document and its declared protocol
        capabilities. This supported adapter hook only adds Validibot's scope
        and makes endpoint origins deterministic behind reverse proxies.
        """

        super().populate_server_metadata(data)
        scopes = data.get("scopes_supported")
        if isinstance(scopes, list) and MCP_OIDC_SCOPE not in scopes:
            scopes.append(MCP_OIDC_SCOPE)

        # Validibot's authorization-response middleware adds ``iss`` to final
        # callbacks. Advertising it lets MCP clients enforce RFC 9207 mix-up
        # protection and use their stable callback/client identities.
        data["authorization_response_iss_parameter_supported"] = True
        data["grant_types_supported"] = ["authorization_code", "refresh_token"]
        data["response_types_supported"] = ["code"]

        site_url = str(settings.SITE_URL).rstrip("/")
        for key in _SERVER_ENDPOINT_KEYS:
            value = data.get(key)
            if not isinstance(value, str):
                continue
            parsed = urlsplit(value)
            suffix = parsed.path
            if parsed.query:
                suffix = f"{suffix}?{parsed.query}"
            data[key] = f"{site_url}{suffix}"
