"""
Tests for ensure_step_input_bindings().

This function creates default StepInputBinding rows for validator-owned
step inputs that don't already have bindings on a given step. This
ensures the step-input resolution engine has the explicit contract it needs
before launch.

The function is called after step creation/update in save_workflow_step().
It only handles CATALOG-origin inputs — FMU and TEMPLATE inputs have
their own dedicated sync functions.
"""

from __future__ import annotations

from io import StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase

from validibot.validations.constants import FMU_MODEL_RESOURCE
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.models import FMUModel
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

EXPECTED_SYSTEM_ARTIFACT_INPUTS = {
    "energyplus-idf-validator": {"primary_model", "weather_file"},
    "fmu-validator": {"fmu_model"},
    "json-schema-validator": {"json_document"},
    "pdf-validator": {"pdf_document"},
    "portfolio-manager-validator": {
        "expected_buildings_list",
        "portfolio_manager_report",
    },
    "schematron-validator": {"xml_document"},
    "shacl-validator": {"data_graph"},
    "therm-validator": {"therm_model"},
    "xml-validator": {"xml_document"},
}

# ── Core binding creation ────────────────────────────────────────────
# These tests verify that the function creates the right bindings for
# CATALOG-origin step inputs owned by the step's validator.


class TestEnsureStepInputBindings(TestCase):
    """Tests for the ensure_step_input_bindings() service function."""

    def test_creates_bindings_for_input_values(self):
        """CATALOG step inputs owned by the validator should each get a
        StepInputBinding so the resolution engine can map submission
        data to validator inputs through explicit, traceable bindings.
        """
        validator = ValidatorFactory()
        input_a = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            native_name="panel_area",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
        )
        input_b = StepIODefinitionFactory(
            validator=validator,
            contract_key="heating_setpoint",
            native_name="heating_setpoint",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 2)
        self.assertEqual(
            StepInputBinding.objects.filter(workflow_step=step).count(),
            2,
        )

        binding_a = StepInputBinding.objects.get(
            workflow_step=step,
            io_definition=input_a,
        )
        self.assertEqual(
            binding_a.source_scope,
            BindingSourceScope.SUBMISSION_PAYLOAD,
        )
        self.assertEqual(binding_a.source_data_path, "")
        self.assertTrue(binding_a.is_required)
        self.assertIsNone(binding_a.default_value)

        binding_b = StepInputBinding.objects.get(
            workflow_step=step,
            io_definition=input_b,
        )
        self.assertEqual(binding_b.source_data_path, "")

    def test_parser_extracted_inputs_use_internal_source_kind(self):
        """EnergyPlus parser-extracted step inputs use INTERNAL source_kind.

        Per ADR-2026-05-22 (validator revision 2+), the legacy
        ``submission.metadata``-bound expectation inputs
        (``expected_floor_area_m2``, ``target_eui_kwh_m2``,
        ``max_unmet_hours``) were removed because they were author
        expectations miscategorized as validator inputs. They were
        replaced by three parser-extracted step inputs (``idf_version``,
        ``zone_count``, ``north_axis_deg``) that the validator parses
        from the IDF itself.

        These parser-extracted step inputs declare
        ``source_kind=INTERNAL`` (the validator parses them; no
        author-supplied payload path is involved). This test verifies
        that semantics is preserved when sync_validators creates the
        StepIODefinition rows.
        """
        call_command("sync_validators")
        validator = Validator.objects.get(slug="energyplus-idf-validator")

        io_definition = StepIODefinition.objects.get(
            validator=validator,
            contract_key="idf_version",
            direction=StepIODirection.INPUT,
        )
        # Per ADR-2026-05-22b, parser-extracted facts are INTERNAL:
        # the validator's extract_input_values() hook produces their
        # values directly, so author-supplied source paths don't apply.
        self.assertEqual(io_definition.source_kind, "internal")
        self.assertFalse(io_definition.is_path_editable)

    def test_artifact_input_uses_submission_file_scope_by_default(self):
        """Artifact ports should not be wired as payload-path value inputs."""
        validator = ValidatorFactory()
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="primary_model",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            role="primary-model",
            min_items=1,
            max_items=1,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 1)
        binding = StepInputBinding.objects.get(
            workflow_step=step,
            io_definition=io_definition,
        )
        self.assertEqual(binding.source_scope, BindingSourceScope.SUBMISSION_FILE)
        self.assertEqual(binding.source_data_path, "primary")
        self.assertTrue(binding.is_required)

    def test_resource_artifact_input_uses_workflow_resource_scope_by_default(self):
        """Resource-backed artifact ports should bind to workflow resources."""
        validator = ValidatorFactory()
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="weather_file",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            role="weather",
            resource_type="energyplus_weather",
            min_items=1,
            max_items=1,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 1)
        binding = StepInputBinding.objects.get(
            workflow_step=step,
            io_definition=io_definition,
        )
        self.assertEqual(binding.source_scope, BindingSourceScope.WORKFLOW_RESOURCE)
        self.assertEqual(binding.source_data_path, "energyplus_weather")
        self.assertTrue(binding.is_required)

    def test_library_fmu_model_port_uses_system_scope_by_default(self):
        """Library FMU validators should bind their attached FMU model directly."""
        fmu_model = FMUModel.objects.create(
            name="Library FMU",
            file=SimpleUploadedFile("library.fmu", b"fmu-bytes"),
        )
        validator = ValidatorFactory(
            validation_type=ValidationType.FMU,
            fmu_model=fmu_model,
        )
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="fmu_model",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            role="fmu",
            resource_type=FMU_MODEL_RESOURCE,
            min_items=1,
            max_items=1,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 1)
        binding = StepInputBinding.objects.get(
            workflow_step=step,
            io_definition=io_definition,
        )
        self.assertEqual(binding.source_scope, BindingSourceScope.SYSTEM)
        self.assertEqual(binding.source_data_path, "fmu_model")
        self.assertTrue(binding.is_required)

    def test_every_system_file_port_gets_an_explicit_valid_default_binding(self):
        """Every deployed file input must enter runtime through one binding.

        This matrix is intentionally explicit: adding or renaming a system file
        port is semantic backend drift and must update this test alongside the
        validator version. It prevents production-shaped tests from passing on
        partial factory rows while a clean installation fails before dispatch.
        """
        call_command(
            "sync_validators",
            stdout=StringIO(),
            stderr=StringIO(),
        )

        observed: dict[str, set[str]] = {}
        validators = (
            Validator.objects.filter(
                is_system=True,
                availability_state=ValidatorAvailabilityState.AVAILABLE,
                step_io_definitions__direction=StepIODirection.INPUT,
                step_io_definitions__io_medium=StepIOMedium.ARTIFACT,
            )
            .distinct()
            .order_by("slug", "version")
        )
        for validator in validators:
            with self.subTest(validator=validator.slug):
                ports = list(
                    validator.step_io_definitions.filter(
                        direction=StepIODirection.INPUT,
                        io_medium=StepIOMedium.ARTIFACT,
                    ).order_by("contract_key")
                )
                observed[validator.slug] = {port.contract_key for port in ports}

                step = WorkflowStepFactory(validator=validator)
                ensure_step_input_bindings(step)
                bindings = {
                    binding.io_definition.contract_key: binding
                    for binding in StepInputBinding.objects.filter(
                        workflow_step=step,
                        io_definition__in=ports,
                    ).select_related("io_definition")
                }

                self.assertEqual(set(bindings), observed[validator.slug])
                for port in ports:
                    binding = bindings[port.contract_key]
                    self.assertIn(binding.source_scope, port.allowed_source_scopes)
                    if binding.source_scope == BindingSourceScope.SUBMISSION_FILE:
                        self.assertEqual(binding.source_data_path, "primary")

        self.assertEqual(observed, EXPECTED_SYSTEM_ARTIFACT_INPUTS)


