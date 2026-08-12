"""Tests for the Constants primitive (the ``c.*`` / ``const.*`` namespace).

ADR-2026-06-18 adds **Constants** as a distinct workflow primitive: a
workflow-scoped, author-defined *fixed* value referenced in assertions as
``c.<name>``. This suite covers the Phase 1 community runtime foundation —
everything needed for a constant to be defined, stored exactly, and read by
both assertion evaluators:

* **Value coercion / storage** — the type contract is enforced at save time,
  and a ``NUMBER`` is stored as a canonical decimal *string* so ``0.40``
  survives verbatim (the attestation-fidelity guarantee). CEL has no decimal
  type, so the value is coerced to ``double`` only for evaluation.
* **Name rules** — valid CEL identifier, reserved roots rejected, unique
  *among constants*, and a constant may share a bare name with a signal (the
  prefix disambiguates) — the per-primitive uniqueness decision.
* **Runtime wiring** — ``c``/``const`` are bound in the CEL context and as a
  nested sub-dict in the Basic evaluator's enriched payload.
* **Stage classification** — ``c.*`` is stage-neutral: a constants-only check
  is INPUT-stage (no needless container dispatch) unless it also reads
  ``o.*``/``output.*``.

Why this matters: constants feed the signed credential, so getting storage
fidelity and the semantic boundaries right here is what makes the eventual
attestation legible and correct.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.test import TestCase

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import ValidationType
from validibot.validations.models import RulesetAssertion
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import resolved_file_input
from validibot.validations.validators.basic import BasicValidator
from validibot.workflows.constants import WorkflowConstantType
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.models import WorkflowConstant
from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.services.constants import ConstantValueError
from validibot.workflows.services.constants import build_workflow_constants_context
from validibot.workflows.services.constants import coerce_constant_value
from validibot.workflows.services.constants import format_constant_display
from validibot.workflows.services.constants import validate_constant_name
from validibot.workflows.services.constants import value_for_cel
from validibot.workflows.services.io.exporter import export_definition
from validibot.workflows.services.io.importer import import_definition
from validibot.workflows.services.versioning import WorkflowVersioningService
from validibot.workflows.tests.factories import WorkflowFactory

# ──────────────────────────────────────────────────────────────────────────
# Value coercion and storage fidelity
# ──────────────────────────────────────────────────────────────────────────
# These are the ADR's core correctness guarantees. They run without a database
# (pure functions) because the storage contract must hold independent of any
# model machinery.


class CoerceConstantValueTests(SimpleTestCase):
    """``coerce_constant_value`` enforces the per-type storage contract.

    This is the single chokepoint that decides what a constant's *stored* form
    looks like, so its behaviour is the storage contract.
    """

    def test_string_is_taken_literally(self):
        """STRING stores the value verbatim — no JSON-parsing/quoting trap.

        The motivating bug: an author types ``EUR`` and an inference-based type
        system tries to JSON-parse it and errors. An explicit STRING type means
        ``EUR`` is just the string ``EUR``.
        """
        assert coerce_constant_value(WorkflowConstantType.STRING, "EUR") == "EUR"

    def test_number_preserves_trailing_zero_as_decimal_string(self):
        """NUMBER stores a canonical decimal *string*, preserving ``0.40``.

        This is the attestation-fidelity guarantee: a JSON float would collapse
        ``0.40`` to ``0.4``. Storing the decimal string keeps the author's
        precision verbatim for the digest, manifest, and credential.
        """
        stored = coerce_constant_value(WorkflowConstantType.NUMBER, "0.40")
        assert stored == "0.40"
        assert isinstance(stored, str)

    def test_number_accepts_integer_and_negative(self):
        """NUMBER accepts plain integers and negatives, stored as exact strings.

        Constants are thresholds; integral and negative thresholds are common
        (e.g. a minimum count, a temperature offset).
        """
        assert coerce_constant_value(WorkflowConstantType.NUMBER, "5") == "5"
        assert coerce_constant_value(WorkflowConstantType.NUMBER, "-3.5") == "-3.5"

    def test_number_rejects_non_numeric(self):
        """A non-numeric NUMBER is rejected at save time, not at run time.

        Guaranteeing the contract at authoring time is the whole point — an
        author should never discover ``abc`` isn't a number when a run fails.
        """
        with pytest.raises(ConstantValueError):
            coerce_constant_value(WorkflowConstantType.NUMBER, "abc")

    def test_number_rejects_non_finite(self):
        """NaN/Infinity are rejected — a threshold must be a real, finite value.

        ``Decimal`` parses ``NaN``/``Infinity``; we reject them explicitly so a
        constant can never carry a value that breaks comparisons or hashing.
        """
        for bad in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(ConstantValueError):
                coerce_constant_value(WorkflowConstantType.NUMBER, bad)

    def test_number_rejects_boolean(self):
        """A boolean is not a number even though ``bool`` subclasses ``int``.

        Without the explicit guard, ``True`` would coerce to ``1`` and silently
        mistype the constant.
        """
        native_bool = True  # a real bool, not the string "true"
        with pytest.raises(ConstantValueError):
            coerce_constant_value(WorkflowConstantType.NUMBER, native_bool)

    def test_boolean_accepts_form_strings(self):
        """BOOLEAN coerces the form's true/false strings to real bools.

        The Add Constant form submits a toggle as a string; both the string and
        native bool must land as a Python bool so CEL sees a boolean.
        """
        native_true = True  # a real bool, exercising the bool passthrough
        assert coerce_constant_value(WorkflowConstantType.BOOLEAN, "true") is True
        assert coerce_constant_value(WorkflowConstantType.BOOLEAN, "false") is False
        assert coerce_constant_value(WorkflowConstantType.BOOLEAN, native_true) is True

    def test_list_parses_json_and_returns_list(self):
        """LIST accepts JSON text (the editor's output) and returns a list.

        Allow-lists (``["EUR", "GBP"]``) are the canonical LIST use case.
        """
        value = coerce_constant_value(WorkflowConstantType.LIST, '["EUR", "GBP"]')
        assert value == ["EUR", "GBP"]

    def test_object_parses_json_and_returns_dict(self):
        """OBJECT accepts JSON text and returns a dict."""
        value = coerce_constant_value(WorkflowConstantType.OBJECT, '{"min": 1}')
        assert value == {"min": 1}

    def test_list_given_wrong_json_type_is_rejected(self):
        """A LIST whose JSON is actually an object is rejected.

        The declared type is a promise; coercion enforces it rather than
        silently storing a mismatched shape.
        """
        with pytest.raises(ConstantValueError):
            coerce_constant_value(WorkflowConstantType.LIST, '{"not": "a list"}')

    def test_structured_constant_rejects_oversized_list(self):
        """An over-long LIST is rejected at save time (bounds guard).

        A constant is a named threshold/allow-list, not a dataset — the cap
        stops it bloating the activation context, manifest, and digest.
        """
        huge = list(range(1000))
        with pytest.raises(ConstantValueError):
            coerce_constant_value(WorkflowConstantType.LIST, huge)

    def test_structured_constant_rejects_excessive_depth(self):
        """A pathologically deep OBJECT is rejected at save time.

        Depth is capped to the runtime CEL context bound so save-time and
        eval-time agree — a constant can never be deeper than CEL would accept.
        """
        deep: dict = {}
        cursor = deep
        for _i in range(40):
            cursor["k"] = {}
            cursor = cursor["k"]
        with pytest.raises(ConstantValueError):
            coerce_constant_value(WorkflowConstantType.OBJECT, deep)


class ValueForCelTests(SimpleTestCase):
    """``value_for_cel`` bridges exact storage to CEL's numeric model.

    Storage is exact (decimal string); CEL has only int/double, so a numeric
    constant is coerced at this boundary — the separation the ADR draws between
    attestation fidelity and evaluation.
    """

    def _number(self, stored: str) -> WorkflowConstant:
        """Build an unsaved NUMBER constant with a given stored string."""
        return WorkflowConstant(
            data_type=WorkflowConstantType.NUMBER,
            value=stored,
        )

    def test_fractional_number_becomes_float(self):
        """A fractional NUMBER coerces to ``float`` for CEL (``double``).

        CEL evaluates numerics as ``double``; ``0.40`` becomes ``0.4`` for the
        comparison even though storage kept the trailing zero.
        """
        expected = 0.4
        result = value_for_cel(self._number("0.40"))
        assert result == expected
        assert isinstance(result, float)

    def test_integral_number_becomes_int(self):
        """An integral NUMBER coerces to ``int`` so ``c.count == 3`` compares.

        Keeping integral constants as ``int`` avoids surprising int-vs-double
        mismatches against integer payload values.
        """
        expected = 3
        result = value_for_cel(self._number("3"))
        assert result == expected
        assert isinstance(result, int)

    def test_list_passes_through(self):
        """Non-numeric types are already CEL-ready and pass through unchanged."""
        const = WorkflowConstant(
            data_type=WorkflowConstantType.LIST,
            value=["EUR", "GBP"],
        )
        assert value_for_cel(const) == ["EUR", "GBP"]


class FormatConstantDisplayTests(SimpleTestCase):
    """The author-facing display preserves precision and reads naturally."""

    def test_number_display_keeps_precision(self):
        """The reference panel shows ``0.40`` (stored), not ``0.4``.

        The panel's value-with-precision is exactly why constants beat inline
        literals — it must show the committed value faithfully.
        """
        const = WorkflowConstant(
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        assert format_constant_display(const) == "c.energy_price = 0.40 (number)"

    def test_list_display_renders_json(self):
        """A LIST renders as JSON in the hint."""
        const = WorkflowConstant(
            name="allowed_currencies",
            data_type=WorkflowConstantType.LIST,
            value=["EUR", "GBP"],
        )
        assert (
            format_constant_display(const)
            == 'c.allowed_currencies = ["EUR", "GBP"] (list)'
        )

    def test_display_value_renders_list_as_json_not_repr(self):
        """``WorkflowConstant.display_value`` gives templates JSON, not Python repr.

        Templates render ``{{ c.display_value }}`` so a LIST shows as
        ``["EUR", "GBP"]`` rather than the ``['EUR', 'GBP']`` repr that
        ``{{ c.value }}`` would print (ADR-2026-06-18 P3).
        """
        const = WorkflowConstant(
            name="allowed_currencies",
            data_type=WorkflowConstantType.LIST,
            value=["EUR", "GBP"],
        )
        assert const.display_value == '["EUR", "GBP"]'

    def test_display_value_preserves_number_precision(self):
        """A NUMBER's display value keeps the stored decimal string (``0.40``)."""
        const = WorkflowConstant(
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        assert const.display_value == "0.40"


# ──────────────────────────────────────────────────────────────────────────
# Name rules
# ──────────────────────────────────────────────────────────────────────────


class ConstantNameRulesTests(SimpleTestCase):
    """Name validation (identifier + reserved) without a database."""

    def test_reserved_root_rejected(self):
        """A constant may not be named after a reserved namespace root.

        ``c``/``const`` are now reserved, so a constant literally named ``c``
        (or ``output``) would shadow a namespace and is rejected.
        """
        assert validate_constant_name("c")  # non-empty error list
        assert validate_constant_name("output")

    def test_invalid_identifier_rejected(self):
        """A name that isn't a valid CEL identifier is rejected.

        ``c.<name>`` must be addressable in CEL, so the name must be a legal
        identifier.
        """
        assert validate_constant_name("2price")
        assert validate_constant_name("has space")

    def test_valid_name_accepted(self):
        """A normal identifier passes with no errors."""
        assert validate_constant_name("energy_price") == []


class ConstantModelTests(TestCase):
    """Database-backed model behaviour: coercion, uniqueness, collisions."""

    @classmethod
    def setUpTestData(cls):
        cls.workflow = WorkflowFactory()

    def test_save_coerces_number_to_decimal_string(self):
        """Saving a NUMBER persists the canonical decimal string.

        ``clean()`` routes the value through ``coerce_constant_value`` so the
        stored form is exact regardless of how the value was supplied.
        """
        const = WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        const.refresh_from_db()
        assert const.value == "0.40"

    def test_duplicate_constant_name_rejected(self):
        """Two constants with the same name in one workflow are rejected.

        Per-constant uniqueness is the primitive's contract; the app-level
        ``clean()`` check surfaces a friendly error before the DB constraint.
        """
        WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="threshold",
            data_type=WorkflowConstantType.NUMBER,
            value="1",
        )
        with pytest.raises(ValidationError):
            WorkflowConstant.objects.create(
                workflow=self.workflow,
                name="threshold",
                data_type=WorkflowConstantType.NUMBER,
                value="2",
            )

    def test_constant_and_signal_may_share_a_bare_name(self):
        """``c.energy_price`` and ``s.energy_price`` may coexist.

        This is the ADR's cross-primitive-collision decision: the namespace
        prefix fully disambiguates, so forbidding the shared bare name would
        contradict our own namespace-design rule. Critically, the constant
        helper must NOT reuse the signal uniqueness check (which would reject
        this).
        """
        WorkflowSignalMapping.objects.create(
            workflow=self.workflow,
            name="energy_price",
            source_path="$.price",
        )
        # Must not raise — different primitive, prefix disambiguates.
        const = WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        assert const.pk is not None

    def test_build_context_orders_by_name_and_coerces(self):
        """The runtime context is a ``{name: cel_value}`` map, sorted by name.

        Sorting by ``name`` (not ``position``/``pk``) is the digest-stability
        decision; here we assert the runtime projection coerces values for CEL.
        """
        WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="allowed",
            data_type=WorkflowConstantType.LIST,
            value=["EUR", "GBP"],
        )
        context = build_workflow_constants_context(self.workflow)
        assert context == {"energy_price": 0.4, "allowed": ["EUR", "GBP"]}


