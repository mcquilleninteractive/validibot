"""
Validation run service — public facade for creating and managing validation runs.

This module is the main entry point for validation run lifecycle management.
It provides two web-layer operations (launch, cancel) and delegates worker-side
execution to the StepOrchestrator. Summary building is delegated to the
SummaryBuilder module.

Architecture:

    ValidationRunService (this file)
        ├── launch()                  — web-side: create run, dispatch to worker
        ├── cancel_run()              — web-side: cancel a pending/running run
        ├── execute_workflow_steps()   → delegates to StepOrchestrator
        └── rebuild_run_summary_record() → delegates to summary_builder

    StepOrchestrator (step_orchestrator.py)
        ├── execute_workflow_steps()   — worker-side: iterate steps
        ├── execute_workflow_step()    — dispatch single step to handler
        └── (step lifecycle, result recording, output extraction)

    SummaryBuilder (summary_builder.py)
        ├── build_run_summary_record() — build summaries from DB findings
        └── rebuild_run_summary_record() — idempotent public entry point

    FindingsPersistence (findings_persistence.py)
        ├── normalize_issue()          — coerce raw issues to ValidationIssue
        ├── persist_findings()         — bulk-create ValidationFinding rows
        └── (severity coercion helpers)

See Also:
    - docs/dev_docs/overview/service_architecture.md
    - GitHub issue #95: Split ValidationRunService into focused modules
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from typing import Any

from attr import dataclass
from attr import field
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status

from validibot.tracking.services import TrackingEventService
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.exceptions import OrgPolicyDeniedError
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationRunSummary
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.step_orchestrator import StepOrchestrator

logger = logging.getLogger(__name__)

GENERIC_EXECUTION_ERROR = _(
    "This validation run could not be completed. Please try again later.",
)

RUN_CANCELED_MESSAGE = _("Run canceled by user.")

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest
    from rest_framework.request import Request

    from validibot.submissions.models import Submission
    from validibot.users.models import Organization
    from validibot.users.models import User
    from validibot.validations.services.models import ValidationRunTaskResult
    from validibot.workflows.models import Workflow


def _send_run_created_signal(validation_run: ValidationRun, workflow_type: str) -> None:
    """Fire the validation_run_created signal (deferred via on_commit)."""
    from validibot.validations.signals import validation_run_created

    validation_run_created.send_robust(
        sender=ValidationRun,
        validation_run=validation_run,
        workflow_type=workflow_type,
    )


def cancel_active_execution(run: ValidationRun) -> bool | None:
    """Best-effort cancel the concrete provider execution for an active step.

    The caller must commit the run's logical terminal state before invoking
    this helper. Provider APIs and PostgreSQL cannot share a transaction, so a
    provider failure is logged and returned without reopening the run. ``None``
    means the active step has no addressable execution identity.
    """
    step_run = (
        ValidationStepRun.objects.filter(
            validation_run=run,
            status__in=[StepStatus.RUNNING, StepStatus.PENDING],
        )
        .order_by("step_order")
        .first()
    )
    if not step_run:
        return None

    from validibot.validations.constants import ExecutionAttemptState
    from validibot.validations.services.execution_attempts import (
        get_active_execution_attempt,
    )
    from validibot.validations.services.execution_attempts import (
        resolve_provider_execution_identity,
    )
    from validibot.validations.services.execution_attempts import (
        transition_execution_attempt,
    )
    from validibot.validations.services.execution_logging import execution_log_context

    identity = resolve_provider_execution_identity(step_run)
    attempt = get_active_execution_attempt(step_run)
    if attempt is not None:
        attempt_target = (
            ExecutionAttemptState.TIMED_OUT
            if run.status == ValidationRunStatus.TIMED_OUT
            else ExecutionAttemptState.CANCELED
        )
        transition_execution_attempt(
            attempt.pk,
            attempt_target,
            last_error_code=(
                "run_timed_out"
                if attempt_target == ExecutionAttemptState.TIMED_OUT
                else "user_canceled"
            ),
            last_error=(
                str(run.error)
                if attempt_target == ExecutionAttemptState.TIMED_OUT
                else str(RUN_CANCELED_MESSAGE)
            ),
        )
    if identity is None:
        return None
    execution_id = identity.execution_id

    try:
        if identity.attempt.deployment_id:
            from validibot.validations.services.execution.registry import (
                get_execution_backend,
            )

            backend = get_execution_backend(identity.attempt.deployment)
            canceled = backend.cancel(execution_id)
        else:
            from validibot.validations.services.runners import get_validator_runner

            canceled = get_validator_runner().cancel(execution_id)
    except Exception:
        logger.warning(
            "Failed to request provider cancellation",
            extra=execution_log_context(
                run,
                step_run=step_run,
                attempt=identity.attempt,
                provider_execution_id=execution_id,
            ),
            exc_info=True,
        )
        return False

    if not canceled:
        logger.warning(
            "Provider did not accept execution cancellation",
            extra=execution_log_context(
                run,
                step_run=step_run,
                attempt=identity.attempt,
                provider_execution_id=execution_id,
            ),
        )
        return False

    logger.info(
        "Requested provider execution cancellation",
        extra=execution_log_context(
            run,
            step_run=step_run,
            attempt=identity.attempt,
            provider_execution_id=execution_id,
        ),
    )
    return True


@dataclass
class ValidationRunLaunchResults:
    validation_run: ValidationRun
    data: dict[str, Any] = field(factory=dict)
    status: int | None = None


def fence_active_execution_attempt(
    run: ValidationRun,
    *,
    target,
    error_code: str,
    error_message: str,
) -> None:
    """Terminally fence an attempt inside the caller's run transaction."""
    step_run = (
        ValidationStepRun.objects.filter(
            validation_run=run,
            status__in=[StepStatus.RUNNING, StepStatus.PENDING],
        )
        .order_by("step_order")
        .first()
    )
    if step_run is None:
        return

    from validibot.validations.services.execution_attempts import (
        get_active_execution_attempt,
    )
    from validibot.validations.services.execution_attempts import (
        transition_execution_attempt,
    )

    attempt = get_active_execution_attempt(step_run, for_update=True)
    if attempt is not None:
        transition_execution_attempt(
            attempt.pk,
            target,
            last_error_code=error_code,
            last_error=error_message,
        )


