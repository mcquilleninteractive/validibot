"""Runtime tests for the Tabular Validator.

These exercise the validator end-to-end via ``TabularValidator().validate()``,
proving the reader → native-validation → finding-mapping chain works against
real ``Validator``/``Ruleset``/``Submission`` rows (not just the unit-tested
modules). The reader/native/schema logic itself is covered exhaustively in
``test_tabular_reader.py`` and ``test_tabular_native.py``; here we verify the
wiring: schema loaded from the ruleset, content read from the submission,
``NativeFinding``s mapped onto ``ValidationIssue`` with their count/samples in
``meta``, configuration/read errors surfaced as findings, and the validator
being discoverable through the registry.

These need the database (factories create rows and the validator runs the
assertion lane), so they use the ``db`` fixture / ``TestCase``.
"""

import json

from django.test import TestCase

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import run_context_with_file
from validibot.validations.validators.tabular.metadata import TABULAR_DATASET_INPUTS
from validibot.validations.validators.tabular.validator import CODE_INVALID_SCHEMA
from validibot.validations.validators.tabular.validator import TabularValidator


def _schema(fields, primary_key=None):
    """Serialise a Table Schema descriptor as ``rules_text`` JSON."""
    descriptor: dict = {"fields": fields}
    if primary_key is not None:
        descriptor["primaryKey"] = primary_key
    return json.dumps(descriptor)


def _validate(*, rules_text, content, metadata=None):
    """Build factory rows and run the validator; return the ValidationResult."""
    validator = ValidatorFactory(
        validation_type=ValidationType.TABULAR,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(ruleset_type=RulesetType.TABULAR, rules_text=rules_text)
    if metadata is not None:
        ruleset.metadata = metadata
        ruleset.save(update_fields=["metadata"])
    submission = SubmissionFactory(content=content, file_type=SubmissionFileType.TEXT)
    return _validate_submission(validator, submission, ruleset)


def _validate_submission(validator, submission, ruleset):
    """Run the validator with its table input supplied through the declared port."""
    return TabularValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context_with_file(
            contract_key="table_document",
            content=submission.content,
            file_type=SubmissionFileType.TEXT,
            name="submission.csv",
        ),
    )


def _codes(result):
    return {issue.code for issue in result.issues}


