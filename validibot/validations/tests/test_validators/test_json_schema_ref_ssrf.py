"""Security regression tests for the JSON Schema validator's $ref handling.

WHY THIS SUITE EXISTS
=====================
``JsonSchemaValidator`` builds a ``Draft202012Validator`` from an
author-controlled ruleset schema. Stock ``jsonschema`` resolves *external*
``$ref`` URIs by fetching them — it opens an outbound socket for ``http(s)://``
refs and reads local files for ``file://`` refs. Because the schema text is
attacker-controllable (any workflow author supplies it), that behaviour is a
classic Server-Side Request Forgery (SSRF) and local-file-read primitive: a
malicious schema could point a ``$ref`` at the cloud metadata endpoint
``http://169.254.169.254/...`` and exfiltrate credentials, or read
``file:///etc/passwd``.

The fix wires an explicit ``referencing.Registry`` whose ``retrieve`` callback
rejects every external URI, so an offending ``$ref`` surfaces as a handled
``Unresolvable`` error (returned as a controlled ERROR issue) instead of a
network/filesystem fetch. Internal ``#/$defs`` refs must keep working.

These tests are deliberately narrow: one proves no socket is opened for a remote
``$ref`` (the core SSRF guarantee), and one proves a legitimate internal ``$ref``
still resolves so the fix did not break valid schemas.
"""

import socket

import pytest

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import run_context_with_file
from validibot.validations.validators.json_schema.validator import JsonSchemaValidator

# A remote $ref aimed at the cloud metadata service — the canonical SSRF target.
# If the validator ever fetches it, credentials could be exfiltrated, so the
# regression test asserts no socket is ever opened toward any host.
_METADATA_SSRF_REF = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"


def _validate_json(validator, submission, ruleset):
    """Validate exact JSON bytes through the declared JSON document port."""
    return JsonSchemaValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context_with_file(
            contract_key="json_document",
            content=submission.content,
            file_type=SubmissionFileType.JSON,
        ),
    )


def test_remote_ref_opens_no_socket_and_yields_controlled_error(db, monkeypatch):
    """A schema with a remote http ``$ref`` must NOT open an outbound socket.

    This is the heart of the SSRF fix. We monkeypatch ``socket.socket.connect``
    so that ANY attempt to open a connection raises loudly. If the old,
    vulnerable code path ran, ``jsonschema`` would try to fetch the remote
    ``$ref`` while resolving the schema and trip this guard. With the locked-down
    ``referencing`` registry in place, resolution fails locally instead, so no
    socket is opened and the validator returns a handled ERROR issue rather than
    fetching the URL or raising an unhandled exception.
    """

    def _forbid_connect(self, *args, **kwargs):
        """Fail the test the instant any outbound connection is attempted."""
        msg = "SSRF: outbound socket opened during JSON Schema validation"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", _forbid_connect)

    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        supports_assertions=False,
    )
    # The whole schema is a single remote $ref, so resolving it is unavoidable
    # during validation — the strongest form of the test.
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.JSON_SCHEMA,
        rules_text=f'{{"$ref": "{_METADATA_SSRF_REF}"}}',
    )
    submission = SubmissionFactory(
        content='{"any": "value"}',
        file_type=SubmissionFileType.JSON,
    )

    # Must return normally (no socket, no unhandled exception) ...
    result = _validate_json(validator, submission, ruleset)

    # ... and the external ref must be reported as a controlled validation error.
    assert result.passed is False
    assert len(result.issues) >= 1
    assert result.issues[0].severity == Severity.ERROR
    # The error explains the external resource was refused, proving we handled it
    # rather than silently fetching it.
    assert "external resource" in result.issues[0].message


def test_internal_ref_still_resolves_after_ssrf_hardening(db):
    """Legitimate intra-schema ``#/$defs`` refs must keep working.

    The SSRF guard rejects *external* URIs only. Internal refs are resolved from
    the schema document itself (never "retrieved"), so they must continue to
    validate correctly. This guards against an over-broad fix that would break
    every schema using ``$defs`` — a common, fully safe pattern.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        supports_assertions=False,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.JSON_SCHEMA,
        rules_text=(
            '{"type": "object", '
            '"properties": {"height": {"$ref": "#/$defs/positive"}}, '
            '"required": ["height"], '
            '"$defs": {"positive": {"type": "integer", "minimum": 0}}}'
        ),
    )

    # A value violating the internal ref's constraint must still be flagged,
    # proving the internal ref resolved and enforced its rule.
    bad_submission = SubmissionFactory(
        content='{"height": -5}',
        file_type=SubmissionFileType.JSON,
    )
    bad_result = _validate_json(validator, bad_submission, ruleset)
    assert bad_result.passed is False

    # A value satisfying the internal ref must pass cleanly.
    good_submission = SubmissionFactory(
        content='{"height": 5}',
        file_type=SubmissionFileType.JSON,
    )
    good_result = _validate_json(validator, good_submission, ruleset)
    assert good_result.passed is True


if __name__ == "__main__":  # pragma: no cover - convenience for direct runs
    pytest.main([__file__, "-v"])
