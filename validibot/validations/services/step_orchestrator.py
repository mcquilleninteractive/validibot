"""
Step orchestrator — iterating, dispatching, and recording workflow steps.

This module contains the core execution loop for validation runs. It iterates
through workflow steps in order, dispatching each to the appropriate handler
(ValidatorStepHandler for validators, action handlers from the registry),
managing step lifecycle (start/finalize), recording results, and handling
async validators that return pending.

Responsibilities:

- Step iteration: Processing workflow steps sequentially, stopping on failure
  or when an async validator returns pending.
- Step lifecycle: Creating step runs (idempotent via get_or_create), finalizing
  them with status, duration, and diagnostics.
- Step dispatch: Routing to processor (validators) or handler (actions) based
  on step type.
- Result recording: Persisting findings, recording step outputs, and
  building metrics for the summary builder.
- Run state transitions: PENDING → RUNNING → SUCCEEDED/FAILED/CANCELED.
- Cross-step values: Collecting output values from prior steps for downstream
  assertions.

This was extracted from ValidationRunService to follow single-responsibility:
the ValidationRunService class handles lifecycle (launch/cancel), this module
handles execution.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from enum import auto
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from validibot.actions.constants import ActionFailureMode
from validibot.actions.constants import CredentialActionType
from validibot.tracking.services import TrackingEventService
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.deferred_credentials import (
    finalize_deferred_signed_credentials,
)
from validibot.validations.services.findings_persistence import normalize_issue
from validibot.validations.services.findings_persistence import persist_findings
from validibot.validations.services.models import ValidationRunTaskResult
from validibot.validations.services.output_hash import safe_stamp_output_hash
from validibot.validations.services.run_context import RunContextBuilder
from validibot.validations.services.step_processor.result import StepProcessingResult
from validibot.validations.services.summary_builder import build_run_summary_record
from validibot.validations.services.summary_builder import extract_assertion_total
from validibot.validations.validators.base import ValidationIssue
from validibot.validations.validators.base import ValidationResult

if TYPE_CHECKING:
    from uuid import UUID

    from validibot.users.models import User
    from validibot.workflows.models import Workflow
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)

GENERIC_EXECUTION_ERROR = _(
    "This validation run could not be completed. Please try again later.",
)

RUN_CANCELED_MESSAGE = _("Run canceled by user.")


class _StepRunDisposition(Enum):
    """Decision produced while admitting one logical step for execution."""

    EXECUTE = auto()
    TERMINAL = auto()
    IN_PROGRESS = auto()


@dataclass(frozen=True, slots=True)
class _StepRunAdmission:
    """Authoritative step row and the only safe action for this delivery."""

    step_run: ValidationStepRun
    disposition: _StepRunDisposition


class StepOrchestrator:
    """
    Iterates through workflow steps, dispatching each to the appropriate handler.

    This is the worker-side entry point for validation run execution. It handles:

    - Idempotent state transitions (PENDING → RUNNING)
    - Sequential step dispatch with failure/async stop conditions
    - Step lifecycle management (create, finalize, record results)
    - Cross-step value propagation for downstream assertions
    - Run finalization (SUCCEEDED/FAILED/CANCELED) with summary building

    See Also:
        - ValidationRunService: The public facade that delegates here
        - FindingsPersistence: Issue normalization and finding persistence
        - SummaryBuilder: Run and step summary aggregation
    """

    def execute_workflow_steps(
        self,
        validation_run_id: UUID | str,
        user_id: int | None,
        resume_from_step: int | None = None,
    ) -> ValidationRunTaskResult:
        """
        Process workflow steps for a ValidationRun.

        Note: This method is meant to be called as part of the "worker"
        Django service layer, ostensibly via a task queue.

        Iterates through the workflow's steps in order, dispatching each to the
        appropriate handler (ValidatorStepHandler for validators, or an action
        handler from the registry). Execution stops on first failure or when
        an async validator returns pending.

        Idempotency:
            - Initial execution (resume_from_step=None): Only proceeds if status
              is PENDING. Transitions to RUNNING atomically.
            - Resume execution (resume_from_step={some step number}): Expects
              status to be RUNNING. No state transition needed.
            - Task queues may deliver the same task multiple times. Step-level
              idempotency is handled by _start_step_run() using get_or_create.

        Args:
            validation_run_id: ID of the ValidationRun to execute (UUID).
            user_id: ID of the user who initiated the run (for tracking).
            resume_from_step: Step order of the last completed step. The
                orchestrator resumes from the *next* step (order__gt). None
                for initial execution.

        Returns:
            ValidationRunTaskResult with the final (or current) run status.
        """

        # Look up the validation run
        try:
            validation_run: ValidationRun = ValidationRun.objects.select_related(
                "workflow",
                "org",
                "project",
                "submission",
            ).get(id=validation_run_id)
        except ValidationRun.DoesNotExist:
            logger.exception(
                "ValidationRun %s missing when execution task started.",
                validation_run_id,
            )
            return ValidationRunTaskResult(
                run_id=validation_run_id,
                status=ValidationRunStatus.FAILED,
                error=GENERIC_EXECUTION_ERROR,
            )

        tracking_service = TrackingEventService()
        actor = self._resolve_run_actor(validation_run, user_id)

        # Idempotency check and state transition based on entry point
        if resume_from_step is None:
            # Initial execution: atomically transition PENDING → RUNNING
            # This prevents race conditions when task queues deliver duplicates
            now = timezone.now()
            updated_count = ValidationRun.objects.filter(
                id=validation_run_id,
                status=ValidationRunStatus.PENDING,
            ).update(
                status=ValidationRunStatus.RUNNING,
                started_at=now,
            )

            if updated_count == 0:
                # Either already started or status changed - fetch current state
                validation_run.refresh_from_db()
                logger.info(
                    "Validation run %s already started (status=%s), skipping",
                    validation_run_id,
                    validation_run.status,
                )
                return ValidationRunTaskResult(
                    run_id=validation_run.id,
                    status=validation_run.status,
                    error="",
                )

            # Update local object to reflect DB state
            validation_run.status = ValidationRunStatus.RUNNING
            validation_run.started_at = now

            tracking_service.log_validation_run_started(
                run=validation_run,
                user=actor,
                extra_data={"status": ValidationRunStatus.RUNNING},
            )
        elif validation_run.status != ValidationRunStatus.RUNNING:
            # Resume from callback: expect status to be RUNNING
            logger.warning(
                "Validation run %s not RUNNING for resume (status=%s), skipping",
                validation_run_id,
                validation_run.status,
            )
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error=_(
                    "Validation run is not in a state that allows execution.",
                ),
            )

        def _was_cancelled() -> bool:
            """Check whether the run has been canceled mid-execution."""
            validation_run.refresh_from_db(fields=["status"])
            return validation_run.status == ValidationRunStatus.CANCELED

        workflow: Workflow = validation_run.workflow
        overall_failed = False
        pending_async = False
        failing_step_id = None
        cancelled = False
        step_metrics: list[StepProcessingResult] = []

        try:
            workflow_steps = workflow.steps.select_related(
                "action",
                "action__definition",
            ).order_by("order")
            # Filter steps for resume execution — resume_from_step is the
            # order of the last *completed* step, so we use __gt to skip it
            # and start from the next one.
            if resume_from_step is not None:
                workflow_steps = workflow_steps.filter(
                    order__gt=resume_from_step,
                )

            for wf_step in workflow_steps:
                if _was_cancelled():
                    cancelled = True
                    break
                admission = self._start_step_run(
                    validation_run=validation_run,
                    workflow_step=wf_step,
                )
                step_run = admission.step_run

                if admission.disposition is _StepRunDisposition.IN_PROGRESS:
                    # Another delivery already owns this logical step. Never
                    # skip past it or reinterpret RUNNING as permission to
                    # repeat an inline validator/action side effect.
                    pending_async = True
                    break
                if admission.disposition is _StepRunDisposition.TERMINAL:
                    # If the step failed, we should stop (same as failure)
                    if step_run.status == StepStatus.FAILED:
                        overall_failed = True
                        failing_step_id = wf_step.id
                        break
                    # Otherwise, continue to the next step
                    continue

                # Route to appropriate execution path based on step type
                if wf_step.validator:
                    # Use processors for validator steps - they handle both
                    # execution AND persistence (findings, output values, stats)
                    try:
                        result: StepProcessingResult = self._execute_validator_step(
                            validation_run=validation_run,
                            step_run=step_run,
                        )
                    except Exception as exc:
                        # _finalize_step_run persists the failure to the DB
                        # (which build_run_summary_record reads). The append
                        # keeps step_metrics consistent with DB state — no
                        # current code reads the values, but the list should
                        # reflect all attempted steps for correctness.
                        self._finalize_step_run(
                            step_run=step_run,
                            status=StepStatus.FAILED,
                            stats=None,
                            error=str(exc),
                        )
                        step_metrics.append(
                            StepProcessingResult(
                                passed=False,
                                step_run=step_run,
                                severity_counts=Counter(),
                                total_findings=0,
                                assertion_failures=0,
                                assertion_total=0,
                            ),
                        )
                        raise
                    step_metrics.append(result)
                    if result.passed is False:
                        overall_failed = True
                        failing_step_id = wf_step.id
                        break
                    if result.passed is None:
                        # Async validator in progress
                        pending_async = True
                        break
                else:
                    # Action steps use StepHandler protocol — dispatch
                    # returns a ValidationResult that _record_step_result
                    # converts to StepProcessingResult with persistence.
                    #
                    # Failure handling depends on the action's failure_mode:
                    # BLOCKING — a failed action fails the run (default).
                    # ADVISORY — the step is marked failed but execution
                    #            continues and the run may still succeed.
                    is_advisory = (
                        wf_step.action
                        and wf_step.action.failure_mode == ActionFailureMode.ADVISORY
                    )
                    if self._is_signed_credential_step(wf_step):
                        result = self._record_deferred_signed_credential_step(
                            step_run=step_run,
                        )
                        step_metrics.append(result)
                        continue
                    try:
                        validation_result: ValidationResult = (
                            self.execute_workflow_step(
                                step=wf_step,
                                validation_run=validation_run,
                            )
                        )
                    except Exception as exc:
                        # Persist failure and keep step_metrics in sync.
                        self._finalize_step_run(
                            step_run=step_run,
                            status=StepStatus.FAILED,
                            stats=None,
                            error=str(exc),
                        )
                        step_metrics.append(
                            StepProcessingResult(
                                passed=False,
                                step_run=step_run,
                                severity_counts=Counter(),
                                total_findings=0,
                                assertion_failures=0,
                                assertion_total=0,
                            ),
                        )
                        if is_advisory:
                            logger.warning(
                                "Advisory action step %s raised %s — "
                                "continuing execution.",
                                wf_step.id,
                                type(exc).__name__,
                            )
                        else:
                            raise
                        continue
                    result: StepProcessingResult = self._record_step_result(
                        validation_run=validation_run,
                        step_run=step_run,
                        validation_result=validation_result,
                    )
                    step_metrics.append(result)
                    if result.passed is False:
                        if is_advisory:
                            logger.info(
                                "Advisory action step %s returned "
                                "passed=False — continuing execution.",
                                wf_step.id,
                            )
                        else:
                            overall_failed = True
                            failing_step_id = wf_step.id
                            break
                    if result.passed is None:
                        pending_async = True
                        break

                if _was_cancelled():
                    cancelled = True
                    break
        except Exception as exc:
            logger.exception("Validation run execution failed")
            validation_run.status = ValidationRunStatus.FAILED
            validation_run.ended_at = timezone.now()
            validation_run.error = GENERIC_EXECUTION_ERROR
            validation_run.error_category = ValidationRunErrorCategory.RUNTIME_ERROR
            validation_run.save(
                update_fields=[
                    "status",
                    "ended_at",
                    "error",
                    "error_category",
                ],
            )
            tracking_service.log_validation_run_status(
                run=validation_run,
                status=ValidationRunStatus.FAILED,
                actor=actor,
                extra_data={"exception": str(exc)},
            )
            build_run_summary_record(
                validation_run=validation_run,
                step_metrics=step_metrics,
            )
            safe_stamp_output_hash(validation_run)
            from validibot.validations.services.run_admission import (
                emit_validation_run_finalized,
            )

            emit_validation_run_finalized(
                sender=self.__class__,
                validation_run=validation_run,
            )
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error=GENERIC_EXECUTION_ERROR,
            )

        if cancelled or _was_cancelled():
            validation_run.status = ValidationRunStatus.CANCELED
            validation_run.error = validation_run.error or RUN_CANCELED_MESSAGE
            if not validation_run.ended_at:
                validation_run.ended_at = timezone.now()
            validation_run.save(
                update_fields=["status", "error", "ended_at"],
            )
            summary_record = build_run_summary_record(
                validation_run=validation_run,
                step_metrics=step_metrics,
            )
            safe_stamp_output_hash(validation_run)
            extra_payload = {
                "step_count": len(step_metrics),
                "finding_count": (
                    summary_record.total_findings if summary_record else 0
                ),
                "duration_ms": validation_run.computed_duration_ms,
            }
            tracking_service.log_validation_run_status(
                run=validation_run,
                status=ValidationRunStatus.CANCELED,
                actor=actor,
                extra_data=extra_payload,
            )
            from validibot.validations.services.run_admission import (
                emit_validation_run_finalized,
            )

            emit_validation_run_finalized(
                sender=self.__class__,
                validation_run=validation_run,
            )
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=ValidationRunStatus.CANCELED,
                error=validation_run.error,
            )

        if pending_async:
            # Leave run/step in RUNNING state. Callback processing will
            # finalize statuses, findings, summaries, and end timestamps.
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error="",
            )

        if overall_failed:
            validation_run.status = ValidationRunStatus.FAILED
            validation_run.error = _(
                "One or more validation steps failed.",
            )
            validation_run.error_category = ValidationRunErrorCategory.VALIDATION_FAILED
        else:
            validation_run.status = ValidationRunStatus.SUCCEEDED
            validation_run.error = ""
            validation_run.error_category = ""
        validation_run.ended_at = timezone.now()
        validation_run.save(
            update_fields=[
                "status",
                "error",
                "error_category",
                "ended_at",
            ],
        )

        summary_record = build_run_summary_record(
            validation_run=validation_run,
            step_metrics=step_metrics,
        )
        safe_stamp_output_hash(validation_run)

        # The signed credential binds to the exact canonical manifest bytes,
        # so the manifest must exist before deferred credential issuance. A
        # generation failure leaves the validation result intact but the Pro
        # issuance service refuses to create an unbound credential.
        from validibot.validations.services.evidence import stamp_evidence_manifest

        stamp_evidence_manifest(validation_run)

        if validation_run.status == ValidationRunStatus.SUCCEEDED:
            if finalize_deferred_signed_credentials(
                validation_run=validation_run,
                finalize_step_run=self._finalize_step_run,
            ):
                summary_record = build_run_summary_record(
                    validation_run=validation_run,
                    step_metrics=step_metrics,
                )
                safe_stamp_output_hash(validation_run)
                stamp_evidence_manifest(validation_run)

        # Emit only after all detailed outputs/evidence are finalized. The
        # retention receiver makes the run immediately eligible for deletion,
        # so emitting earlier would race the purge worker.
        from validibot.validations.services.run_admission import (
            emit_validation_run_finalized,
        )

        emit_validation_run_finalized(
            sender=self.__class__,
            validation_run=validation_run,
        )

        result = ValidationRunTaskResult(
            run_id=validation_run.id,
            status=validation_run.status,
            error=validation_run.error,
        )
        completion_extra: dict[str, Any] = {
            "step_count": len(step_metrics),
            "failing_step_id": failing_step_id,
            "finding_count": (summary_record.total_findings if summary_record else 0),
        }
        extra_payload = {
            **{k: v for k, v in completion_extra.items() if v is not None},
            "duration_ms": validation_run.computed_duration_ms,
        }
        tracking_service.log_validation_run_status(
            run=validation_run,
            status=validation_run.status,
            actor=actor,
            extra_data=extra_payload,
        )

        return result

    # ---------- Step lifecycle ----------

    def _start_step_run(
        self,
        *,
        validation_run: ValidationRun,
        workflow_step: WorkflowStep,
    ) -> _StepRunAdmission:
        """
        Get or create a ValidationStepRun entry for the step.

        A task delivery may create the row and execute it exactly once. Later
        deliveries observe either its terminal result or active ownership. A
        RUNNING/PENDING row is never reinterpreted as a crashed worker because
        that would repeat inline validators and side-effecting actions. The
        watchdog owns recovery of genuinely abandoned work.

        Returns:
            A step admission containing the authoritative row and one of:
            EXECUTE, TERMINAL, or IN_PROGRESS.

        See Also:
            ADR-001: Idempotent Step Execution on Retry
        """
        with transaction.atomic():
            step_run, created = ValidationStepRun.objects.get_or_create(
                validation_run=validation_run,
                workflow_step=workflow_step,
                defaults={
                    "step_order": workflow_step.order or 0,
                    "status": StepStatus.RUNNING,
                    "started_at": timezone.now(),
                },
            )

            if created:
                return _StepRunAdmission(
                    step_run=step_run,
                    disposition=_StepRunDisposition.EXECUTE,
                )

            step_run = ValidationStepRun.objects.select_for_update().get(id=step_run.id)
            if step_run.status in {
                StepStatus.PASSED,
                StepStatus.FAILED,
                StepStatus.SKIPPED,
            }:
                logger.info(
                    "Step run %s already terminal (status=%s), skipping",
                    step_run.id,
                    step_run.status,
                )
                return _StepRunAdmission(
                    step_run=step_run,
                    disposition=_StepRunDisposition.TERMINAL,
                )

            logger.info(
                "Step run %s is already active; duplicate delivery will wait",
                step_run.id,
            )
            return _StepRunAdmission(
                step_run=step_run,
                disposition=_StepRunDisposition.IN_PROGRESS,
            )

    def _finalize_step_run(
        self,
        *,
        step_run: ValidationStepRun,
        status: StepStatus,
        stats: dict[str, Any] | None,
        error: str | None = None,
    ) -> ValidationStepRun:
        """Persist final status, duration, and diagnostics on a step run."""
        ended_at = timezone.now()
        step_run.status = status
        step_run.ended_at = ended_at
        if step_run.started_at:
            step_run.duration_ms = max(
                int(
                    (ended_at - step_run.started_at).total_seconds() * 1000,
                ),
                0,
            )
        else:
            step_run.duration_ms = 0
        step_run.output = stats or {}
        step_run.error = error or ""

        update_fields = [
            "status",
            "ended_at",
            "duration_ms",
            "output",
            "error",
        ]

        # Trust ADR Phase 5 Session A — promote the validator backend
        # image digest from the validator's stats bag onto the typed
        # column so trust auditors can query it directly. The Cloud
        # Run launcher writes the column at job-launch time and
        # leaves stats unset; the Docker runner threads the digest
        # through ``stats``. We only overwrite the column when stats
        # carries a non-empty digest, so the launch-time write isn't
        # clobbered by an async finalize whose stats bag never had
        # the field.
        backend_digest = (stats or {}).get("validator_backend_image_digest")
        if backend_digest:
            step_run.validator_backend_image_digest = str(backend_digest)
            update_fields.append("validator_backend_image_digest")

        step_run.save(update_fields=update_fields)
        return step_run

    def _is_signed_credential_step(self, workflow_step: WorkflowStep) -> bool:
        """Return True when ``workflow_step`` is the signed credential action."""
        action = getattr(workflow_step, "action", None)
        definition = getattr(action, "definition", None)
        return bool(
            definition and definition.type == CredentialActionType.SIGNED_CREDENTIAL
        )

    def _record_deferred_signed_credential_step(
        self,
        *,
        step_run: ValidationStepRun,
    ) -> StepProcessingResult:
        """Mark the credential step passed and defer issuance to finalization.

        Signed credentials need the run's final status, timestamps, and summary
        record to build the canonical output hash. Those values do not exist
        while the step loop is still in progress, so the actual issuance work
        is deferred until the run reaches its terminal state.
        """
        stats = {"credential_issuance": "deferred"}
        finalized_step = self._finalize_step_run(
            step_run=step_run,
            status=StepStatus.PASSED,
            stats=stats,
            error=None,
        )
        return StepProcessingResult(
            passed=True,
            step_run=finalized_step,
            severity_counts=Counter(),
            total_findings=0,
            assertion_failures=0,
            assertion_total=0,
        )

    # ---------- Step dispatch ----------

    def execute_workflow_step(
        self,
        step: WorkflowStep,
        validation_run: ValidationRun,
    ) -> ValidationResult:
        """
        Dispatch a single workflow step to its handler.

        Routes steps to the correct handler:
        1. Resolves the handler (ValidatorStepHandler or action handler).
        2. Builds a RunContext with the validation_run, step, and any values
           from prior steps (for cross-step assertions).
        3. Calls handler.execute(run_context) and maps the StepResult back to
           ValidationResult for backwards compatibility.

        Args:
            step: The WorkflowStep to execute (has .validator or .action).
            validation_run: The parent ValidationRun being processed.

        Returns:
            ValidationResult with passed=True/False for sync handlers, or
            passed=None for async handlers awaiting callback.
        """
        from validibot.actions.handlers import ValidatorStepHandler
        from validibot.actions.protocols import StepResult
        from validibot.actions.registry import get_action_handler

        # 1. Resolve handler before building context. Missing-handler errors
        # should not require a valid step order or upstream database query.
        handler = None

        if step.validator:
            handler = ValidatorStepHandler()
        elif step.action:
            action_type = step.action.definition.type
            handler_class = get_action_handler(action_type)
            if handler_class:
                handler = handler_class()

        # 3. Handle missing handler gracefully
        if handler is None:
            if step.action:
                error_msg = f"No handler registered for action type: {action_type}"
                logger.error(
                    "No handler for action: type=%s step_id=%s run_id=%s",
                    action_type,
                    getattr(step, "id", None),
                    validation_run.id,
                )
            else:
                error_msg = _(
                    "WorkflowStep has no validator or action configured.",
                )
                logger.error(
                    "WorkflowStep has no validator or action: step_id=%s run_id=%s",
                    getattr(step, "id", None),
                    validation_run.id,
                )
            step_result = StepResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=error_msg,
                        severity=Severity.ERROR,
                        code="missing_handler",
                    ),
                ],
            )
            return ValidationResult(
                passed=step_result.passed,
                issues=[normalize_issue(i) for i in step_result.issues],
                output_values=step_result.output_values,
                stats=step_result.stats,
            )

        # 2. Prepare context only once we know something can execute.
        context = RunContextBuilder(validation_run, step).build()

        # 3. Execute Handler
        step_result = handler.execute(context)

        # 4. Map StepResult → ValidationResult
        # _record_step_result() expects a ValidationResult so it can
        # persist findings and convert to StepProcessingResult.
        validation_result = ValidationResult(
            passed=step_result.passed,
            issues=[normalize_issue(i) for i in step_result.issues],
            output_values=step_result.output_values,
            stats=step_result.stats,
        )
        validation_result.workflow_step_name = step.name

        logger.info(
            "Step executed: step_id=%s handler=%s passed=%s",
            getattr(step, "id", None),
            handler.__class__.__name__,
            validation_result.passed,
        )

        return validation_result

    # ---------- Result recording ----------

    def _record_step_result(
        self,
        *,
        validation_run: ValidationRun,
        step_run: ValidationStepRun,
        validation_result: ValidationResult,
    ) -> StepProcessingResult:
        """
        Persist action step results and build a StepProcessingResult.

        Used only for action steps (Slack, signed credential issuance, etc.). Validator
        steps go through _execute_validator_step() where the processor
        handles persistence directly.

        After a handler returns, this method:
        1. Persists issues as ValidationFinding rows.
        2. Persists any outputs on the canonical step-run value field for
           downstream assertions to access.
        3. For sync results (passed=True/False): finalizes the step_run.
        4. For async results (passed=None): keeps step_run as RUNNING.

        Precondition: issues in validation_result must already be
        normalized (via normalize_issue). This is guaranteed when the
        caller is execute_workflow_step(), which normalizes issues as
        part of the StepResult → ValidationResult mapping. If a new
        caller is added, it must normalize issues before calling here.
        """
        issues = list(validation_result.issues or [])
        severity_counts, assertion_failures = persist_findings(
            validation_run=validation_run,
            step_run=step_run,
            issues=issues,
        )
        stats = dict(validation_result.stats or {})
        # Add assertion_failures to stats so it gets persisted in
        # step_run.output. This is needed for build_run_summary_record()
        # to calculate totals correctly in resume scenarios.
        stats["assertion_failures"] = assertion_failures
        # Persist action outputs on their canonical step-run field. The context
        # builder applies the stable step_key namespace when a later step runs.
        step_run.output_values = dict(validation_result.output_values or {})
        step_run.save(update_fields=["output_values"])
        if validation_result.passed is None:
            # Async validator still running; keep status as RUNNING and
            # persist any interim stats for observability.
            step_run.output = stats
            step_run.status = StepStatus.RUNNING
            step_run.save(update_fields=["output", "status"])
            finalized_step = step_run
        else:
            step_status = (
                StepStatus.PASSED if validation_result.passed else StepStatus.FAILED
            )
            finalized_step = self._finalize_step_run(
                step_run=step_run,
                status=step_status,
                stats=stats,
                error=None,
            )
        return StepProcessingResult(
            passed=validation_result.passed,
            step_run=finalized_step,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=assertion_failures,
            assertion_total=extract_assertion_total(stats),
        )

    def _execute_validator_step(
        self,
        *,
        validation_run: ValidationRun,
        step_run: ValidationStepRun,
    ) -> StepProcessingResult:
        """
        Execute a validator step using the processor abstraction.

        Processors handle both execution (calling the validator) AND persistence
        (findings, output values, assertion stats). This eliminates the separate
        _record_step_result() call for validator steps.

        Args:
            validation_run: The parent ValidationRun being processed.
            step_run: The ValidationStepRun to execute.

        Returns:
            The processor's result with pass/fail status, severity counts,
            and assertion stats.
        """
        from validibot.validations.services.step_processor import get_step_processor

        processor = get_step_processor(validation_run, step_run)
        return processor.execute()

    # ---------- Helpers ----------

    def _resolve_run_actor(
        self,
        validation_run: ValidationRun,
        user_id: int | None,
    ) -> User | None:
        """Resolve the user who initiated or owns the validation run."""
        if getattr(validation_run, "user_id", None):
            return validation_run.user
        submission_user = getattr(
            getattr(validation_run, "submission", None),
            "user",
            None,
        )
        if submission_user and getattr(
            submission_user,
            "is_authenticated",
            False,
        ):
            return submission_user
        if not user_id:
            return None
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(pk=user_id).first()
        return cast("User | None", user)
