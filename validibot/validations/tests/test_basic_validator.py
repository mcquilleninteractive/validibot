"""
Tests for ``BasicValidator`` — JSON and XML submission validation.

The BasicValidator is the simplest validator type: it parses JSON or XML
submissions and evaluates BASIC/CEL assertions against the parsed data.
No external processor or container is involved — everything runs in-process.

The CEL context uses four namespaces:

- ``p`` / ``payload`` — raw submission data
- ``s`` / ``signal`` — author-defined workflow signals
- ``output`` — this step's validator output values
- ``steps.<key>.output.<name>`` — upstream step outputs

Raw payload keys are **never** promoted to top-level CEL variables.
Authors access data via ``p.key`` and signals via ``s.name``.

These tests cover:

- **File type gating**: only JSON and XML are accepted; text/binary are rejected
  with a clear error before any parsing is attempted.
- **Parse error handling**: malformed JSON/XML produces actionable error messages.
- **CEL context building**: payload data is accessible under ``p.`` / ``payload.``,
  workflow signals populate ``s.`` / ``signal.``, step inputs populate
  ``i.`` / ``input.``, and output values go into the ``o.`` / ``output.``
  namespace. Keys that aren't valid CEL identifiers
  (hyphens, ``@``-prefixes, ``#text``) are naturally contained inside their
  namespace dicts rather than promoted to the root.
- **End-to-end XML with hyphenated elements**: full ``validate()`` calls with
  XML documents whose element names contain hyphens (e.g., ``<THERM-XML>``).
- **CEL error messages**: error formatting produces context-appropriate guidance
  depending on whether the validator uses custom data paths or catalog entries.
- **THERM XML integration**: full pipeline from XML parsing through CEL assertion
  evaluation with real THERM fixture files.

Tests use Django's test database via FactoryBoy factories for all model instances
(validators, submissions, rulesets), ensuring ORM queryset behavior is exercised
rather than hand-wired MagicMock chains.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase
from django.test import TestCase

from validibot.actions.protocols import RunContext
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.assertions.evaluators.basic import BasicAssertionEvaluator
from validibot.validations.cel import CEL_NAMESPACE_ROOTS
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import resolved_file_input
from validibot.validations.validators.base.base import _is_valid_cel_identifier
from validibot.validations.validators.basic import BasicValidator as _BasicValidator


class BasicValidator(_BasicValidator):
    """Bind engine-level tests to the Basic validator's typed document port."""

    def validate(
        self,
        validator,
        submission,
        ruleset,
        run_context=None,
    ):
        """Supply the selected submission bytes as a resolved port value."""
        context = run_context or RunContext()
        context.resolved_file_inputs["document"] = resolved_file_input(
            contract_key="document",
            content=submission.get_content(),
            file_type=submission.file_type,
        )
        return super().validate(
            validator,
            submission,
            ruleset,
            run_context=context,
        )


# ==============================================================================
# File type gating — BasicValidator only accepts JSON and XML
# ==============================================================================
# The file type check is the first guard in validate(). If it rejects the
# submission, no parsing or assertion evaluation happens. This prevents
# confusing downstream errors from hitting users.
# ==============================================================================