# ── Filtering: only the right I/O definitions get bindings ───────────────────
# These tests verify that output values and non-CATALOG I/O definitions are
# correctly excluded so we don't create spurious bindings.


class TestEnsureStepInputBindingsFiltering(TestCase):
    """Tests that the function only creates bindings for the correct subset
    of I/O definitions (CATALOG-origin inputs)."""

    def test_skips_output_values(self):
        """Step outputs should not get bindings because they are produced
        by the validator, not consumed from submission data.
        """
        validator = ValidatorFactory()
        StepIODefinitionFactory(
            validator=validator,
            contract_key="energy_demand",
            direction=StepIODirection.OUTPUT,
            origin_kind=StepIOOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 0)
        self.assertFalse(
            StepInputBinding.objects.filter(workflow_step=step).exists(),
        )

    def test_skips_step_owned_io_definitions(self):
        """FMU and TEMPLATE origin I/O definitions are step-owned and managed by
        their own sync functions, so ensure_step_input_bindings should
        not create bindings for them.
        """
        validator = ValidatorFactory()
        # FMU-origin I/O definition owned by the validator (unusual but possible
        # in test scenarios — the key filter is origin_kind, not owner FK).
        StepIODefinitionFactory(
            validator=validator,
            contract_key="fmu_temp",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.FMU,
        )
        StepIODefinitionFactory(
            validator=validator,
            contract_key="template_var",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.TEMPLATE,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 0)
        self.assertFalse(
            StepInputBinding.objects.filter(workflow_step=step).exists(),
        )


