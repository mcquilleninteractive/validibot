"""Tests for the support bundle redaction rules.

The support bundle is what operators send to support@validibot.com when
something breaks. The redaction contract is therefore load-bearing in a
trust sense: a leaked secret in a bundle is a real incident, even if the
operator was just trying to get help.

These tests pin the contract:

1. **Name-based redaction** — settings whose names match a sensitive
   fragment are redacted regardless of value type.
2. **Allowlist exceptions** — ``USE_AUTH``, ``AUTH_USER_MODEL``, etc.
   pass through despite matching ``AUTH``.
3. **Value-shape redaction** — defense-in-depth: settings that *look*
   like credentials (PEM, JWT, Bearer, embedded URL auth) are
   redacted even if the name didn't trip the first check.
4. **Schema versioning** — ``SUPPORT_BUNDLE_SCHEMA_VERSION`` is a
   Pydantic Literal, rejecting ``v2`` at parse time.
5. **Frozen + ``extra='forbid'``** — accidental field additions or
   mutations raise rather than silently propagating.

These are pure unit tests on the redaction primitives. Tests for the
``collect_support_bundle`` management command live alongside in a sibling
file because they need Django's test database for migration recording.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from validibot.core.support_bundle import REDACTED_SENTINEL
from validibot.core.support_bundle import SUPPORT_BUNDLE_SCHEMA_VERSION
from validibot.core.support_bundle import MigrationState
from validibot.core.support_bundle import OutboundCallStatus
from validibot.core.support_bundle import RedactedSetting
from validibot.core.support_bundle import SupportBundleAppSnapshot
from validibot.core.support_bundle import VersionInfo
from validibot.core.support_bundle import is_sensitive_setting
from validibot.core.support_bundle import looks_like_secret_value
from validibot.core.support_bundle import redact_setting_value
from validibot.core.support_bundle import redact_text_for_bundle

# ──────────────────────────────────────────────────────────────────────
# Name-based redaction
# ──────────────────────────────────────────────────────────────────────


class TestIsSensitiveSetting:
    """Setting names that signal credentials are flagged for redaction."""

    @pytest.mark.parametrize(
        "name",
        [
            "DJANGO_SECRET_KEY",
            "SECRET_KEY",
            "DJANGO_API_KEY_DIGEST_KEY",
            "API_KEY_DIGEST_KEY",
            "DATABASE_PASSWORD",
            "POSTGRES_PASSWORD",
            "OIDC_CLIENT_SECRET",
            "API_KEY",
            "AWS_API_KEY",
            "GITHUB_TOKEN",
            "WORKER_API_TOKEN",
            "SIGNING_KEY_PATH",
            "MFA_ENCRYPTION_KEY",
            "SENTRY_DSN",
            "STRIPE_WEBHOOK_SECRET",
            "AUTH_TOKEN",
            "GOOGLE_CREDENTIALS",
            "PRIVATE_KEY_PEM",
            "PASSWD_HASH",
        ],
    )
    def test_credential_name_triggers_redaction(self, name):
        """Known credential-bearing settings must never escape into a bundle."""
        assert is_sensitive_setting(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "DEBUG",
            "ALLOWED_HOSTS",
            "TIME_ZONE",
            "DEPLOYMENT_TARGET",
            "INSTALLED_APPS",
            "DATA_STORAGE_ROOT",
            "EMAIL_HOST",
            "VALIDIBOT_VERSION",
            "USE_TZ",
        ],
    )
    def test_non_credential_name_passes(self, name):
        """Operational settings must remain visible enough to diagnose failures."""
        assert is_sensitive_setting(name) is False

    @pytest.mark.parametrize(
        "name",
        [
            "USE_AUTH",
            "AUTH_USER_MODEL",
            "AUTHENTICATION_BACKENDS",
            "PASSWORD_HASHERS",
            "AUTH_PASSWORD_VALIDATORS",
        ],
    )
    def test_allowlisted_exceptions_pass_despite_fragment_match(self, name):
        """Names like ``USE_AUTH`` match ``AUTH`` but aren't credentials."""
        assert is_sensitive_setting(name) is False


# ──────────────────────────────────────────────────────────────────────
# Value-shape redaction (defense in depth)
# ──────────────────────────────────────────────────────────────────────


