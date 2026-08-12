"""
End-to-end tests for EnergyPlus template pipeline via ``EnergyPlusValidator``.

These tests exercise the full Django-side validation lifecycle for
parameterized EnergyPlus templates:

1. Template detection from ``WorkflowStepResource`` (role=MODEL_TEMPLATE)
2. JSON parameter parsing and validation (``_parse_submission_params``)
3. Variable merging with author defaults (``merge_and_validate_template_parameters``)
4. Template substitution (``substitute_template_parameters``)
5. In-memory submission content override (``submission.content``)
6. Envelope building with correct file metadata (name, mime_type)
7. Preprocessing metadata propagation (``template_parameters_used``)

The execution backend is mocked because actual EnergyPlus simulation
requires Docker + the validator container image, which is not available
in the test environment.  The mock backend captures the
``ExecutionRequest`` so we can verify preprocessing produced the correct
resolved IDF and the envelope has the right metadata.

For direct-mode (non-template) submissions, these tests verify that
preprocessing is a no-op and the original content flows through
unchanged — including epJSON files, which require different envelope
metadata (``model.epjson`` / ``ENERGYPLUS_EPJSON``).

All tests use ``@pytest.mark.django_db`` and FactoryBoy factories for
model creation, following the project's Django testing standards.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from validibot.actions.protocols import RunContext
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.base.config import (
    get_validator_class as get_validator,
)
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Demo template from tests/assets/idf/window_glazing_template.idf
# ---------------------------------------------------------------------------

_DEMO_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "assets"
    / "idf"
    / "window_glazing_template.idf"
)

# Minimal template for faster tests that don't need the full shoebox model
_MINIMAL_TEMPLATE = """\
Version,
    24.2;  !- Version Identifier

WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $SHGC,                   !- Solar Heat Gain Coefficient
    $VISIBLE_TRANSMITTANCE;  !- Visible Transmittance
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_template_workflow(
    *,
    validator: Validator,
    template_content: str | None = None,
    step_config: dict | None = None,
    template_variables: list[dict] | None = None,
):
    """Create a complete workflow graph for template-mode E2E testing.

    Returns (org, workflow, step, validator, validation_run, step_run) — all
    the objects needed to call ``EnergyPlusValidator.validate()``.

    Uses FactoryBoy throughout.  The step gets:
    - A MODEL_TEMPLATE resource with the template IDF content
    - A WEATHER_FILE resource with dummy weather data (the mock backend
      never runs EnergyPlus, so content doesn't matter)
    - StepIODefinition + StepInputBinding rows for template variables
    """
    from validibot.validations.services.template_step_io import (
        sync_step_template_io_definitions,
    )

    content = template_content or _MINIMAL_TEMPLATE
    tpl_vars = template_variables or [
        {"name": "U_FACTOR", "variable_type": "number"},
        {"name": "SHGC", "variable_type": "number"},
        {"name": "VISIBLE_TRANSMITTANCE", "variable_type": "number"},
    ]

    org = OrganizationFactory()
    workflow = WorkflowFactory(org=org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config=step_config or {"case_sensitive": True},
    )
    sync_step_template_io_definitions(step, tpl_vars)

    # Attach the template as a step-owned resource
    WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        validator_resource_file=None,
        step_resource_file=SimpleUploadedFile(
            "template.idf",
            content.encode("utf-8"),
            content_type="text/plain",
        ),
        filename="template.idf",
        resource_type="energyplus_model_template",
    )

    # Attach a dummy weather file (required by envelope builder validation)
    WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=ValidatorResourceFileFactory(validator=validator),
    )
    ensure_step_input_bindings(step)

    # Create with a dummy submission first (required by factory's LazyAttributes)
    # We'll replace it with the real submission in each test.
    dummy_submission = SubmissionFactory(workflow=workflow, org=org, project=None)
    validation_run = ValidationRunFactory(
        submission=dummy_submission,
        workflow=workflow,
        org=org,
        project=None,
    )
    step_run = ValidationStepRunFactory(
        validation_run=validation_run,
        workflow_step=step,
    )

    return org, workflow, step, validator, validation_run, step_run