# ──────────────────────────────────────────────────────────────────────────
# Runtime wiring — CEL context and Basic enriched payload
# ──────────────────────────────────────────────────────────────────────────


class ConstantsRuntimeWiringTests(TestCase):
    """``c``/``const`` are bound in both evaluators' contexts."""

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(validation_type=ValidationType.BASIC)

    def _engine_with_constants(self, constants: dict) -> BasicValidator:
        """A BasicValidator whose run context carries a constants map.

        Mirrors the workflow-signals test: the constants come from the run
        context (built at run start), not the payload.
        """
        engine = BasicValidator()
        engine.run_context = MagicMock(
            workflow_constants=constants,
            workflow_signals={},
            validation_run=None,
            step=None,
        )
        return engine

    def test_cel_context_binds_c_and_const(self):
        """``_build_cel_context`` binds the constants map under both spellings.

        Both ``c`` and ``const`` must point at the same map (alias), like
        ``s``/``signal``.
        """
        engine = self._engine_with_constants({"energy_price": 0.4})
        context = engine._build_cel_context({}, self.validator)
        assert context["c"] == {"energy_price": 0.4}
        assert context["const"] == {"energy_price": 0.4}

    def test_basic_payload_injects_nested_c_dict(self):
        """``_enrich_basic_payload`` injects ``c``/``const`` as nested sub-dicts.

        Nesting (not flattening like ``s.*``) is what lets a Basic target
        ``c.energy_price`` resolve to ``payload["c"]["energy_price"]`` and
        coexist with a bare-key signal of the same name.
        """
        engine = self._engine_with_constants({"energy_price": 0.4})
        enriched = engine._enrich_basic_payload({}, stage="input")
        assert enriched["c"] == {"energy_price": 0.4}
        assert enriched["const"] == {"energy_price": 0.4}

    def test_basic_assertion_resolves_constant_target(self):
        """A Basic assertion can compare a ``c.<name>`` target to a literal.

        End-to-end: a constant injected via the run context is walked by the
        Basic evaluator's dotted-path resolver (into the nested ``c`` sub-dict)
        and compared to the stored RHS. The run context is passed to
        ``validate()`` so it carries the constants map through enrichment.
        """
        from validibot.actions.protocols import RunContext
        from validibot.validations.constants import RulesetType
        from validibot.validations.models import Ruleset

        ruleset = Ruleset.objects.create(
            name="c-target",
            ruleset_type=RulesetType.BASIC,
        )
        RulesetAssertion.objects.create(
            ruleset=ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.EQ,
            target_data_path="c.energy_price",
            rhs={"value": 0.4},
        )
        submission = SubmissionFactory(
            content='{"unrelated": "x"}',
            file_type=SubmissionFileType.JSON,
        )
        run_context = RunContext(
            validation_run=MagicMock(id=1),
            step=MagicMock(id=1),
            workflow_constants={"energy_price": 0.4},
            resolved_file_inputs={
                "document": resolved_file_input(
                    contract_key="document",
                    content=submission.content,
                    file_type=SubmissionFileType.JSON,
                )
            },
        )
        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset, run_context)
        assert result.passed, [i.message for i in result.issues]


