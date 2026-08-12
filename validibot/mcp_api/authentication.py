"""Service-to-service authentication for the legacy agent HTTP adapter.

The compatibility routes verify a trusted service identity before resolving an
end user from a forwarded credential header. Embedded MCP does not use this
authentication path.

Two verification modes:

* **Production compatibility:** an allowlisted Cloud Run caller mints a
  Cloud Run OIDC identity token and sends it as ``Authorization: Bearer
  <token>``. Verified with google-auth against the configured audience.
* **Local compatibility tests:** a shared secret in ``X-MCP-Service-Key``
  is accepted as a fallback from the ``MCP_SERVICE_KEY`` Django setting.

Cloud's x402-paid agent routes use a sibling auth class
(``validibot_cloud.agents.authentication.AgentRouteAuthentication``)
that extracts payment context instead of a user identity. That one
stays in cloud because it depends on x402 data formats and the
``AgentValidationRun`` model.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass

from allauth.idp.oidc.adapter import get_adapter
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed

from validibot.users.services.api_keys import verify_api_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPAuthenticatedUserContext:
    """Describe the end-user identity forwarded by a trusted caller.

    The caller authenticates the end user's bearer token, then forwards either
    the OIDC ``sub`` claim or a legacy Validibot
    API token to Django over a trusted service-to-service channel. Views
    use this context to distinguish which compatibility path produced
    the Django user.
    """

    user_identifier: str
    auth_kind: str


class MCPServiceAuthentication(BaseAuthentication):
    """Verify the trusted caller of the legacy compatibility route."""

    def authenticate_header(self, request) -> str:
        """Advertise a challenge so authentication failures become HTTP 401."""

        return 'Bearer realm="validibot-mcp-service"'

    def _verify_service_identity(self, request) -> None:
        """Verify that the caller is an allowed compatibility service.

        In production, the caller sends a Cloud Run OIDC identity
        token in the Authorization header. In local dev, a shared secret
        in X-MCP-Service-Key is accepted as a fallback.

        Raises:
            AuthenticationFailed: If neither verification method succeeds.
        """
        # Local dev: shared secret fallback.
        service_key = getattr(settings, "MCP_SERVICE_KEY", "")
        if service_key:
            header_key = request.headers.get("X-MCP-Service-Key", "")
            if header_key and hmac.compare_digest(header_key, service_key):
                return

        # Production: Cloud Run OIDC token.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if self._verify_oidc_token(token):
                return

        raise AuthenticationFailed(
            "Missing or invalid service identity. Provide a valid "
            "Cloud Run OIDC token or X-MCP-Service-Key.",
        )

    # Lazy-initialized google-auth transport. Reused across requests to
    # avoid creating a new requests.Session (and TCP connection pool)
    # per OIDC verification. Initialized on first use; None if
    # google-auth is not installed (local dev without GCP deps).
    _google_transport: object | None = None
    _google_transport_initialized: bool = False

    @classmethod
    def _get_google_transport(cls):
        """Return a reusable google-auth HTTP transport, or None."""
        if not cls._google_transport_initialized:
            try:
                from google.auth.transport import requests as google_requests

                cls._google_transport = google_requests.Request()
            except ImportError:
                cls._google_transport = None
            cls._google_transport_initialized = True
        return cls._google_transport

    def _verify_oidc_token(self, token: str) -> bool:
        """Verify a Cloud Run OIDC identity token + enforce SA allowlist.

        Two-step verification mirroring
        :class:`validibot.core.api.task_auth.CloudTasksOIDCAuthentication`:

        1. Google-issued + audience match (``id_token.verify_oauth2_token``).
           Prevents forged / cross-audience token replay.
        2. Service-account allowlist + ``email_verified`` check. Without
           this step, any Google service account that can mint a token
           with our audience passes — an allowlist narrows that to our
           own MCP-invoker SA(s). Empty allowlist in production is a
           deployment error and fails closed.

        Returns True if *both* checks pass. Returns False for invalid /
        rejected tokens so the caller can fall back to the service-key
        path before raising.
        """
        transport = self._get_google_transport()
        if transport is None:
            return False

        expected_audience = getattr(settings, "MCP_OIDC_AUDIENCE", "")
        if not expected_audience:
            return False

        try:
            from google.auth import exceptions as google_auth_exceptions
            from google.oauth2 import id_token

            claims = id_token.verify_oauth2_token(
                token,
                transport,
                audience=expected_audience,
            )
        except (google_auth_exceptions.TransportError, ValueError):
            return False

        # Allowlist + email_verified. We don't fall through to the
        # service-key path on allowlist rejection — a valid Google
        # token from an unauthorised SA is a signal we want a clean
        # 401 with a log line, not a silent continue-on.
        return self._claims_pass_allowlist(claims)

    @staticmethod
    def _claims_pass_allowlist(claims: dict) -> bool:
        """Return True iff the token's subject is in the SA allowlist.

        ``email_verified`` must be True; Google sets this to False for
        tokens where the ``email`` claim wasn't verified at issuance,
        and an unverified email can't be trusted as an identity.
        """

        allowlist_raw = getattr(settings, "MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS", [])
        # Accept either a list in settings or a comma-separated string
        # (what the env-var path typically yields) — match the shape
        # ``task_auth.CloudTasksOIDCAuthentication`` tolerates.
        if isinstance(allowlist_raw, str):
            allowlist = {
                e.strip().lower() for e in allowlist_raw.split(",") if e.strip()
            }
        else:
            allowlist = {
                str(e).strip().lower() for e in allowlist_raw if str(e).strip()
            }

        if not allowlist:
            logger.error(
                "MCP OIDC allowlist is empty; cannot authorise caller. "
                "Set MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS.",
            )
            return False

        email = (claims.get("email") or "").lower()
        email_verified = claims.get("email_verified", False)

        if not email_verified:
            logger.warning(
                "MCP OIDC token rejected: email_verified=false (email=%s)",
                email,
            )
            return False

        if email not in allowlist:
            logger.warning(
                "MCP OIDC token rejected: non-allowlisted SA (%s)",
                email,
            )
            return False

        return True


class MCPUserRouteAuthentication(MCPServiceAuthentication):
    """Verify the compatibility caller and resolve the forwarded end user.

    Used by Django endpoints that exist purely to support the
    authenticated agent experience. The trusted caller has already validated
    the user's bearer credential; this class converts the forwarded
    OIDC subject or legacy API token into a concrete Django
    ``request.user``.
    """

    def authenticate(self, request):
        """Verify service identity and resolve the forwarded user."""

        self._verify_service_identity(request)

        user_sub = request.headers.get("X-Validibot-User-Sub", "").strip()
        if user_sub:
            user = get_adapter().get_user_by_sub(None, user_sub)
            if user is None:
                raise AuthenticationFailed("Unknown MCP user subject.")
            context = MCPAuthenticatedUserContext(
                user_identifier=user_sub,
                auth_kind="oidc_sub",
            )
            return (user, context)

        api_token = request.headers.get("X-Validibot-Api-Token", "").strip()
        if api_token:
            api_key = verify_api_key(api_token)
            if api_key is not None:
                context = MCPAuthenticatedUserContext(
                    user_identifier=api_key.redacted_key,
                    auth_kind="api_key",
                )
                return (api_key.user, context)

            token = Token.objects.select_related("user").filter(key=api_token).first()
            if token is None or not token.user.is_active:
                raise AuthenticationFailed("Unknown or inactive API token.")
            context = MCPAuthenticatedUserContext(
                user_identifier=token.key,
                auth_kind="legacy_api_token",
            )
            return (token.user, context)

        raise AuthenticationFailed(
            "Missing X-Validibot-User-Sub or X-Validibot-Api-Token header.",
        )
