"""Darwin Core occurrence validation via the Tabular Validator.

This is the worked use case behind the "Validate Darwin Core with Validibot"
blog post. It proves that the first tier of OBIS-style quality control — the
checks that are expressible as a column schema plus per-row rules — can be done
with nothing but a Validibot ``tabular-validator`` ruleset, no custom code.

[Darwin Core](https://dwc.tdwg.org/) is the TDWG standard for biodiversity
occurrence records; [OBIS](https://obis.org/) ingests it and runs the
[obis-qc](https://github.com/iobis/obis-qc) quality-control pass over the data.
Each obis-qc check reads a Darwin Core term and asserts something about it. We
reproduce the schema-expressible subset here and map every check to the finding
it produces, which is the table the blog post walks through.

Two lanes of the validator are exercised, because Darwin Core QC splits the
same way obis-qc does:

* **Native, per-column checks** (``native.py``) — presence, type, numeric range,
  controlled-vocabulary (enum), regex, and uniqueness. These cover single-field
  obis-qc flags like ``LAT_OUT_OF_RANGE`` and ``NO_MATCH``-adjacent presence
  rules.
* **CEL row assertions** (``row_eval.py``) — cross-field rules a flat schema
  cannot express, such as depth ordering (``MIN_DEPTH_EXCEEDS_MAX``) and the
  Null Island guard (``ZERO_COORD``).

The checks that need *external* reference data — on-land detection, bathymetry,
WoRMS taxon resolution — are deliberately out of scope: a flat tabular schema
cannot reach a coastline grid or a species register, so those would be a
separate validator backend, not part of this example.

These tests call ``TabularValidator().validate()`` directly (the same harness as
``test_validators/test_tabular.py``) rather than going through the API + polling
flow used by the other use-case tests. The reason is precision: the blog needs
each Darwin Core rule pinned to an exact finding code and column, and the direct
call lets us assert that mapping without the noise of run orchestration. The
end-to-end engine wiring (reader → native → finding mapping → assertion lane) is
already covered next to the validator package.
"""

from __future__ import annotations

import json

import pytest

from tests.helpers.assets import load_test_asset
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.services.findings_display import format_failed_rows
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import run_context_with_file
from validibot.validations.validators.tabular.native import CODE_ENUM_VIOLATION
from validibot.validations.validators.tabular.native import CODE_OUT_OF_RANGE
from validibot.validations.validators.tabular.native import CODE_PATTERN_MISMATCH
from validibot.validations.validators.tabular.native import CODE_REQUIRED_VALUE_MISSING
from validibot.validations.validators.tabular.native import CODE_TYPE_ERROR
from validibot.validations.validators.tabular.native import CODE_UNIQUE_VIOLATION
from validibot.validations.validators.tabular.native import DEFAULT_REPORT_MAX_EXAMPLES
from validibot.validations.validators.tabular.row_eval import CODE_ROW_ASSERTION_FAILED
from validibot.validations.validators.tabular.schema import parse_table_schema
from validibot.validations.validators.tabular.validator import TabularValidator

pytestmark = pytest.mark.django_db

# The twelve Darwin Core occurrence terms the example schema declares, in order.
# Asserting against this exact list keeps the schema asset and the tests honest
# about which terms are in play.
DARWIN_CORE_COLUMNS = [
    "occurrenceID",
    "basisOfRecord",
    "occurrenceStatus",
    "scientificName",
    "scientificNameID",
    "eventDate",
    "decimalLatitude",
    "decimalLongitude",
    "coordinateUncertaintyInMeters",
    "minimumDepthInMeters",
    "maximumDepthInMeters",
    "individualCount",
]

# Cross-field rules a flat schema can't express, attached as ``row.*`` CEL
# assertions. These mirror obis-qc's MIN_DEPTH_EXCEEDS_MAX and ZERO_COORD flags.
DEPTH_ORDER_RULE = "row.minimumDepthInMeters <= row.maximumDepthInMeters"
NULL_ISLAND_RULE = "!(row.decimalLatitude == 0.0 && row.decimalLongitude == 0.0)"