# ──────────────────────────────────────────────────────────────────────────
# Stage classification — c.* is input-stage-neutral
# ──────────────────────────────────────────────────────────────────────────
# These assert the ADR's stage-neutral decision. ``resolved_run_stage`` is a
# pure property (no DB when there is no step-I/O-definition target), so unsaved
# assertions suffice.


class ConstantsStageClassificationTests(SimpleTestCase):
    """``c.*`` never forces OUTPUT stage unless ``o.*`` is also referenced."""

    def test_basic_constant_target_classifies_input(self):
        """A Basic ``c.<name>`` target is an INPUT-stage gate.

        A constant is design-time-known, so a constants-only check must not
        wait for an advanced validator's container to run.
        """
        assertion = RulesetAssertion(
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.LT,
            target_data_path="c.max_site_eui",
            rhs={"value": 120},
        )
        assert assertion.resolved_run_stage == CatalogRunStage.INPUT

    def test_cel_constant_only_classifies_input(self):
        """A CEL check mixing payload and ``c.*`` (no outputs) is INPUT.

        ``payload.cost == payload.energy * c.energy_price`` references no output
        namespace, so it adds no runtime dependency and gates early. This is the
        exact case the ADR flagged as previously mis-classified OUTPUT.
        """
        assertion = RulesetAssertion(
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "payload.cost == payload.energy * c.energy_price"},
        )
        assert assertion.resolved_run_stage == CatalogRunStage.INPUT

    def test_cel_constant_plus_output_stays_output(self):
        """A CEL check reading ``c.*`` AND ``o.*`` stays OUTPUT.

        Constants are stage-neutral, but a genuine output reference still needs
        results — the constant must not drag it earlier than it can run.
        """
        assertion = RulesetAssertion(
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "o.site_eui < c.max_site_eui"},
        )
        assert assertion.resolved_run_stage == CatalogRunStage.OUTPUT

    def test_const_long_form_classifies_input(self):
        """The long ``const.`` spelling is recognised by the stage classifier.

        Both spellings are namespace roots; the classifier must treat them
        identically or the alias would silently change stage behaviour.
        """
        assertion = RulesetAssertion(
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "const.threshold > 5"},
        )
        assert assertion.resolved_run_stage == CatalogRunStage.INPUT

    def test_identifier_starting_with_c_does_not_false_match(self):
        """An expression like ``count.x`` must NOT be read as the ``c`` root.

        The negative-lookbehind in the constants pattern prevents a false match
        that would wrongly reclassify an unrelated output-stage assertion.
        """
        assertion = RulesetAssertion(
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "count.value > 5"},
        )
        # No recognised early namespace, no i.* opt-in → legacy OUTPUT default.
        assert assertion.resolved_run_stage == CatalogRunStage.OUTPUT


