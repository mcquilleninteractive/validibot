"""Tests for the community credential-issuer extension boundary.

These tests keep signed-credential orchestration independent of commercial
packages. They pin the harmless community default, deterministic single
provider registration, and the public identifier returned to workflow code.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ImproperlyConfigured

from validibot.validations.services.credential_issuance import (
    CredentialIssuerUnavailableError,
)
from validibot.validations.services.credential_issuance import get_credential_issuer
from validibot.validations.services.credential_issuance import (
    issue_registered_credential,
)
from validibot.validations.services.credential_issuance import (
    register_credential_issuer,
)
from validibot.validations.services.credential_issuance import reset_credential_issuer


class TestCredentialIssuerRegistry:
    """Pin the one-way commercial extension contract."""

    def setup_method(self):
        """Start every test without a process-global provider."""
        reset_credential_issuer()

    def teardown_method(self):
        """Prevent test providers from leaking into later tests."""
        reset_credential_issuer()

    def test_community_default_reports_unavailable(self):
        """A community-only install must fail clearly without importing Pro."""
        with pytest.raises(
            CredentialIssuerUnavailableError,
            match="not installed",
        ):
            issue_registered_credential(MagicMock())

    def test_registered_provider_returns_public_identifier(self):
        """Workflow code should receive only the provider-neutral identifier."""
        step_run = MagicMock()
        issuer = MagicMock(return_value="credential-123")
        register_credential_issuer(issuer, provider_name="test.issuer")

        credential_id = issue_registered_credential(step_run)

        assert credential_id == "credential-123"
        issuer.assert_called_once_with(step_run)

    def test_same_provider_registration_is_idempotent(self):
        """Django startup reloads must not create a false provider conflict."""
        issuer = MagicMock()
        register_credential_issuer(issuer, provider_name="test.issuer")
        register_credential_issuer(issuer, provider_name="test.issuer")

        assert get_credential_issuer() is issuer

    def test_distinct_provider_registration_fails_closed(self):
        """Package load order must never silently choose a signing provider."""
        register_credential_issuer(MagicMock(), provider_name="first.issuer")

        with pytest.raises(ImproperlyConfigured, match="Only one"):
            register_credential_issuer(MagicMock(), provider_name="second.issuer")
