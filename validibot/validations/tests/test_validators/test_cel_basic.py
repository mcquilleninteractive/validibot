"""Tests for CEL expression evaluation in the BasicValidator.

Covers payload, validator-output, nested-value, and literal CEL expressions,
including helper functions (size, matches, startsWith), when-guard skipping,
invalid-expression error reporting, dotted-key resolution, and missing-value
handling.

These BasicValidator tests primarily exercise raw submission data through the
``p`` (payload) namespace (for example, ``p.price < 10``). Author-named
workflow signals use ``s.*``; validator outputs and upstream step outputs use
``output`` and ``steps`` respectively.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from django.test import TestCase

from validibot.actions.protocols import RunContext
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import resolved_file_input
from validibot.validations.validators.basic import BasicValidator as _BasicValidator


class BasicValidator(_BasicValidator):
    """Bind each focused engine test to the Basic validator's document port."""

    def validate(
        self,
        validator,
        submission,
        ruleset,
        run_context=None,
    ):
        """Supply the exact submission bytes through a typed resolved input."""
        context = run_context or RunContext()
        context.resolved_file_inputs["document"] = resolved_file_input(
            contract_key="document",
            content=submission.content,
            file_type=SubmissionFileType.JSON,
        )
        return super().validate(
            validator,
            submission,
            ruleset,
            run_context=context,
        )


class CelBasicValidatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            org=cls.org,
        )
        # Step I/O definitions spanning input/output/derived
        cls.input_definition = StepIODefinitionFactory(
            validator=cls.validator,
            contract_key="price",
            direction="input",
        )
        cls.output_definition = StepIODefinitionFactory(
            validator=cls.validator,
            contract_key="result.total",
            direction="output",
        )
        cls.output_status = StepIODefinitionFactory(
            validator=cls.validator,
            contract_key="result.status",
            direction="output",
        )
        cls.required_entry = StepIODefinitionFactory(
            validator=cls.validator,
            contract_key="required_value",
            direction="input",
        )
        cls.list_definition = StepIODefinitionFactory(
            validator=cls.validator,
            contract_key="items",
            direction="input",
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.BASIC,
        )
        cls.project = ProjectFactory(org=cls.org)

    def _submission(self, payload: dict) -> SubmissionFactory:
        submission = SubmissionFactory(org=self.org, project=self.project)
        submission.content = json.dumps(payload)
        submission.save(update_fields=["content"])
        return submission

    def test_true_expression_on_payload_value(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.price < 10"},
        )
        submission = self._submission({"price": 5})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_list_size_helper(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "size(p.items) == 2"},
        )
        submission = self._submission({"items": [{"sku": "A"}, {"sku": "B"}]})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_false_expression_on_payload_value(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.price < 10"},
            severity=Severity.ERROR,
        )
        submission = self._submission({"price": 25})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("Peak too high", result.issues[0].message)

    # ── Quantity-aware finding messages ─────────────────────────────
    # Direct comparisons preserve enough dimensional meaning to format both
    # operands; arithmetic and compound CEL expressions deliberately do not.

    def test_simple_output_comparison_formats_template_values_with_units(self):
        """A direct output comparison should format both message operands.

        EnergyPlus EUI findings motivated this behavior: the output catalog
        already declares ``kWh/m²``, so showing a raw float and unitless target
        would discard useful contract metadata at the final presentation step.
        """

        StepIODefinitionFactory(
            validator=self.validator,
            contract_key="eui",
            label="Modeled Site EUI (kWh/m²)",
            direction="output",
            unit="kWh/m²",
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "o.eui < 0.5"},
            message_template="EUI was {{ o.eui }} which is greater than target of 0.5",
        )
        engine = BasicValidator()

        result = engine.evaluate_assertions_for_stage(
            validator=self.validator,
            ruleset=self.ruleset,
            payload={"eui": 452.2485348642507},
            stage="output",
        )

        self.assertEqual(len(result.issues), 1)
        self.assertEqual(
            result.issues[0].message,
            "EUI was 452.25 kWh/m² which is greater than target of 0.50 kWh/m²",
        )

    def test_simple_output_comparison_generates_unit_aware_default_message(self):
        """A missing custom message should still identify both values and units."""

        StepIODefinitionFactory(
            validator=self.validator,
            contract_key="eui",
            label="Modeled Site EUI (kWh/m²)",
            direction="output",
            unit="kWh/m²",
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "o.eui < 0.5"},
            message_template="",
        )
        engine = BasicValidator()

        result = engine.evaluate_assertions_for_stage(
            validator=self.validator,
            ruleset=self.ruleset,
            payload={"eui": 452.2485348642507},
            stage="output",
        )

        self.assertEqual(len(result.issues), 1)
        self.assertEqual(
            result.issues[0].message,
            "Modeled Site EUI was 452.25 kWh/m²; expected < 0.50 kWh/m².",
        )

    def test_arithmetic_expression_does_not_infer_quantity_units(self):
        """CEL arithmetic must not inherit a source value's unit automatically.

        Dividing or converting a declared output can change its dimension.  The
        conservative parser therefore leaves complex templates exactly as the
        author wrote them instead of attaching potentially false units.
        """

        StepIODefinitionFactory(
            validator=self.validator,
            contract_key="eui",
            direction="output",
            unit="kWh/m²",
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "o.eui / 2.0 < 0.5"},
            message_template="Adjusted EUI was {{ o.eui }}",
        )
        engine = BasicValidator()

        result = engine.evaluate_assertions_for_stage(
            validator=self.validator,
            ruleset=self.ruleset,
            payload={"eui": 452.2485348642507},
            stage="output",
        )

        self.assertEqual(len(result.issues), 1)
        self.assertEqual(
            result.issues[0].message,
            "Adjusted EUI was 452.2485348642507",
        )

    def test_when_guard_skips_expression(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.price < 10"},
            when_expression="p.price > 100",
        )
        submission = self._submission({"price": 20})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_invalid_expression_reports_error(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.price < "},  # invalid CEL
        )
        submission = self._submission({"price": 5})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("CEL evaluation failed", result.issues[0].message)

    # ── now() in generic CEL is bound to the run clock (P2 regression) ──
    # now() is allowlisted for authoring across ALL validator types, so an
    # author can save it on a Basic/JSON/XML step. Previously the generic
    # runtime never bound now(), so such an assertion failed every run — the
    # authoring allowlist and the runtime disagreed. The fix pins now() to
    # run.started_at in the generic evaluator (matching the tabular row stage).

    def test_now_is_bound_to_the_run_clock(self):
        """A saved generic CEL assertion using ``now()`` evaluates against the
        run's pinned clock (``run.started_at``), not the wall clock.

        We pin ``started_at`` to a known instant and assert ``now()`` equals it
        exactly — proving the binding flows from the run context through the
        generic evaluator, so a time-relative assertion is deterministic.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'now() == timestamp("2026-06-01T12:00:00Z")'},
            severity=Severity.ERROR,
        )
        submission = self._submission({})
        run_context = RunContext(
            validation_run=SimpleNamespace(
                started_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            ),
        )
        engine = BasicValidator()

        result = engine.validate(
            self.validator,
            submission,
            self.ruleset,
            run_context=run_context,
        )

        self.assertTrue(result.passed, result.issues)
        self.assertEqual(len(result.issues), 0)

    def test_now_without_run_clock_fails_cleanly(self):
        """Without a run clock ``now()`` stays unbound and the assertion fails
        cleanly (one issue) — never the wall clock, never a 500.

        This is the deliberate degradation: ``now()`` is usable when a run pins
        a clock and fails loudly when one isn't available, so it can never
        silently read a nondeterministic wall-clock value.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'now() == timestamp("2026-06-01T12:00:00Z")'},
            severity=Severity.ERROR,
        )
        submission = self._submission({})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)

    def test_dotted_slug_resolution(self):
        """Dotted path access on the payload namespace should resolve
        nested values correctly. ``p.metrics.avg`` navigates into the
        raw payload dict via CEL's native dot-access on MapType.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.metrics.avg == 3"},
        )
        submission = self._submission({"metrics": {"avg": 3}})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_missing_payload_key_produces_evaluation_error(self):
        """Accessing a missing payload key via ``p.missing_key`` produces
        a CEL evaluation error (field not found).  Under the namespaced
        design, there is no implicit None injection for missing keys —
        the author must guard with ``has(p.missing_key)`` when absence is
        permitted.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.required_value == null"},
        )
        submission = self._submission({"price": 5})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("CEL evaluation failed", result.issues[0].message)

    def test_matches_helper_on_input(self):
        slug = "serial"
        StepIODefinitionFactory(
            validator=self.validator,
            contract_key=slug,
            direction="input",
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'p.serial.matches("ITEM-[0-9]+")'},
        )
        submission = self._submission({"serial": "ITEM-123"})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_output_stage_assertion(self):
        output_definition = self.output_definition
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_io_definition=output_definition,
            target_data_path="",
            rhs={"expr": "output.result.total == 5"},
        )
        self._submission({"price": 1})
        engine = BasicValidator()
        # Manually call assertion evaluation for output context
        result = engine.evaluate_assertions_for_stage(
            ruleset=self.ruleset,
            validator=self.validator,
            payload={"result": {"total": 5}},
            stage="output",
        )
        self.assertEqual(len(result.issues), 0)

    def test_startswith_helper_on_output(self):
        status_definition = self.output_status
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_io_definition=status_definition,
            target_data_path="",
            rhs={"expr": 'output.result.status.startsWith("OK")'},
        )
        engine = BasicValidator()
        result = engine.evaluate_assertions_for_stage(
            ruleset=self.ruleset,
            validator=self.validator,
            payload={"result": {"status": "OK_PASSED"}},
            stage="output",
        )
        self.assertEqual(len(result.issues), 0)

    def test_simple_string_prefix(self):
        StepIODefinitionFactory(
            validator=self.validator,
            contract_key="serial",
            direction="input",
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'p.serial.startsWith("ITEM-")'},
        )
        submission = self._submission({"serial": "ITEM-1234"})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)

    def test_failure_message_template_interpolates_workflow_constants(self):
        """CEL finding messages render constants from the same ``c.*`` context.

        Constants are most useful in CEL expressions, so a failure message that
        explains the failed threshold must be able to display the same constant
        value that the expression evaluated.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.name == c.bubba"},
            message_template="Not the same as bubba's value {{ c.bubba }}",
        )
        submission = self._submission({"name": "not-bubba"})
        engine = BasicValidator()
        run_context = RunContext(workflow_constants={"bubba": "dance"})

        result = engine.validate(
            self.validator,
            submission,
            self.ruleset,
            run_context=run_context,
        )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(
            result.issues[0].message,
            "Not the same as bubba's value dance",
        )

    def test_success_message_template_interpolates_workflow_constants(self):
        """CEL success findings render constants from the same ``c.*`` context."""
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.name == c.bubba"},
            success_message="Matched bubba's value {{ c.bubba }}",
        )
        submission = self._submission({"name": "dance"})
        engine = BasicValidator()
        run_context = RunContext(workflow_constants={"bubba": "dance"})

        result = engine.validate(
            self.validator,
            submission,
            self.ruleset,
            run_context=run_context,
        )

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].message, "Matched bubba's value dance")

    def test_simple_math(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "1 + 1 == 2"},
        )
        submission = self._submission({"price": 1})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)

    def test_list_index_and_map_access(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.items[0]['sku'] == 'A'"},
        )
        submission = self._submission({"items": [{"sku": "A"}, {"sku": "B"}]})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
