"""Tests for Portfolio Manager authoring and Django-side orchestration.

The isolated backend owns untrusted XLS/XLSX/XML/ZIP parsing, while the
community application owns workflow configuration, the versioned EBL resource,
catalog outputs, and execution routing. These tests pin that boundary so the
convenience form cannot drift from the shared envelope contract.
"""

import hashlib
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from validibot_shared.portfolio_manager import PortfolioManagerInputEnvelope
from validibot_shared.portfolio_manager import PortfolioManagerOutputs
from validibot_shared.portfolio_manager import PortfolioManagerPropertyResult

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
from validibot.validations.constants import PORTFOLIO_MANAGER_EBL_RESOURCE
from validibot.validations.constants import PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import ValidationType
from validibot.validations.forms import RulesetAssertionForm
from validibot.validations.models import ResolvedInputTrace
from validibot.validations.models import StepInputBinding
from validibot.validations.models import Validator
from validibot.validations.services.cloud_run.envelope_builder import (
    build_input_envelope,
)
from validibot.validations.services.file_identity import FileIdentity
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.portfolio_manager.output_groups import (
    ALL_PROPERTY_OUTPUT_KEYS,
)
from validibot.validations.validators.portfolio_manager.output_groups import (
    GROUPED_PROPERTY_OUTPUT_KEYS,
)
from validibot.validations.validators.portfolio_manager.output_groups import (
    SINGLE_PROPERTY_OUTPUT_KEYS,
)
from validibot.validations.validators.portfolio_manager.output_groups import (
    referenced_output_keys,
)
from validibot.validations.validators.portfolio_manager.validator import (
    PortfolioManagerValidator,
)
from validibot.workflows.forms import DisplayStepOutputsForm
from validibot.workflows.forms import PortfolioManagerStepConfigForm
from validibot.workflows.forms import get_config_form_class
from validibot.workflows.mixins import WorkflowStepAssertionsMixin
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import build_step_io_context

EBL_JSON = b"""{
  "schema_version": "1.0",
  "id_field": {
    "kind": "standard_id",
    "name": "State of Washington Clean Buildings Standard"
  },
  "euit_unit": "kBtu/ft2/year",
  "buildings": [
    {"id_value": "WA-001", "euit": "40.0"},
    {"id_value": "WA-002"}
  ]
}"""
CONFIGURED_MAXIMUM_REPORT_AGE_MONTHS = 30
SHA256_HEX_LENGTH = 64
CONFIGURED_ARCHIVE_MEMBER_LIMIT = 75
CONFIGURED_MEMBER_BYTES = 15_000_000
MEASURED_WNEUI = 39.5
RESOLVED_EUIT = 40.0
BOUND_EUIT = 42.0
SINGLE_OUTPUT_COUNT = 26
GROUPED_OUTPUT_COUNT = 32
TOTAL_SCALAR_OUTPUT_COUNT = 58


def _base_form_data(**overrides):
    """Return a minimally explicit browser-shaped Portfolio Manager form."""
    data = {
        "name": "Check Portfolio Manager report",
        "submission_structure": "single_report",
        "near_target_percent": "5",
        "minimum_reporting_period_months": "12",
    }
    data.update(overrides)
    return data


def _sync_system_validators() -> None:
    """Create the exact validator catalog that production startup uses."""
    call_command(
        "sync_validators",
        stdout=StringIO(),
        stderr=StringIO(),
    )


def _file_identity(uri: str, content: bytes) -> FileIdentity:
    """Build a local immutable file identity for an envelope assertion."""
    digest = hashlib.sha256(content).hexdigest()
    return FileIdentity(
        uri=uri,
        size_bytes=len(content),
        sha256=digest,
        storage_version=f"sha256:{digest}",
    )


def test_registry_exposes_a_dedicated_advanced_backend_contract() -> None:
    """The validator must route through isolation rather than TabularValidator."""
    config = get_config(ValidationType.PORTFOLIO_MANAGER)

    assert config is not None
    assert config.slug == "portfolio-manager-validator"
    assert config.image_name == "validibot-validator-backend-portfolio-manager"
    assert ValidationType.PORTFOLIO_MANAGER in ADVANCED_VALIDATION_TYPES
    assert get_config_form_class(ValidationType.PORTFOLIO_MANAGER) is (
        PortfolioManagerStepConfigForm
    )