class TabularValidatorRuntimeTests(TestCase):
    """The validator's behaviour on valid input, native failures, and errors."""

    def test_valid_csv_passes_with_output_values(self):
        """A CSV that satisfies the schema passes, emits no issues, and returns
        the ``i.*`` dataset input values — the baseline happy path.
        """
        result = _validate(
            rules_text=_schema(
                [
                    {
                        "name": "lat",
                        "type": "number",
                        "constraints": {"minimum": -90, "maximum": 90},
                    },
                    {"name": "lon", "type": "number"},
                ],
            ),
            content="lat,lon\n10,20\n-5,30\n",
        )
        self.assertTrue(result.passed, result.issues)
        self.assertEqual(result.issues, [])
        self.assertEqual(
            tuple(result.output_values),
            tuple(contract_key for contract_key, _label in TABULAR_DATASET_INPUTS),
        )
        self.assertEqual(result.output_values["num_rows"], 2)
        self.assertEqual(result.output_values["column_names"], ["lat", "lon"])

    def test_native_finding_is_mapped_to_issue_with_meta(self):
        """An out-of-range value fails the run, and the native finding's count
        and sample rows survive into ``ValidationIssue.meta`` — proving the
        mapping carries the richer reporting shape, not just a message.
        """
        result = _validate(
            rules_text=_schema(
                [
                    {
                        "name": "lat",
                        "type": "number",
                        "constraints": {"minimum": -90, "maximum": 90},
                    },
                ],
            ),
            content="lat\n10\n200\n-95\n",
        )
        self.assertFalse(result.passed)
        issue = next(i for i in result.issues if i.code == "tabular.out_of_range")
        self.assertEqual(issue.path, "lat")
        self.assertEqual(issue.meta["count"], 2)
        self.assertEqual(issue.meta["sample_rows"], [2, 3])

    def test_missing_required_column_fails(self):
        """A required column absent from the file is reported as a finding."""
        result = _validate(
            rules_text=_schema(
                [{"name": "lat", "constraints": {"required": True}}],
            ),
            content="lon\n20\n",
        )
        self.assertFalse(result.passed)
        self.assertIn("tabular.missing_required_column", _codes(result))

    def test_uniqueness_violation_fails(self):
        """A duplicate value in a ``unique`` column fails the run."""
        result = _validate(
            rules_text=_schema(
                [{"name": "id", "constraints": {"unique": True}}],
            ),
            content="id\nA\nB\nA\n",
        )
        self.assertFalse(result.passed)
        self.assertIn("tabular.unique_violation", _codes(result))

    def test_invalid_schema_is_a_finding_not_a_crash(self):
        """A ruleset whose Table Schema won't parse fails with a clear
        configuration finding rather than raising — a misconfigured step
        should report, not 500.
        """
        result = _validate(rules_text="{ not valid json", content="a\n1\n")
        self.assertFalse(result.passed)
        self.assertIn(CODE_INVALID_SCHEMA, _codes(result))

    def test_read_error_is_a_finding(self):
        """A malformed body (a ragged row) surfaces as a parse-error finding —
        the strict reader's failure becomes a structured issue.
        """
        result = _validate(
            rules_text=_schema([{"name": "a"}, {"name": "b"}]),
            content="a,b\n1,2\n3,4,5\n",  # row 2 is ragged
        )
        self.assertFalse(result.passed)
        self.assertIn("tabular.parse_error", _codes(result))

    def test_row_cel_assertion_is_evaluated_and_not_double_handled(self):
        """A row CEL assertion (``options.tabular_stage == "row"``) is evaluated
        by the validator's row loop and surfaces a ``tabular.row_assertion_failed``
        finding for the offending rows.

        ``assertion_stats.total == 1`` is the key check that the *generic*
        assertion lane skipped it: if the lane had also picked it up (it
        classifies a ``row.*``-only CEL assertion as OUTPUT), the total would be
        2 and we'd see a spurious error from evaluating ``row.*`` against the
        unbound generic context.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            supports_assertions=True,
        )
        ruleset = RulesetFactory(
            ruleset_type=RulesetType.TABULAR,
            rules_text=_schema([{"name": "lat", "type": "number"}]),
        )
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "row.lat >= -90 && row.lat <= 90"},
            options={"tabular_stage": "row"},
            severity=Severity.ERROR,
            message_template="Latitude out of range.",
        )
        submission = SubmissionFactory(
            content="lat\n10\n200\n",
            file_type=SubmissionFileType.TEXT,
        )

        result = _validate_submission(validator, submission, ruleset)

        self.assertFalse(result.passed)
        issue = next(
            i for i in result.issues if i.code == "tabular.row_assertion_failed"
        )
        self.assertEqual(issue.message, "Latitude out of range.")
        self.assertEqual(issue.meta["sample_rows"], [2])
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 1)

    def test_failing_dataset_assertion_short_circuits_native_and_row(self):
        """A failing dataset (i.*) assertion stops before native/row work (ADR).

        The dataset gate runs first; when it fails with ERROR severity the native
        and row passes are skipped, so an out-of-range value native validation
        *would* have flagged never produces a finding, and the result records the
        short-circuit. This is the ADR's "a failing dataset assertion
        short-circuits the native + row work" contract.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            supports_assertions=True,
        )
        ruleset = RulesetFactory(
            ruleset_type=RulesetType.TABULAR,
            rules_text=_schema(
                [
                    {
                        "name": "a",
                        "type": "integer",
                        "constraints": {"minimum": 0, "maximum": 10},
                    },
                ],
            ),
        )
        # A dataset gate that fails on any file with more than one row.
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "i.num_rows <= 1"},
            options={"tabular_stage": "dataset"},
            severity=Severity.ERROR,
            message_template="Too many rows.",
        )
        # ``a=999`` is out of range — native validation WOULD flag it if it ran.
        submission = SubmissionFactory(
            content="a\n999\n998\n",
            file_type=SubmissionFileType.TEXT,
        )

        result = _validate_submission(validator, submission, ruleset)

        self.assertFalse(result.passed)
        codes = {issue.code for issue in result.issues}
        # Native was skipped, so the out-of-range value produced no finding.
        self.assertNotIn("tabular.out_of_range", codes)
        self.assertEqual(
            result.stats.get("short_circuited"),
            "dataset_assertion_failed",
        )

    def test_column_cel_assertion_runs_after_rows_and_is_not_double_handled(self):
        """A ``col.*`` assertion uses typed aggregates exactly once.

        The generic lane must skip the assertion because it cannot bind ``col``;
        assertion statistics therefore remain one total and one failure.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            supports_assertions=True,
        )
        ruleset = RulesetFactory(
            ruleset_type=RulesetType.TABULAR,
            rules_text=_schema([{"name": "depth", "type": "number"}]),
        )
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "col.depth.null_ratio < 0.25"},
            options={"tabular_stage": "column"},
            severity=Severity.ERROR,
            message_template="Too many depth values are missing.",
        )
        submission = SubmissionFactory(
            content="depth,marker\n1,a\n,b\n,c\n4,d\n",
            file_type=SubmissionFileType.TEXT,
        )

        result = _validate_submission(validator, submission, ruleset)

        self.assertFalse(result.passed)
        issue = next(
            item
            for item in result.issues
            if item.code == "tabular.column_assertion_failed"
        )
        self.assertEqual(issue.message, "Too many depth values are missing.")
        self.assertEqual(issue.path, "depth")
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 1)

    def test_headerless_uses_declared_names(self):
        """With ``has_header=false`` in metadata, declared field names align by
        position so native checks address the right columns.
        """
        result = _validate(
            rules_text=_schema(
                [
                    {
                        "name": "lat",
                        "type": "number",
                        "constraints": {"minimum": -90, "maximum": 90},
                    },
                    {"name": "lon", "type": "number"},
                ],
            ),
            content="200,20\n",  # no header row; lat=200 is out of range
            metadata={"has_header": False},
        )
        self.assertFalse(result.passed)
        issue = next(i for i in result.issues if i.code == "tabular.out_of_range")
        self.assertEqual(issue.path, "lat")

    def test_headerless_duplicate_schema_fields_is_a_finding_not_a_crash(self):
        """A headerless file validated against a schema with duplicate field
        names fails cleanly with a configuration finding — never a 500.

        This is the P1 regression. For a headerless file the schema field
        names become the dataframe's column labels; a duplicate label made
        ``frame[name]`` return a DataFrame, so native validation's
        ``frame[field.name].tolist()`` raised ``AttributeError`` mid-run.
        parse_table_schema now rejects the duplicate, and the validator turns
        that ``ValueError`` into a CODE_INVALID_SCHEMA finding — defense in
        depth even if a bad descriptor somehow reaches run time.
        """
        result = _validate(
            # Two fields both named "lat" — would crash native validation on a
            # headerless read before parse_table_schema learned to reject it.
            rules_text=_schema(
                [{"name": "lat", "type": "number"}, {"name": "lat"}],
            ),
            content="10,20\n-5,30\n",  # no header; names come from the schema
            metadata={"has_header": False},
        )
        self.assertFalse(result.passed)
        self.assertIn(CODE_INVALID_SCHEMA, _codes(result))


class TabularValidatorRegistrationTests(TestCase):
    """The validator must be discoverable through the registry at runtime."""

    def test_validator_is_registered_for_tabular_type(self):
        """``get_validator_class(TABULAR)`` resolves to ``TabularValidator``,
        proving the ``config.py`` is discovered and registered at startup.
        """
        from validibot.validations.validators.base.config import get_validator_class

        self.assertIs(get_validator_class(ValidationType.TABULAR), TabularValidator)
