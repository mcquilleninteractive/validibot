"""XSD layer of the calibration-certificate two-layer demo (blog worked example).

These fixtures (``tests/assets/schematron/calibration/``) back the public blog
post on pairing an XSD structural check with a Schematron business-rule check,
using a small DCC-inspired calibration certificate. This module owns the **XSD
half** of that story; the Schematron half — which needs the real Saxon/XSLT-2.0
engine — lives in the validator-backends repo's layer-C engine tests
(``validator_backends/schematron/tests/test_calibration_engine.py``), because
``lxml.isoschematron`` (the community test substitute) is XSLT-1.0 only and
cannot run the ``queryBinding="xslt2"`` rules.

The load-bearing claim proven here is the premise the whole two-layer argument
rests on: **both** the "valid" and the "invalid" certificate are *structurally
valid against the XSD*. If the invalid document failed the XSD, the second
(Schematron) layer would have nothing meaningful left to catch, and the blog's
thesis — "XSD proves shape, Schematron proves meaning" — would be hollow. So a
regression here (e.g. an over-tightened XSD that starts rejecting the invalid
file) would silently undermine the published example.

The tests drive the real ``XmlSchemaValidator`` with factory-built models, so
they exercise the same execution path a workflow's XML Validator step uses, not
a hand-rolled lxml call.
"""

from __future__ import annotations

from pathlib import Path

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import run_context_with_file
from validibot.validations.validators.xml_schema.validator import XmlSchemaValidator

# Fixtures resolve relative to the repo root (pytest's rootdir), mirroring the
# other asset-backed suites (e.g. ``test_schematron_fixture_helper``).
CALIBRATION = Path("tests/assets/schematron/calibration")


def _xsd_ruleset():
    """Build a ruleset carrying the demo calibration XSD as its schema text.

    The schema travels in ``rules_text`` with ``schema_type`` in metadata, the
    way a real reusable XSD asset does, so the validator resolves it exactly as
    it would for an author-uploaded schema.
    """
    return RulesetFactory(
        ruleset_type=RulesetType.XML_SCHEMA,
        rules_text=(CALIBRATION / "calibration-certificate-demo.xsd").read_text(
            encoding="utf-8",
        ),
        metadata={"schema_type": XMLSchemaType.XSD.value},
    )


def _xml_submission(filename: str):
    """Wrap one calibration XML fixture as an XML submission."""
    return SubmissionFactory(
        content=(CALIBRATION / filename).read_text(encoding="utf-8"),
        file_type=SubmissionFileType.XML,
    )


def _validate_xml(validator, submission, ruleset):
    """Validate the fixture through the XML validator's declared input port."""
    return XmlSchemaValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context_with_file(
            contract_key="xml_document",
            content=submission.content,
            file_type=SubmissionFileType.XML,
        ),
    )


# ── The XSD accepts a well-formed calibration certificate ────────────────────
# Baseline: the "good" document is genuinely well-shaped, so the structural
# layer must pass it cleanly. Establishes that the XSD is not vacuously
# rejecting everything before we make the subtler claim below.


def test_valid_certificate_passes_the_xsd(db):
    """The blog's clean certificate is structurally valid against the demo XSD.

    This is the uncontroversial baseline: a document that is correct in every
    way passes the shape contract with zero schema errors.
    """
    validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)

    submission = _xml_submission("calibration-certificate-valid.xml")
    result = _validate_xml(
        validator,
        submission,
        _xsd_ruleset(),
    )

    assert result.passed is True
    assert result.stats["schema_error_count"] == 0


# ── The XSD ALSO accepts the "invalid" certificate ───────────────────────────
# This is the crux of the whole two-layer demo. The invalid file is broken only
# in ways a grammar cannot express (dates that contradict, units that mismatch,
# arithmetic that doesn't reconcile), so it is still *structurally* valid. If
# this ever starts failing, the Schematron layer would be catching an XSD reject
# instead of demonstrating the capability gap it exists to fill.


def test_invalid_certificate_still_passes_the_xsd(db):
    """The business-invalid certificate is nonetheless XSD-valid.

    The document has the wrong issue date, a missing accreditation id, a
    mismatched result unit, an out-of-range point, and a verdict that
    contradicts its tolerance — none of which an XSD can see. Proving it passes
    the structural layer is what gives the Schematron layer something to catch,
    and is the exact premise the published example depends on.
    """
    validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)

    submission = _xml_submission("calibration-certificate-invalid.xml")
    result = _validate_xml(
        validator,
        submission,
        _xsd_ruleset(),
    )

    assert result.passed is True
    assert result.stats["schema_error_count"] == 0