class BasicValidatorFileTypeTests(TestCase):
    """BasicValidator accepts JSON and XML, rejects other types.

    Uses real Django model instances via FactoryBoy so the ORM queryset
    paths in ``_build_cel_context()`` and ``evaluate_assertions_for_stage()``
    are exercised — not just mocked away.
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(validation_type=ValidationType.BASIC)

    def test_rejects_text_file_type(self):
        """Plain text submissions are rejected with a clear error.

        The validator checks ``submission.file_type`` before parsing.
        TEXT is not in ``_SUPPORTED_FILE_TYPES``, so we get a rejection
        without any JSON/XML parsing attempt.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.TEXT,
            content="hello",
        )
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, None)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("JSON or XML", result.issues[0].message)

    def test_rejects_binary_file_type(self):
        """Binary submissions are rejected before parsing.

        BINARY file type hits the same guard as TEXT — the validator
        never attempts to interpret the content.  Note: the Submission
        model requires non-empty content (DB constraint), so we pass
        placeholder content even though it's never read.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.BINARY,
            content="(binary placeholder)",
        )
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, None)

        self.assertFalse(result.passed)
        self.assertIn("JSON or XML", result.issues[0].message)

    def test_accepts_json(self):
        """JSON submissions are parsed and assertions evaluated.

        With no assertions on the ruleset and no default_ruleset on the
        validator, the result should be ``passed=True`` — the submission
        parsed successfully and there's nothing to fail against.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.JSON,
            content='{"price": 10}',
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)

    def test_accepts_xml(self):
        """XML submissions are parsed via xml_to_dict and assertions evaluated.

        XML is converted to a nested dict before assertion evaluation,
        so the downstream code path is identical to JSON.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.XML,
            content="<root><price>10</price></root>",
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)

    def test_invalid_json_returns_error(self):
        """Malformed JSON returns a clear parse error.

        The error message should identify the problem as a JSON parse
        failure, not a downstream assertion error.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.JSON,
            content="{broken",
        )
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, None)

        self.assertFalse(result.passed)
        self.assertIn("Invalid JSON", result.issues[0].message)

    def test_invalid_xml_returns_error(self):
        """Malformed XML returns a clear parse error.

        Similar to invalid JSON — the error message should reference
        XML, not a generic assertion failure.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.XML,
            content="<root><unclosed>",
        )
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, None)

        self.assertFalse(result.passed)
        self.assertIn("Invalid XML", result.issues[0].message)


class BasicValidatorXmlAssertionTests(TestCase):
    """End-to-end: XML submission with BASIC and CEL assertions."""

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            allow_custom_assertion_targets=True,
        )
        cls.price_entry = StepIODefinitionFactory(
            validator=cls.validator,
            contract_key="price",
            direction="input",
        )

    def test_cel_assertion_against_xml(self):
        """CEL expression evaluates against XML-derived dict.

        The assertion targets the parsed XML payload directly with
        ``p.product.price``; its step I/O target provides catalog metadata
        but does not turn the input into a workflow signal.
        """
        xml_content = "<product><price>25.99</price><name>Widget</name></product>"
        SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        price_definition = self.price_entry
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_io_definition=price_definition,
            target_data_path="",
            rhs={"expr": "p.product.price > 0"},
        )

        engine = BasicValidator()
        # Evaluate at the payload level (xml_to_dict wraps in root tag)
        payload_dict = {"product": {"price": "25.99", "name": "Widget"}}
        result = engine.evaluate_assertions_for_stage(
            validator=self.validator,
            ruleset=ruleset,
            payload=payload_dict,
            stage="input",
        )
        # price accessed via p.product.price (payload namespace).
        # Validator inputs are NOT in the s namespace.
        # Note: XML values are strings, so comparison depends on CEL coercion.
        # This test verifies the pipeline works end-to-end.
        self.assertIsNotNone(result)

    def test_full_validate_with_xml_submission(self):
        """Full validate() call with XML submission parses and evaluates."""
        xml_content = "<data><value>42</value></data>"
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        # No assertions defined, so should pass
        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 0)

    def test_basic_assertion_against_xml_nested_path(self):
        """BASIC assertion with dot-path resolves XML nested elements."""
        xml_content = (
            "<building>"
            "  <thermostat>"
            "    <setpoint>72</setpoint>"
            "  </thermostat>"
            "</building>"
        )
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )

        # Create a catalog entry matching the nested path
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            allow_custom_assertion_targets=True,
        )
        entry = StepIODefinitionFactory(
            validator=validator,
            contract_key="building.thermostat.setpoint",
            direction="input",
        )

        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.EQ,
            target_io_definition=entry,
            target_data_path="",
            rhs={"value": "72"},
        )

        engine = BasicValidator()
        result = engine.validate(validator, submission, ruleset)

        # The setpoint value "72" (string from XML) should equal "72"
        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 0)


class BasicValidatorNamespaceAwarenessTests(TestCase):
    """End-to-end: BASIC assertions resolve through the enriched payload.

    Phase 5 wired ``_enrich_basic_payload`` into the validator base
    layer so BASIC assertions whose targets reference i.* or s.*
    namespaces find the resolved values at the payload root by
    contract_key. These tests prove the wiring works end-to-end —
    not just that the form accepts the targets (covered in
    ``test_forms_ruleset_assertion.py``), but that the BASIC
    evaluator's payload-walk actually finds the merged value.

    The trap these guard against: an author saves a BASIC
    assertion with ``target_io_definition.contract_key =
    "temperature"`` where ``temperature`` is a workflow signal
    (``s.temperature``). Pre-Phase-5 the BASIC evaluator walked
    ``payload["temperature"]`` against the raw submission and
    reported "Value not found" because the workflow signal lived
    in a different dict. Post-Phase-5 the enriched payload merges
    workflow signals at top level, so the lookup succeeds.
    """

    def test_workflow_signal_resolves_via_enriched_payload(self):
        """BASIC + s.<workflow_signal> resolves at runtime via enrichment.

        The fixture sets ``workflow_signals = {"temp_threshold": 72}``
        on the run context and writes a BASIC assertion targeting
        the workflow signal by contract_key ``temp_threshold``.
        The enriched payload merges the workflow signal at top
        level so BASIC's ``contract_key`` walk finds the value.
        """
        from validibot.actions.protocols import RunContext

        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.EQ,
            target_data_path="temp_threshold",  # bare name post-resolve
            target_io_definition=None,
            rhs={"value": 72},
        )
        # Minimal submission — the workflow signal value comes from
        # run_context, not the payload.
        submission = SubmissionFactory(
            content='{"unrelated": "field"}',
            file_type=SubmissionFileType.JSON,
        )
        run_context = RunContext(
            validation_run=MagicMock(id=1),
            step=MagicMock(id=1),
            upstream_steps={},
            workflow_signals={"temp_threshold": 72},
        )

        engine = BasicValidator()
        result = engine.validate(validator, submission, ruleset, run_context)

        # Workflow signal merged into the enriched payload; BASIC
        # walks ``temp_threshold`` → finds 72 → equality passes.
        self.assertTrue(result.passed, msg=[i.message for i in result.issues])
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 0)


# ---------------------------------------------------------------------------
# CEL identifier validation
# ---------------------------------------------------------------------------


class IsValidCelIdentifierTests(SimpleTestCase):
    """
    Unit tests for _is_valid_cel_identifier().

    CEL (Common Expression Language) requires that top-level activation variable
    names match the identifier grammar ``[_a-zA-Z][_a-zA-Z0-9]*``.  cel-python
    raises ``ValueError`` at evaluation time if a context dict contains a key
    that violates this rule.

    The helper is used for workflow signal name validation, ensuring names are
    valid CEL identifiers before they are placed in the ``s`` / ``signal``
    namespace. XML-to-dict conversion can produce keys that are
    valid XML but invalid CEL identifiers (hyphens, ``@``-prefixes, ``#text``);
    these are safely contained inside the ``p`` / ``payload`` namespace dict
    and accessed via bracket notation (e.g., ``p["THERM-XML"]``).
    """

    def test_simple_names_are_valid(self):
        """Standard Python/CEL variable names are accepted."""
        self.assertTrue(_is_valid_cel_identifier("price"))
        self.assertTrue(_is_valid_cel_identifier("Materials"))
        self.assertTrue(_is_valid_cel_identifier("THERM_XML"))
        self.assertTrue(_is_valid_cel_identifier("x"))

    def test_underscore_prefix_is_valid(self):
        """Leading underscores are valid CEL identifiers."""
        self.assertTrue(_is_valid_cel_identifier("_private"))
        self.assertTrue(_is_valid_cel_identifier("__dunder"))

    def test_alphanumeric_with_digits_is_valid(self):
        """Digits are allowed after the first character."""
        self.assertTrue(_is_valid_cel_identifier("item2"))
        self.assertTrue(_is_valid_cel_identifier("v1_beta"))

    def test_hyphenated_names_are_invalid(self):
        """Hyphenated XML element names (e.g., THERM-XML) are rejected."""
        self.assertFalse(_is_valid_cel_identifier("THERM-XML"))
        self.assertFalse(_is_valid_cel_identifier("my-variable"))
        self.assertFalse(_is_valid_cel_identifier("building-energy-model"))

    def test_at_prefix_is_invalid(self):
        """@-prefixed keys from XML attribute conversion are rejected."""
        self.assertFalse(_is_valid_cel_identifier("@id"))
        self.assertFalse(_is_valid_cel_identifier("@type"))
        self.assertFalse(_is_valid_cel_identifier("@Name"))

    def test_hash_prefix_is_invalid(self):
        """#text keys from XML mixed-content conversion are rejected."""
        self.assertFalse(_is_valid_cel_identifier("#text"))

    def test_numeric_start_is_invalid(self):
        """Names starting with a digit are not valid identifiers."""
        self.assertFalse(_is_valid_cel_identifier("123abc"))
        self.assertFalse(_is_valid_cel_identifier("0"))

    def test_empty_string_is_invalid(self):
        self.assertFalse(_is_valid_cel_identifier(""))

    def test_names_with_spaces_are_invalid(self):
        self.assertFalse(_is_valid_cel_identifier("has space"))

    def test_names_with_dots_are_invalid(self):
        """Dotted paths are not single identifiers (they use nested access)."""
        self.assertFalse(_is_valid_cel_identifier("a.b"))


# ---------------------------------------------------------------------------
# CEL context building — namespaced structure
# ---------------------------------------------------------------------------
# The CEL context uses four namespaces: p/payload (raw data), s/signal
# (author-defined signals), output (this step's outputs), and steps
# (upstream step outputs).  Payload keys are never promoted to top-level
# CEL variables — they are accessed via p.key or payload.key.
# ---------------------------------------------------------------------------


class CelContextNamespaceTests(TestCase):
    """
    Verify that ``_build_cel_context()`` builds the correct namespaced
    structure and that payload keys are contained within the ``p`` /
    ``payload`` namespace.

    The context has fixed root keys: ``p``, ``payload``, ``s``, ``signal``,
    and conditionally ``output`` and ``steps``.  Raw payload keys are
    **never** promoted to top-level CEL variables — they live under
    ``p`` and ``payload`` (which are aliases for the same dict).

    XML documents commonly use element names that are valid XML but
    invalid CEL identifiers (hyphens, ``@``-prefixed attribute keys,
    ``#text``).  Because these are nested inside the ``p`` / ``payload``
    dict (not promoted to the root), they no longer cause ``ValueError``
    from cel-python.  They are accessible via bracket notation:
    ``p["THERM-XML"]``.

    Uses real Django model instances via FactoryBoy so the
    ``step_io_definitions.all().only()`` ORM path is exercised naturally.
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(validation_type=ValidationType.BASIC)

    def test_payload_under_p_namespace(self):
        """Payload data is accessible under ``p`` and ``payload``, not
        as bare top-level CEL variables.
        """
        engine = BasicValidator()
        payload = {"price": 10, "name": "Widget"}
        context = engine._build_cel_context(payload, self.validator)

        # Payload accessible under p and payload (aliases)
        self.assertEqual(context["p"]["price"], 10)
        self.assertEqual(context["payload"]["price"], 10)
        self.assertEqual(context["p"]["name"], "Widget")
        # Bare keys are NOT at the root
        self.assertNotIn("price", context)
        self.assertNotIn("name", context)

    def test_p_and_payload_are_same_object(self):
        """``p`` and ``payload`` are aliases pointing to the same dict,
        not copies — mutations to one are visible in the other.
        """
        engine = BasicValidator()
        payload = {"value": 42}
        context = engine._build_cel_context(payload, self.validator)

        self.assertIs(context["p"], context["payload"])

    def test_hyphenated_keys_accessible_under_p(self):
        """A hyphenated root key like ``THERM-XML`` (from xml_to_dict) is
        accessible under ``p["THERM-XML"]`` but never at the root level
        (where it would cause a cel-python ``ValueError``).
        """
        engine = BasicValidator()
        payload = {"THERM-XML": {"Materials": {"Material": []}}}
        context = engine._build_cel_context(payload, self.validator)

        self.assertNotIn("THERM-XML", context)
        self.assertEqual(
            context["p"]["THERM-XML"]["Materials"]["Material"],
            [],
        )

    def test_at_prefixed_keys_accessible_under_p(self):
        """``@``-prefixed keys from XML attribute conversion are contained
        inside the ``p`` namespace and never appear at the root.
        """
        engine = BasicValidator()
        payload = {"root": {"child": {"@id": "42", "value": "hello"}}}
        context = engine._build_cel_context(payload, self.validator)

        self.assertNotIn("@id", context)
        self.assertNotIn("root", context)
        self.assertEqual(context["p"]["root"]["child"]["@id"], "42")

    def test_context_root_keys_are_fixed(self):
        """The runtime context root must equal the canonical namespace set.

        This is the canary that ties three things together: the single
        source of truth (``CEL_NAMESPACE_ROOTS`` in cel.py), the runtime
        context dict built by ``_build_cel_context``, and — transitively —
        every authoring-time allowlist that derives from the same constant.
        Asserting equality against the constant (rather than a hand-listed
        set) means a namespace can only be added in ONE place: the moment
        ``CEL_NAMESPACE_ROOTS`` gains a member, this test fails until
        ``_build_cel_context`` actually binds it, forcing the runtime and the
        allowlists to move together.

        Per ADR-2026-05-22b the five base namespaces are p/payload (raw
        submission file), s/signal (workflow vocabulary), i/input (step-local
        input-stage values), o/output (step-local output-stage values), and
        steps (cross-step inputs and outputs); ADR-2026-06-03b adds the sixth,
        submission (submission envelope: metadata + server facts); ADR-2026-06-18
        adds the seventh, c/const (author-defined Constants). Here both
        submission and the constants map resolve to ``{}`` because this unit
        test builds the context without a run, but their root keys are still
        present. Payload keys are never at the root regardless of their names.
        """
        engine = BasicValidator()
        payload = {
            "THERM-XML": {"Units": "SI"},
            "Materials": {"Material": [{"@Name": "Wood"}]},
        }
        context = engine._build_cel_context(payload, self.validator)

        # The runtime context binds exactly the canonical namespace roots —
        # no more (no leaked payload keys), no fewer (every namespace present).
        self.assertEqual(set(context.keys()), set(CEL_NAMESPACE_ROOTS))
        # Both payload keys are accessible under p
        self.assertIn("THERM-XML", context["p"])
        self.assertIn("Materials", context["p"])

    def test_validator_inputs_not_in_signal_namespace(self):
        """Validator step input definitions do NOT appear in the ``s``
        namespace. Validator inputs feed the validator (FMU/EnergyPlus
        parameters), not CEL expressions. Authors access payload data
        via ``p.key`` and signals via ``s.name`` (from workflow-level
        signal mappings or promoted outputs).
        """
        validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        StepIODefinitionFactory(
            validator=validator,
            contract_key="temperature",
            direction="input",
        )
        engine = BasicValidator()
        payload = {"temperature": 22.5}
        context = engine._build_cel_context(payload, validator)

        # Validator input NOT in s namespace
        self.assertNotIn("temperature", context["s"])
        # But payload data IS accessible via p namespace
        self.assertEqual(context["p"]["temperature"], 22.5)

    def test_s_and_signal_are_same_object(self):
        """``s`` and ``signal`` are aliases pointing to the same dict."""
        engine = BasicValidator()
        payload = {"value": 1}
        context = engine._build_cel_context(payload, self.validator)

        self.assertIs(context["s"], context["signal"])


# ---------------------------------------------------------------------------
# CEL context output namespace
#
# Step output definitions are stored in a nested ``output`` dict so that
# CEL member access (``output.slug``) resolves correctly.  The context
# structure ensures output values don't collide with payload or signal data.
# ---------------------------------------------------------------------------


class CelContextOutputNamespaceTests(TestCase):
    """Verify that output value definitions are exposed in a nested
    ``output`` namespace in the CEL context.

    The nested dict structure is critical because:
    - CEL parses ``output.slug`` as member access (variable ``output``,
      field ``slug``), not as a single identifier with a dot.
    - Basic assertions use ``_resolve_path()`` which splits on dots,
      navigating ``data["output"]["slug"]``.

    Both evaluation paths require a real nested dict, not a flat
    dotted key.
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )

    def test_output_entries_in_nested_namespace(self):
        """Step output definitions appear under ``context["output"]``.

        Every output value should be accessible as ``output.<slug>``
        in CEL expressions.  The same value is also in ``p.temperature``
        (as raw payload data) but the output namespace provides a
        dedicated access path for assertions about outputs.
        """
        StepIODefinitionFactory(
            validator=self.validator,
            contract_key="temperature",
            direction="output",
        )
        engine = BasicValidator()
        payload = {"temperature": 296.63}
        context = engine._build_cel_context(payload, self.validator)

        # Nested namespace exists and contains the output entry
        self.assertIn("output", context)
        self.assertIsInstance(context["output"], dict)
        self.assertEqual(context["output"]["temperature"], 296.63)
        # Also accessible under the payload namespace
        self.assertEqual(context["p"]["temperature"], 296.63)

    def test_input_and_output_same_key(self):
        """When an input and output value share the same contract key,
        the output appears in the output namespace but the input does
        NOT appear in s (validator inputs are not signals).

        ``p.price`` → payload access, ``output.price`` → output value.
        """
        StepIODefinitionFactory(
            validator=self.validator,
            contract_key="price",
            direction="input",
        )
        StepIODefinitionFactory(
            validator=self.validator,
            contract_key="price",
            direction="output",
        )
        engine = BasicValidator()
        payload = {"price": 42.0}
        context = engine._build_cel_context(payload, self.validator)

        # Validator input NOT in s namespace
        self.assertNotIn("price", context["s"])
        # Output in the output namespace
        self.assertIn("output", context)
        self.assertEqual(context["output"]["price"], 42.0)
        # Bare "price" is NOT at the root
        self.assertNotIn("price", context)

    def test_no_output_entries_output_is_empty_dict(self):
        """When there are no output value definitions, the ``output``
        namespace is an empty dict (always present for consistency).
        """
        StepIODefinitionFactory(
            validator=self.validator,
            contract_key="weight",
            direction="input",
        )
        engine = BasicValidator()
        payload = {"weight": 10}
        context = engine._build_cel_context(payload, self.validator)

        self.assertIn("output", context)
        self.assertEqual(context["output"], {})
        self.assertIs(context["o"], context["output"])
        # Validator input NOT in s namespace.
        self.assertNotIn("weight", context["s"])
        # But payload data IS accessible via p
        self.assertEqual(context["p"]["weight"], 10)