# Two "semantic" rules that show the gap between a *schema-valid* row and a
# *meaningful* one — the heart of why manual assertions exist alongside the
# schema:
#
# * PRESENT_HAS_COUNT_RULE is a CROSS-FIELD CONDITIONAL the flat schema cannot
#   express: a value's validity in one column depends on another column. It
#   mirrors obis-qc treating ``individualCount == 0`` as an absence record.
# * POSITIVE_UNCERTAINTY_RULE closes an INCLUSIVE-BOUND gap: the schema declares
#   ``minimum: 0`` (inclusive), so 0 passes, but Darwin Core says zero is not a
#   valid coordinate uncertainty. The strict ``> 0`` lives in the assertion.
PRESENT_HAS_COUNT_RULE = 'row.occurrenceStatus != "present" || row.individualCount >= 1'
PRESENT_HAS_COUNT_MESSAGE = "A present occurrence must record at least one individual."
POSITIVE_UNCERTAINTY_RULE = "row.coordinateUncertaintyInMeters > 0.0"
POSITIVE_UNCERTAINTY_MESSAGE = (
    "coordinateUncertaintyInMeters must be greater than zero."
)


def _asset(name: str) -> str:
    """Read a Darwin Core CSV/JSON asset as decoded text."""
    return load_test_asset(f"assets/csv/darwin_core/{name}").decode("utf-8")


def _schema_descriptor() -> dict:
    """Parse the schema asset's JSON text into a Table Schema descriptor dict."""
    return json.loads(_asset("occurrence_schema.json"))