# ──────────────────────────────────────────────────────────────────────────
# Versioned trust contract — add/edit/delete guard
# ──────────────────────────────────────────────────────────────────────────
# A constant's value determines pass/fail, so it sits inside the versioned
# trust contract on the same footing as assertions. Once a versioned workflow
# is locked (or has runs), the contract is fixed: adding, editing a semantic
# field, or deleting a constant is rejected; cosmetic fields stay editable.
# These tests lock the workflow (cheaper than creating a real run; the gate
# ``requires_new_version_for_contract_edits`` fires on locked OR has-runs).


class ConstantVersioningGuardTests(TestCase):
    """Add/edit/delete of constants is blocked once the contract is locked."""

    def setUp(self):
        """A versioned workflow with one constant, not yet locked."""
        self.workflow = WorkflowFactory()  # history_policy defaults to versioned
        self.constant = WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )

    def _lock(self):
        """Lock the workflow so contract edits require a new version."""
        self.workflow.is_locked = True
        self.workflow.save(update_fields=["is_locked"])

    def test_cannot_add_constant_after_lock(self):
        """Adding a constant to a locked workflow is rejected.

        A new constant a later assertion could reference changes the contract,
        so it must force a new version — the create path is guarded, not only
        edits.
        """
        self._lock()
        with pytest.raises(ValidationError):
            WorkflowConstant.objects.create(
                workflow=self.workflow,
                name="max_eui",
                data_type=WorkflowConstantType.NUMBER,
                value="120",
            )

    def test_cannot_edit_value_after_lock(self):
        """Editing a constant's value after lock is rejected.

        This is the core trust hole: changing ``0.40`` to ``0.45`` would make a
        receipt that passed yesterday fail today under the same "workflow vX".
        """
        self._lock()
        self.constant.value = "0.45"
        with pytest.raises(ValidationError):
            self.constant.save()

    def test_cosmetic_edit_allowed_after_lock(self):
        """Editing only ``description``/``position`` stays allowed after lock.

        Cosmetic fields don't change pass/fail, so blocking them would be
        needless friction — the guard must distinguish semantic from cosmetic.
        """
        self._lock()
        new_position = 3
        self.constant.description = "agreed €/kWh per the 2026 contract"
        self.constant.position = new_position
        self.constant.save()  # must not raise
        self.constant.refresh_from_db()
        assert self.constant.position == new_position

    def test_cannot_delete_constant_after_lock(self):
        """Deleting a constant after lock is rejected.

        Removing a constant changes the contract/digest exactly as editing one
        does, so delete is guarded too (ADR-2026-06-18 add/edit/delete).
        """
        self._lock()
        with pytest.raises(ValidationError):
            self.constant.delete()

    def test_mutable_history_workflow_allows_edits(self):
        """A mutable-history workflow opts out of the version gate.

        The guard is a *versioned*-history reproducibility feature; mutable
        workflows deliberately allow in-place contract edits.
        """
        self.workflow.history_policy = WorkflowHistoryPolicy.MUTABLE
        self.workflow.is_locked = True
        self.workflow.save(update_fields=["history_policy", "is_locked"])
        self.constant.value = "0.45"
        self.constant.save()  # must not raise
        self.constant.refresh_from_db()
        assert self.constant.value == "0.45"