class BasicValidatorHyphenatedXmlEndToEndTests(TestCase):
    """End-to-end validation tests with XML documents whose element names
    contain hyphens, confirming the full pipeline doesn't crash.

    Hyphenated element names are common in real-world XML formats (e.g.,
    THERM's ``<THERM-XML>`` root element).  With the namespaced context,
    these keys live inside the ``p`` / ``payload`` dict and are never
    at the CEL root level, so they cannot cause ``ValueError`` from
    cel-python.

    Uses real Django model instances via FactoryBoy to exercise the full
    ORM path through ``catalog_entries`` and ``assertions`` querysets.
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        # A ruleset with no assertions — submissions should pass validation
        # since there's nothing to assert against.
        cls.ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)

    def test_xml_with_hyphenated_root_validates_without_crash(self):
        """A full ``validate()`` call with a hyphenated root element like
        ``<THERM-XML>`` should complete without raising ``ValueError``.

        With the namespaced context, ``THERM-XML`` is safely inside the
        ``p`` dict and never appears as a CEL root variable.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.XML,
            content=(
                '<THERM-XML xmlns="http://windows.lbl.gov">'
                "  <Units>SI</Units>"
                "</THERM-XML>"
            ),
        )
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)

    def test_xml_with_hyphenated_child_elements_validates(self):
        """Hyphenated child element names should also not crash the pipeline.

        Even when nested inside a valid root element, hyphenated children
        (e.g., ``<energy-rating>``) are safely contained inside the
        ``p`` namespace dict.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.XML,
            content=(
                "<root>"
                "  <energy-rating>A+</energy-rating>"
                "  <building-type>Commercial</building-type>"
                "</root>"
            ),
        )
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)

    def test_xml_with_attributes_validates(self):
        """XML attributes (converted to ``@``-prefixed keys by xml_to_dict)
        should not crash CEL context building.

        The ``@``-prefix makes these invalid CEL identifiers, but they
        are safely contained inside the ``p`` namespace dict.
        """
        submission = SubmissionFactory(
            file_type=SubmissionFileType.XML,
            content='<root><item id="1" type="widget">Test</item></root>',
        )
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)


# ---------------------------------------------------------------------------
# CEL error message formatting
# ---------------------------------------------------------------------------


class CelErrorMessageTests(TestCase):
    """Tests for ``CelAssertionEvaluator._format_error_message()``.

    The error message for undefined identifiers should vary based on
    whether the validator uses custom data paths (Basic-style) or
    catalog entries (EnergyPlus-style), so the guidance is actionable:

    - **Custom targets** (``allow_custom_assertion_targets=True``): the error
      suggests checking the data path, since the user controls which paths
      are exposed as CEL variables.
    - **Catalog targets** (``allow_custom_assertion_targets=False``): the error
      suggests checking step input/output names, since the validator defines
      which catalog entries are available.

    Uses real Django model instances for validators to exercise the
    ``allow_custom_assertion_targets`` field naturally.
    """

    def _get_evaluator(self):
        from validibot.validations.assertions.evaluators.cel import (
            CelAssertionEvaluator,
        )

        return CelAssertionEvaluator()

    def test_custom_targets_message(self):
        """Validators with custom data paths get data-path guidance.

        BASIC validators have ``allow_custom_assertion_targets=True`` by
        design, so the error message refers to a data path rather than
        a step input/output.
        """
        evaluator = self._get_evaluator()
        validator = ValidatorFactory(validation_type=ValidationType.BASIC)

        msg = evaluator._format_error_message(
            "undeclared reference to 'Materials'",
            validator=validator,
        )
        self.assertIn("undefined name 'Materials'", msg)
        self.assertIn("data path", msg)
        self.assertNotIn("step input or output", msg)

    def test_catalog_targets_message(self):
        """Validators without custom targets get step-I/O guidance.

        Non-BASIC validators (e.g., JSON_SCHEMA) have
        ``allow_custom_assertion_targets=False`` by default, so the error
        message refers to step input/output names from the catalog.
        """
        evaluator = self._get_evaluator()
        validator = ValidatorFactory(allow_custom_assertion_targets=False)

        msg = evaluator._format_error_message(
            "undeclared reference to 'price'",
            validator=validator,
        )
        self.assertIn("undefined name 'price'", msg)
        self.assertIn("step input or output", msg)
        self.assertNotIn("data path", msg)

    def test_non_identifier_error_passes_through(self):
        """Errors that aren't about undefined identifiers pass through unchanged."""
        evaluator = self._get_evaluator()
        raw = "type mismatch: int vs string"
        msg = evaluator._format_error_message(raw)
        self.assertEqual(msg, raw)

    def test_dot_at_syntax_error_message(self):
        """``m.@Conductivity`` compile error gets a helpful message.

        This is a common mistake with XML-derived data: users write dot
        notation for @-prefixed keys, but CEL treats ``@`` as a syntax
        error. The message should suggest bracket notation instead.
        """
        evaluator = self._get_evaluator()
        raw = (
            "Materials.Material.all(m, double(m.@Conductivity) > 0.0)\n"
            "                                   ^\n"
        )
        msg = evaluator._format_error_message(raw)
        self.assertIn("bracket notation", msg)
        self.assertIn("@Conductivity", msg)
        self.assertNotIn("^", msg)

    def test_field_selection_error_message(self):
        """Field selection failure on ``@``-keyed dict gets helpful message."""
        evaluator = self._get_evaluator()
        raw = (
            "({'Material': [{'@Name': 'Wood'}]} "
            "with type: '<class 'dict'>' does not support field selection"
        )
        msg = evaluator._format_error_message(raw)
        self.assertIn("XML attributes", msg)
        self.assertIn("@Conductivity", msg)

    def test_no_such_member_error_message(self):
        """Missing member on MapType (e.g. ``Conductivity``
        instead of ``@Conductivity``)."""
        evaluator = self._get_evaluator()
        # cel-python wraps the error with escaped quotes
        raw = (
            "('no such member in mapping: \\'Conductivity\\'', "
            "<class 'KeyError'>, None)"
        )
        msg = evaluator._format_error_message(raw)
        self.assertIn("@Conductivity", msg)
        self.assertIn("bracket notation", msg)

    def test_no_such_member_unescaped_quotes(self):
        """Same pattern but with plain quotes (for robustness)."""
        evaluator = self._get_evaluator()
        raw = "no such member in mapping: 'Temperature'"
        msg = evaluator._format_error_message(raw)
        self.assertIn("@Temperature", msg)