class TestLooksLikeSecretValue:
    """Values whose shape suggests credentials are flagged."""

    # ``detect-private-key`` pre-commit hook excludes this file
    # explicitly — see .pre-commit-config.yaml. The synthetic PEM
    # strings below carry zero real key material; they exist purely
    # to exercise the redaction regex.
    @pytest.mark.parametrize(
        "value",
        [
            # Long hex (SHA-256ish secret keys)
            "abc123def456abc123def456abc123def456",
            "f" * 64,
            # JWT
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0In0.abc",
            # PEM private key (with key-type prefix and without)
            "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----",
            "-----BEGIN PRIVATE KEY-----\nMIIB...",
            # Bearer token
            "Bearer abc123def456",
            "bearer Eyj.token.here",
            # Connection string with embedded auth
            "postgres://user:pass@host:5432/db",
            "https://user:secret@api.example.com/path",
        ],
    )
    def test_secret_shapes_are_flagged(self, value):
        """Value inspection catches secrets whose setting name appears harmless."""
        assert looks_like_secret_value(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "hello world",
            "validibot",
            "False",
            "/srv/validibot/data",
            "https://api.example.com/path",  # no embedded auth
            "0.4.0",
            "abc123",  # too short for hex pattern
            "",
        ],
    )
    def test_normal_values_pass(self, value):
        """Ordinary diagnostic values should not be destroyed by over-redaction."""
        assert looks_like_secret_value(value) is False

    def test_non_string_returns_false(self):
        """Non-string values can't match string patterns."""
        assert looks_like_secret_value(42) is False
        assert looks_like_secret_value(True) is False  # noqa: FBT003 — testing arg type
        assert looks_like_secret_value([]) is False
        assert looks_like_secret_value({}) is False
        assert looks_like_secret_value(None) is False


# ──────────────────────────────────────────────────────────────────────
# redact_setting_value: name + value combined
# ──────────────────────────────────────────────────────────────────────


class TestRedactSettingValue:
    """The name+value redaction is the public single-call API."""

    def test_name_match_redacts_regardless_of_value_type(self):
        """Sensitive names are authoritative even for structured or benign values."""
        # A sensitive name redacts even if the value is a list / dict /
        # nothing-that-looks-like-a-secret.
        assert redact_setting_value("API_KEY", "anything") == REDACTED_SENTINEL
        assert (
            redact_setting_value("DATABASE_PASSWORD", ["x", "y"]) == REDACTED_SENTINEL
        )
        assert redact_setting_value("SECRET_KEY", {"sub": "ok"}) == REDACTED_SENTINEL

    def test_value_shape_redacts_when_name_doesnt(self):
        """A value-shape match catches 'innocent' setting names with secret bodies."""
        # ``DEBUG_HOOK`` doesn't match a sensitive fragment by name —
        # but the value's a JWT. Redact via the second-pass check.

        synthetic_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0In0.abc"
        assert redact_setting_value("DEBUG_HOOK", synthetic_jwt) == REDACTED_SENTINEL

    def test_normal_value_passes(self):
        """Safe values retain their native types for useful support diagnostics."""
        # Booleans pass through unchanged — assert by equality, not by
        # ``is`` (ruff FBT003 flags boolean literals in positional args).
        assert redact_setting_value("DEBUG", value=False) is False
        assert redact_setting_value("ALLOWED_HOSTS", ["example.com"]) == [
            "example.com",
        ]
        assert (
            redact_setting_value("DATA_STORAGE_ROOT", "/srv/validibot/data")
            == "/srv/validibot/data"
        )

    def test_explicit_exception_passes_through(self):
        """``USE_AUTH=True`` is not a credential."""
        assert redact_setting_value("USE_AUTH", value=True) is True


# ──────────────────────────────────────────────────────────────────────
# Schema version
# ──────────────────────────────────────────────────────────────────────


