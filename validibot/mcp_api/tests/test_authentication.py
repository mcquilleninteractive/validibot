"""Tests for legacy adapter identity and forwarded user authentication.

The compatibility API trusts forwarded user headers *after* the service
identity is verified. Any gap in the service-identity verification
translates directly into user-impersonation risk, so the allowlist is
the single most important defence here. These tests lock it in:

* empty allowlist rejects even a valid Google token;
* ``email_verified=False`` rejects;
* an SA outside the allowlist rejects;
* the happy path (valid token + matching email + verified) accepts.

We never actually call ``google.oauth2.id_token.verify_oauth2_token``
in these tests — we patch it to return a canned claims dict so the
test is hermetic and doesn't need network access. That leaves the
allowlist logic itself as the subject under test, which is exactly
where review finding 2 identified the gap.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import override_settings

from validibot.mcp_api.authentication import MCPServiceAuthentication
from validibot.mcp_api.authentication import MCPUserRouteAuthentication
from validibot.users.services.api_keys import issue_api_key


def _verify_returning(claims: dict):
    """Build a stand-in for ``id_token.verify_oauth2_token``.

    Accepts the positional args google-auth uses (token, transport,
    audience) and returns the canned claims dict regardless — the
    per-test expectation goes in the claims, not the arguments.
    """

    def _verify(token, transport, audience):
        return dict(claims)

    return _verify


@override_settings(MCP_OIDC_AUDIENCE="https://app.validibot.com")
class OIDCAllowlistTests(SimpleTestCase):
    """The allowlist must be enforced on every OIDC verification."""

    def _run(self, claims: dict) -> bool:
        """Drive ``_verify_oidc_token`` with a canned claims dict.

        Patches both the transport (so the lazy init doesn't try
        real network I/O) and the google-auth verify call.
        """

        auth = MCPServiceAuthentication()

        with (
            patch.object(
                MCPServiceAuthentication,
                "_get_google_transport",
                return_value=object(),  # sentinel; we never actually use it
            ),
            patch(
                "google.oauth2.id_token.verify_oauth2_token",
                side_effect=_verify_returning(claims),
            ),
        ):
            return auth._verify_oidc_token("ignored-token-string")

    @override_settings(MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS=[])
    def test_empty_allowlist_rejects_valid_token(self) -> None:
        """An empty allowlist must reject even a correctly-issued token.

        Rationale: without a narrowing allowlist, any Google SA that
        can mint a token with our audience would pass ``verify_oauth2_token``.
        Fail-closed is the safer default for a security boundary.
        """

        claims = {
            "email": "whatever@project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        self.assertFalse(self._run(claims))

    @override_settings(
        MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS=[
            "mcp@project.iam.gserviceaccount.com",
        ],
    )
    def test_unverified_email_rejects(self) -> None:
        """``email_verified=False`` must reject even an allowlisted SA.

        Google sets ``email_verified`` to False for tokens where the
        email wasn't established at issuance — those can't be trusted
        as identity claims even if the SA otherwise matches.
        """

        claims = {
            "email": "mcp@project.iam.gserviceaccount.com",
            "email_verified": False,
        }
        self.assertFalse(self._run(claims))

    @override_settings(
        MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS=[
            "mcp@project.iam.gserviceaccount.com",
        ],
    )
    def test_non_allowlisted_sa_rejects(self) -> None:
        """A valid Google token from an SA NOT in the allowlist rejects.

        This is the whole point of the allowlist — a Google-issued
        token with matching audience is necessary but not sufficient.
        """

        claims = {
            "email": "some-other@project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        self.assertFalse(self._run(claims))

    @override_settings(
        MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS=[
            "mcp@project.iam.gserviceaccount.com",
        ],
    )
    def test_happy_path_allowlisted_verified_accepts(self) -> None:
        """Token signed by an allowlisted SA with verified email → accept."""

        claims = {
            "email": "mcp@project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        self.assertTrue(self._run(claims))

    @override_settings(
        MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS=(
            "mcp@project.iam.gserviceaccount.com,"
            "other-mcp@project.iam.gserviceaccount.com"
        ),
    )
    def test_comma_separated_allowlist_shape_tolerated(self) -> None:
        """The env var path hands us a comma-separated string — the
        allowlist check must split it without the caller having to.

        Mirrors the shape tolerance ``task_auth`` has for the same
        env var shape on Cloud Tasks auth.
        """

        claims = {
            "email": "other-mcp@project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        self.assertTrue(self._run(claims))

    @override_settings(
        MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS=[
            "MCP@project.iam.gserviceaccount.com",
        ],
    )
    def test_allowlist_match_is_case_insensitive(self) -> None:
        """Email comparisons must be case-insensitive both ways.

        Gmail / Workspace tokens may arrive with any case in the
        ``email`` claim. A case-sensitive check here would reject
        legitimate callers on a superficial capitalisation difference.
        """

        claims = {
            "email": "mcp@PROJECT.IAM.gserviceaccount.com",
            "email_verified": True,
        }
        self.assertTrue(self._run(claims))


@pytest.mark.django_db
class TestMCPUserRouteAuthentication:
    """Forwarded end-user headers must resolve through trusted credentials."""

    @override_settings(MCP_SERVICE_KEY="test-mcp-service-key")
    def test_forwarded_hashed_api_key_resolves_user(self, user):
        """New ``vbk_`` keys work for MCP routes without DRF Token storage."""

        issued = issue_api_key(user=user)
        request = RequestFactory().get(
            "/mcp/fake/",
            HTTP_X_MCP_SERVICE_KEY="test-mcp-service-key",
            HTTP_X_VALIDIBOT_API_TOKEN=issued.full_key,
        )

        resolved_user, context = MCPUserRouteAuthentication().authenticate(request)

        assert resolved_user == user
        assert context.auth_kind == "api_key"
        assert context.user_identifier == issued.api_key.redacted_key
