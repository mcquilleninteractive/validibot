"""
Tests for step handlers and the dispatcher logic in execute_workflow_step.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from validibot.actions.handlers import ValidatorStepHandler
from validibot.actions.protocols import RunContext
from validibot.actions.protocols import StepResult
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationType
from validibot.validations.services.custom_validator_contracts import (
    sync_configured_io_contract,
)
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base import ValidationResult
from validibot.workflows.tests.factories import WorkflowStepFactory


class TestValidatorStepHandler:
    """Tests for ValidatorStepHandler."""

    def test_returns_error_when_step_has_no_validator(self):
        """Handler should return failed StepResult when step has no validator."""
        handler = ValidatorStepHandler()
        context = RunContext(
            validation_run=MagicMock(),
            step=MagicMock(validator=None),
            upstream_steps={},
        )

        result = handler.execute(context)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "no validator configured" in result.issues[0].message.lower()

    @pytest.mark.django_db
    def test_returns_error_for_unsupported_file_type(self):
        """Handler should return the typed port diagnostic for a PDF/JSON mismatch."""
        run = ValidationRunFactory(
            submission__file_type=SubmissionFileType.PDF,
            submission__original_filename="document.pdf",
            submission__content="%PDF-1.7",
        )
        validator = ValidatorFactory(validation_type=ValidationType.JSON_SCHEMA)
        sync_configured_io_contract(validator=validator)
        step = WorkflowStepFactory(workflow=run.workflow, validator=validator)
        ensure_step_input_bindings(step)

        handler = ValidatorStepHandler()
        context = RunContext(
            validation_run=run,
            step=step,
            upstream_steps={},
        )

        result = handler.execute(context)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "primary file is PDF" in result.issues[0].message
        assert result.issues[0].code == "input_file_type_incompatible"

    def test_returns_error_when_validator_not_found(self):
        """Handler should fail gracefully when validator class cannot be loaded."""
        validator = MagicMock()
        validator.validation_type = "nonexistent_type"
        validator.supports_file_type = MagicMock(return_value=True)

        run = MagicMock()
        run.submission = MagicMock(file_type="json")

        step = MagicMock(validator=validator)

        handler = ValidatorStepHandler()
        context = RunContext(
            validation_run=run,
            step=step,
            upstream_steps={},
        )

        result = handler.execute(context)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "failed to load" in result.issues[0].message.lower()


class TestExecuteWorkflowStepDispatcher:
    """Tests for the dispatcher logic in execute_workflow_step."""

    @pytest.mark.django_db
    def test_dispatches_to_validator_handler_when_step_has_validator(
        self,
        monkeypatch,
    ):
        """Dispatcher handlers must receive the canonical upstream context."""
        run = ValidationRunFactory()
        upstream_step = WorkflowStepFactory(workflow=run.workflow, order=10)
        step = WorkflowStepFactory(workflow=run.workflow, order=20)
        ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
            output_values={"site_eui": 80.0},
        )

        mock_result = StepResult(
            passed=True,
            issues=[],
            stats={"test": True},
            output_values={"action_receipt": "receipt-123"},
        )
        received_contexts = []

        def mock_execute(self, context):
            received_contexts.append(context)
            return mock_result

        monkeypatch.setattr(ValidatorStepHandler, "execute", mock_execute)

        service = ValidationRunService()
        result = service.execute_workflow_step(step=step, validation_run=run)

        assert isinstance(result, ValidationResult)
        assert result.passed is True
        assert result.stats.get("test") is True
        assert result.output_values == {"action_receipt": "receipt-123"}
        assert received_contexts[0].upstream_steps == {
            upstream_step.step_key: {
                "input": {},
                "output": {"site_eui": 80.0},
                "artifact": {},
            },
        }

    @pytest.mark.django_db
    def test_returns_failed_result_when_step_has_no_handler(self):
        """Should return failed result when step has no validator or action."""
        run = ValidationRunFactory()
        step = MagicMock(validator=None, action=None, name="orphan_step")

        service = ValidationRunService()
        result = service.execute_workflow_step(step=step, validation_run=run)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "no validator or action" in result.issues[0].message.lower()

    @pytest.mark.django_db
    def test_returns_failed_result_when_action_handler_not_registered(self):
        """Should return failed result when action type has no handler."""
        run = ValidationRunFactory()

        mock_action = MagicMock()
        mock_action.definition.type = "unregistered_action_type"

        step = MagicMock(validator=None, action=mock_action, name="action_step")

        service = ValidationRunService()
        result = service.execute_workflow_step(step=step, validation_run=run)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "no handler registered" in result.issues[0].message.lower()