class TestSchemaVersion:
    """The schema version is pinned and rejects unknown values."""

    def _minimal_kwargs(self) -> dict:
        return {
            "captured_at": "2026-05-05T12:00:00Z",
            "versions": VersionInfo(
                validibot_version="0.4.0",
                python_version="3.13.1",
                postgres_server_version="16.0",
                target="self_hosted",
            ),
            "migrations": MigrationState(head={}),
            "settings": [],
            "outbound_calls": OutboundCallStatus(
                sentry_enabled=False,
                posthog_enabled=False,
                email_configured=False,
                runtime_license_check_enabled=False,
            ),
        }

    def test_default_schema_version_is_v1(self):
        """Support tooling needs every new snapshot to declare the v1 schema."""
        snapshot = SupportBundleAppSnapshot(**self._minimal_kwargs())
        assert snapshot.schema_version == "validibot.support-bundle.v1"
        assert snapshot.schema_version == SUPPORT_BUNDLE_SCHEMA_VERSION

    def test_rejects_other_schema_versions(self):
        """Future-proofing: a v2 schema deserialized by v1 code fails fast."""
        with pytest.raises(ValidationError):
            SupportBundleAppSnapshot(
                **self._minimal_kwargs(),
                schema_version="validibot.support-bundle.v2",
            )


# ──────────────────────────────────────────────────────────────────────
# Strict shape (frozen + extra='forbid')
# ──────────────────────────────────────────────────────────────────────


class TestStrictShape:
    """Snapshots are frozen and reject unknown fields."""

    def _minimal_kwargs(self) -> dict:
        return {
            "captured_at": "2026-05-05T12:00:00Z",
            "versions": VersionInfo(
                validibot_version="0.4.0",
                python_version="3.13.1",
                postgres_server_version="16.0",
                target="self_hosted",
            ),
            "migrations": MigrationState(head={}),
            "settings": [],
            "outbound_calls": OutboundCallStatus(
                sentry_enabled=False,
                posthog_enabled=False,
                email_configured=False,
                runtime_license_check_enabled=False,
            ),
        }

    def test_unknown_top_level_field_rejected(self):
        """Unknown fields must expose producer/consumer drift rather than disappear."""
        with pytest.raises(ValidationError):
            SupportBundleAppSnapshot(
                **self._minimal_kwargs(),
                surprise_field="not allowed",
            )

    def test_snapshot_is_frozen(self):
        """``frozen=True`` means re-assignment fails — snapshots are immutable."""
        snapshot = SupportBundleAppSnapshot(**self._minimal_kwargs())
        with pytest.raises(ValidationError):
            snapshot.captured_at = "tampered"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────
# RedactedSetting round-trip
# ──────────────────────────────────────────────────────────────────────


class TestRedactedSetting:
    """The ``RedactedSetting`` row preserves type for non-redacted values."""

    def test_non_redacted_preserves_type(self):
        """``DEBUG=False`` round-trips as bool, not string."""
        s = RedactedSetting(name="DEBUG", value=False, redacted=False)
        assert s.value is False
        assert s.redacted is False

    def test_redacted_value_is_sentinel(self):
        """A stable sentinel lets operators distinguish redaction from missing data."""
        s = RedactedSetting(name="SECRET_KEY", value=REDACTED_SENTINEL, redacted=True)
        assert s.value == REDACTED_SENTINEL
        assert s.redacted is True

    def test_unknown_field_rejected(self):
        """Strict rows prevent unreviewed data from entering support bundles."""
        with pytest.raises(ValidationError):
            RedactedSetting(
                name="X",
                value="x",
                redacted=False,
                surprise="bad",
            )


# ──────────────────────────────────────────────────────────────────────
# Log-text redaction (bundle host-side artefacts)
# ──────────────────────────────────────────────────────────────────────