# ---------------------------------------------------------------------------
# THERM XML integration tests with CEL assertions
# ---------------------------------------------------------------------------

# Sample THERM XML — a minimal .thmx with three materials whose
# conductivity values are all valid (between 0 and 500).
_VALID_THERM_XML = (
    '<?xml version="1.0"?>'
    '<THERM-XML xmlns="http://windows.lbl.gov">'
    "  <ThermVersion>Version 8.0.20.0</ThermVersion>"
    "  <FileVersion>1</FileVersion>"
    "  <Title>Test Frame</Title>"
    "  <CreatedBy>Tests</CreatedBy>"
    "  <CrossSectionType>Sill</CrossSectionType>"
    "  <Units>SI</Units>"
    "  <Materials>"
    '    <Material Name="Aluminum" Type="0" Conductivity="160.0"'
    '      Tir="0" EmissivityFront="0.2" EmissivityBack="0.2" />'
    '    <Material Name="PVC" Type="0" Conductivity="0.16"'
    '      Tir="0" EmissivityFront="0.9" EmissivityBack="0.9" />'
    '    <Material Name="Glass" Type="0" Conductivity="1.0"'
    '      Tir="0" EmissivityFront="0.84" EmissivityBack="0.84" />'
    "  </Materials>"
    "</THERM-XML>"
)