def test_output_groups_are_complete_disjoint_authoring_inventories() -> None:
    """The two authoring groups must cover all 58 scalar outputs exactly once."""
    validator_config = get_config(ValidationType.PORTFOLIO_MANAGER)
    catalog_output_keys = {
        entry.slug
        for entry in validator_config.catalog_entries
        if entry.run_stage == StepIODirection.OUTPUT
        and entry.io_medium == StepIOMedium.VALUE
    }

    assert len(SINGLE_PROPERTY_OUTPUT_KEYS) == SINGLE_OUTPUT_COUNT
    assert len(GROUPED_PROPERTY_OUTPUT_KEYS) == GROUPED_OUTPUT_COUNT
    assert not set(SINGLE_PROPERTY_OUTPUT_KEYS) & set(GROUPED_PROPERTY_OUTPUT_KEYS)
    assert len(ALL_PROPERTY_OUTPUT_KEYS) == TOTAL_SCALAR_OUTPUT_COUNT
    assert catalog_output_keys == ALL_PROPERTY_OUTPUT_KEYS
    assert referenced_output_keys('"o.property_count"') == set()


def test_form_preserves_each_explicit_author_policy_choice() -> None:
    """V1 exposes policy fields directly and must not hide a preset shortcut."""
    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(
            maximum_reporting_period_age_months=(
                str(CONFIGURED_MAXIMUM_REPORT_AGE_MONTHS)
            ),
            require_benchmark_ready="on",
            require_washington_standard_id="on",
            meter_gap_policy="warning",
            estimated_energy_policy="error",
        ),
    )

    assert "profile" not in form.fields
    assert form.is_valid(), form.errors
    assert form.cleaned_data["require_complete_reporting_period"] is False
    assert (
        form.cleaned_data["maximum_reporting_period_age_months"]
        == CONFIGURED_MAXIMUM_REPORT_AGE_MONTHS
    )
    assert form.cleaned_data["require_benchmark_ready"] is True
    assert form.cleaned_data["require_form_c_ready"] is False
    assert form.cleaned_data["require_washington_standard_id"] is True
    assert form.cleaned_data["meter_gap_policy"] == "warning"
    assert form.cleaned_data["estimated_energy_policy"] == "error"


@pytest.mark.django_db
def test_single_to_grouped_change_is_blocked_by_single_output_assertion() -> None:
    """A mode change cannot leave a basic assertion targeting a single fact."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.PORTFOLIO_MANAGER,
    )
    step = WorkflowStepFactory(
        validator=validator,
        ruleset=ruleset,
        config={"submission_structure": "single_report"},
    )
    output_definition = validator.step_io_definitions.get(
        contract_key="energy_star_score",
        direction=StepIODirection.OUTPUT,
    )
    RulesetAssertionFactory(
        ruleset=ruleset,
        target_io_definition=output_definition,
        target_data_path="",
    )

    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(submission_structure="zip_collection"),
        step=step,
        validator=validator,
    )

    assert not form.is_valid()
    error = str(form.errors["submission_structure"])
    assert "Single property outputs" in error
    assert "o.energy_star_score" in error


@pytest.mark.django_db
def test_grouped_to_single_change_is_blocked_by_grouped_output_assertion() -> None:
    """A mode change cannot leave a CEL assertion using a grouped aggregate."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.PORTFOLIO_MANAGER,
    )
    step = WorkflowStepFactory(
        validator=validator,
        ruleset=ruleset,
        config={"submission_structure": "zip_collection"},
    )
    expression = 'o["target_coverage_percent"] >= 95'
    RulesetAssertionFactory(
        ruleset=ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        operator=AssertionOperator.CEL_EXPR,
        target_io_definition=None,
        target_data_path=expression,
        rhs={"expr": expression},
    )

    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(submission_structure="single_report"),
        step=step,
        validator=validator,
    )

    assert not form.is_valid()
    error = str(form.errors["submission_structure"])
    assert "Grouped property outputs" in error
    assert "o.target_coverage_percent" in error