class SignalMappingVersioningGuardTests(TestCase):
    """The same add/edit/delete guard now protects signal mappings.

    ADR-2026-06-18 closes a pre-existing gap: a signal's resolved value
    determines pass/fail just like a constant, so an unguarded post-run edit
    silently changed what "passed against workflow vX" meant.
    """

    def setUp(self):
        self.workflow = WorkflowFactory()
        self.mapping = WorkflowSignalMapping.objects.create(
            workflow=self.workflow,
            name="reported_total",
            source_path="$.total",
        )
        self.workflow.is_locked = True
        self.workflow.save(update_fields=["is_locked"])

    def test_cannot_edit_source_path_after_lock(self):
        """Editing a signal's source_path after lock is rejected.

        ``source_path`` is the resolution contract; changing it re-points what
        the signal reads from the submission.
        """
        self.mapping.source_path = "$.grand_total"
        with pytest.raises(ValidationError):
            self.mapping.save()

    def test_cannot_delete_signal_after_lock(self):
        """Deleting a signal mapping after lock is rejected."""
        with pytest.raises(ValidationError):
            self.mapping.delete()


# ──────────────────────────────────────────────────────────────────────────
# Clone + VAF round-trip — constants travel with the workflow
# ──────────────────────────────────────────────────────────────────────────