# ── Idempotency and safety ───────────────────────────────────────────
# These tests verify that the function is safe to call repeatedly and
# does not overwrite existing bindings that may have been customised.


class TestEnsureStepInputBindingsIdempotency(TestCase):
    """Tests for idempotency and preservation of existing bindings."""

    def test_idempotent(self):
        """Running twice should create bindings only on the first call.
        The second call should detect existing bindings and skip them,
        returning 0 new bindings created.
        """
        validator = ValidatorFactory()
        StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        first_count = ensure_step_input_bindings(step)
        second_count = ensure_step_input_bindings(step)

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 0)
        self.assertEqual(
            StepInputBinding.objects.filter(workflow_step=step).count(),
            1,
        )

    def test_preserves_existing_bindings(self):
        """If a binding already exists with a custom source_data_path
        (e.g., set by the workflow author), ensure_step_input_bindings
        must not overwrite it. This protects author customisations.
        """
        validator = ValidatorFactory()
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            native_name="panel_area",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        # Simulate an author-customised binding with a nested path.
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=io_definition,
            source_data_path="building.envelope.panel_area",
            is_required=False,
        )

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 0)
        binding = StepInputBinding.objects.get(
            workflow_step=step,
            io_definition=io_definition,
        )
        # The custom path must be preserved, not overwritten.
        self.assertEqual(
            binding.source_data_path,
            "building.envelope.panel_area",
        )
        self.assertFalse(binding.is_required)

    def test_returns_count(self):
        """The return value should be the exact number of newly created
        bindings, which callers can use for logging or diagnostics.
        """
        validator = ValidatorFactory()
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="input_a",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
        )
        StepIODefinitionFactory(
            validator=validator,
            contract_key="input_b",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        # Pre-create one binding so only one should be new.
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=input_definition,
        )

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 1)

    def test_no_validator_noop(self):
        """Steps without a validator (e.g., action steps) should return 0
        without querying for I/O definitions or creating any bindings.

        We use a step with its validator_id cleared in memory (not saved)
        because the DB-level XOR constraint requires either a validator or
        an action to be set.
        """
        step = WorkflowStepFactory()
        # Simulate an action step by clearing the validator FK in memory.
        step.validator = None
        step.validator_id = None

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 0)


# ── source_kind and is_path_editable ────────────────────────────────
# These fields live on StepIODefinition and describe how the port's
# value is obtained. They should NOT affect binding creation — bindings
# are always created regardless of source_kind or path editability.


class TestEnsureStepInputBindingsSourceKind(TestCase):
    """Verify that source_kind/is_path_editable don't affect binding creation."""

    def test_internal_non_editable_inputs_still_get_bindings(self):
        """INTERNAL I/O definitions with is_path_editable=False should still get
        StepInputBinding rows. The binding exists so the resolution engine
        activates — the is_path_editable flag only controls UI editability,
        not whether a binding is created.
        """
        from validibot.validations.constants import StepIOSourceKind

        validator = ValidatorFactory()
        StepIODefinitionFactory(
            validator=validator,
            contract_key="site_eui",
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_input_bindings(step)

        self.assertEqual(count, 1)
        self.assertTrue(
            StepInputBinding.objects.filter(workflow_step=step).exists(),
        )