class TestLogTextRedaction:
    """``redact_text_for_bundle`` scrubs free-form text for support bundles.

    The settings-side redaction fixes one class of leak (Django
    settings whose names match a credential pattern). This second
    layer protects host/cloud logs the recipe pipes into the bundle
    — those would otherwise carry tokens, embedded URL credentials,
    and PEM blocks straight into the zip operators send to support.
    """

    def test_bearer_tokens_are_redacted(self):
        """Authorization tokens are credentials and must not leave the deployment."""
        # Bearer in an Authorization header — the Authorization-header
        # pattern catches the whole line and replaces with
        # ``Authorization: [REDACTED]``. The standalone Bearer pattern
        # is for cases where ``Bearer <tok>`` appears outside a
        # header. Both end with the token gone, which is the
        # invariant we care about.
        log = "GET /api/x HTTP/1.1\nAuthorization: Bearer abc123def456\n"
        redacted = redact_text_for_bundle(log)
        assert "abc123def456" not in redacted
        assert "[REDACTED]" in redacted

    def test_bearer_outside_header_is_redacted(self):
        """Standalone ``Bearer <tok>`` (no Authorization header) is also caught."""
        # In a stack trace, log message, or arbitrary debug output
        # the Authorization-header pattern doesn't fire — but the
        # bare ``Bearer <token>`` pattern still does.
        log = "Calling with auth=Bearer abc123def456 returned 401"
        redacted = redact_text_for_bundle(log)
        assert "abc123def456" not in redacted
        assert "Bearer [REDACTED]" in redacted

    def test_authorization_header_redacted_regardless_of_scheme(self):
        """Non-Bearer authentication schemes can still carry reusable secrets."""
        log = "X-API-Key: super-secret-token-value-here-please"
        redacted = redact_text_for_bundle(log)
        assert "super-secret-token-value-here-please" not in redacted
        assert "[REDACTED]" in redacted

    def test_cookie_headers_are_redacted(self):
        """Cookie / Set-Cookie carry session IDs, CSRF tokens, etc."""
        log = (
            "Cookie: sessionid=abc123def456; csrftoken=xyz789; theme=dark\n"
            "Set-Cookie: sessionid=newvalue; HttpOnly; Path=/\n"
        )
        redacted = redact_text_for_bundle(log)
        # The cookie values must not appear.
        assert "abc123def456" not in redacted
        assert "xyz789" not in redacted
        assert "newvalue" not in redacted
        assert "[REDACTED]" in redacted

    def test_csrf_header_is_redacted(self):
        """X-CSRFToken / X-CSRF-Token carry session-fixation-sensitive material."""
        for header in ("X-CSRFToken: abc123def456", "X-CSRF-Token: xyz789xyz789"):
            redacted = redact_text_for_bundle(header)
            assert "abc123def456" not in redacted
            assert "xyz789xyz789" not in redacted

    def test_session_attributes_redacted_outside_header(self):
        """Bare ``sessionid=...`` or ``csrftoken=...`` in log bodies are caught."""
        log = (
            "Request: GET /admin?sessionid=abc123def456&page=1\n"
            "Setting csrftoken=xyz789 for user 42\n"
        )
        redacted = redact_text_for_bundle(log)
        assert "abc123def456" not in redacted
        assert "xyz789" not in redacted

    def test_x402_payment_header_is_redacted(self):
        """``X-X402-Payment`` carries signed payment proof — trust-boundary data."""
        log = "X-X402-Payment: eyJzaWduYXR1cmUiOiJhYmMxMjMifQ=="
        redacted = redact_text_for_bundle(log)
        assert "eyJzaWduYXR1cmUiOiJhYmMxMjMifQ==" not in redacted
        assert "[REDACTED]" in redacted

    def test_x402_v2_spec_payment_headers_are_redacted(self):
        """The actual x402 v2 header names (``PAYMENT-SIGNATURE`` etc.) must redact.

        Why this is a separate test from the legacy ``X-X402-Payment``
        case: the x402 v2 specification (and our MCP server's
        ``validibot_mcp.x402``) uses unprefixed names ``PAYMENT-
        SIGNATURE``, ``PAYMENT-REQUIRED``, and ``PAYMENT-RESPONSE``.
        Redacting only the ``X-X402-*`` alias would leave the actual
        signed-payment payload (``PAYMENT-SIGNATURE``) intact in logs
        — that's the failure mode the second-pass review caught.

        ``PAYMENT-REQUIRED`` and ``PAYMENT-RESPONSE`` aren't
        cryptographic secrets, but they carry receiving wallet
        addresses and settlement transaction hashes — financial PII
        worth scrubbing from a bundle that may end up in a customer-
        support thread.
        """
        log = (
            "PAYMENT-SIGNATURE: eyJwYXltZW50IjoiQUJDREVGMTIzIn0=\n"
            "PAYMENT-REQUIRED: eyJzY2hlbWUiOiJleGFjdCIsImFkZHJlc3MiOiIweGFiYyJ9\n"
            "PAYMENT-RESPONSE: eyJ0eEhhc2giOiIweGRlYWRiZWVmIn0=\n"
        )
        redacted = redact_text_for_bundle(log)
        # Each base64 payload must be scrubbed.
        assert "eyJwYXltZW50IjoiQUJDREVGMTIzIn0=" not in redacted
        assert "eyJzY2hlbWUiOiJleGFjdCIsImFkZHJlc3MiOiIweGFiYyJ9" not in redacted
        assert "eyJ0eEhhc2giOiIweGRlYWRiZWVmIn0=" not in redacted
        # Header names should still appear (so an operator can see
        # which headers were present without the values).
        assert "PAYMENT-SIGNATURE" in redacted
        assert "PAYMENT-REQUIRED" in redacted
        assert "PAYMENT-RESPONSE" in redacted
        assert "[REDACTED]" in redacted

    def test_validibot_service_identity_header_is_redacted(self):
        """Compatibility-route Cloud Run identity tokens remain sensitive."""
        log = "X-Validibot-Service-Identity: eyJhbGciOiJSUzI1NiJ9.body.sig"
        redacted = redact_text_for_bundle(log)
        assert "eyJhbGciOiJSUzI1NiJ9.body.sig" not in redacted

    def test_mcp_and_user_identity_headers_are_redacted(self):
        """Legacy adapter keys and forwarded user identities remain sensitive."""
        log = (
            "X-MCP-Service-Key: local-dev-secret-abc123\n"
            "X-Validibot-Api-Token: token-for-user-42-do-not-leak\n"
            "X-Validibot-User-Sub: oidc-subject-claim-value\n"
        )
        redacted = redact_text_for_bundle(log)
        assert "local-dev-secret-abc123" not in redacted
        assert "token-for-user-42-do-not-leak" not in redacted
        assert "oidc-subject-claim-value" not in redacted

    def test_proxy_authorization_is_redacted(self):
        """Proxy-Authorization carries HTTP proxy credentials."""
        log = "Proxy-Authorization: Basic dXNlcjpwYXNz"
        redacted = redact_text_for_bundle(log)
        assert "dXNlcjpwYXNz" not in redacted

    def test_jwt_anywhere_in_text_is_redacted(self):
        """JWTs embedded in arbitrary exception text remain sensitive."""
        # JWT in the middle of a stack trace.
        log = (
            "ERROR fetching: token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJ0ZXN0In0.AbC123-_DefGhi at line 42"
        )
        redacted = redact_text_for_bundle(log)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        assert "[REDACTED-JWT]" in redacted

    def test_pem_blocks_are_redacted_multiline(self):
        """Multiline private keys must be removed without erasing surrounding logs."""
        # Synthetic PEM, no real key material. detect-private-key
        # excludes this file via .pre-commit-config.yaml.
        log = (
            "Loading signing key:\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
            "-----END RSA PRIVATE KEY-----\n"
            "Done."
        )
        redacted = redact_text_for_bundle(log)
        # The base64 body must not survive.
        assert (
            "MIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            not in redacted
        )
        assert "[REDACTED-PEM-PRIVATE-KEY]" in redacted
        # Surrounding context ("Loading signing key", "Done.") survives.
        assert "Loading signing key" in redacted
        assert "Done." in redacted

    def test_url_embedded_basic_auth_redacts_credential_only(self):
        """URL credentials are removed while retaining the endpoint for diagnosis."""
        log = "Connecting to postgres://operator:hunter2@db.internal:5432/validibot"
        redacted = redact_text_for_bundle(log)
        assert "operator:hunter2" not in redacted
        assert "hunter2" not in redacted
        # The host portion of the URL survives so support sees what
        # endpoint the operator was hitting.
        assert "@db.internal:5432/validibot" in redacted
        assert "[REDACTED]" in redacted

    def test_long_hex_strings_are_redacted_but_image_digests_pass(self):
        """Secret-like hex is scrubbed without hiding public image identifiers."""
        digest_body = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        log = (
            "SECRET_KEY value: 0123456789abcdef0123456789abcdef0123456789abcdef\n"
            f"Pulling image@sha256:{digest_body}"
        )
        redacted = redact_text_for_bundle(log)
        # The long hex value is replaced.
        assert "0123456789abcdef0123456789abcdef0123456789abcdef" not in redacted
        assert "[REDACTED-HEX]" in redacted
        # The image digest survives — sha256:... is a public identifier
        # operators routinely paste in tickets to identify versions.
        assert f"sha256:{digest_body}" in redacted

    def test_key_value_patterns_with_sensitive_names(self):
        """Inline configuration fragments need the same name-based protection."""
        log = "config: DATABASE_PASSWORD=hunter2; OIDC_CLIENT_SECRET=def456; DEBUG=True"
        redacted = redact_text_for_bundle(log)
        assert "hunter2" not in redacted
        assert "def456" not in redacted
        # DEBUG=True is not sensitive — survives.
        assert "DEBUG=True" in redacted
        assert "DATABASE_PASSWORD=[REDACTED]" in redacted

    def test_idempotent_on_already_redacted_text(self):
        """Running the redactor twice must produce the same output.

        Re-running on already-redacted text is a real scenario when
        operators inspect a bundle, edit it, and re-zip. The
        redaction sentinels (``[REDACTED]``, ``[REDACTED-JWT]``,
        etc.) must NOT match any pattern.
        """
        original = "Authorization: Bearer abc123 and a secret eyJxxx.yyy.zzz"
        once = redact_text_for_bundle(original)
        twice = redact_text_for_bundle(once)
        assert once == twice

    def test_normal_log_lines_pass_through(self):
        """Useful run diagnostics survive when they contain no secret material."""
        log = (
            "2026-05-06T14:30:22Z INFO Validation run started run_id=abc123\n"
            "2026-05-06T14:30:23Z INFO Validation run completed status=SUCCEEDED\n"
        )
        redacted = redact_text_for_bundle(log)
        # Non-secret content survives.
        assert "Validation run started" in redacted
        assert "status=SUCCEEDED" in redacted


# ──────────────────────────────────────────────────────────────────────
# Pattern-drift contract: standalone redactor stays in sync
# ──────────────────────────────────────────────────────────────────────


class TestStandaloneRedactorPatternDrift:
    """The standalone ``deploy/self-hosted/scripts/redact-text.py``
    must apply the SAME redactions as ``redact_text_for_bundle``.

    The standalone exists because the GCP bundle recipe runs on the
    operator's workstation (no Validibot venv assumed); it imports
    only ``re`` and ``sys`` so it works against any system Python 3.
    The trade-off is that the patterns live in two places — this
    test catches accidental drift.

    We don't import the standalone module via ``importlib`` because
    its filename has a hyphen. Instead, we exec it as a subprocess
    against a representative input and assert the output matches
    what ``redact_text_for_bundle`` produces.
    """

    def test_standalone_output_matches_canonical(self):
        """Host-side redaction must remain byte-for-byte aligned with Django."""
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        script = repo_root / "deploy" / "self-hosted" / "scripts" / "redact-text.py"
        assert script.is_file(), f"{script} missing — see ADR Phase 6 follow-ups."

        # Representative input touching every pattern category, including
        # the trust-boundary headers added in the P1 review (cookie,
        # x402, service identity, legacy adapter key, and user subject).
        digest_body = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        test_input = (
            "Authorization: Bearer abc123def456\n"
            "X-API-Key: my-token-value\n"
            "Cookie: sessionid=abc; csrftoken=xyz\n"
            "Set-Cookie: sessionid=newvalue; HttpOnly\n"
            "X-CSRFToken: csrftoken-value-here\n"
            "X-X402-Payment: signed-payment-proof-blob\n"
            "X-Validibot-Service-Identity: oidc-id-token\n"
            "X-Validibot-Api-Token: user-token-here\n"
            "X-MCP-Service-Key: local-dev-shared-secret\n"
            "Proxy-Authorization: Basic dXNlcjpwYXNz\n"
            "JWT: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0In0.AbC123\n"
            "PEM: -----BEGIN RSA PRIVATE KEY-----\nMIIEoBASE64\n"
            "-----END RSA PRIVATE KEY-----\n"
            "URL: https://op:secret@host.example/path\n"
            "HASH: 0123456789abcdef0123456789abcdef0123456789abcdef\n"
            f"DIGEST: sha256:{digest_body}\n"
            "Bare cookie: sessionid=session-value-bare\n"
            "PASSWORD=hunter2 and DEBUG=True\n"
        )
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/env", "python3", str(script)],
            input=test_input,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        canonical = redact_text_for_bundle(test_input)

        assert result.stdout == canonical, (
            "Standalone redact-text.py drifted from "
            "validibot.core.support_bundle.redact_text_for_bundle. "
            "Re-sync the patterns in both files; see the docstring at "
            "the top of redact-text.py."
        )
