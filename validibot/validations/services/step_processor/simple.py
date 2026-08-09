"""
Simple validation processor for inline validators.

Handles JSON Schema, XML Schema, Basic, and AI validators that run
synchronously in the Django process.
"""

from __future__ import annotations

import logging

from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.services.step_processor.base import ValidationStepProcessor
from validibot.validations.services.step_processor.result import StepProcessingResult
from validibot.validations.validators.base import ValidationIssue

logger = logging.getLogger(__name__)


class SimpleValidationProcessor(ValidationStepProcessor):
    """
    Processor for simple validators (JSON Schema, XML Schema, Basic, AI).

    These validators:
    - Run inline in the Django process
    - Complete synchronously
    - Have a single assertion stage (input)

    The validator handles ALL validation logic including input-stage assertions.
    The processor just calls validator.validate() and persists results.
    """

    def execute(self) -> StepProcessingResult:
        """
        Execute the validation step.

        Calls the validator's validate() method (which handles validation logic
        and input-stage assertion evaluation), then persists the results.
        """
        try:
            validator_instance = self._get_validator()
        except KeyError as e:
            logger.exception(
                "Failed to load validator for validation step %s",
                self.step_run.id,
            )
            return self._handle_validator_not_found(e)

        run_context = self._build_run_context()
        from validibot.validations.services.resolved_files import resolve_file_inputs

        try:
            run_context.resolved_file_inputs = resolve_file_inputs(
                run=self.validation_run,
                step=self.workflow_step,
                step_run=self.step_run,
            )
        except ValueError as exc:
            logger.warning(
                "Could not resolve file inputs for simple validation step %s: %s",
                self.step_run.id,
                exc,
            )
            return self._handle_error(exc)

        try:
            # Call validator.validate() - this does EVERYTHING:
            # - Validation logic (schema checking, AI prompting, etc.)
            # - Input-stage assertion evaluation
            # - Returns combined issues with assertion outcomes
            result = validator_instance.validate(
                validator=self.validator,
                submission=self.validation_run.submission,
                ruleset=self.ruleset,
                run_context=run_context,
            )
        except Exception as e:
            logger.exception(
                "Error executing simple validation step %s",
                self.step_run.id,
            )
            return self._handle_error(e)

        self.store_input_values(run_context.step_input_contract_values)

        # Persist findings from validator result
        severity_counts, assertion_failures = self.persist_findings(result.issues)

        # Store assertion counts for run summary (using typed fields)
        self.store_assertion_counts(
            result.assertion_stats.failures,
            result.assertion_stats.total,
        )

        # Store step output values for downstream steps.
        stats = dict(result.stats or {})
        self.store_output_values(result.output_values or {})

        # Finalize the step
        status = StepStatus.PASSED if result.passed else StepStatus.FAILED
        self.finalize_step(status, stats)

        return StepProcessingResult(
            passed=result.passed,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=result.assertion_stats.failures,
            assertion_total=result.assertion_stats.total,
        )

    def _handle_error(self, error: Exception) -> StepProcessingResult:
        """Handle validation errors gracefully."""
        issues = [
            ValidationIssue(
                path="",
                message=str(error),
                severity=Severity.ERROR,
            ),
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error=str(error))

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )

    def _handle_validator_not_found(self, error: Exception) -> StepProcessingResult:
        """Handle missing/unregistered validator gracefully."""
        error_msg = (
            f"Failed to load validator for type "
            f"'{self.validator.validation_type}': {error}"
        )
        issues = [
            ValidationIssue(
                path="",
                message=error_msg,
                severity=Severity.ERROR,
                code="validator_not_found",
            ),
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error=error_msg)

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )
