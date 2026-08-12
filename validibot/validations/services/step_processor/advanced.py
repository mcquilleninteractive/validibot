"""
Advanced validation processor for container-based validators.

Handles EnergyPlus and FMU validators that run in Docker containers
and may complete synchronously or asynchronously.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidatorExecutionProfile
from validibot.validations.models import ValidationFinding
from validibot.validations.services.step_processor.base import ValidationStepProcessor
from validibot.validations.services.step_processor.result import StepProcessingResult
from validibot.validations.validators.base import ValidationIssue
from validibot.validations.validators.base import ValidationResult

logger = logging.getLogger(__name__)


class AdvancedValidationProcessor(ValidationStepProcessor):
    """
    Processor for advanced validators (EnergyPlus, FMU).

    These validators:
    - Run in Docker containers
    - May complete synchronously (Docker Compose) or asynchronously (GCP)
    - Have two assertion stages (input and output)

    ## Execution Modes

    **Synchronous (Docker Compose, local dev, test):**
    - Container runs and blocks until complete
    - `execute()` calls `validator.post_execute_validate()` directly
    - Returns complete result immediately

    **Asynchronous (GCP Cloud Run, future AWS):**
    - Container job is launched and returns immediately
    - `execute()` returns `passed=None` (pending)
    - Later, callback arrives and `complete_from_callback()` is called

    ## Assertion Stages

    **Input stage:** Evaluated in `validator.validate()` BEFORE container launch.
    **Output stage:** Evaluated in `validator.post_execute_validate()` AFTER container
    completes.

    ## Status Semantics

    - `ValidationStatus.SUCCESS` from a **shipped** validator (`is_system=True`)
      is treated as a pass even if the container reported ERROR messages. This
      is deliberate: EnergyPlus, for example, legitimately exits 0 (SUCCESS)
      while emitting `** Severe **` ERROR-severity lines it considers non-fatal.
      Shipped validators are trusted to use their envelope status as the
      authoritative pass/fail signal; we surface a warning when SUCCESS coincides
      with ERROR findings, but keep the step PASSED.
    - `ValidationStatus.SUCCESS` from a **custom** validator (`is_system=False`,
      i.e. a user-added container) with ERROR findings **fails** the step. Custom
      containers are not trusted to honour the status contract — a buggy or
      naive one may set SUCCESS ("my container ran") while emitting ERROR
      findings ("the data is invalid"). For a validation + attestation product
      we must not PASS the step, and let a signed credential be issued, over data
      the validator itself flagged as an ERROR.
    - Output-stage assertion failures always fail the step, regardless of
      container status or validator trust.
    """

    def execute(self) -> StepProcessingResult:
        """
        Execute the validation step.

        Calls the validator's validate() method to launch the container.
        For sync backends, also calls post_execute_validate() to evaluate
        output-stage assertions. For async backends, returns pending.
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

        # Input contracts are evaluated before an execution attempt or provider
        # dispatch exists. A concrete mismatch is a normal validation failure,
        # not an infrastructure launch failure.
        from validibot.validations.services.input_contracts import InputResolutionError
        from validibot.validations.services.input_contracts import (
            validate_runtime_input_contracts,
        )

        try:
            validate_runtime_input_contracts(
                run=self.validation_run,
                step=self.workflow_step,
            )
        except InputResolutionError as exc:
            return self._handle_error(exc)

        # Create durable identity before any provider work. A redelivery that
        # observes an already-claimed attempt must not call the provider again;
        # reconciliation owns that case.
        from validibot.core.deployment import (
            supports_author_selectable_validator_execution_profiles,
        )
        from validibot.core.deployment import (
            uses_managed_validator_execution_deployments,
        )
        from validibot.validations.services.execution import get_execution_backend
        from validibot.validations.services.execution.deployments import (
            effective_execution_budget_seconds,
        )
        from validibot.validations.services.execution.deployments import (
            effective_execution_profile,
        )
        from validibot.validations.services.execution_attempts import (
            get_or_create_execution_attempt,
        )

        managed = uses_managed_validator_execution_deployments()
        default_backend = None if managed else get_execution_backend()
        execution_profile = (
            effective_execution_profile(step=self.workflow_step)
            if supports_author_selectable_validator_execution_profiles()
            else ValidatorExecutionProfile.FAST_RESPONSE
        )
        execution_budget_seconds = effective_execution_budget_seconds(
            step=self.workflow_step
        )
        attempt, attempt_created = get_or_create_execution_attempt(
            self.step_run,
            runner_type=(default_backend.backend_name if default_backend else None),
            validator=self.validator,
            managed=managed,
            effective_budget_seconds=execution_budget_seconds,
            execution_profile=execution_profile,
        )
        run_context.execution_attempt = attempt
        run_context.execution_deployment = attempt.deployment
        if not attempt_created and attempt.state != ExecutionAttemptState.PENDING:
            logger.info(
                "Execution attempt %s is already %s; skipping provider relaunch",
                attempt.pk,
                attempt.state,
            )
            existing_counts = self._get_existing_finding_counts()
            return StepProcessingResult(
                passed=None,
                step_run=self.step_run,
                severity_counts=existing_counts,
                total_findings=sum(existing_counts.values()),
                assertion_failures=0,
                assertion_total=0,
            )

        try:
            # Call validator.validate() - this:
            # - Evaluates input-stage assertions
            # - Launches the container
            # - For sync backends: blocks and returns with output_envelope
            # - For async backends: returns immediately with passed=None
            result = validator_instance.validate(
                validator=self.validator,
                submission=self.validation_run.submission,
                ruleset=self.ruleset,
                run_context=run_context,
            )
        except Exception as e:
            logger.exception(
                "Error executing advanced validation step %s",
                self.step_run.id,
            )
            attempt.refresh_from_db(fields=["state"])
            if attempt.state == ExecutionAttemptState.PENDING:
                from validibot.validations.services.execution_attempts import (
                    transition_execution_attempt,
                )

                transition_execution_attempt(
                    attempt.pk,
                    ExecutionAttemptState.FAILED,
                    last_error_code="validator_preparation_failed",
                    last_error=str(e),
                )
            return self._handle_error(e)

        # Persist input-stage findings (from assertions evaluated in validate())
        severity_counts, assertion_failures = self.persist_findings(result.issues)

        if result.passed is False:
            # A provider that accepted work moves the attempt out of PENDING
            # inside its backend. Therefore a definitive failure that leaves
            # PENDING occurred before launch and is safe to mark FAILED.
            attempt.refresh_from_db(fields=["state"])
            if attempt.state == ExecutionAttemptState.PENDING:
                from validibot.validations.services.execution_attempts import (
                    transition_execution_attempt,
                )

                target = (
                    ExecutionAttemptState.CANCELED
                    if (result.stats or {}).get("dispatch_skipped")
                    == "input_stage_assertion_failed"
                    else ExecutionAttemptState.FAILED
                )
                transition_execution_attempt(attempt.pk, target)

        # Handle sync vs async completion
        if result.passed is None:
            # Async execution - container launched, waiting for callback
            self._record_pending_state(result)
            return StepProcessingResult(
                passed=None,
                step_run=self.step_run,
                severity_counts=severity_counts,
                total_findings=sum(severity_counts.values()),
                assertion_failures=result.assertion_stats.failures,
                assertion_total=result.assertion_stats.total,
            )
        # Sync execution - container completed, call post_execute_validate()
        # We already persisted input-stage findings above, so we must APPEND
        # output-stage findings to preserve them.
        #
        # Persist preprocessing metadata (e.g., template_parameters_used,
        # template_warnings) into step_run.output NOW, before
        # _complete_with_envelope() rebuilds the output from the envelope.
        # finalize_step() merges new stats into existing output, so this
        # metadata will survive the envelope serialization.
        # (On the async path, _record_pending_state() does the same thing.)
        if result.stats:
            self._persist_initial_stats(result.stats)

        if result.output_envelope is None:
            # Per ADR-2026-05-22 + May 2026 review's P2 finding: a
            # validator result with stats["dispatch_skipped"] ==
            # "input_stage_assertion_failed" means the input-stage
            # gate deliberately skipped container dispatch because an
            # ERROR-severity input-stage assertion failed. This is NOT
            # an execution failure — the validator worked correctly
            # and the assertion correctly fired. Without this branch,
            # _handle_execution_failure() would surface "Validation
            # execution failed" and report assertion_failures=0 /
            # assertion_total=0, contradicting the validator result's
            # real assertion_stats and confusing the author.
            if (result.stats or {}).get("dispatch_skipped") == (
                "input_stage_assertion_failed"
            ):
                return self._handle_input_stage_gated(
                    severity_counts=severity_counts,
                    result=result,
                )
            # If the validator already reported a definitive failure (e.g.,
            # container image not found, execution error), finalize with those
            # findings instead of adding a generic "missing envelope" message.
            if result.passed is False and result.issues:
                return self._handle_execution_failure(severity_counts)
            return self._handle_missing_envelope()
        return self._complete_with_envelope(
            validator_instance,
            run_context,
            result.output_envelope,
            severity_counts,
            append_findings=True,  # Preserve input-stage findings
        )

    def _persist_initial_stats(self, stats: dict[str, Any]) -> None:
        """Store launch metadata before final-envelope artifact registration."""
        self.step_run.output = {
            **(self.step_run.output or {}),
            **stats,
        }
        update_fields = ["output"]
        backend_digest = stats.get("validator_backend_image_digest")
        if backend_digest:
            # Promote the runner identity before artifact registration so
            # every artifact records the backend that produced it. Final
            # envelope serialization does not itself carry this value.
            self.step_run.validator_backend_image_digest = str(backend_digest)
            update_fields.append("validator_backend_image_digest")
        self.step_run.save(update_fields=update_fields)

    def complete_from_callback(self, output_envelope: Any) -> StepProcessingResult:
        """
        Complete the step after receiving async callback.

        Called by ValidationCallbackService after downloading the output
        envelope from cloud storage.

        IMPORTANT: This APPENDS findings to existing ones (from input-stage
        assertions). It does NOT delete pre-existing findings.
        """
        validator_instance = self._get_validator()
        run_context = self._build_run_context()

        # Get existing severity counts from input-stage findings
        existing_counts = self._get_existing_finding_counts()

        return self._complete_with_envelope(
            validator_instance,
            run_context,
            output_envelope,
            existing_counts,
            append_findings=True,
        )

    def _complete_with_envelope(
        self,
        validator_instance,
        run_context,
        output_envelope: Any,
        existing_severity_counts: Counter,
        *,
        append_findings: bool = False,
    ) -> StepProcessingResult:
        """
        Complete the step using the output envelope.

        Called by both sync execution and async callback paths.
        """
        # Call validator.post_execute_validate() - this:
        # 1. Extracts step output values from the envelope
        # 2. Evaluates output-stage assertions using those values
        # 3. Extracts issues from envelope messages
        # 4. Returns ValidationResult with output_values populated
        post_result = validator_instance.post_execute_validate(
            output_envelope,
            run_context,
        )

        from validibot_shared.validations.envelopes import ValidationStatus

        # ── Container-reported ERROR findings on a SUCCESS envelope ──
        #
        # ``container_error_issues`` are ERROR-severity findings the container
        # emitted itself. The ``assertion_id is None`` filter excludes
        # output-stage assertion findings, which are handled separately by the
        # ``has_assertion_errors`` branch below — do not broaden it.
        #
        # A validator can report status SUCCESS while still emitting such
        # findings. How we treat that depends on whether the validator is
        # trusted (see the class docstring's Status Semantics):
        #
        # * SHIPPED (``is_system=True``): trusted. EnergyPlus exits 0 (SUCCESS)
        #   while writing ``** Severe **`` ERROR lines it considers non-fatal,
        #   so we keep the step PASSED and surface a WARNING.
        # * CUSTOM (``is_system=False``): untrusted. We must not PASS — and let
        #   a credential be issued — over data the validator flagged as ERROR,
        #   so the findings win and the step FAILS (see status block below).
        container_error_issues = [
            issue
            for issue in post_result.issues
            if issue.severity == Severity.ERROR and issue.assertion_id is None
        ]
        success_with_container_errors = (
            output_envelope.status == ValidationStatus.SUCCESS
            and bool(container_error_issues)
        )
        custom_container_failure = (
            success_with_container_errors and not self.validator.is_system
        )
        if success_with_container_errors:
            logger.warning(
                "Advanced validator reported SUCCESS with ERROR findings: "
                "step_run_id=%s error_count=%s is_system=%s",
                self.step_run.id,
                len(container_error_issues),
                self.validator.is_system,
            )
            if custom_container_failure:
                note_msg = (
                    "The custom validator reported success but emitted "
                    "error-level findings. The step is failed because custom "
                    "validators are not trusted to override their own errors."
                )
                note_severity = Severity.ERROR
                note_code = "advanced_validation_custom_success_with_errors"
            else:
                note_msg = (
                    "Note: the advanced validation indicated it passed, "
                    "but there were errors reported."
                )
                note_severity = Severity.WARNING
                note_code = "advanced_validation_success_with_errors"
            post_result.issues.append(
                ValidationIssue(
                    path="",
                    message=note_msg,
                    severity=note_severity,
                    code=note_code,
                )
            )

        # Persist output-stage findings (APPEND for callbacks)
        output_counts, output_assertion_failures = self.persist_findings(
            post_result.issues,
            append=append_findings,
        )

        # Merge severity counts
        severity_counts = existing_severity_counts + output_counts

        # Store canonical values for downstream steps.
        self.store_output_values(post_result.output_values or {})

        # Calculate total assertion counts (input + output stages)
        input_stats = self._get_stored_assertion_stats()
        assertion_total = input_stats.total + post_result.assertion_stats.total
        assertion_failures = input_stats.failures + post_result.assertion_stats.failures

        # Store final assertion counts
        self.store_assertion_counts(assertion_failures, assertion_total)

        # Determine final status.
        #
        # ``custom_container_failure`` (computed above) forces FAILED when a
        # NON-system container reports SUCCESS yet emitted ERROR findings.
        # Output-stage assertion failures always fail the step regardless of
        # validator trust.
        status = self._map_envelope_status(output_envelope.status)
        has_assertion_errors = post_result.assertion_stats.failures > 0
        if has_assertion_errors or custom_container_failure:
            status = StepStatus.FAILED
        error = self._extract_error(output_envelope)
        if custom_container_failure and not error:
            # A SUCCESS envelope yields no envelope-level error string, so give
            # the failed step a meaningful reason for the run record.
            error = note_msg

        # Include full envelope in step output (JSON-safe serialization)
        stats = self._serialize_envelope(output_envelope)
        from validibot.validations.services.artifacts import register_output_artifacts

        artifact_refs = register_output_artifacts(
            step_run=self.step_run,
            output_envelope=output_envelope,
        )
        if artifact_refs:
            stats["artifact_refs"] = artifact_refs
        if success_with_container_errors:
            warnings = stats.get("warnings", []) if isinstance(stats, dict) else []
            warnings.append(note_msg)
            stats["warnings"] = warnings
        self.finalize_step(status, stats, error)

        return StepProcessingResult(
            passed=(status == StepStatus.PASSED),
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=assertion_failures,
            assertion_total=assertion_total,
        )

    def _record_pending_state(self, result: ValidationResult) -> None:
        """Record execution metadata and input-stage assertion stats for async steps."""
        self.step_run.output = {
            "assertion_total": result.assertion_stats.total,
            "assertion_failures": result.assertion_stats.failures,
            **(result.stats or {}),
        }
        self.step_run.status = StepStatus.RUNNING
        self.step_run.save(update_fields=["output", "status"])

    def _get_existing_finding_counts(self) -> Counter:
        """Get severity counts from existing findings (for callback path)."""
        counts: Counter = Counter()
        findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
        )
        for finding in findings:
            counts[finding.severity] += 1
        return counts

    def _map_envelope_status(self, envelope_status) -> StepStatus:
        """Map ValidationStatus from envelope to StepStatus."""
        from validibot_shared.validations.envelopes import ValidationStatus

        mapping = {
            ValidationStatus.SUCCESS: StepStatus.PASSED,
            ValidationStatus.FAILED_VALIDATION: StepStatus.FAILED,
            ValidationStatus.FAILED_RUNTIME: StepStatus.FAILED,
            ValidationStatus.CANCELLED: StepStatus.SKIPPED,
        }
        return mapping.get(envelope_status, StepStatus.FAILED)

    def _extract_error(self, output_envelope) -> str:
        """Extract error message from envelope."""
        from validibot_shared.validations.envelopes import ValidationStatus

        if output_envelope.status == ValidationStatus.SUCCESS:
            return ""

        error_messages = [
            msg.text
            for msg in output_envelope.messages
            if str(msg.severity).upper() == "ERROR"
        ]
        return "\n".join(error_messages)

    def _serialize_envelope(self, output_envelope) -> dict:
        """Serialize envelope to JSON-safe dict for step_run.output."""
        if hasattr(output_envelope, "model_dump"):
            return output_envelope.model_dump(mode="json")
        if isinstance(output_envelope, dict):
            return output_envelope
        return {}

    def _handle_error(self, error: Exception) -> StepProcessingResult:
        """Handle validation errors gracefully."""
        from validibot.validations.services.input_contracts import InputResolutionError

        if isinstance(error, InputResolutionError):
            issues = [
                ValidationIssue(
                    path=diagnostic.contract_key,
                    message=diagnostic.message,
                    severity=Severity.ERROR,
                    code=diagnostic.code,
                    meta=diagnostic.as_meta(),
                )
                for diagnostic in error.diagnostics
            ]
        else:
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
            total_findings=len(issues),
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

    def _handle_execution_failure(
        self,
        severity_counts: Counter,
    ) -> StepProcessingResult:
        """Handle case where the validator returned a definitive error.

        Called when the execution backend reports a concrete failure (e.g.,
        container image not found, container crash) — the error findings
        were already persisted by ``persist_findings()`` earlier. We just
        finalize the step without adding a redundant generic message.
        """
        error_msg = "Validation execution failed"
        self.finalize_step(StepStatus.FAILED, {}, error=error_msg)

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=0,
            assertion_total=0,
        )

    def _handle_missing_envelope(self) -> StepProcessingResult:
        """Handle case where sync backend didn't return envelope."""
        issues = [
            ValidationIssue(
                path="",
                message="Sync execution completed but no output envelope received. "
                "This indicates a backend configuration issue.",
                severity=Severity.ERROR,
            ),
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error="Missing output envelope")

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )

    def _handle_input_stage_gated(
        self,
        *,
        severity_counts: Counter,
        result: Any,
    ) -> StepProcessingResult:
        """Handle a result where input-stage assertions blocked dispatch.

        Per ADR-2026-05-22, an ERROR-severity input-stage assertion
        skips container dispatch. The validator returns a result with:
          - ``output_envelope is None`` (no container ran)
          - ``passed is False`` (assertions failed)
          - ``issues`` containing the assertion findings (already
            persisted by ``persist_findings()`` earlier in execute())
          - ``stats["dispatch_skipped"] == "input_stage_assertion_failed"``
          - ``assertion_stats`` populated with the real input-stage
            totals

        This is a legitimate authoring outcome — the assertions did
        what they were configured to do. Treat it as an assertion
        failure (StepStatus.FAILED with the real assertion counts),
        not as a generic execution failure.

        Per the May 2026 review's P2 finding.
        """
        # Use the canonical stats from the validator result, not
        # zeros. AdvancedValidator._evaluate_input_stage_and_persist()
        # populates result.assertion_stats with the real totals.
        assertion_failures = result.assertion_stats.failures
        assertion_total = result.assertion_stats.total

        # Persist the canonical counts on step_run.output so the run
        # summary picks them up. (The validator also wrote these via
        # _persist_input_stage_assertion_counts for async-survival,
        # but this call ensures the sync path's final state matches.)
        self.store_assertion_counts(
            assertion_failures=assertion_failures,
            assertion_total=assertion_total,
        )

        # Finalize as FAILED with a clear, accurate error message —
        # not "Validation execution failed" (which would imply a
        # platform problem).
        self.finalize_step(
            StepStatus.FAILED,
            {},
            error="Input-stage assertions failed; container dispatch skipped",
        )

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=assertion_failures,
            assertion_total=assertion_total,
        )