class ValidationRunService:
    """
    Public facade for validation run lifecycle management.

    This is the stable API that views, API endpoints, task queues, and callback
    handlers use. It handles run creation (launch) and cancellation directly,
    and delegates step execution and summary building to focused internal
    modules.

    Internal orchestration is handled by:
    - StepOrchestrator: Step iteration, dispatch, and result recording
    - SummaryBuilder: Run/step summary aggregation from DB findings
    - FindingsPersistence: Issue normalization and finding persistence

    Main entry points:

        launch(request, org, workflow, submission, ...)
            Creates a ValidationRun and dispatches execution. Called by views/API.

        execute_workflow_steps(validation_run_id, user_id)
            Processes workflow steps sequentially. Called by task queue.

        cancel_run(run, actor)
            Cancels a run that hasn't completed yet.

        rebuild_run_summary_record(validation_run)
            Rebuilds summary records from persisted findings. Called by
            async validator callback handler.

    See Also:
        - StepOrchestrator: Worker-side step execution
        - SummaryBuilder: Summary aggregation
        - FindingsPersistence: Issue normalization and finding creation
    """

    def __init__(self) -> None:
        self._orchestrator = StepOrchestrator()

    # ---------- Launch (views call this) ----------

    def launch(
        self,
        request: HttpRequest | Request | None,
        org: Organization,
        workflow: Workflow,
        submission: Submission,
        user_id: int,
        metadata: dict[str, Any] | None = None,
        *,
        actor: User | None = None,
        extra: dict[str, Any] | None = None,
        source: ValidationRunSource = ValidationRunSource.LAUNCH_PAGE,
    ) -> ValidationRunLaunchResults:
        """
        Create a ValidationRun and dispatch execution.

        This is the web-layer entry point called by views and API endpoints.
        It validates preconditions, creates the run record, and dispatches
        execution to the appropriate backend (Celery, Cloud Tasks, etc.).

        Args:
            request: Optional HTTP request carrying the authenticated user.
            org: The organization under which the run is created.
            workflow: The workflow to execute.
            submission: The file/content to validate.
            user_id: ID of the user initiating the run.
            metadata: Optional metadata to associate with the run.
            actor: Explicit authenticated user for non-HTTP launch channels.
                HTTP callers normally leave this unset and use ``request.user``.
            extra: Additional fields to pass to ValidationRun.objects.create().
            source: Origin of the run (LAUNCH_PAGE, API, etc.).

        Returns:
            ValidationRunLaunchResults with the run and HTTP status code:
            - 201 Created if execution completed (SUCCEEDED, FAILED, CANCELED)
            - 202 Accepted if still processing (PENDING, RUNNING)

        Raises:
            ValueError: If required arguments are missing.
            PermissionError: If user lacks execute permission on workflow.
        """
        from validibot.core.tasks import enqueue_validation_run
        from validibot.validations.services.run_admission import admit_validation_run

        start_time = time.perf_counter()
        launch_user = actor or getattr(request, "user", None)
        if not org:
            err_msg = "Organization must be provided"
            raise ValueError(err_msg)
        if not launch_user or not getattr(launch_user, "is_authenticated", False):
            err_msg = "An authenticated launch user is required"
            raise ValueError(err_msg)
        if not submission:
            err_msg = "Submission must be provided"
            raise ValueError(err_msg)
        if not workflow.can_execute(user=launch_user):
            err_msg = "User does not have permission to execute this workflow"
            raise PermissionError(err_msg)

        # Check organization-level policies (trial expiry, quotas, etc.)
        # In community edition no policies are registered, so this is a no-op.
        # When validibot-cloud is installed, metering policies check usage quotas
        # and credit balances. The workflow_type context kwarg lets cloud
        # policies distinguish BASIC vs ADVANCED workflows.
        from validibot.core.policies import check_org_policies

        workflow_type = getattr(workflow, "workflow_type", "BASIC")
        # Pass the launching user so the registry can bypass all org
        # policies for a superuser (operator), who is not a tenant subject
        # to commercial quotas/billing/rate limits.
        allowed, reason = check_org_policies(
            org,
            "launch_validation_run",
            user=launch_user,
            workflow_type=workflow_type,
        )
        if not allowed:
            # Distinct from the can_execute() PermissionError above: here the
            # user IS permitted but an org policy (billing/quota/credits/rate
            # limit) blocked the launch. Raise OrgPolicyDeniedError so the views can
            # surface ``reason`` verbatim instead of the generic permission
            # message. OrgPolicyDeniedError subclasses PermissionError, so callers
            # that only catch PermissionError still treat it as a refusal.
            raise OrgPolicyDeniedError(reason)

        run_user = None
        if getattr(submission, "user_id", None):
            run_user = submission.user
        elif getattr(launch_user, "is_authenticated", False):
            run_user = launch_user

        run_extra = dict(extra or {})
        with transaction.atomic():
            # Admission owns the canonical lock order: workflow first, then
            # submission. The workflow lock serializes this first run with
            # editing-policy transitions and Mutable semantic mutations; the
            # submission lock serializes launch with retention purge.
            validation_run = admit_validation_run(
                org=org,
                workflow=workflow,
                submission=submission,
                user=run_user,
                source=source,
                extra=run_extra,
            )
            admitted_submission = validation_run.submission
            if admitted_submission is None:
                msg = "An admitted validation run must retain its submission"
                raise RuntimeError(msg)
            submission = admitted_submission

            # Run-created hooks fire INSIDE this transaction, under any row
            # locks they take, so a commercial package (cloud) can reserve
            # resources (e.g. a compute-credit hold) atomically with the run and
            # abort the launch by raising — rolling back the run. Community
            # registers no hooks, so this is a no-op there. A raise here (e.g.
            # PermissionError for insufficient credits) propagates like the
            # earlier check_org_policies denial above.
            from validibot.core.run_hooks import run_created_hooks

            # Thread the launching user so a commercial reservation hook can
            # waive its hold for a superuser (operator), mirroring the
            # superuser bypass in check_org_policies above.
            #
            # A reservation hook (e.g. cloud's compute-credit hold) signals an
            # unfundable launch by raising PermissionError with a specific,
            # user-facing reason ("Insufficient compute credits ..."). Re-raise
            # it as OrgPolicyDeniedError so the views surface that reason verbatim
            # rather than the generic permission message — the raise still
            # propagates out of this atomic block and rolls the run back. We
            # leave a genuine OrgPolicyDeniedError untouched (it's already the right
            # type) and don't disturb non-permission errors.
            try:
                run_created_hooks(
                    validation_run,
                    workflow_type=workflow_type,
                    launching_user=launch_user,
                )
            except OrgPolicyDeniedError:
                raise
            except PermissionError as exc:
                raise OrgPolicyDeniedError(str(exc)) from exc

            try:
                if hasattr(submission, "latest_run_id"):
                    submission.latest_run = validation_run
                    submission.save(update_fields=["latest_run"])
            except Exception:
                logger.exception(
                    "Failed to update submission.latest_run for submission",
                    extra={"submission_id": submission.id},
                )

            tracking_service = TrackingEventService()
            created_extra: dict[str, Any] = {}
            if metadata:
                created_extra["metadata_keys"] = sorted(metadata.keys())
            # Flag the org's very first run so the activation funnel can be
            # segmented on first-run conversion (a key activation metric).
            run_org = getattr(validation_run, "org", None)
            if run_org is not None:
                created_extra["is_first_run"] = not (
                    type(validation_run)
                    .objects.filter(org=run_org)
                    .exclude(pk=validation_run.pk)
                    .exists()
                )
            tracking_service.log_validation_run_created(
                run=validation_run,
                user=run_user,
                submission_id=submission.pk,
                extra_data=created_extra or None,
            )

        # Notify listeners that a run was created (e.g., cloud metering
        # records basic launches). Deferred via on_commit so the run is
        # guaranteed to be persisted.
        _run = validation_run
        _wtype = workflow_type
        transaction.on_commit(
            lambda: _send_run_created_signal(_run, _wtype),
        )

        # Dispatch execution to the appropriate backend:
        # - Test: Synchronous inline execution
        # - Local dev: HTTP call to worker
        # - Docker Compose: Celery task queue
        # - GCP: Cloud Tasks
        # - AWS: TBD (future)
        try:
            enqueue_validation_run(
                validation_run_id=validation_run.id,
                user_id=launch_user.id,
            )
        except Exception:
            logger.exception(
                "Failed to enqueue validation run %s",
                validation_run.id,
            )
            validation_run.status = ValidationRunStatus.FAILED
            validation_run.error = GENERIC_EXECUTION_ERROR
            validation_run.error_category = ValidationRunErrorCategory.RUNTIME_ERROR
            validation_run.ended_at = timezone.now()
            validation_run.save(
                update_fields=["status", "error", "error_category", "ended_at"],
            )

            # This is a terminal transition that bypasses the normal finalize
            # paths, so emit the finalized signal here too — otherwise a launch
            # that reserved a compute-credit hold (cloud) would not release it
            # until the reaper runs. Idempotent with the reaper.
            from validibot.validations.services.run_admission import (
                emit_validation_run_finalized,
            )

            emit_validation_run_finalized(
                sender=self.__class__,
                validation_run=validation_run,
            )

        # Refresh from DB to get any updates made during execution
        # This is primarily for test mode where execute_workflow_steps() runs
        # synchronously,
        # but also provides correct status if execution completed very quickly
        validation_run.refresh_from_db()

        # Return appropriate HTTP status based on run state:
        # - 201 Created if execution reached any terminal state
        # - 202 Accepted if still processing (PENDING, RUNNING)
        # Use the canonical terminal-status set so TIMED_OUT (and
        # any future terminal additions) get the right HTTP code —
        # without this a timed-out synchronous run would return 202
        # Accepted, misleading the caller into polling.
        http_status: int
        if validation_run.status in VALIDATION_RUN_TERMINAL_STATUSES:
            http_status = status.HTTP_201_CREATED
        else:
            http_status = status.HTTP_202_ACCEPTED

        results: ValidationRunLaunchResults = ValidationRunLaunchResults(
            validation_run=validation_run,
            status=http_status,
        )

        logger.info(
            "Validation run %s launch completed in %.2f ms (status=%s, enqueued)",
            validation_run.id,
            (time.perf_counter() - start_time) * 1000,
            validation_run.status,
        )
        return results

    # ---------- Cancel ----------

    def cancel_run(
        self,
        *,
        run: ValidationRun,
        actor: User | None = None,
    ) -> tuple[ValidationRun, bool]:
        """Fence a validation run, then stop any known provider execution."""

        if run is None:
            raise ValueError("run is required to cancel a validation")

        with transaction.atomic():
            locked_run = ValidationRun.objects.select_for_update().get(pk=run.pk)
            if locked_run.status == ValidationRunStatus.CANCELED:
                return locked_run, True

            if locked_run.status not in (
                ValidationRunStatus.PENDING,
                ValidationRunStatus.RUNNING,
            ):
                return locked_run, False

            locked_run.status = ValidationRunStatus.CANCELED
            if not locked_run.ended_at:
                locked_run.ended_at = timezone.now()
            if not locked_run.error:
                locked_run.error = RUN_CANCELED_MESSAGE
            locked_run.save(update_fields=["status", "ended_at", "error"])

            from validibot.validations.constants import ExecutionAttemptState

            fence_active_execution_attempt(
                locked_run,
                target=ExecutionAttemptState.CANCELED,
                error_code="user_canceled",
                error_message=str(RUN_CANCELED_MESSAGE),
            )

        # External work stays outside the database transaction. The terminal
        # decision is authoritative even if the provider is unavailable.
        cancel_active_execution(locked_run)

        tracking_service = TrackingEventService()
        extra = {"duration_ms": locked_run.computed_duration_ms}
        tracking_service.log_validation_run_status(
            run=locked_run,
            status=ValidationRunStatus.CANCELED,
            actor=actor,
            extra_data=extra,
        )

        from validibot.validations.services.run_admission import (
            emit_validation_run_finalized,
        )

        emit_validation_run_finalized(
            sender=self.__class__,
            validation_run=locked_run,
        )

        return locked_run, True

    # ---------- Delegated to StepOrchestrator ----------

    def execute_workflow_steps(
        self,
        validation_run_id: UUID | str,
        user_id: int | None,
        resume_from_step: int | None = None,
    ) -> ValidationRunTaskResult:
        """Process workflow steps for a ValidationRun.

        Delegates to StepOrchestrator. See StepOrchestrator.execute_workflow_steps
        for full documentation.
        """
        return self._orchestrator.execute_workflow_steps(
            validation_run_id=validation_run_id,
            user_id=user_id,
            resume_from_step=resume_from_step,
        )

    def execute_workflow_step(self, step, validation_run):
        """Dispatch a single workflow step to its handler.

        Delegates to StepOrchestrator. See StepOrchestrator.execute_workflow_step
        for full documentation.
        """
        return self._orchestrator.execute_workflow_step(
            step=step,
            validation_run=validation_run,
        )

    # ---------- Delegated to SummaryBuilder ----------

    def rebuild_run_summary_record(
        self,
        *,
        validation_run: ValidationRun,
    ) -> ValidationRunSummary:
        """Rebuild run and step summary records from persisted state.

        Delegates to summary_builder.rebuild_run_summary_record.
        """
        from validibot.validations.services.summary_builder import (
            rebuild_run_summary_record,
        )

        return rebuild_run_summary_record(validation_run=validation_run)
