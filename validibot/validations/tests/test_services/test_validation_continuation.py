"""Tests for durable callback-driven validation-run continuations.

The continuation row closes the transaction-to-queue failure window without a
generic outbox. These tests protect its commit coupling, deterministic delivery,
claim fencing, stale-owner repair, and idempotent worker semantics.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from validibot.validations.constants import ValidationContinuationState
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRunContinuation
from validibot.validations.services.models import ValidationRunTaskResult
from validibot.validations.services.validation_continuation import (
    ContinuationDispatchOutcome,
)
from validibot.validations.services.validation_continuation import (
    ValidationContinuationBusyError,
)
from validibot.validations.services.validation_continuation import (
    dispatch_validation_run_continuation,
)
from validibot.validations.services.validation_continuation import (
    execute_validation_run_continuation,
)
from validibot.validations.services.validation_continuation import (
    repair_validation_run_continuations,
)
from validibot.validations.services.validation_continuation import (
    stage_validation_run_continuation,
)
from validibot.validations.tests.factories import CallbackReceiptFactory
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationRunContinuationFactory
from validibot.validations.tests.factories import ValidationStepRunFactory

SECOND_DISPATCH_ATTEMPT = 2


class TestContinuationCommitCoupling(TestCase):
    """Prove the resume decision and callback result share one commit boundary."""

    @patch(
        "validibot.validations.services.validation_continuation."
        "dispatch_validation_run_continuation",
    )
    def test_committed_continuation_dispatches_only_after_commit(self, dispatch):
        """A queue producer cannot observe database state that may roll back."""
        step_run = ValidationStepRunFactory(
            validation_run__status=ValidationRunStatus.RUNNING,
            status="PASSED",
        )
        receipt = CallbackReceiptFactory(
            validation_run=step_run.validation_run,
            execution_attempt=ExecutionAttemptFactory(step_run=step_run),
        )

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            continuation = stage_validation_run_continuation(
                validation_run=step_run.validation_run,
                completed_step_run=step_run,
                callback_receipt=receipt,
            )
            dispatch.assert_not_called()

        dispatch.assert_called_once_with(continuation.pk)
        self.assertTrue(
            ValidationRunContinuation.objects.filter(pk=continuation.pk).exists()
        )

    @patch(
        "validibot.validations.services.validation_continuation."
        "dispatch_validation_run_continuation",
    )
    def test_rolled_back_callback_creates_neither_work_nor_delivery(self, dispatch):
        """A failed callback transaction cannot leak an orphan queue task."""
        step_run = ValidationStepRunFactory(
            validation_run__status=ValidationRunStatus.RUNNING,
            status="PASSED",
        )
        receipt = CallbackReceiptFactory(
            validation_run=step_run.validation_run,
            execution_attempt=ExecutionAttemptFactory(step_run=step_run),
        )
        continuation_id = None

        def stage_then_roll_back():
            """Create the row inside the transaction that deliberately fails."""
            nonlocal continuation_id
            with transaction.atomic():
                continuation = stage_validation_run_continuation(
                    validation_run=step_run.validation_run,
                    completed_step_run=step_run,
                    callback_receipt=receipt,
                )
                continuation_id = continuation.pk
                raise RuntimeError("force rollback")

        with (
            self.captureOnCommitCallbacks(execute=True),
            pytest.raises(RuntimeError, match="force rollback"),
        ):
            stage_then_roll_back()

        dispatch.assert_not_called()
        self.assertFalse(
            ValidationRunContinuation.objects.filter(pk=continuation_id).exists()
        )


@pytest.mark.django_db
class TestContinuationDispatch:
    """Exercise queue delivery as a recoverable, token-fenced operation."""

    @patch("validibot.core.tasks.enqueue_validation_run")
    def test_dispatch_uses_stable_transport_identity(self, enqueue):
        """Every retry needs one durable task identity for queue-side convergence."""
        continuation = ValidationRunContinuationFactory()
        enqueue.return_value = "queues/work/tasks/accepted"

        outcome = dispatch_validation_run_continuation(continuation.pk)

        continuation.refresh_from_db()
        assert outcome is ContinuationDispatchOutcome.DISPATCHED
        assert continuation.state == ValidationContinuationState.DISPATCHED
        assert continuation.dispatch_attempts == 1
        assert continuation.transport_task_id == "queues/work/tasks/accepted"
        enqueue.assert_called_once_with(
            validation_run_id=continuation.validation_run_id,
            user_id=continuation.validation_run.user_id,
            resume_from_step=continuation.resume_from_step,
            continuation_id=continuation.pk,
            task_id=continuation.task_id,
        )

    @patch("validibot.core.tasks.enqueue_validation_run")
    def test_failed_dispatch_returns_to_pending_for_repair(self, enqueue):
        """A broker outage after commit must remain visible and safely retryable."""
        continuation = ValidationRunContinuationFactory()
        enqueue.side_effect = [ConnectionError("broker unavailable"), "task-name"]

        first = dispatch_validation_run_continuation(continuation.pk)
        continuation.refresh_from_db()
        first_task_id = continuation.task_id

        assert first is ContinuationDispatchOutcome.FAILED
        assert continuation.state == ValidationContinuationState.PENDING
        assert continuation.last_error_code == "transport_dispatch_failed"

        second = dispatch_validation_run_continuation(continuation.pk)
        continuation.refresh_from_db()

        assert second is ContinuationDispatchOutcome.DISPATCHED
        assert continuation.state == ValidationContinuationState.DISPATCHED
        assert continuation.dispatch_attempts == SECOND_DISPATCH_ATTEMPT
        assert {call.kwargs["task_id"] for call in enqueue.call_args_list} == {
            first_task_id
        }

    @patch("validibot.core.tasks.enqueue_validation_run")
    def test_non_running_run_closes_continuation_without_dispatch(self, enqueue):
        """Terminal or not-yet-started runs cannot be reopened by stale work."""
        continuation = ValidationRunContinuationFactory()
        continuation.validation_run.status = ValidationRunStatus.CANCELED
        continuation.validation_run.save(update_fields=["status"])

        outcome = dispatch_validation_run_continuation(continuation.pk)

        continuation.refresh_from_db()
        assert outcome is ContinuationDispatchOutcome.NOT_REQUIRED
        assert continuation.state == ValidationContinuationState.NOT_REQUIRED
        enqueue.assert_not_called()


@pytest.mark.django_db
class TestContinuationExecution:
    """Prove at-least-once worker delivery cannot repeat active workflow work."""

    @patch(
        "validibot.validations.services.validation_run."
        "ValidationRunService.execute_workflow_steps"
    )
    def test_worker_executes_and_completes_its_exact_claim(self, execute):
        """A normal delivery records execution ownership and its terminal result."""
        continuation = ValidationRunContinuationFactory(
            state=ValidationContinuationState.DISPATCHED,
        )
        expected = ValidationRunTaskResult(
            run_id=continuation.validation_run_id,
            status=ValidationRunStatus.RUNNING,
            error="",
        )
        execute.return_value = expected

        result = execute_validation_run_continuation(continuation.pk)

        continuation.refresh_from_db()
        assert result == expected
        assert continuation.state == ValidationContinuationState.COMPLETED
        assert continuation.execution_attempts == 1
        assert continuation.completed_at is not None
        execute.assert_called_once_with(
            validation_run_id=continuation.validation_run_id,
            user_id=continuation.validation_run.user_id,
            resume_from_step=continuation.resume_from_step,
        )

    @patch(
        "validibot.validations.services.validation_run."
        "ValidationRunService.execute_workflow_steps"
    )
    def test_active_execution_rejects_a_concurrent_delivery(self, execute):
        """A second worker must retry later instead of entering workflow code."""
        continuation = ValidationRunContinuationFactory(
            state=ValidationContinuationState.EXECUTING,
            execution_started_at=timezone.now(),
            execution_token=None,
        )

        with pytest.raises(ValidationContinuationBusyError):
            execute_validation_run_continuation(continuation.pk)

        execute.assert_not_called()

    @patch(
        "validibot.validations.services.validation_run."
        "ValidationRunService.execute_workflow_steps",
        side_effect=OSError("worker lost storage"),
    )
    def test_failed_execution_releases_claim_for_transport_retry(self, execute):
        """Transient worker failure must not strand the continuation as EXECUTING."""
        continuation = ValidationRunContinuationFactory(
            state=ValidationContinuationState.DISPATCHED,
        )

        with pytest.raises(OSError, match="worker lost storage"):
            execute_validation_run_continuation(continuation.pk)

        continuation.refresh_from_db()
        assert continuation.state == ValidationContinuationState.DISPATCHED
        assert continuation.execution_token is None
        assert continuation.last_error_code == "continuation_execution_failed"
        execute.assert_called_once()

    @patch(
        "validibot.validations.services.validation_run."
        "ValidationRunService.execute_workflow_steps"
    )
    def test_completed_delivery_returns_current_run_without_reexecution(self, execute):
        """A queue replay after completion must become a side-effect-free read."""
        continuation = ValidationRunContinuationFactory(
            state=ValidationContinuationState.COMPLETED,
            completed_at=timezone.now(),
        )

        result = execute_validation_run_continuation(continuation.pk)

        assert result.run_id == continuation.validation_run_id
        assert result.status == continuation.validation_run.status
        execute.assert_not_called()


@pytest.mark.django_db
class TestContinuationRepair:
    """Keep abandoned producer and worker claims rediscoverable by the watchdog."""

    @patch("validibot.core.tasks.enqueue_validation_run", return_value="task-name")
    def test_repair_takes_over_a_stale_dispatch_claim(self, enqueue, settings):
        """A producer crash after claiming cannot permanently lose committed work."""
        settings.VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS = 60
        continuation = ValidationRunContinuationFactory(
            state=ValidationContinuationState.DISPATCHING,
            dispatch_started_at=timezone.now() - timedelta(minutes=2),
        )

        report = repair_validation_run_continuations(limit=10)

        continuation.refresh_from_db()
        assert report.examined == 1
        assert report.dispatched == 1
        assert continuation.state == ValidationContinuationState.DISPATCHED
        assert continuation.dispatch_attempts == 1
        enqueue.assert_called_once()

    @patch("validibot.core.tasks.enqueue_validation_run", return_value="task-name")
    def test_repair_redelivers_a_stale_execution_claim(self, enqueue, settings):
        """A worker crash remains recoverable through the same durable identity."""
        settings.VALIDATION_CONTINUATION_EXECUTION_STALE_SECONDS = 60
        continuation = ValidationRunContinuationFactory(
            state=ValidationContinuationState.EXECUTING,
            execution_started_at=timezone.now() - timedelta(minutes=2),
        )

        report = repair_validation_run_continuations(limit=10)

        continuation.refresh_from_db()
        assert report.dispatched == 1
        assert continuation.state == ValidationContinuationState.DISPATCHED
        assert continuation.execution_started_at is None
        enqueue.assert_called_once()

    @patch("validibot.core.tasks.enqueue_validation_run")
    def test_dry_run_reports_candidates_without_claiming_them(self, enqueue, settings):
        """Operators need a truthful preview that cannot mutate delivery state."""
        settings.VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS = 60
        continuation = ValidationRunContinuationFactory()

        report = repair_validation_run_continuations(limit=10, dry_run=True)

        continuation.refresh_from_db()
        assert report.examined == 1
        assert report.dispatched == 0
        assert continuation.state == ValidationContinuationState.PENDING
        assert continuation.dispatch_attempts == 0
        enqueue.assert_not_called()