def _run(*, content: str, row_rules: tuple[tuple[str, str], ...] = ()):
    """Validate *content* against the Darwin Core schema asset.

    Builds the same three rows a real step would (validator + ruleset +
    submission), attaches any cross-field ``row_rules`` as CEL row assertions,
    and returns the ``ValidationResult``. ``row_rules`` is a tuple of
    ``(expression, message)`` pairs so a test can opt into the cross-field lane
    without dragging in assertions it doesn't care about.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.TABULAR,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.TABULAR,
        rules_text=_asset("occurrence_schema.json"),
    )
    for expression, message in row_rules:
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": expression},
            options={"tabular_stage": "row"},
            severity=Severity.ERROR,
            message_template=message,
        )
    submission = SubmissionFactory(content=content, file_type=SubmissionFileType.TEXT)
    run_context = run_context_with_file(
        contract_key="table_document",
        content=content,
        file_type=SubmissionFileType.TEXT,
        name="occurrence.csv",
    )
    return TabularValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context,
    )


def _paths_for(result, code: str) -> set[str]:
    """Return the set of columns (``issue.path``) flagged under *code*."""
    return {issue.path for issue in result.issues if issue.code == code}


def _issue_with(result, code: str):
    """Return the single issue with *code* (fails loudly if not exactly one)."""
    matches = [issue for issue in result.issues if issue.code == code]
    assert len(matches) == 1, f"expected one {code} issue, got {len(matches)}"
    return matches[0]


# ── The schema asset is itself a valid Table Schema ─────────────────────────
# A broken descriptor would make every other test fail in a confusing way, so we
# assert up front that the asset parses and declares what we think it does. This
# is the contract between the JSON file and the rest of the suite.
def test_schema_asset_is_a_well_formed_table_schema():
    """The shipped descriptor parses into the expected DwC columns + key.

    Guards the asset: if someone edits ``occurrence_schema.json`` and breaks the
    column set or the primary key, this fails first and points at the cause
    rather than letting a downstream data test fail mysteriously.
    """
    descriptor = _schema_descriptor()
    schema = parse_table_schema(descriptor)

    assert [field.name for field in schema.fields] == DARWIN_CORE_COLUMNS
    assert schema.primary_key == ("occurrenceID",)
    assert descriptor["primaryKey"] == "occurrenceID"
    # Spot-check that the constraint vocabulary survived the round-trip: the
    # enum on basisOfRecord, the WoRMS regex on scientificNameID, and the lat
    # range are the load-bearing rules the blog highlights.
    by_name = {field.name: field for field in schema.fields}
    assert by_name["basisOfRecord"].constraints.enum is not None
    assert by_name["scientificNameID"].constraints.pattern is not None
    assert by_name["decimalLatitude"].constraints.minimum == -90  # noqa: PLR2004
    # individualCount uses an *inclusive* minimum of 0 — the very gap the
    # POSITIVE_UNCERTAINTY_RULE / cross-field assertions exist to close, so the
    # asset must keep declaring it inclusively for that lesson to hold.
    assert by_name["individualCount"].type == "integer"
    assert by_name["coordinateUncertaintyInMeters"].constraints.minimum == 0


# ── Happy path: clean Darwin Core passes ────────────────────────────────────
# The baseline. Four real-shaped marine occurrence records that satisfy every
# column rule must pass with zero findings and surface the declared dataset outputs
# a downstream "row count" assertion would consume.
def test_valid_occurrence_file_passes_with_output_values():
    """A conformant Darwin Core file passes and exposes its dataset outputs.

    This is the contract the blog opens with: point the validator at good data
    and it gets out of the way. We also assert the output values so readers
    see that row/column metadata is available for dataset-level assertions.
    """
    result = _run(content=_asset("occurrence_valid.csv"))

    assert result.passed, result.issues
    assert result.issues == []
    assert result.output_values["num_rows"] == 4  # noqa: PLR2004
    assert result.output_values["column_names"] == DARWIN_CORE_COLUMNS


# ── Per-column (native) checks: one Darwin Core rule per finding ─────────────
# The heart of the example. ``occurrence_invalid.csv`` is built so each row
# violates exactly one column rule (plus a duplicate occurrenceID at the end),
# so the set of finding codes is a clean, predictable mapping from Darwin Core
# rule -> Validibot finding. This is the table the blog post renders.
def test_invalid_file_flags_each_darwin_core_column_rule():
    """Each native Darwin Core rule produces its expected finding code + column.

    Why this matters: it pins the Darwin-Core-term -> finding-code mapping that
    the blog post documents. If the validator's reporting shape drifts (a code
    renamed, a column no longer attributed), this test catches it and the post
    stays accurate. Every assertion ties a specific DwC term to the obis-qc-style
    check it stands in for.
    """
    result = _run(content=_asset("occurrence_invalid.csv"))

    assert not result.passed
    observed_codes = {issue.code for issue in result.issues}
    assert observed_codes == {
        CODE_OUT_OF_RANGE,
        CODE_ENUM_VIOLATION,
        CODE_PATTERN_MISMATCH,
        CODE_REQUIRED_VALUE_MISSING,
        CODE_TYPE_ERROR,
        CODE_UNIQUE_VIOLATION,
    }

    # Coordinate ranges: latitude 95 and longitude 200 are each out of bounds,
    # and the finding is attributed to the right DwC term (obis-qc LAT/LON_OUT).
    assert _paths_for(result, CODE_OUT_OF_RANGE) == {
        "decimalLatitude",
        "decimalLongitude",
    }
    # Controlled vocabularies: an unknown basisOfRecord and a bad
    # occurrenceStatus both fail enum membership — the DwC-blessed value sets.
    assert _paths_for(result, CODE_ENUM_VIOLATION) == {
        "basisOfRecord",
        "occurrenceStatus",
    }
    # Identifier format: a non-WoRMS scientificNameID fails the LSID regex.
    assert _paths_for(result, CODE_PATTERN_MISMATCH) == {"scientificNameID"}
    # Presence: an empty required scientificName is a nullability violation,
    # kept distinct from a type error (the empty cell is "missing", not "wrong").
    assert _paths_for(result, CODE_REQUIRED_VALUE_MISSING) == {"scientificName"}
    # Type: "abc" in decimalLatitude can't be coerced to a number.
    assert _paths_for(result, CODE_TYPE_ERROR) == {"decimalLatitude"}
    # Uniqueness: occurrenceID is the primary key, and occ-101 appears twice —
    # reported once, citing both offending data rows (1 and 8, 1-based).
    unique_issue = _issue_with(result, CODE_UNIQUE_VIOLATION)
    assert unique_issue.path == "occurrenceID"
    assert unique_issue.meta["sample_rows"] == [1, 8]


# ── Cross-field (CEL) checks: rules a flat schema can't express ──────────────
# Depth ordering and the Null Island guard compare two columns within a row, so
# they live as row.* CEL assertions, evaluated per row by the validator's own
# loop. The fixture rows are valid column-by-column on purpose, so only the
# cross-field rule fires — isolating this lane from the native one.
def test_cross_field_rules_flag_depth_order_and_null_island():
    """Depth ordering and Null Island are caught by row CEL assertions.

    These reproduce obis-qc's MIN_DEPTH_EXCEEDS_MAX and ZERO_COORD flags, which
    a column schema can't express because each compares two fields of the same
    row. The test proves the row lane fires for exactly the offending rows and
    that the validator counts these as assertions (not rows) in its stats — the
    distinction the blog draws between "schema" and "row rules".
    """
    result = _run(
        content=_asset("occurrence_cross_field.csv"),
        row_rules=(
            (DEPTH_ORDER_RULE, "Minimum depth exceeds maximum depth."),
            (NULL_ISLAND_RULE, "Coordinates are at Null Island (0, 0)."),
        ),
    )

    assert not result.passed
    # Both cross-field rules surface under the same row-assertion code; they are
    # told apart by their message and the row they cite.
    row_issues = [i for i in result.issues if i.code == CODE_ROW_ASSERTION_FAILED]
    by_message = {issue.message: issue for issue in row_issues}
    assert set(by_message) == {
        "Minimum depth exceeds maximum depth.",
        "Coordinates are at Null Island (0, 0).",
    }
    # Row 1 has min=300 > max=50; row 2 sits at (0, 0). Row 3 is clean.
    assert by_message["Minimum depth exceeds maximum depth."].meta["sample_rows"] == [1]
    assert by_message["Coordinates are at Null Island (0, 0)."].meta["sample_rows"] == [
        2,
    ]
    # Two row assertions ran, both failed — counted as assertions, not rows.
    assert result.assertion_stats.total == 2  # noqa: PLR2004
    assert result.assertion_stats.failures == 2  # noqa: PLR2004


# ── Schema-valid but semantically wrong: where manual assertions earn their keep
# This is the headline of the example. Every row in the fixture is *structurally*
# valid Darwin Core — it passes the Frictionless schema with zero findings — yet
# two of the three rows are *meaningfully* wrong and are caught only by manual
# CEL assertions. The two phases (schema alone vs. schema + assertions) make the
# syntactic-vs-semantic distinction concrete.
def test_schema_valid_rows_can_still_fail_manual_assertions():
    """A row can pass the column schema and still fail a manual assertion.

    Why this matters: it is the whole reason assertions exist alongside the
    schema. A Frictionless Table Schema checks each column's *shape* (type,
    range, enum, regex); it cannot see a cross-field relationship, and its
    ``minimum: 0`` is inclusive. So a ``present`` record with
    ``individualCount = 0`` and a record with ``coordinateUncertaintyInMeters =
    0`` are both perfectly schema-valid, yet both are wrong by Darwin Core's
    meaning. We prove the schema accepts them, then prove the assertions reject
    them — and that *no* native/schema finding fires, isolating the failure to
    the semantic lane.
    """
    content = _asset("occurrence_schema_valid_assertion_invalid.csv")

    # Phase 1 — schema alone: every row is structurally valid Darwin Core, so the
    # native lane finds nothing. This is the "passes the Frictionless schema"
    # half of the demonstration.
    schema_only = _run(content=content)
    assert schema_only.passed, schema_only.issues
    assert schema_only.issues == []

    # Phase 2 — add the two manual assertions. Now the same rows fail, but on
    # *meaning*, not shape.
    with_rules = _run(
        content=content,
        row_rules=(
            (PRESENT_HAS_COUNT_RULE, PRESENT_HAS_COUNT_MESSAGE),
            (POSITIVE_UNCERTAINTY_RULE, POSITIVE_UNCERTAINTY_MESSAGE),
        ),
    )

    assert not with_rules.passed
    # The crux: every issue is a row-assertion failure — there is not a single
    # native/schema finding, because the data is schema-valid. The failures are
    # purely semantic.
    assert {issue.code for issue in with_rules.issues} == {CODE_ROW_ASSERTION_FAILED}

    by_message = {issue.message: issue for issue in with_rules.issues}
    assert set(by_message) == {PRESENT_HAS_COUNT_MESSAGE, POSITIVE_UNCERTAINTY_MESSAGE}
    # Row 1 is present with individualCount 0 (fails the cross-field rule);
    # row 2 has coordinateUncertaintyInMeters 0 (fails the strict-positive rule);
    # row 3 satisfies both. Each rule cites exactly its offending row.
    assert by_message[PRESENT_HAS_COUNT_MESSAGE].meta["sample_rows"] == [1]
    assert by_message[POSITIVE_UNCERTAINTY_MESSAGE].meta["sample_rows"] == [2]
    assert with_rules.assertion_stats.total == 2  # noqa: PLR2004
    assert with_rules.assertion_stats.failures == 2  # noqa: PLR2004


# ── At scale: a failing assertion caps examples, then says "and more"
# A single row assertion that fails on thousands of rows must stay one readable
# finding: the true total in ``count``, a bounded set of example row numbers,
# and an explicit truncation marker so the reader knows the list is partial.
def test_row_assertion_reports_default_failing_rows_then_truncates():
    """A bulk row-assertion failure uses the shared example cap.

    This is the headline behaviour behind "report which rows failed": with 150
    rows all violating the positive-uncertainty rule, the finding carries the
    full ``count`` (150) but only the first configured-default example rows,
    and the shared display helper makes that truncation explicit. Importing the
    production constant keeps this use-case contract aligned with the native
    and row-assertion validators rather than duplicating a stale literal.
    """
    failing_rows = 150
    header = ",".join(DARWIN_CORE_COLUMNS)

    def zero_uncertainty_row(index: int) -> str:
        # A fully schema-valid present occurrence whose only flaw is a zero
        # coordinate uncertainty — so only the manual rule fires, on every row.
        # occurrenceID stays unique to satisfy the primary key.
        return (
            f"occ-{index},HumanObservation,present,Gadus morhua,"
            f"urn:lsid:marinespecies.org:taxname:126436,2019-05-05,"
            f"60.0,5.0,0,0,100,1"
        )

    content = "\n".join(
        [header, *(zero_uncertainty_row(i) for i in range(1, failing_rows + 1))],
    )

    result = _run(
        content=content,
        row_rules=((POSITIVE_UNCERTAINTY_RULE, POSITIVE_UNCERTAINTY_MESSAGE),),
    )

    assert not result.passed
    issue = next(i for i in result.issues if i.code == CODE_ROW_ASSERTION_FAILED)
    # Every row failed (the true total), but only the default examples are listed.
    assert issue.meta["count"] == failing_rows
    assert len(issue.meta["sample_rows"]) == DEFAULT_REPORT_MAX_EXAMPLES
    assert issue.meta["sample_rows"][0] == 1
    assert issue.meta["sample_rows"][-1] == DEFAULT_REPORT_MAX_EXAMPLES
    # The shared display helper turns that meta into the user-facing line, with
    # the truncation made explicit rather than hidden.
    rendered = format_failed_rows(issue.meta)
    assert rendered.startswith("row numbers: 1, 2, 3,")
    assert rendered.endswith(
        f"(showing first {DEFAULT_REPORT_MAX_EXAMPLES} of {failing_rows})",
    )


# ── The eventDate nuance: Darwin Core dates are not strict ISO datetimes ─────
# Darwin Core eventDate permits truncated dates and intervals, which the
# validator's `date` type would reject. The schema therefore types eventDate as
# string + regex. This test documents the boundary: truncated/interval forms
# pass, free text fails — the trade-off the blog calls out explicitly.
def test_eventdate_accepts_truncated_and_interval_but_rejects_freetext():
    """The eventDate regex admits real DwC date forms and rejects prose.

    Why string + regex instead of the `date` type: DwC eventDate legitimately
    carries `2009`, `2009-02`, and `2009-02-20/2009-03-01`, which strict ISO
    8601 coercion would flag as type errors. The regex accepts those while still
    rejecting free text like "spring 2009" — proving the schema follows Darwin
    Core's reality, not a stricter ideal. The free-text row is the only failure,
    and it is attributed to eventDate.
    """
    header = ",".join(DARWIN_CORE_COLUMNS)

    def row(occ_id: str, event_date: str) -> str:
        # A row valid in every column except (optionally) eventDate, so the only
        # thing under test is the date value. The trailing ``1`` is
        # individualCount (a present occurrence with one individual).
        return (
            f"{occ_id},HumanObservation,present,Gadus morhua,"
            f"urn:lsid:marinespecies.org:taxname:126436,{event_date},"
            f"60.0,5.0,50,0,100,1"
        )

    content = "\n".join(
        [
            header,
            row("occ-301", "2009"),  # year only
            row("occ-302", "2009-02"),  # year-month
            row("occ-303", "2009-02-20"),  # full date
            row("occ-304", "2009-02-20/2009-03-01"),  # interval
            row("occ-305", "spring 2009"),  # free text -> must fail
        ],
    )

    result = _run(content=content)

    assert not result.passed
    pattern_issue = _issue_with(result, CODE_PATTERN_MISMATCH)
    assert pattern_issue.path == "eventDate"
    # Only the fifth data row (free text) is flagged; the four real DwC forms
    # passed the pattern.
    assert pattern_issue.meta["sample_rows"] == [5]
