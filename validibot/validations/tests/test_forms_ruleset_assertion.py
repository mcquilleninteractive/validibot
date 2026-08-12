"""
Tests for RulesetAssertionForm — target resolution, CEL identifier validation,
and FMU variable collision detection.

The assertion form handles step I/O definitions from the validator catalog
and step-level FMU variables discovered from model metadata. Both sources
participate in target resolution
for basic assertions and identifier validation for CEL expressions.

CEL expressions use a namespaced identifier convention:

- ``p.key`` / ``payload.key`` — raw submission data
- ``s.name`` / ``signal.name`` — author-defined workflow signals
- ``i.name`` / ``input.name`` — this step's inputs
- ``o.name`` / ``output.name`` — this step's outputs
- ``steps.key.output.name`` — upstream step outputs

Bare identifiers (not prefixed with a namespace) are rejected unless they
are CEL builtins, literals, or single-letter loop variables.  These tests
verify that the form enforces this convention correctly.
"""

from __future__ import annotations

from django.test import TestCase

from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.forms import RulesetAssertionForm
from validibot.validations.models import RulesetAssertion
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.utils import update_custom_validator


class RulesetAssertionFormTests(TestCase):
    """Tests for catalog-entry-backed assertions and CEL identifier validation."""

    def _form(
        self,
        *,
        validator,
        catalog_entries,
        data: dict,
        fmu_variables=None,
        workflow_signal_names=None,
    ):
        """Build an assertion form with step I/O definitions."""
        return RulesetAssertionForm(
            data=data,
            catalog_entries=catalog_entries or [],
            validator=validator,
            fmu_variables=fmu_variables,
            workflow_signal_names=workflow_signal_names,
        )

    def test_cel_disallows_bare_identifiers_when_custom_targets_disabled(self):
        """Bare (un-namespaced) identifiers are rejected when custom targets
        are disabled.

        The validator requires all CEL identifiers to use namespace prefixes
        (``s.``, ``p.``, ``output.``, etc.).  ``rating`` here is bare and
        unknown, so the form should reject it with the "Bare identifiers
        are not allowed" error message.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        self.assertFalse(validator.allow_custom_assertion_targets)
        entry = StepIODefinitionFactory(validator=validator, contract_key="price")
        RulesetFactory()
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": "s.price > 0 && rating > 10",
                "when_expression": "",
            },
        )
        self.assertFalse(form._validator_allows_custom_targets())
        self.assertFalse(form.is_valid())
        self.assertIn("Bare identifiers are not allowed", str(form.errors))

    def test_cel_allows_namespaced_identifiers_when_custom_targets_enabled(self):
        """Namespaced identifiers are accepted when custom targets are enabled.

        When ``allow_custom_assertion_targets=True``, any properly namespaced
        expression (using ``p.``, ``s.``, ``output.``, etc.) is accepted
        without checking whether referenced step I/O exists in the catalog.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        self.assertTrue(validator.allow_custom_assertion_targets)
        entry = StepIODefinitionFactory(validator=validator, contract_key="price")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": "p.price > 0 && s.rating > 10",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())

    def test_cel_syntax_error_is_rejected_on_save(self):
        """A syntactically invalid CEL expression fails the form, not run time.

        ADR-2026-05-26 requires the assertion form to compile every CEL
        expression on save. ``p.price >`` passes the identifier and delimiter
        checks but does not parse, so before this fix it saved cleanly and only
        failed when a submission was validated. The form must compile it here.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        entry = StepIODefinitionFactory(validator=validator, contract_key="price")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": "p.price >",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Not a valid CEL expression", str(form.errors))

    def test_cel_description_persisted_into_rhs_payload(self):
        """An optional CEL description is saved alongside the expression in rhs.

        Why it matters: the description is the human label shown on the
        assertion card (see ``RulesetAssertion.target_display``), so it must
        survive form submission. It rides in the same ``rhs`` JSONField as the
        expression — ``{"expr": ..., "description": ...}`` — mirroring exactly
        where SHACL stores its description, with no schema change. This pins the
        clean() contract that the mutation service later writes to the model.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        entry = StepIODefinitionFactory(validator=validator, contract_key="eui")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_description": "Site EUI within ASHRAE target",
                "cel_expression": "i.eui <= 50.0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        rhs_payload = form.cleaned_data["rhs_payload"]
        self.assertEqual(rhs_payload["expr"], "i.eui <= 50.0")
        self.assertEqual(rhs_payload["description"], "Site EUI within ASHRAE target")

    def test_cel_blank_description_persists_as_empty_string(self):
        """Omitting the description stores an empty string, never a missing key.

        Why it matters: ``target_display`` reads ``rhs.get("description")`` and
        falls back to the expression when it is falsy. Persisting a consistent
        empty string (rather than omitting the key) keeps the payload shape
        identical whether or not the author filled the field, which makes the
        round-trip back into the edit form deterministic.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        entry = StepIODefinitionFactory(validator=validator, contract_key="eui")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": "i.eui <= 50.0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["rhs_payload"]["description"], "")

    def test_initial_from_instance_round_trips_cel_description(self):
        """Editing a CEL assertion repopulates the description field.

        Why it matters: the edit modal is built from
        ``initial_from_instance``. If it didn't read back ``rhs["description"]``,
        a saved label would silently vanish on the next edit (and a re-save would
        blank it). This closes the save → edit → save loop for the new field.
        """
        assertion = RulesetAssertion(
            assertion_type=AssertionType.CEL_EXPRESSION,
            target_data_path="o.eui <= 50.0",
            rhs={"expr": "o.eui <= 50.0", "description": "Site EUI within target"},
            severity=Severity.ERROR,
        )

        initial = RulesetAssertionForm.initial_from_instance(assertion)

        self.assertEqual(initial["cel_expression"], "o.eui <= 50.0")
        self.assertEqual(initial["cel_description"], "Site EUI within target")

    def test_cel_allows_multi_letter_macro_loop_variable(self):
        """A comprehension macro's loop variable may be any length, not 1 letter.

        Regression: the identifier check only exempted single-letter loop
        variables, so a readable rule like
        ``i.namespaces_present.all(ns, ns in [...])`` was wrongly rejected with
        "Bare identifiers are not allowed: ns" — even though ``ns`` is bound by
        the ``.all(...)`` macro, not a free data reference. ``room.size`` also
        exercises ``loopvar.field`` access inside the body.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        entry = StepIODefinitionFactory(validator=validator, contract_key="ns_list")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": (
                    'i.ns_list.all(ns, ns in ["a", "b"]) '
                    "&& s.rooms.exists(room, room.size > 0)"
                ),
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())

    def test_cel_macro_exemption_does_not_allow_other_bare_identifiers(self):
        """The loop-variable exemption must not blanket-allow bare identifiers.

        ``ns`` is exempt (the macro binds it), but ``rating`` is still a free,
        un-namespaced reference and must be rejected — proving the fix doesn't
        weaken the check.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        entry = StepIODefinitionFactory(validator=validator, contract_key="ns_list")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": 'i.ns_list.all(ns, ns == "x") && rating > 1',
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Bare identifiers are not allowed", str(form.errors))
        self.assertIn("rating", str(form.errors))

    def test_cel_accepts_v1_tabular_helper_functions(self):
        """The V1 Tabular Validator helpers must be accepted at authoring
        time, not rejected as unknown identifiers.

        Why it matters: ``is_iso8601``, ``parse_date``, and ``now`` are
        registered in three places (docs, this form allowlist, and the
        runtime binding). This pins registration #2 — without the form's
        ``custom_helpers`` allowlist entry (sourced from
        ``V1_CEL_HELPER_NAMES``), ``is_iso8601(p.x)`` would be flagged as a
        bare unknown identifier and the author could never save the rule,
        even though it binds and executes correctly at runtime. The
        expression exercises all three helper names in one realistic
        "ISO date and not in the future" check.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        entry = StepIODefinitionFactory(validator=validator, contract_key="event_date")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": (
                    "is_iso8601(p.event_date) && parse_date(p.event_date) <= now()"
                ),
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())

    def test_tabular_step_accepts_row_assertion_and_tags_row_stage(self):
        """A ``row.*`` CEL assertion is accepted on a Tabular Validator step and
        tagged ``options.tabular_stage == "row"``.

        Why it matters: the ``row.*`` namespace is only valid on a tabular step,
        and the validator buckets assertions by ``tabular_stage`` — so authoring
        a row assertion must both pass the (scoped) identifier check AND store
        the stage tag that routes it into the per-row engine.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "",
                "severity": Severity.ERROR,
                "cel_expression": "row.lat >= -90 && row.lat <= 90",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(
            form.cleaned_data["options_payload"],
            {"tabular_stage": "row", "report_max_examples": 10},
        )

    def test_tabular_dataset_assertion_is_tagged_dataset_stage(self):
        """A tabular CEL assertion over ``i.*`` (no ``row.*``) is tagged as the
        dataset stage, so it flows through the generic input lane rather than
        the per-row loop.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "",
                "severity": Severity.ERROR,
                "cel_expression": "i.num_rows >= 100",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(
            form.cleaned_data["options_payload"],
            {"tabular_stage": "dataset"},
        )

    def test_row_namespace_rejected_on_non_tabular_step(self):
        """``row.*`` is scoped to tabular steps: a JSON Schema step must reject
        it as an unknown identifier, so a stray ``row.x`` elsewhere is flagged
        rather than silently accepted and then unbound at runtime.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "",
                "severity": Severity.ERROR,
                "cel_expression": "row.lat >= 0",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Bare identifiers are not allowed", str(form.errors))

    def test_tabular_step_accepts_column_assertion_and_tags_column_stage(self):
        """A supported ``col.*`` aggregate saves as a V2 column-stage assertion.

        The stage tag routes it away from the generic and row evaluators and
        into the one-shot aggregate evaluator.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "",
                "severity": Severity.ERROR,
                "cel_expression": "col.lat.null_ratio < 0.05",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(
            form.cleaned_data["options_payload"],
            {"tabular_stage": "column"},
        )

    def test_tabular_column_assertion_rejects_unknown_aggregate(self):
        """An aggregate outside the ADR contract is rejected at save time.

        This prevents an author from publishing ``col.lat.mean`` and discovering
        only at run time that the aggregate map has no ``mean`` member.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "",
                "severity": Severity.ERROR,
                "cel_expression": "col.lat.mean > 0",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Unknown column aggregate", str(form.errors))

    def test_row_assertion_persists_custom_example_limit(self):
        """A row assertion stores its bounded diagnostic sample limit.

        The evaluator still counts every failure; this option controls only how
        many example row numbers are attached to the finding.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            is_system=False,
        )
        form = RulesetAssertionForm(
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "",
                "severity": Severity.ERROR,
                "cel_expression": "row.lat >= 0",
                "when_expression": "",
                "report_max_examples": 7,
            },
            catalog_entries=[],
            validator=validator,
            requested_tabular_stage="row",
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data["options_payload"]["report_max_examples"], 7)

    def _tabular_row_form(self, expression, *, tabular_columns):
        """Build an assertion form for a row CEL expression on a tabular step."""
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            is_system=False,
        )
        return RulesetAssertionForm(
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "",
                "severity": Severity.ERROR,
                "cel_expression": expression,
                "when_expression": "",
            },
            catalog_entries=[],
            validator=validator,
            tabular_columns=tabular_columns,
        )

    def test_row_assertion_accepts_declared_columns(self):
        """A row assertion referencing only declared columns saves cleanly —
        the baseline that proves the column check doesn't false-positive.
        """
        form = self._tabular_row_form(
            "row.lat >= -90 && row.lat <= 90",
            tabular_columns={"lat", "lon"},
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())

    def test_row_assertion_rejects_undeclared_column(self):
        """A row assertion referencing a column not in the step's schema is
        rejected at save time — the ADR's column-existence obligation, catching
        a typo before it fails every run.
        """
        form = self._tabular_row_form("row.typo >= 0", tabular_columns={"lat", "lon"})
        self.assertFalse(form.is_valid())
        self.assertIn("not declared in the step", str(form.errors))

    def test_row_assertion_bracket_access_undeclared_column_rejected(self):
        """Bracket access (``row["..."]``, the spelling for non-identifier
        column names) is checked too — an undeclared bracketed column is
        rejected, so the check can't be evaded by spelling.
        """
        form = self._tabular_row_form(
            'row["dwc:missing"] != ""',
            tabular_columns={"dwc:eventDate"},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("not declared in the step", str(form.errors))

    def test_row_assertion_column_check_skipped_without_schema(self):
        """When no schema is configured yet (no declared columns), the column
        check is skipped — authoring isn't blocked before the schema exists,
        and the runtime still guards against an unbound column reference.
        """
        form = self._tabular_row_form("row.anything >= 0", tabular_columns=set())
        self.assertTrue(form.is_valid(), form.errors.as_json())

    def test_basic_assertion_accepts_parser_managed_input_target(self):
        """BASIC + i.<parser_input> is accepted post Phase 5.

        Previously the form rejected this because the BASIC evaluator
        walked the raw payload by contract_key, ignoring parser-
        extracted facts. Phase 5 fixed the runtime trap at the
        validator base layer (``BaseValidator._enrich_basic_payload``
        merges resolved bindings + workflow signals + parser facts
        into the BASIC payload by their bare contract_key), so the
        form-side rejection is no longer needed.

        Regression test: BASIC + i.<parser_input> now saves cleanly
        and resolves to a StepIODefinition target the evaluator can
        walk against the enriched payload.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        # Parser-managed input: source_kind=internal mimics how the
        # EnergyPlus catalog declares zone_count (the IDF parser
        # fills it, not a payload binding).
        parser_input = StepIODefinitionFactory(
            validator=validator,
            contract_key="zone_count",
            direction="input",
            source_kind="internal",
            is_path_editable=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[parser_input],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": f"i.{parser_input.contract_key}",
                "operator": "ge",
                "comparison_value": "1",
                "severity": Severity.ERROR,
                "cel_expression": "",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # The form resolves the target to the catalog row.
        resolved = form.cleaned_data["resolved_io_definition"]
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.contract_key, "zone_count")

    def test_basic_assertion_accepts_author_bound_input_target(self):
        """BASIC + i.<author_bound_input> is accepted post Phase 5.

        Previously this was rejected because the BASIC evaluator
        walked the raw payload by ``contract_key`` and ignored the
        ``StepInputBinding``'s ``source_data_path``. Phase 5 fixed
        the runtime trap at the validator base layer: the validator
        calls ``_enrich_basic_payload`` which runs
        ``_resolve_bound_input_context`` and merges the binding's
        resolved value into the payload under the bare
        ``contract_key``. BASIC's ``contract_key`` lookup now hits
        the merged value directly — no payload-walk indirection.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        bound_input = StepIODefinitionFactory(
            validator=validator,
            contract_key="temperature",
            direction="input",
            source_kind="payload_path",
            is_path_editable=True,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[bound_input],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": f"i.{bound_input.contract_key}",
                "operator": "ge",
                "comparison_value": "0",
                "severity": Severity.ERROR,
                "cel_expression": "",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        resolved = form.cleaned_data["resolved_io_definition"]
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.contract_key, "temperature")

    def test_basic_assertion_accepts_workflow_signal_target(self):
        """BASIC + s.<workflow_signal> is accepted post Phase 5.

        Previously this was rejected because the BASIC evaluator
        walked the raw payload, ignoring ``workflow_signals``.
        Phase 5 fixed the runtime trap: the validator's
        ``_enrich_basic_payload`` helper merges
        ``run_context.workflow_signals`` into the payload by their
        bare name before evaluation. The BASIC evaluator's lookup
        for ``site_area`` now finds the workflow signal's resolved
        value at the payload root.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            workflow_signal_names={"site_area"},
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "s.site_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
                "cel_expression": "",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # No StepIODefinition for workflow signals — the form
        # stores the bare name in target_data_path_value.
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "site_area",
        )

    def test_basic_assertion_allows_output_target(self):
        """BASIC + o.* still works — the guard is INPUT-only.

        Output targets resolve from extract_output_values() and the
        validator output envelope, which BASIC's payload walk DOES
        handle correctly (the output dict is the payload at output
        stage). Only INPUT targets need to be redirected to CEL.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        output_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="site_eui",
            direction="output",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[output_definition],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": f"o.{output_definition.contract_key}",
                "operator": "lt",
                "comparison_value": "100",
                "severity": Severity.ERROR,
                "cel_expression": "",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_update_custom_validator_persists_validator_fields(self):
        from validibot.validations.tests.factories import CustomValidatorFactory

        custom = CustomValidatorFactory()
        original_version = custom.validator.version
        updated = update_custom_validator(
            custom,
            name="New Name",
            short_description="New short",
            description="New Desc",
            notes="New Notes",
            allow_custom_assertion_targets=True,
            input_data_format="json",
        )
        updated.validator.refresh_from_db()
        self.assertEqual(updated.validator.name, "New Name")
        self.assertEqual(updated.validator.short_description, "New short")
        self.assertEqual(updated.validator.description, "New Desc")
        self.assertEqual(updated.validator.version, original_version)
        self.assertTrue(updated.validator.allow_custom_assertion_targets)
        document_port = updated.validator.step_io_definitions.get(
            contract_key="document",
            direction="input",
        )
        self.assertEqual(document_port.accepted_data_formats, ["json"])
        self.assertEqual(updated.notes, "New Notes")

    def test_target_resolution_prefers_input_without_prefix(self):
        """Bare-name target resolution prefers the input direction.

        The target_data_path bare-name resolution prefers the input
        definition over the output (richer metadata). The CEL expression
        uses i.* to reach the same value — which is the namespace
        that actually carries it at runtime. (Pre-May 2026 follow-up
        review this used s.temperature, which was the mental-model
        trap — runtime puts step inputs in i.*, never s.*.)
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC, is_system=False
        )
        input_entry = StepIODefinitionFactory(
            validator=validator,
            contract_key="temperature",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "temperature",
                "severity": Severity.ERROR,
                "cel_expression": "i.temperature > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())
        # CEL expressions set target_catalog_entry to None — they declare
        # their own targets inside the expression text.
        self.assertIsNone(form.cleaned_data["target_catalog_entry"])

    def test_output_requires_prefix_on_collision(self):
        """When the same contract_key exists as both input and output,
        the bare-name target resolves to the input (richer metadata).

        The CEL expression in each case explicitly chooses the
        intended namespace — i.* for the input form, o.* for the
        prefixed-output form. (Pre-May 2026 follow-up review the
        first form used s.price; that was the mental-model trap
        we're now guarding against.)
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC, is_system=False
        )
        input_entry = StepIODefinitionFactory(
            validator=validator,
            contract_key="price",
            direction="input",
        )
        output_entry = StepIODefinitionFactory.build(
            validator=validator,
            contract_key="price",
            direction="output",
        )

        form = self._form(
            validator=validator,
            catalog_entries=[input_entry, output_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "price",
                "severity": Severity.ERROR,
                "cel_expression": "i.price > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())

        form_prefixed = self._form(
            validator=validator,
            catalog_entries=[input_entry, output_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "output.price",
                "severity": Severity.ERROR,
                "cel_expression": "output.price > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form_prefixed.is_valid())
        # CEL expressions set target_catalog_entry to None
        self.assertIsNone(form_prefixed.cleaned_data["target_catalog_entry"])


# ==============================================================================
# FMU variable form validation
#
# Step-level FMU uploads store variable metadata (name, causality) as
# StepIODefinition rows.  The assertion form must accept these variables
# as valid targets and enforce the ``output.`` prefix convention for
# disambiguation when a name appears as both input and output.
# ==============================================================================


class FMUVariableTargetResolutionTests(TestCase):
    """Tests for basic-assertion target resolution with FMU variables.

    FMU variables are provided as step-owned StepIODefinition rows with
    origin_kind=FMU.  The form reads them from the ``catalog_entries``
    parameter (which now contains all available step I/O definitions).
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        cls.validator.__class__.objects.filter(pk=cls.validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        cls.validator.refresh_from_db()

    def _make_fmu_step_io(self, fmu_variables):
        """Create StepIODefinition objects from fmu_variables dicts."""
        from validibot.validations.constants import StepIOOriginKind
        from validibot.validations.models import StepIODefinition
        from validibot.workflows.tests.factories import WorkflowStepFactory

        step = WorkflowStepFactory()
        io_definitions = []
        for var in fmu_variables:
            name = var["name"]
            causality = var.get("causality", "input")
            direction = "input" if causality == "input" else "output"
            io_definition = StepIODefinition.objects.create(
                workflow_step=step,
                contract_key=name,
                native_name=name,
                direction=direction,
                origin_kind=StepIOOriginKind.FMU,
                data_type="number",
            )
            io_definitions.append(io_definition)
        return io_definitions

    def _fmu_form(self, *, data, fmu_variables):
        """Create a form with FMU step I/O definitions."""
        io_definitions = self._make_fmu_step_io(fmu_variables)
        return RulesetAssertionForm(
            data=data,
            catalog_entries=io_definitions,
            validator=self.validator,
        )

    def test_bare_fmu_input_rejected(self):
        """A bare name (no prefix) is rejected even for FMU inputs.

        Users must reference FMU inputs via the input namespace
        (``i.Q_cooling_max``), which is separate from workflow signals.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "Q_cooling_max",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("for workflow signals", str(form.errors))

    def test_s_prefixed_fmu_input_is_rejected(self):
        """An FMU step input must use ``i.*`` because ``s.*`` means signal."""
        form = self._fmu_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "s.Q_cooling_max",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("use i.Q_cooling_max", str(form.errors))

    def test_bare_fmu_output_rejected(self):
        """A bare name (no prefix) is rejected even for FMU outputs.

        Users must reference FMU outputs via the output namespace
        (``o.T_room``).
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("for workflow signals", str(form.errors))

    def test_o_prefixed_fmu_output_accepted(self):
        """``o.T_room`` resolves to the FMU output StepIODefinition.

        The ``o.`` prefix targets the validator output namespace.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "o.T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["resolved_io_definition"])
        self.assertEqual(
            form.cleaned_data["resolved_io_definition"].contract_key,
            "T_room",
        )

    def test_output_prefix_resolves_fmu_output(self):
        """``output.T_room`` resolves to the FMU output StepIODefinition.

        The ``output.`` prefix is used for explicit disambiguation.
        With the unified step I/O model, this resolves to a StepIODefinition.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "output.T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["resolved_io_definition"])
        self.assertEqual(
            form.cleaned_data["resolved_io_definition"].contract_key,
            "T_room",
        )

    def test_bare_collision_name_rejected(self):
        """A bare name that's both an FMU input and output is rejected
        because all targets now require a namespace prefix.

        The user must write ``o.T_room`` for the output or ``s.T_room``
        for the step input.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "T_room", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("for workflow signals", str(form.errors))

    def test_collision_resolved_with_output_prefix(self):
        """``output.T_room`` resolves to the output StepIODefinition
        even when the name collides with an input variable.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "T_room", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "output.T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["resolved_io_definition"])
        self.assertEqual(
            form.cleaned_data["resolved_io_definition"].contract_key,
            "T_room",
        )


class FMUVariableCelIdentifierTests(TestCase):
    """Tests for CEL identifier validation with FMU variables.

    When ``allow_custom_assertion_targets`` is False, the form validates
    that all identifiers in a CEL expression use namespace prefixes.
    FMU variable names must be referenced with the ``i.`` / ``input.``
    prefix for inputs or ``o.`` / ``output.`` for outputs; bare identifiers
    are rejected.
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        cls.validator.__class__.objects.filter(pk=cls.validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        cls.validator.refresh_from_db()

    def _make_fmu_step_io(self, fmu_variables):
        """Create StepIODefinition objects from fmu_variables dicts.

        Converts the legacy dict format (name, causality) into
        step-owned StepIODefinition rows with origin_kind=FMU.
        """
        from validibot.validations.constants import StepIOOriginKind
        from validibot.validations.models import StepIODefinition
        from validibot.workflows.tests.factories import WorkflowStepFactory

        step = WorkflowStepFactory()
        io_definitions = []
        for var in fmu_variables:
            name = var["name"]
            causality = var.get("causality", "input")
            direction = "input" if causality == "input" else "output"
            io_definition = StepIODefinition.objects.create(
                workflow_step=step,
                contract_key=name,
                native_name=name,
                direction=direction,
                origin_kind=StepIOOriginKind.FMU,
                data_type="number",
            )
            io_definitions.append(io_definition)
        return io_definitions

    def _cel_form(self, *, expression, fmu_variables):
        io_definitions = self._make_fmu_step_io(fmu_variables)
        return RulesetAssertionForm(
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": expression,
                "when_expression": "",
            },
            catalog_entries=io_definitions,
            validator=self.validator,
        )

    def test_namespaced_fmu_names_accepted(self):
        """Namespace-prefixed FMU variable names are valid CEL identifiers.

        FMU inputs live in i.* (resolved from StepInputBindings before
        the container runs) and FMU outputs live in o.* (extracted from
        the output envelope). Both should be accepted when properly
        namespaced.

        Pre-May 2026 follow-up review: this test used ``s.Q_cooling_max``
        for an FMU input — the mental-model trap that runtime does
        NOT inject step inputs into s.*. Updated to use the correct
        namespace per ADR-2026-05-22b.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            expression="o.T_room < i.Q_cooling_max",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_output_prefix_accepted(self):
        """``output.T_room`` is a valid CEL identifier for an FMU output.

        The ``output.`` prefix allows explicit disambiguation in CEL
        expressions, even when there's no name collision.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "T_room", "causality": "output"},
            ],
            expression="output.T_room < 300",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_bare_identifier_rejected(self):
        """Bare (un-namespaced) identifiers are rejected.

        Even when a bare identifier matches a known FMU variable name,
        the form requires namespace prefixes.  This ensures users get
        clear feedback directing them to use ``s.`` or ``p.`` prefixes.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "T_room", "causality": "output"},
            ],
            expression="s.T_room < unknown_var",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Bare identifiers are not allowed", str(form.errors))

    def test_mixed_input_and_output_prefixed_identifiers(self):
        """CEL expressions can use ``i.`` for step inputs and ``o.`` for step outputs.

        This is the typical pattern for assertions that compare an
        output value against a user-provided input value, e.g.,
        ``o.Q_cooling_actual < i.Q_cooling_max * 0.85``.

        Pre-May 2026 follow-up review: this test used s.* for both
        sides — the trap is that runtime puts step inputs in i.*,
        not s.*, so the assertion would have read null at runtime.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "Q_cooling_actual", "causality": "output"},
            ],
            expression="o.Q_cooling_actual < i.Q_cooling_max",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_bare_fmu_name_rejected(self):
        """A bare FMU variable name (without namespace prefix) is rejected.

        Even though ``Q_cooling_max`` is a known FMU input variable, the
        CEL namespace convention requires it to be referenced as ``s.Q_cooling_max``.
        Bare multi-character identifiers that aren't CEL builtins are rejected.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            expression="Q_cooling_max > 0",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Bare identifiers are not allowed", str(form.errors))


# ==============================================================================
# Prefix-based target resolution
#
# All assertion targets must use a namespace prefix (s., p., o.) unless
# the validator enables custom targets.  These tests verify the new
# prefix-based resolution logic.
# ==============================================================================


class PrefixBasedTargetResolutionTests(TestCase):
    """Tests for the prefix-based assertion target resolution.

    After the refactor, assertion targets must use explicit namespace
    prefixes:

    - ``s.<name>`` for workflow signals (always accepted)
    - ``p.<path>`` for payload data (always accepted)
    - ``o.<name>`` for validator outputs (resolved to StepIODefinition)
    - Bare names are rejected unless ``allow_custom_assertion_targets``
    """

    def _form(self, *, validator, catalog_entries, data, workflow_signal_names=None):
        return RulesetAssertionForm(
            data=data,
            catalog_entries=catalog_entries or [],
            validator=validator,
            workflow_signal_names=workflow_signal_names,
        )

    def test_i_prefix_resolves_known_input_via_cel(self):
        """``i.<name>`` works in CEL for any declared step input.

        Step inputs live in the i.* CEL namespace at runtime. The
        catalog declares them; the CEL identifier validator accepts
        i.<contract_key> for any known input.

        This replaces an older test that blessed s.<panel_area> via
        CEL. The May 2026 follow-up review surfaced that as a
        mental-model trap (same shape as the BASIC s.<input> trap):
        the form would resolve the target, but at runtime the value
        lives in i.*, not s.*, so the assertion silently reads null.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "i.panel_area",
                "severity": Severity.ERROR,
                "cel_expression": "i.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_rejects_s_prefix_on_known_step_input(self):
        """CEL rejects ``s.<known_input>`` and points the author at i.*.

        Why it matters: ``inputs_by_slug`` is checked before
        ``workflow_signal_names`` in ``_resolve_target_data_path``,
        and the CEL identifier validator was previously accepting
        ANY ``s.<name>`` reference as long as it looked
        namespace-prefixed. But step inputs live in i.* at runtime —
        they are never injected into s.*. Without this rejection,
        ``s.panel_area`` saves cleanly and then reads null in every
        evaluation.

        Regression test for the May 2026 P2 review finding that
        identified this as the CEL-side equivalent of the BASIC
        s.<input> trap fixed earlier.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        # No workflow_signal_names with this name — the name only
        # exists as a step input.
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "s.panel_area",
                "severity": Severity.ERROR,
                "cel_expression": "s.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        # The error must point the author at i.<name> as the fix.
        self.assertIn("i.panel_area", joined)
        self.assertIn("step inputs live in the i.* namespace", joined.lower())

    def test_cel_allows_s_prefix_when_name_is_both_input_and_workflow_signal(self):
        """A name that exists as BOTH a step input AND a workflow signal
        is a legitimate s.* target — the workflow signal half of the
        collision is real.

        The guard exists specifically to catch names that are ONLY
        known as step inputs (where s.<name> would silently read
        null). If the name is also a workflow signal, then s.<name>
        will resolve correctly at runtime (to the workflow signal's
        value), so the assertion is fine.

        Critically, the test must actually create the collision —
        passing the StepIODefinition into ``catalog_entries`` so
        ``inputs_by_slug`` contains ``panel_area`` AND seeding
        ``workflow_signal_names`` with the same name. Without
        ``catalog_entries=[input_definition]`` the test passed by accident:
        ``inputs_by_slug`` was empty, the guard's collision check
        never fired, and the test only proved "s.<workflow_signal>
        is allowed" — not the harder "s.<both_input_and_signal> is
        still allowed" case the guard's collision-aware branch is
        meant to handle.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            workflow_signal_names={"panel_area"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "s.panel_area",
                "severity": Severity.ERROR,
                "cel_expression": "s.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # Belt-and-braces: confirm the test fixture actually
        # exercises the collision path. If inputs_by_slug doesn't
        # contain panel_area, the form would have passed for the
        # wrong reason.
        self.assertIn("panel_area", form.inputs_by_slug)
        self.assertIn("panel_area", form.workflow_signal_names)

    def test_cel_rejects_s_bracket_access_on_known_step_input(self):
        """CEL rejects ``s["<known_input>"]`` the same way as ``s.<known_input>``.

        Per the CEL spec, ``m.x`` and ``m["x"]`` are equivalent for
        maps with valid-identifier keys, so an author can express
        the same wrong reference via bracket access:
        ``s["panel_area"] > 0``. The previous guard only scanned
        the stripped expression (string literals removed first),
        which meant the bracket form bypassed the check — leaving
        the same mental-model trap through a different valid CEL
        spelling.

        A pre-strip scan over the bracket-access pattern catches
        both quote styles (``"`` and ``'``).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": 's["panel_area"]',
                "severity": Severity.ERROR,
                "cel_expression": 's["panel_area"] > 0',
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        # Error points at i.<name> as the fix (same as dot-access path).
        self.assertIn("i.panel_area", joined)
        self.assertIn("step inputs live in the i.* namespace", joined.lower())

    def test_cel_rejects_signal_bracket_access_with_single_quotes(self):
        """The bracket-access guard catches single-quoted keys too.

        CEL accepts both quote styles, and we ship the long-form
        ``signal["name"]`` alias as well — covering both ensures
        an author can't slip the trap through by mixing quote style
        or namespace alias.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "signal['panel_area']",
                "severity": Severity.ERROR,
                "cel_expression": "signal['panel_area'] > 0",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        self.assertIn("i.panel_area", joined)

    def test_cel_bracket_guard_skips_text_inside_string_literal(self):
        """The bracket guard must not false-positive on string contents.

        ``p.note == 's["panel_area"]'`` is a perfectly valid CEL
        expression that compares the value at ``p.note`` against the
        literal string ``s["panel_area"]``. No bracket access happens
        at runtime — the text just looks like one.

        The previous regex-based guard scanned the raw expression and
        false-positively rejected this with the step-input namespace
        error. The lexical scanner skips CEL string literals so the
        bracket match only fires on real syntax.

        Reproduction of the May 2026 P2 review finding (string-
        literal false positive).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "p.note",
                "severity": Severity.ERROR,
                # The bracket-looking text is inside a single-quoted
                # CEL string — it's not bracket access, it's a string
                # comparison.
                "cel_expression": "p.note == 's[\"panel_area\"]'",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_bracket_guard_catches_hyphenated_contract_key(self):
        """The bracket guard catches slug-shaped contract_keys with hyphens.

        ``StepIODefinition.contract_key`` is a Django ``SlugField``
        which allows ``-`` characters, so a catalog can legitimately
        contain a row keyed ``panel-area``. The previous regex used
        an identifier-shaped pattern (``[A-Za-z_][A-Za-z0-9_]*``) so
        ``s["panel-area"]`` slipped past — the same mental-model trap
        through a key the regex didn't recognize.

        The lexical scanner extracts the bracket contents verbatim
        and the form looks up the key in ``inputs_by_slug``
        directly, so any slug shape the catalog can produce is
        caught.

        Reproduction of the May 2026 P2 review finding (hyphenated
        key bypass).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel-area",
            direction="input",
        )
        # Sanity: the SlugField really stored the hyphen.
        self.assertEqual(input_definition.contract_key, "panel-area")

        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                # The dot-access form is rejected by CEL syntax
                # itself (``-`` isn't a valid identifier char), so
                # bracket access is the only spelling that reaches
                # this catalog row. We're testing the guard
                # specifically for the case where the runtime
                # spelling is forced into bracket syntax.
                "target_data_path": 's["panel-area"]',
                "severity": Severity.ERROR,
                "cel_expression": 's["panel-area"] > 0',
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        # Error points the author at i.<name> (using the same
        # hyphenated key — the i.* namespace handles the same
        # contract_keys the catalog declares).
        self.assertIn("panel-area", joined)
        self.assertIn("step inputs live in the i.* namespace", joined.lower())

    def test_cel_bracket_guard_skips_member_access_p_s(self):
        """``p.s["panel_area"]`` is payload member access, not the s.* namespace.

        CEL allows arbitrary nesting: ``p`` is the payload root, and
        ``p.s`` selects a field named ``s`` on the payload. The
        subsequent ``["panel_area"]`` is then bracket access on that
        field's value (probably a map). None of that touches the s.*
        CEL namespace — the s.* guard must not false-positive on it.

        The scanner enforces this by inspecting the previous non-
        whitespace character: if it's ``.``, the candidate is a
        field access on something else, not a top-level namespace
        reference.

        Reproduction of the May 2026 P2 review finding (member-
        access false positive).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "p.s",
                "severity": Severity.ERROR,
                "cel_expression": 'p.s["panel_area"] == 1',
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_bracket_guard_skips_member_access_payload_signal(self):
        """``payload.signal["panel-area"]`` is payload member access.

        Same trap as ``p.s["…"]`` but with the long-form aliases
        (``payload`` instead of ``p``, ``signal`` instead of ``s``)
        and a hyphenated key to confirm the slug-aware lookup also
        respects the member-access exclusion.

        Reproduction of the May 2026 P2 review finding (long-form
        member-access false positive).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel-area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "payload.signal",
                "severity": Severity.ERROR,
                "cel_expression": 'payload.signal["panel-area"] == 1',
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_bracket_guard_skips_member_access_with_whitespace_dot(self):
        """``p . s["panel_area"]`` (whitespace around the dot) still member access.

        CEL is tolerant of whitespace between the receiver, the
        member-access operator, and the field name. The scanner's
        member-access check walks back over whitespace to find the
        previous non-whitespace character before deciding whether
        it's a ``.``.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "p.s",
                "severity": Severity.ERROR,
                "cel_expression": 'p . s["panel_area"] == 1',
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_allows_s_bracket_access_when_name_is_workflow_signal(self):
        """Bracket-access guard honours the same collision allowance.

        ``s["panel_area"]`` is legitimate when ``panel_area`` is
        a real workflow signal — runtime resolves it via the
        workflow_signals dict. The guard must mirror the dot-access
        branch's collision exception so workflow-signal references
        through bracket syntax aren't false-positives.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            workflow_signal_names={"panel_area"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": 's["panel_area"]',
                "severity": Severity.ERROR,
                "cel_expression": 's["panel_area"] > 0',
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_basic_s_prefix_known_input_is_rejected(self):
        """A declared step input is not a signal and therefore cannot use ``s.*``."""
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "s.panel_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("use i.panel_area", str(form.errors))

    def test_s_prefix_unknown_input_rejected(self):
        """``s.<name>`` is rejected when the name doesn't match any
        declared step input and custom targets are not allowed.

        The evaluator can only resolve ``s.*`` targets that are known workflow
        signals. An unknown ``s.`` name would
        silently fail at runtime, so the form rejects it up front.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "s.panel_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("target_data_path", form.errors)

    def test_s_prefix_resolves_workflow_signal_via_cel(self):
        """``s.<name>`` resolves to a workflow-level signal (signal
        mapping or promoted upstream output) when used in a CEL
        assertion.

        Workflow signals are passed to the form via
        ``workflow_signal_names`` and should always be valid CEL
        targets regardless of the ``allow_custom_assertion_targets``
        setting. This ensures autocomplete choices that include
        workflow signals are never rejected by the form's own
        validation.

        BASIC targeting of s.* is blocked separately
        (``_reject_namespaced_basic_target``) because the BASIC
        evaluator walks the raw payload, not the s.* namespace —
        so this test uses CEL, which DOES resolve s.* through
        the namespaced context.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            workflow_signal_names={"site_area", "building_height"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "s.site_area",
                "severity": Severity.ERROR,
                "cel_expression": "s.site_area > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_s_prefix_prefers_validator_input_over_workflow_signal(self):
        """When a name exists as both a validator input and a workflow signal,
        the validator input takes precedence in target resolution.

        This precedence rule lives in ``_resolve_target_data_path``
        — the step input definition wins because it's a richer
        target (provides StepIODefinition metadata for evaluators
        that can use it).

        The CEL identifier validator separately allows ``s.panel_area``
        here because ``panel_area`` IS also a real workflow signal
        (in ``workflow_signal_names``) — at runtime, s.panel_area
        will resolve to the workflow signal's value. The "s.<input>
        but not workflow signal" case is rejected by a different
        guard (see ``test_cel_rejects_s_prefix_on_known_step_input``).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            workflow_signal_names={"panel_area"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "s.panel_area",
                "severity": Severity.ERROR,
                "cel_expression": "s.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # CEL assertions don't use resolved_io_definition at evaluation
        # time — the form clears it (see clean()). But the
        # autocomplete and CEL-identifier validation paths still
        # exercise the same resolution lookup, so the precedence
        # rule is still meaningful to test.

    def test_p_prefix_always_accepted(self):
        """``p.<path>`` targets are always accepted without requiring
        ``allow_custom_assertion_targets``.

        Payload paths reference raw submission data and are resolved
        at the input stage before the validator runs.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "p.building.floor_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["resolved_io_definition"])
        # The "p." prefix is stripped so the evaluator resolves the
        # bare path against the raw payload dict.
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "building.floor_area",
        )
        from validibot.validations.constants import CatalogRunStage

        self.assertEqual(
            form.cleaned_data["resolved_stage"],
            CatalogRunStage.INPUT,
        )

    def test_payload_prefix_accepted(self):
        """The long-form ``payload.<path>`` prefix is also accepted.

        This is an alias for ``p.`` and should resolve identically.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "payload.zones[0].temp",
                "operator": "lt",
                "comparison_value": "30",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # The "payload." prefix is stripped — the evaluator resolves
        # the bare path against the payload dict.
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "zones[0].temp",
        )

    def test_o_prefix_resolves_output_value(self):
        """``o.<name>`` resolves to the output StepIODefinition.

        Output-prefixed targets are resolved against the validator's
        declared output values and set the stage to OUTPUT.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        output_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="site_eui",
            direction="output",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[output_definition],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "o.site_eui",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["resolved_io_definition"])
        self.assertEqual(
            form.cleaned_data["resolved_io_definition"].contract_key,
            "site_eui",
        )
        from validibot.validations.constants import CatalogRunStage

        self.assertEqual(
            form.cleaned_data["resolved_stage"],
            CatalogRunStage.OUTPUT,
        )

    def test_bare_name_rejected_without_custom_targets(self):
        """A bare name without any namespace prefix is rejected when
        ``allow_custom_assertion_targets`` is False.

        The error message should direct the user to use ``s.``, ``p.``,
        or ``o.`` prefixes.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "temperature",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("for workflow signals", str(form.errors))

    def test_bare_name_accepted_with_custom_targets(self):
        """A bare dotted path is accepted when the validator enables
        custom assertion targets.

        Custom validators may deliberately expose free-form data paths instead
        of a fixed StepIODefinition catalog.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "metrics.custom.value",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "metrics.custom.value",
        )

    def test_signal_prefix_long_form_accepted_for_basic(self):
        """The long-form ``signal.<name>`` prefix is accepted post Phase 5.

        Both ``s.`` and ``signal.`` route through the same
        workflow-signal resolution path. Previously both were
        rejected for BASIC because the evaluator walked the raw
        payload. Phase 5's ``_enrich_basic_payload`` merges
        workflow signals into the payload by their bare name, so
        the ``contract_key`` lookup finds the value.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            workflow_signal_names={"panel_area"},
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "signal.panel_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # No StepIODefinition — workflow signal stored as bare path.
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "panel_area",
        )

    # ── Smart-quote (curly quote) detection ─────────────────────────
    # A CEL string literal must use straight quotes. When an author pastes
    # an expression from a document, email, or chat, the quotes around a
    # string list often arrive as curly "smart" quotes (U+201C/D, U+2018/9).
    # CEL's lexer — and our string-literal stripper — only recognise straight
    # quotes, so the text *inside* a smart-quoted literal (e.g. a URI list)
    # gets scanned as bare identifiers. The author then sees a baffling
    # "unknown identifier" error naming fragments of their own string. These
    # tests prove we intercept that case with a message that names the real
    # fix (replace the curly quotes) instead.

    def test_cel_rejects_curly_double_quotes_with_clear_message(self):
        """Curly double quotes get a targeted "smart quotes" error.

        Why it matters: this is the exact failure a SHACL author hits when
        pasting a namespace allow-list — ``["http://…"]`` arrives with
        ``“ ”`` around each URI, the URI body is lexed as ``http``,
        ``onuma.com`` … and the bare-identifier check fires with a message
        that points at the author's data, not the actual mistake. The
        smart-quote guard must win first and explain the real fix.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        # A string list using curly double quotes around each element.
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": ("p.ns.all(x, x in [“http://onuma.com/schema#”])"),
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        # The error must name the real cause, not "bare identifiers".
        self.assertIn("curly", joined.lower())
        self.assertNotIn("Bare identifiers", joined)

    def test_cel_rejects_curly_single_quotes_with_clear_message(self):
        """Curly single quotes are caught the same way as double quotes.

        Why it matters: single-quoted CEL string literals are equally valid,
        so a pasted ``'…'`` that arrives as ``‘ ’`` must trigger the same
        helpful guidance rather than slipping into the identifier scanner.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": "p.note == ‘hello’",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        self.assertIn("curly", joined.lower())

    def test_cel_allows_straight_quoted_string_list(self):
        """The straight-quote version of the same expression is accepted.

        This is the control for the smart-quote tests above: the only
        difference is the quote characters, so a passing straight-quote
        case proves the guard targets the quotes specifically and does not
        reject the surrounding expression shape.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        output_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="ns",
            direction="output",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[output_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": ('o.ns.all(x, x in ["http://onuma.com/schema#"])'),
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    # ── Wrong-stage namespace detection (i.<output> / o.<input>) ─────
    # Step outputs live in the o.* CEL namespace and step inputs in i.*.
    # Crossing them passes the namespace-root check (both roots are valid)
    # but reads null at runtime, because i.* never carries outputs and o.*
    # never carries inputs. SHACL is the headline case: every SHACL output
    # (namespaces_present, conforms, …) is a step output, so an author who
    # writes ``i.namespaces_present`` would save a silently-null assertion.
    # These tests prove the form catches the mistake and names the correct
    # spelling, mirroring the existing s.<input> trap guard.

    def test_cel_rejects_input_prefix_on_known_step_output(self):
        """``i.<output>`` is rejected and the author is pointed at o.*.

        Why it matters: this is the precise trap a SHACL author falls into.
        ``namespaces_present`` is an output-only value, so ``i.*`` will be
        empty for it at run time and the assertion would silently evaluate
        against null. The guard must reject the save and suggest
        ``o.namespaces_present``.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            is_system=False,
        )
        output_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="namespaces_present",
            direction="output",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[output_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": (
                    'i.namespaces_present.all(ns, ns in ["http://onuma.com/schema#"])'
                ),
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        self.assertIn("o.namespaces_present", joined)
        self.assertIn("step output", joined.lower())
        # Must NOT degrade into the generic bare-identifier message.
        self.assertNotIn("Bare identifiers", joined)

    def test_cel_rejects_output_prefix_on_known_step_input(self):
        """The mirror image: ``o.<input>`` is rejected and points at i.*.

        Why it matters: the same wrong-stage class of bug applies in reverse.
        A step input referenced through ``o.*`` reads null at run time, so the
        guard should be symmetric and suggest the ``i.<name>`` spelling.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": "o.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        self.assertIn("i.panel_area", joined)
        self.assertIn("step input", joined.lower())

    def test_cel_allows_output_prefix_on_known_step_output(self):
        """``o.<output>`` (the correct spelling) validates cleanly.

        This is the positive control for the wrong-stage guard: a SHACL
        output referenced via ``o.*`` must pass, proving the guard fires on
        the wrong namespace only — never on the correct one.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            is_system=False,
        )
        output_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="namespaces_present",
            direction="output",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[output_definition],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": (
                    'o.namespaces_present.all(ns, ns in ["http://onuma.com/schema#"])'
                ),
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_wrong_stage_guard_skips_name_that_is_also_workflow_signal(self):
        """A name that is also a workflow signal is exempt from the guard.

        Why it matters: the guard must only fire for names that are
        *exclusively* a step output (or input). If the same name is also a
        workflow signal — a promoted upstream value — then it legitimately
        resolves through s.* at run time, so the author has not made the
        wrong-stage mistake and the save must be allowed. This mirrors the
        collision exception already in the s.<input> guard.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            is_system=False,
        )
        output_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="namespaces_present",
            direction="output",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[output_definition],
            workflow_signal_names={"namespaces_present"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": "i.namespaces_present == true",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