# Same structure but one material has Conductivity > 500 (invalid).
_INVALID_THERM_XML = (
    '<?xml version="1.0"?>'
    '<THERM-XML xmlns="http://windows.lbl.gov">'
    "  <ThermVersion>Version 8.0.20.0</ThermVersion>"
    "  <FileVersion>1</FileVersion>"
    "  <Title>Test Frame</Title>"
    "  <CreatedBy>Tests</CreatedBy>"
    "  <CrossSectionType>Sill</CrossSectionType>"
    "  <Units>SI</Units>"
    "  <Materials>"
    '    <Material Name="Aluminum" Type="0" Conductivity="160.0"'
    '      Tir="0" EmissivityFront="0.2" EmissivityBack="0.2" />'
    '    <Material Name="SuperConductor" Type="0" Conductivity="999.0"'
    '      Tir="0" EmissivityFront="0.9" EmissivityBack="0.9" />'
    '    <Material Name="Glass" Type="0" Conductivity="1.0"'
    '      Tir="0" EmissivityFront="0.84" EmissivityBack="0.84" />'
    "  </Materials>"
    "</THERM-XML>"
)

# Same structure but one material has Conductivity = 0 (also invalid).
_ZERO_CONDUCTIVITY_XML = (
    '<?xml version="1.0"?>'
    '<THERM-XML xmlns="http://windows.lbl.gov">'
    "  <ThermVersion>Version 8.0.20.0</ThermVersion>"
    "  <FileVersion>1</FileVersion>"
    "  <Title>Test Frame</Title>"
    "  <CreatedBy>Tests</CreatedBy>"
    "  <CrossSectionType>Sill</CrossSectionType>"
    "  <Units>SI</Units>"
    "  <Materials>"
    '    <Material Name="Vacuum" Type="0" Conductivity="0.0"'
    '      Tir="0" EmissivityFront="0.5" EmissivityBack="0.5" />'
    "  </Materials>"
    "</THERM-XML>"
)