@pytest.mark.django_db
def test_structure_change_is_allowed_when_assertions_remain_compatible() -> None:
    """Changing structure remains simple when no assertion uses the old group."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    step = WorkflowStepFactory(
        validator=validator,
        config={"submission_structure": "single_report"},
    )

    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(submission_structure="zip_collection"),
        step=step,
        validator=validator,
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_authoring_surfaces_show_only_the_selected_output_group() -> None:
    """The card, display form, and assertion picker must share one inventory."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    step = WorkflowStepFactory(
        validator=validator,
        config={"submission_structure": "single_report"},
    )

    single_context = build_step_io_context(step)
    single_display_form = DisplayStepOutputsForm(step=step, validator=validator)
    single_mixin = WorkflowStepAssertionsMixin()
    single_mixin.step = step
    single_choices = single_mixin.get_catalog_choices()

    assert single_context["output_group_label"] == "Single property outputs"
    assert {row["slug"] for row in single_context["output_values"]} == set(
        SINGLE_PROPERTY_OUTPUT_KEYS
    )
    assert {
        value
        for value, _label in single_display_form.fields["display_step_outputs"].choices
    } == set(SINGLE_PROPERTY_OUTPUT_KEYS)
    single_output_choices = [
        (value, str(label)) for value, label in single_choices if value.startswith("o.")
    ]
    assert {value.removeprefix("o.") for value, _label in single_output_choices} == set(
        SINGLE_PROPERTY_OUTPUT_KEYS
    )
    assert all("Single property outputs" in label for _, label in single_output_choices)

    step.config = {"submission_structure": "zip_collection"}
    step.save(update_fields=["config", "modified"])
    grouped_context = build_step_io_context(step)
    grouped_display_form = DisplayStepOutputsForm(step=step, validator=validator)
    grouped_mixin = WorkflowStepAssertionsMixin()
    grouped_mixin.step = step
    grouped_choices = grouped_mixin.get_catalog_choices()

    assert grouped_context["output_group_label"] == "Grouped property outputs"
    assert {row["slug"] for row in grouped_context["output_values"]} == set(
        GROUPED_PROPERTY_OUTPUT_KEYS
    )
    assert {
        value
        for value, _label in grouped_display_form.fields["display_step_outputs"].choices
    } == set(GROUPED_PROPERTY_OUTPUT_KEYS)
    grouped_output_choices = [
        (value, str(label))
        for value, label in grouped_choices
        if value.startswith("o.")
    ]
    assert {
        value.removeprefix("o.") for value, _label in grouped_output_choices
    } == set(GROUPED_PROPERTY_OUTPUT_KEYS)
    assert all(
        "Grouped property outputs" in label for _, label in grouped_output_choices
    )