class ConstantClonePortabilityTests(TestCase):
    """Constants are carried forward by clone and survive export/import."""

    def test_clone_copies_constants(self):
        """``clone()`` deep-copies constants into the new version and counts them.

        Constants are part of the versioned contract, so a new version must
        reproduce them — and the CloneReport surfaces the count for callers.
        """
        workflow = WorkflowFactory()
        WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)

        assert report.components_copied["constants"] == 1
        new_workflow = type(workflow).objects.get(pk=report.new_workflow_id)
        copied = new_workflow.constants.get()
        assert copied.name == "energy_price"
        assert copied.value == "0.40"  # decimal-string precision preserved
        # The clone is a distinct row on the new workflow.
        assert copied.workflow_id == new_workflow.pk

    def test_constants_round_trip_through_vaf(self):
        """An export→import reproduces identical constants.

        Without this, a portable workflow would silently lose the named
        thresholds its assertions depend on. Decimal precision and structured
        values must survive verbatim.
        """
        src_org = OrganizationFactory()
        src_user = UserFactory(orgs=[src_org])
        src_user.set_current_org(src_org)
        workflow = WorkflowFactory(org=src_org, user=src_user)
        WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        WorkflowConstant.objects.create(
            workflow=workflow,
            name="allowed_currencies",
            data_type=WorkflowConstantType.LIST,
            value=["EUR", "GBP"],
        )

        definition, files = export_definition(workflow)
        # The constants block is present in the portable definition.
        assert {c["name"] for c in definition["workflow"]["constants"]} == {
            "energy_price",
            "allowed_currencies",
        }

        dst_org = OrganizationFactory()
        dst_user = UserFactory(orgs=[dst_org])
        dst_user.set_current_org(dst_org)
        result = import_definition(
            definition,
            files=files,
            org=dst_org,
            user=dst_user,
        )

        imported = {c.name: c for c in result.workflow.constants.all()}
        assert imported["energy_price"].value == "0.40"
        assert imported["energy_price"].data_type == WorkflowConstantType.NUMBER
        assert imported["allowed_currencies"].value == ["EUR", "GBP"]