def _make_direct_workflow(*, validator: Validator):
    """Create a workflow graph for direct-mode (no template) E2E testing.

    The step has a WEATHER_FILE but no MODEL_TEMPLATE resource, so
    preprocessing is a no-op.
    """
    org = OrganizationFactory()
    workflow = WorkflowFactory(org=org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config={},
    )

    # Weather file only — no template
    WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=ValidatorResourceFileFactory(validator=validator),
    )
    ensure_step_input_bindings(step)

    dummy_submission = SubmissionFactory(workflow=workflow, org=org, project=None)
    validation_run = ValidationRunFactory(
        submission=dummy_submission,
        workflow=workflow,
        org=org,
        project=None,
    )
    step_run = ValidationStepRunFactory(
        validation_run=validation_run,
        workflow_step=step,
    )

    return org, workflow, step, validator, validation_run, step_run


@pytest.fixture
def energyplus_system_validator() -> Validator:
    """Return the installed EnergyPlus catalog row with its declared ports."""
    call_command("sync_validators", stdout=StringIO(), stderr=StringIO())
    config = get_config(ValidationType.ENERGYPLUS)
    return Validator.objects.get(slug=config.slug, version=config.version)


def _make_mock_backend(*, capture_request: list | None = None):
    """Create a mock execution backend that returns a successful response.

    If ``capture_request`` is provided (a list), the mock appends the
    ``ExecutionRequest`` to it so the test can inspect what was dispatched.
    """
    mock_backend = MagicMock()
    mock_backend.is_available.return_value = True
    mock_backend.is_async = False

    def fake_execute(request):
        if capture_request is not None:
            capture_request.append(request)
        return ExecutionResponse(
            execution_id="mock-exec-001",
            is_complete=True,
            output_envelope=None,
            error_message="Mock backend: no real container",
        )

    mock_backend.execute.side_effect = fake_execute
    return mock_backend


def _call_validate(
    validator_model,
    submission,
    step,
    validation_run,
    step_run,
):
    """Call ``EnergyPlusValidator.validate()`` with proper run context.

    Returns the ``ValidationResult``.
    """
    if not submission.original_filename:
        submission.original_filename = "parameters.json"
        submission.save(update_fields=["original_filename"])
    validator_cls = get_validator(ValidationType.ENERGYPLUS)
    validator_instance = validator_cls()
    run_context = RunContext(
        validation_run=validation_run,
        step=step,
    )
    return validator_instance.validate(
        validator=validator_model,
        submission=submission,
        ruleset=None,
        run_context=run_context,
    )


# ===========================================================================
# Template mode — happy path
# ===========================================================================
# These tests verify the full pipeline from JSON parameter submission
# through template resolution to backend dispatch.  The backend is mocked
# so we can inspect the preprocessed submission content.
# ===========================================================================