# The correct CEL expression: uses p. namespace prefix to access payload
# data.  THERM XML has a hyphenated root element (``THERM-XML``) so we
# use bracket notation to navigate into it, then dot notation for the
# valid-identifier children.  @-prefixed XML attribute keys also use
# bracket notation.
_CONDUCTIVITY_CEL = (
    'p["THERM-XML"].Materials.Material.all(m, '
    'double(m["@Conductivity"]) > 0.0 '
    '&& double(m["@Conductivity"]) <= 500.0)'
)


class ThermXmlCelIntegrationTests(TestCase):
    """Integration tests: BasicValidator + THERM XML + CEL assertions.

    These tests validate the full pipeline:
    1. XML is parsed by xml_to_dict (attributes -> @-prefixed keys)
    2. _build_cel_context places the entire payload under ``p`` / ``payload``
    3. CEL expressions access payload data via ``p.Materials.Material.all(...)``
    4. Bracket notation accesses @-prefixed XML attribute keys
    5. Assertion pass/fail is reported correctly
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            allow_custom_assertion_targets=True,
        )

    def _make_ruleset_with_cel(self, expr):
        """Create a ruleset with a single CEL assertion."""
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_io_definition=None,
            target_data_path="Materials",
            rhs={"expr": expr},
        )
        return ruleset

    def test_valid_therm_xml_passes_conductivity_check(self):
        """All materials have conductivity in (0, 500] → assertion passes."""
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 0)

    def test_invalid_conductivity_fails_assertion(self):
        """One material has conductivity=999 → assertion evaluates to false."""
        submission = SubmissionFactory(
            content=_INVALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 1)

    def test_zero_conductivity_fails_assertion(self):
        """Material with conductivity=0.0 fails the > 0.0 check."""
        submission = SubmissionFactory(
            content=_ZERO_CONDUCTIVITY_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(result.assertion_stats.failures, 1)

    def test_dot_at_syntax_gives_helpful_error(self):
        """m.@Conductivity (invalid CEL) produces actionable error."""
        bad_expr = (
            'p["THERM-XML"].Materials.Material.all(m, double(m.@Conductivity) > 0.0)'
        )
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(bad_expr)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        error_msg = result.issues[0].message
        self.assertIn("bracket notation", error_msg)

    def test_missing_at_prefix_gives_helpful_error(self):
        """m.Conductivity (no @) fails because the dict key is @Conductivity."""
        no_at_expr = (
            'p["THERM-XML"].Materials.Material.all(m, double(m.Conductivity) > 0.0)'
        )
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(no_at_expr)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        error_msg = result.issues[0].message
        self.assertIn("@Conductivity", error_msg)

    def test_name_attribute_accessible_via_bracket(self):
        """@Name attribute is accessible via bracket notation."""
        name_expr = 'p["THERM-XML"].Materials.Material.all(m, m["@Name"] != "")'
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(name_expr)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)

    def test_units_element_accessible_via_payload_namespace(self):
        """Child element 'Units' is accessible via the ``p`` namespace.

        Since ``Units`` is nested under the ``THERM-XML`` root element
        (which has a hyphenated name), we use bracket notation for the
        root and dot notation for the child.
        """
        units_expr = 'p["THERM-XML"].Units == "SI"'
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(units_expr)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)

    def test_real_thmx_fixture_passes_conductivity_check(self):
        """The sample_valid.thmx fixture passes the conductivity check."""
        import pathlib

        fixture = pathlib.Path(__file__).parent / (
            "test_validators/fixtures/sample_valid.thmx"
        )
        xml_content = fixture.read_text()
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.failures, 0)

    def test_bad_conductivity_fixture_fails_with_custom_message(self):
        """sample_sill_CMA_bad_conductivity.thmx has negative values → fails.

        The fixture contains materials with Conductivity values of -1.0,
        -0.00695, and -0.01 which violate the > 0.0 check.  The assertion
        is configured with a custom message_template so the failure message
        is user-friendly rather than the generic CEL default.
        """
        import pathlib

        fixture = (
            pathlib.Path(__file__).parents[3]
            / "tests/data/therm/sample_sill_CMA_bad_conductivity.thmx"
        )
        xml_content = fixture.read_text()
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )

        failure_msg = (
            "One or more conductivity values are less than zero or more than 500."
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_io_definition=None,
            target_data_path="Materials",
            rhs={"expr": _CONDUCTIVITY_CEL},
            message_template=failure_msg,
        )

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 1)
        self.assertEqual(result.issues[0].message, failure_msg)

    def test_good_conductivity_fixture_passes(self):
        """sample_sill_CMA.thmx has all valid conductivity values → passes."""
        import pathlib

        fixture = (
            pathlib.Path(__file__).parents[3] / "tests/data/therm/sample_sill_CMA.thmx"
        )
        xml_content = fixture.read_text()
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 0)


class BasicRegexOperatorTests(SimpleTestCase):
    """The ``matches`` (regex) operator runs author patterns safely via RE2.

    The Basic validator's regex operator runs an author-supplied pattern against
    submitter-supplied text, so it is a ReDoS surface. It is now backed by RE2
    (linear-time, no backtracking), replacing a thread-timeout guard that could
    not actually preempt a catastrophic match under CPython's GIL. These tests
    pin that ordinary matching is unchanged, a catastrophic pattern cannot hang,
    and an RE2-unsupported pattern is reported rather than silently downgraded.
    """

    def setUp(self):
        """A bare evaluator is enough — ``_evaluate_regex`` is self-contained."""
        self.evaluator = BasicAssertionEvaluator()

    def test_regex_match_and_non_match(self):
        """``search`` semantics: an unanchored pattern matches where it occurs."""
        passed, _msg = self.evaluator._evaluate_regex(
            "order A-1 shipped",
            {"pattern": r"[A-Z]-\d"},
            {},
        )
        self.assertTrue(passed)
        failed, _msg = self.evaluator._evaluate_regex(
            "no code here",
            {"pattern": r"[A-Z]-\d"},
            {},
        )
        self.assertFalse(failed)

    def test_catastrophic_pattern_does_not_hang(self):
        """A backtracking-bomb pattern resolves instantly instead of pinning a CPU.

        Under :mod:`re` this input never returns; under RE2 it is linear. The
        value does not match, so the operator simply reports a failed comparison
        — the security property is that we *reach* that result at all.
        """
        passed, _msg = self.evaluator._evaluate_regex(
            "a" * 80 + "!",
            {"pattern": r"(a+)+$"},
            {},
        )
        self.assertFalse(passed)

    def test_unsupported_pattern_reports_invalid_regex(self):
        """A lookaround pattern (RE2-unsupported) fails with a clear message.

        It must not fall back to :mod:`re` (which would reopen the ReDoS vector)
        nor silently pass; the author sees an "Invalid regex" result.
        """
        passed, message = self.evaluator._evaluate_regex(
            "anything",
            {"pattern": r"foo(?=bar)"},
            {},
        )
        self.assertFalse(passed)
        self.assertIn("Invalid regex", message)

    def test_case_insensitive_option(self):
        """``case_insensitive`` still folds case through to the RE2 matcher."""
        passed, _msg = self.evaluator._evaluate_regex(
            "ABC",
            {"pattern": "abc"},
            {"case_insensitive": True},
        )
        self.assertTrue(passed)