@pytest.mark.django_db
def test_cel_form_rejects_an_output_from_the_other_structure() -> None:
    """Typing a hidden grouped output manually must not bypass the filtered list."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    allowed_definitions = list(
        validator.step_io_definitions.filter(
            contract_key__in=SINGLE_PROPERTY_OUTPUT_KEYS,
        )
    )
    form = RulesetAssertionForm(
        data={
            "assertion_type": AssertionType.CEL_EXPRESSION,
            "severity": Severity.ERROR,
            "cel_expression": "o.property_count > 0",
            "when_expression": "",
        },
        catalog_entries=allowed_definitions,
        validator=validator,
    )

    assert not form.is_valid()
    assert "selected submission structure" in str(form.errors["cel_expression"])


def test_ebl_upload_rejects_duplicate_json_keys() -> None:
    """Duplicate JSON keys cannot silently replace roster or target evidence."""
    duplicate = EBL_JSON.replace(
        b'"schema_version": "1.0",',
        b'"schema_version": "1.0", "schema_version": "1.0",',
    )
    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(submission_structure="zip_collection"),
        files={
            "expected_buildings_list": SimpleUploadedFile(
                "buildings.json",
                duplicate,
                content_type="application/json",
            )
        },
    )

    assert not form.is_valid()
    assert "duplicate JSON key" in str(form.errors["expected_buildings_list"])


@pytest.mark.django_db
def test_saving_zip_configuration_persists_a_hashed_ebl_resource() -> None:
    """A valid roster is stored as a step-owned resource, not config JSON."""
    from validibot.validations.tests.factories import ValidatorFactory
    from validibot.workflows.tests.factories import WorkflowFactory
    from validibot.workflows.views_helpers import save_workflow_step

    workflow = WorkflowFactory()
    validator = ValidatorFactory(
        slug="portfolio-manager-validator",
        validation_type=ValidationType.PORTFOLIO_MANAGER,
        is_system=True,
        supports_assertions=True,
    )
    default_euit_input = StepIODefinitionFactory(
        validator=validator,
        contract_key="default_euit_kbtu_ft2_yr",
        native_name="default_euit_kbtu_ft2_yr",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.NUMBER,
        io_medium=StepIOMedium.VALUE,
    )
    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(
            submission_structure="zip_collection",
            default_euit_kbtu_ft2_yr="42",
            max_archive_members="75",
            max_member_size_mb="15",
            max_uncompressed_size_mb="200",
        ),
        files={
            "expected_buildings_list": SimpleUploadedFile(
                "buildings.json",
                EBL_JSON,
                content_type="application/json",
            )
        },
        validator=validator,
        workflow=workflow,
    )
    assert form.is_valid(), form.errors

    step = save_workflow_step(workflow, validator, form)

    resource = step.step_resources.get(
        role=WorkflowStepResource.EXPECTED_BUILDINGS_LIST
    )
    assert resource.resource_type == PORTFOLIO_MANAGER_EBL_RESOURCE
    assert len(resource.content_hash) == SHA256_HEX_LENGTH
    assert "expected_buildings_list" not in step.config
    assert step.config["max_archive_members"] == CONFIGURED_ARCHIVE_MEMBER_LIMIT
    assert step.config["max_member_bytes"] == CONFIGURED_MEMBER_BYTES
    target_binding = StepInputBinding.objects.get(
        workflow_step=step,
        io_definition=default_euit_input,
    )
    assert target_binding.source_scope == BindingSourceScope.CONSTANT
    assert target_binding.default_value == BOUND_EUIT


@pytest.mark.django_db
def test_synced_catalog_creates_portfolio_manager_input_bindings() -> None:
    """The report, EBL, and EUIt inputs must receive their intended scopes."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    step = WorkflowStepFactory(validator=validator)

    ensure_step_input_bindings(step)

    bindings = {
        binding.io_definition.contract_key: binding
        for binding in StepInputBinding.objects.filter(
            workflow_step=step,
        ).select_related("io_definition")
    }
    assert bindings["portfolio_manager_report"].source_scope == (
        BindingSourceScope.SUBMISSION_FILE
    )
    assert bindings["expected_buildings_list"].source_scope == (
        BindingSourceScope.WORKFLOW_RESOURCE
    )
    assert bindings["expected_buildings_list"].is_required is False
    assert bindings["default_euit_kbtu_ft2_yr"].source_scope == (
        BindingSourceScope.CONSTANT
    )
    assert bindings["default_euit_kbtu_ft2_yr"].is_required is False