class TestEnergyPlusTemplateE2E:
    """End-to-end tests for the EnergyPlus parameterized template pipeline."""

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_template_happy_path_resolves_and_dispatches(
        self,
        mock_get_backend,
        caplog,
        energyplus_system_validator,
    ):
        """Submit valid parameters to a template step and verify:

        1. Preprocessing resolves the template — submission.content now
           contains the resolved IDF (not the original JSON)
        2. All ``$VARIABLE`` placeholders are replaced with actual values
        3. ``submission.original_filename`` is set to ``resolved_model.idf``
        4. The backend receives the execution request
        5. Result stats include ``template_parameters_used``
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        org, workflow, step, validator, run, step_run = _make_template_workflow(
            validator=energyplus_system_validator,
        )
        params = json.dumps(
            {
                "U_FACTOR": "2.5",
                "SHGC": "0.4",
                "VISIBLE_TRANSMITTANCE": "0.38",
            }
        )
        submission = SubmissionFactory(
            content=params,
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        result = _call_validate(validator, submission, step, run, step_run)

        # Backend was called (preprocessing didn't error out)
        assert len(captured) == 1
        dispatched = captured[0]
        assert dispatched.submission is submission

        # Submission content was replaced with resolved IDF
        resolved = submission.get_content()
        assert "2.5" in resolved
        assert "0.4" in resolved
        assert "0.38" in resolved
        assert "$U_FACTOR" not in resolved
        assert "$SHGC" not in resolved
        assert "$VISIBLE_TRANSMITTANCE" not in resolved

        # Filename was updated for IDF
        assert submission.original_filename == "resolved_model.idf"

        # Preprocessing metadata is in result stats
        assert "template_parameters_used" in result.stats
        assert result.stats["template_parameters_used"]["U_FACTOR"] == "2.5"
        assert result.stats["template_parameters_used"]["SHGC"] == "0.4"

        # Template bindings were resolved before the JSON submission was
        # replaced by the concrete IDF. They must remain the canonical i.*
        # values without a second, misleading lookup against IDF text.
        step_run.refresh_from_db()
        assert step_run.input_values["u_factor"] == "2.5"
        assert step_run.input_values["shgc"] == "0.4"
        assert step_run.input_values["visible_transmittance"] == "0.38"
        assert "could not be resolved" not in caplog.text

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_template_with_demo_idf(
        self,
        mock_get_backend,
        energyplus_system_validator,
    ):
        """Verify the full-size demo template (window glazing shoebox model)
        resolves correctly through the pipeline.

        This uses the ``window_glazing_template.idf`` asset — the
        canonical demo template for window-glazing simulations.
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        demo_content = _DEMO_TEMPLATE_PATH.read_text()
        org, workflow, step, validator, run, step_run = _make_template_workflow(
            validator=energyplus_system_validator,
            template_content=demo_content,
        )
        params = json.dumps(
            {
                "U_FACTOR": "1.70",
                "SHGC": "0.25",
                "VISIBLE_TRANSMITTANCE": "0.42",
            }
        )
        submission = SubmissionFactory(
            content=params,
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        result = _call_validate(validator, submission, step, run, step_run)

        # Backend was called
        assert len(captured) == 1

        # Resolved IDF contains substituted values
        resolved = submission.get_content()
        assert "1.70" in resolved
        assert "0.25" in resolved
        assert "0.42" in resolved

        # No unresolved placeholders remain
        assert "$U_FACTOR" not in resolved
        assert "$SHGC" not in resolved
        assert "$VISIBLE_TRANSMITTANCE" not in resolved

        # Metadata preserved
        assert result.stats["template_parameters_used"]["U_FACTOR"] == "1.70"

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_template_defaults_fill_missing_params(
        self,
        mock_get_backend,
        energyplus_system_validator,
    ):
        """When a variable has a default and the submitter omits it, the
        default value should appear in the resolved IDF.

        This tests the integration between preprocessing and
        ``merge_and_validate_template_parameters()`` — the merge step
        fills gaps using author-defined defaults from the step config.
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        org, workflow, step, validator, run, step_run = _make_template_workflow(
            validator=energyplus_system_validator,
            template_variables=[
                {"name": "U_FACTOR", "variable_type": "number"},
                {"name": "SHGC", "variable_type": "number"},
                {
                    "name": "VISIBLE_TRANSMITTANCE",
                    "variable_type": "number",
                    "default": "0.30",
                },
            ],
        )
        # Only provide U_FACTOR and SHGC — VT has a default
        params = json.dumps({"U_FACTOR": "2.0", "SHGC": "0.25"})
        submission = SubmissionFactory(
            content=params,
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        result = _call_validate(validator, submission, step, run, step_run)

        assert len(captured) == 1
        resolved = submission.get_content()
        assert "0.30" in resolved  # Default was used
        assert (
            result.stats["template_parameters_used"]["VISIBLE_TRANSMITTANCE"] == "0.30"
        )


# ===========================================================================
# Template mode — validation errors
# ===========================================================================
# Preprocessing validation errors should be caught by the validator and
# returned as ValidationResult(passed=False) with clear error messages.
# The backend should NOT be called when preprocessing fails.
# ===========================================================================


class TestEnergyPlusTemplateValidation:
    """Template preprocessing validation errors surface as failed results."""

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_missing_required_param_returns_failure(
        self,
        mock_get_backend,
        energyplus_system_validator,
    ):
        """When a required parameter is missing (no default), validate()
        returns a failure result naming the missing variable.  The backend
        is never called — the error is caught during preprocessing.
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        org, workflow, step, validator, run, step_run = _make_template_workflow(
            validator=energyplus_system_validator,
        )
        # Missing VISIBLE_TRANSMITTANCE — all three are required (no defaults)
        params = json.dumps({"U_FACTOR": "2.0", "SHGC": "0.25"})
        submission = SubmissionFactory(
            content=params,
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        result = _call_validate(validator, submission, step, run, step_run)

        assert result.passed is False
        assert len(captured) == 0  # Backend never called
        error_text = " ".join(i.message for i in result.issues)
        assert "VISIBLE_TRANSMITTANCE" in error_text

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_invalid_json_returns_failure(
        self,
        mock_get_backend,
        energyplus_system_validator,
    ):
        """Non-JSON submission content to a template step should return
        a clear error about invalid JSON.  This catches the common case
        where someone accidentally submits a raw IDF to a template workflow.
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        org, workflow, step, validator, run, step_run = _make_template_workflow(
            validator=energyplus_system_validator,
        )
        submission = SubmissionFactory(
            content="This is not JSON!",
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        result = _call_validate(validator, submission, step, run, step_run)

        assert result.passed is False
        assert len(captured) == 0
        error_text = " ".join(i.message for i in result.issues)
        assert "JSON" in error_text

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_value_out_of_range_returns_failure(
        self,
        mock_get_backend,
        energyplus_system_validator,
    ):
        """When a parameter value is outside the author-defined range,
        merge validation catches it and returns a failure result.
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        org, workflow, step, validator, run, step_run = _make_template_workflow(
            validator=energyplus_system_validator,
            template_variables=[
                {
                    "name": "U_FACTOR",
                    "variable_type": "number",
                    "min_value": 0.1,
                    "max_value": 7.0,
                },
                {"name": "SHGC", "variable_type": "number"},
                {"name": "VISIBLE_TRANSMITTANCE", "variable_type": "number"},
            ],
        )
        params = json.dumps(
            {
                "U_FACTOR": "-1.0",
                "SHGC": "0.25",
                "VISIBLE_TRANSMITTANCE": "0.3",
            }
        )
        submission = SubmissionFactory(
            content=params,
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        result = _call_validate(validator, submission, step, run, step_run)

        assert result.passed is False
        assert len(captured) == 0
        error_text = " ".join(i.message for i in result.issues)
        assert "U_FACTOR" in error_text

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_typo_in_parameter_name(
        self,
        mock_get_backend,
        energyplus_system_validator,
    ):
        """A misspelled parameter name should produce an error about the
        missing required parameter.  The merge step warns about unrecognized
        parameters and errors on missing required ones.
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        org, workflow, step, validator, run, step_run = _make_template_workflow(
            validator=energyplus_system_validator,
        )
        params = json.dumps(
            {
                "U_FACTR": "2.0",  # Typo — should be U_FACTOR
                "SHGC": "0.25",
                "VISIBLE_TRANSMITTANCE": "0.3",
            }
        )
        submission = SubmissionFactory(
            content=params,
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        result = _call_validate(validator, submission, step, run, step_run)

        assert result.passed is False
        assert len(captured) == 0
        error_text = " ".join(i.message for i in result.issues)
        # Should mention the missing required param
        assert "U_FACTOR" in error_text


# ===========================================================================
# Direct mode (no template)
# ===========================================================================
# When a step has no MODEL_TEMPLATE resource, preprocessing is a no-op.
# The submission flows through unchanged to the backend.
# ===========================================================================


class TestEnergyPlusDirectModeE2E:
    """Direct-mode submissions bypass template preprocessing entirely."""

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_direct_mode_bypasses_preprocessing(
        self,
        mock_get_backend,
        energyplus_system_validator,
    ):
        """A step without a MODEL_TEMPLATE resource should pass the
        original submission content unchanged to the backend.

        Direct IDF validation is a current first-class mode alongside
        parameterized templates; it must pass submissions through unchanged.
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        org, workflow, step, validator, run, step_run = _make_direct_workflow(
            validator=energyplus_system_validator,
        )
        original_content = "Version,24.2;"
        submission = SubmissionFactory(
            content=original_content,
            original_filename="my_model.idf",
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        result = _call_validate(validator, submission, step, run, step_run)

        # Backend was called
        assert len(captured) == 1

        # Content unchanged
        assert submission.get_content() == original_content
        assert submission.original_filename == "my_model.idf"

        # No template metadata in stats
        assert "template_parameters_used" not in (result.stats or {})

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_epjson_direct_mode_preserves_filename(
        self,
        mock_get_backend,
        energyplus_system_validator,
    ):
        """Direct epJSON submissions should preserve their original
        filename so the envelope builder can derive the correct
        ``name`` and ``mime_type`` for the InputFileItem.

        This is critical for the runner, which uses file_item.name as
        the local filename — an epJSON file saved as ``model.idf``
        would cause EnergyPlus to try to parse JSON as IDF text.
        """
        captured = []
        mock_get_backend.return_value = _make_mock_backend(capture_request=captured)

        org, workflow, step, validator, run, step_run = _make_direct_workflow(
            validator=energyplus_system_validator,
        )
        submission = SubmissionFactory(
            content='{"Version": {"Version Identifier": "24.2"}}',
            original_filename="model.epjson",
            workflow=workflow,
            org=org,
            project=None,
        )
        run.submission = submission
        run.save(update_fields=["submission"])

        _call_validate(validator, submission, step, run, step_run)

        assert len(captured) == 1
        # Filename preserved — preprocessing was a no-op
        assert submission.original_filename == "model.epjson"
