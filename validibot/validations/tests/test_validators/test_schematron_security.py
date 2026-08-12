"""Security tests for the Schematron submission guard (ADR-2026-07-01, D8a).

The submitted XML is untrusted and ultimately feeds an XSLT engine, so the
Django-side guard (``security.assert_submission_is_safe_xml``) must reject
the classic XML attack classes *before any container is launched*:

- XXE / external entity resolution (file disclosure, SSRF)
- entity-expansion bombs ("billion laughs" memory exhaustion)
- DTD declarations outright (defusedxml ``forbid_dtd`` posture, matching
  ``validations/xml_utils.py``)
- oversize payloads and pathological nesting depth (resource caps)

Per the project testing standard, features handling user input always get
malicious-input coverage; these are the OWASP-style vectors relevant to an
XML→XSLT pipeline. The Saxon-side lockdown (doc()/document() denial etc.) is
container-side and covered by the backend repo's tests (layer C).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError

from validibot.validations.validators.schematron.security import SchematronSecurityError
from validibot.validations.validators.schematron.security import (
    assert_submission_is_safe_xml,
)
from validibot.validations.validators.schematron.validator import SchematronValidator

# Small caps for the resource-limit tests (defaults are 10 MB / depth 200).
TINY_MAX_BYTES = 64
TINY_MAX_DEPTH = 3
# A byte cap generous enough that the depth-case schema is refused for DEPTH,
# not size (so the two caps are exercised independently).
HARD_MAX_BYTES_FOR_DEPTH_CASE = 10_000

XXE_PAYLOAD = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    "<foo>&xxe;</foo>"
)

BILLION_LAUGHS = (
    '<?xml version="1.0"?>'
    "<!DOCTYPE lolz ["
    '<!ENTITY lol "lol">'
    '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
    "]>"
    "<lolz>&lol3;</lolz>"
)

PLAIN_DTD = '<?xml version="1.0"?><!DOCTYPE foo SYSTEM "foo.dtd"><foo/>'


class _StubSubmission:
    """Minimal duck-typed submission for exercising preprocess_submission."""

    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


def test_wellformed_xml_within_limits_passes():
    """A benign, well-formed document sails through the guard untouched."""
    assert_submission_is_safe_xml("<invoice><total>121.00</total></invoice>")


def test_xxe_external_entity_is_rejected():
    """An external-entity (XXE) payload is rejected, never resolved.

    Resolving it would disclose local files (or reach the network) from the
    worker — the canonical XML attack this guard exists to stop.
    """
    with pytest.raises(SchematronSecurityError, match="forbidden constructs"):
        assert_submission_is_safe_xml(XXE_PAYLOAD)


def test_billion_laughs_entity_expansion_is_rejected():
    """An entity-expansion bomb is rejected before any expansion happens.

    Expanding it would exhaust worker memory; defusedxml refuses entity
    declarations outright so the bomb never detonates.
    """
    with pytest.raises(SchematronSecurityError, match="forbidden constructs"):
        assert_submission_is_safe_xml(BILLION_LAUGHS)


def test_dtd_declaration_is_rejected_outright():
    """Any DTD declaration is rejected (forbid_dtd posture).

    Even a "harmless" external DTD reference is an SSRF/lookup vector; the
    xml_utils precedent forbids DTDs wholesale and this guard matches it.
    """
    with pytest.raises(SchematronSecurityError, match="forbidden constructs"):
        assert_submission_is_safe_xml(PLAIN_DTD)


def test_oversize_payload_is_rejected_with_a_clear_message():
    """A payload over the size cap is refused before parsing.

    Size is checked on raw bytes first so a huge document never even reaches
    the parser. The message names both sizes so users understand the limit.
    """
    big = "<a>" + "x" * TINY_MAX_BYTES + "</a>"
    with pytest.raises(SchematronSecurityError, match="too large"):
        assert_submission_is_safe_xml(big, max_bytes=TINY_MAX_BYTES)


def test_pathological_nesting_depth_is_rejected():
    """Nesting beyond the depth cap is refused (stack/recursion guard)."""
    deep = "<a><b><c><d><e/></d></c></b></a>"
    with pytest.raises(SchematronSecurityError, match="nests deeper"):
        assert_submission_is_safe_xml(deep, max_depth=TINY_MAX_DEPTH)


def test_empty_and_malformed_submissions_are_rejected():
    """Empty and non-well-formed submissions fail with user-facing messages."""
    with pytest.raises(SchematronSecurityError, match="empty"):
        assert_submission_is_safe_xml("   ")
    with pytest.raises(SchematronSecurityError, match="not well-formed"):
        assert_submission_is_safe_xml("<open><unclosed></open>")


def test_uploaded_schematron_source_is_validated_at_authoring_time():
    """The step-config guard accepts real .sch and rejects everything else.

    Authors get immediate feedback at upload: a well-formed document with
    the ISO Schematron root passes; a random XML document (or an XXE
    payload smuggled as "rules") is rejected with a clear message instead
    of failing later inside the container.
    """
    from validibot.validations.validators.schematron.security import (
        validate_schematron_source,
    )

    validate_schematron_source(
        '<schema xmlns="http://purl.oclc.org/dsdl/schematron">'
        "<pattern><rule context='/'><assert test='true()'>ok</assert>"
        "</rule></pattern></schema>",
    )

    with pytest.raises(SchematronSecurityError, match="root"):
        validate_schematron_source("<not-schematron/>")
    with pytest.raises(SchematronSecurityError, match="forbidden constructs"):
        validate_schematron_source(XXE_PAYLOAD)
    with pytest.raises(SchematronSecurityError, match="empty"):
        validate_schematron_source("   ")


def test_schematron_source_guard_rejects_entity_bombs_in_the_rules():
    """An author cannot upload a billion-laughs ``.sch`` any more than an XXE one.

    The rules document is XML too, so the authoring guard applies the same
    ``forbid_dtd`` posture: a nested-entity bomb carried in the rules is refused
    at upload (its DTD is rejected outright) rather than shipped to the container
    to be expanded. Malicious author input is guarded exactly like malicious
    submitter input.
    """
    from validibot.validations.validators.schematron.security import (
        validate_schematron_source,
    )

    with pytest.raises(SchematronSecurityError, match="forbidden constructs"):
        validate_schematron_source(BILLION_LAUGHS)


def test_schematron_source_guard_enforces_size_and_depth_caps(settings):
    """Oversize / pathologically deep rules are refused at authoring time.

    The rules guard shares the submission guard's resource caps, so an author
    cannot paste a 100 MB or absurdly nested ``.sch`` that would burden the
    container. The size cap is checked before parsing; the depth cap uses the
    same iterative walk as the submission guard.
    """
    from validibot.validations.validators.schematron.security import (
        validate_schematron_source,
    )

    ns = 'xmlns="http://purl.oclc.org/dsdl/schematron"'

    settings.SCHEMATRON_MAX_INPUT_BYTES = 200
    oversize = f"<schema {ns}>" + "<pattern/>" * 50 + "</schema>"
    with pytest.raises(SchematronSecurityError, match="too large"):
        validate_schematron_source(oversize)

    settings.SCHEMATRON_MAX_INPUT_BYTES = HARD_MAX_BYTES_FOR_DEPTH_CASE
    settings.SCHEMATRON_MAX_INPUT_DEPTH = TINY_MAX_DEPTH
    deep = (
        f"<schema {ns}><pattern><rule context='/'>"
        "<assert test='true()'><nested/></assert></rule></pattern></schema>"
    )
    with pytest.raises(SchematronSecurityError, match="deeper"):
        validate_schematron_source(deep)


def test_preprocess_submission_converts_guard_failures_to_validation_errors(
    monkeypatch,
):
    """The validator's preprocess hook rejects unsafe XML pre-dispatch.

    ``AdvancedValidator.validate()`` converts the ``ValidationError`` raised
    here into a clean failure result — meaning no container launch and no
    compute cost for a payload we would refuse anyway (D8a).
    """
    validator = SchematronValidator()
    monkeypatch.setattr(
        validator,
        "resolve_file_input",
        lambda *args, **kwargs: SimpleNamespace(content=XXE_PAYLOAD.encode()),
    )
    with pytest.raises(ValidationError):
        validator.preprocess_submission(
            step=None,
            submission=_StubSubmission(XXE_PAYLOAD),
        )
    # And a benign submission passes the same hook without complaint.
    monkeypatch.setattr(
        validator,
        "resolve_file_input",
        lambda *args, **kwargs: SimpleNamespace(content=b"<invoice/>"),
    )
    assert (
        validator.preprocess_submission(
            step=None,
            submission=_StubSubmission("<invoice/>"),
        )
        == {}
    )