@pytest.mark.django_db
def test_zip_envelope_materializes_report_ebl_and_resolved_inputs() -> None:
    """The app must send one strict, traceable contract to the isolated backend."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    step = WorkflowStepFactory(
        validator=validator,
        config={
            "submission_structure": "zip_collection",
            "default_euit_kbtu_ft2_yr": 41,
            "compare_to_euit": True,
            "near_target_percent": 5,
            "require_complete_reporting_period": True,
            "require_form_c_ready": True,
            "require_washington_standard_id": True,
            "meter_gap_policy": "error",
        },
    )
    ebl_resource = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.EXPECTED_BUILDINGS_LIST,
        validator_resource_file=None,
        step_resource_file=SimpleUploadedFile(
            "expected-buildings.json",
            EBL_JSON,
            content_type="application/json",
        ),
        filename="expected-buildings.json",
        resource_type=PORTFOLIO_MANAGER_EBL_RESOURCE,
    )
    ensure_step_input_bindings(step)

    report_bytes = b"PK synthetic zip identity"
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        project=step.workflow.project,
        user=step.workflow.user,
        content="placeholder",
        file_type=SubmissionFileType.BINARY,
        original_filename="portfolio.zip",
        size_bytes=len(report_bytes),
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    step_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        input_values={"default_euit_kbtu_ft2_yr": BOUND_EUIT},
    )
    ExecutionAttemptFactory(step_run=step_run)
    report_identity = _file_identity(
        "file:///validibot/input/portfolio.zip",
        report_bytes,
    )
    ebl_identity = _file_identity(
        "file:///validibot/input/resources/expected-buildings.json",
        EBL_JSON,
    )

    envelope = build_input_envelope(
        run,
        callback_url="http://localhost/callback/",
        callback_id=None,
        execution_bundle_uri="file:///validibot/output",
        skip_callback=True,
        input_file_uris={"primary_file_uri": report_identity},
        resource_uri_overrides={str(ebl_resource.pk): ebl_identity},
    )

    assert isinstance(envelope, PortfolioManagerInputEnvelope)
    assert envelope.input_files[0].port_key == "portfolio_manager_report"
    assert envelope.input_files[0].name == "portfolio.zip"
    assert envelope.resource_files[0].port_key == "expected_buildings_list"
    assert envelope.resource_files[0].sha256 == ebl_identity.sha256
    assert float(envelope.inputs.default_euit_kbtu_ft2_yr) == BOUND_EUIT
    assert envelope.inputs.max_input_bytes == (PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES)
    assert envelope.inputs.require_complete_reporting_period is True
    assert envelope.inputs.require_form_c_ready is True
    assert envelope.inputs.require_washington_standard_id is True
    assert envelope.inputs.meter_gap_policy == "error"

    traces = {
        trace.input_contract_key: trace
        for trace in ResolvedInputTrace.objects.filter(step_run=step_run)
    }
    assert traces["portfolio_manager_report"].resolved is True
    assert traces["expected_buildings_list"].resolved is True


def test_preflight_enforces_the_author_selected_submission_shape(monkeypatch) -> None:
    """Single mode refuses a ZIP before container compute is allocated."""
    validator = PortfolioManagerValidator()
    step = SimpleNamespace(config={"submission_structure": "single_report"})
    submission = SimpleNamespace()
    resolved = SimpleNamespace(
        name="portfolio.zip",
        identity=SimpleNamespace(size_bytes=100),
    )
    monkeypatch.setattr(
        validator,
        "resolve_file_input",
        lambda *args, **kwargs: resolved,
    )

    with pytest.raises(ValidationError, match="Single-report mode"):
        validator.preprocess_submission(step=step, submission=submission)


def test_single_property_outputs_are_available_to_cel() -> None:
    """Measured WNEUI and resolved EUIt are projected onto the scalar catalog."""
    outputs = PortfolioManagerOutputs(
        submission_structure="single_report",
        file_count=1,
        valid_file_count=1,
        invalid_file_count=0,
        property_count=1,
        reporting_cycle_count=1,
        reporting_cycles_match=True,
        property_results=[
            PortfolioManagerPropertyResult(
                member_name="report.xlsx",
                carrier="xlsx",
                property_id="123",
                weather_normalized_site_eui_kbtu_ft2_yr="39.5",
                resolved_euit_kbtu_ft2_yr="40",
                resolved_euit_source="default",
                meets_euit=True,
            )
        ],
    )

    values = PortfolioManagerValidator().extract_output_values(
        SimpleNamespace(outputs=outputs)
    )

    assert values["weather_normalized_site_eui_kbtu_ft2_yr"] == MEASURED_WNEUI
    assert values["resolved_euit_kbtu_ft2_yr"] == RESOLVED_EUIT
    assert values["meets_euit"] is True
