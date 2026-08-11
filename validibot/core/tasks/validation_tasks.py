"""
Celery tasks for validation execution.

This module defines the main validation execution task that is picked up
by Celery workers in Docker Compose deployments.

The task is dispatched by CeleryDispatcher and executes the validation
workflow synchronously within the worker process.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import OperationalError
from django.utils import timezone

from validibot.validations.services.validation_continuation import (
    ValidationContinuationBusyError,
)

logger = logging.getLogger(__name__)

# Exceptions that indicate transient failures worth retrying.
# Other exceptions (programming errors, business logic failures) should not retry.
RETRYABLE_EXCEPTIONS = (
    OperationalError,  # Database connection issues
    ConnectionError,  # Network issues
    TimeoutError,  # Timeouts
    OSError,  # File system / network issues
)

# Generic error message shown to users when a task-level failure occurs.
TASK_FAILURE_ERROR = (
    "A system error prevented the validation from completing. "
    "Please try again in a few minutes."
)


@shared_task(
    bind=True,
    name="validibot.execute_validation_run",
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_validation_run_task(
    self,
    validation_run_id: str,
    user_id: int | None,
    resume_from_step: int | None = None,
    continuation_id: str | None = None,
) -> None:
    """
    Celery task to execute a validation run.

    This task is picked up by Celery workers and executes the validation
    workflow. It wraps ValidationRunService.execute_workflow_steps().

    Args:
        self: Celery task instance (bound task).
        validation_run_id: ID of the ValidationRun to execute.
        user_id: ID of the user who initiated the run, if one exists.
        resume_from_step: Step order to resume from (None for initial execution).
        continuation_id: Durable continuation identity for callback-driven resumes.
    """
    from validibot.validations.services.validation_run import ValidationRunService

    logger.info(
        "Celery task: executing validation_run_id=%s user_id=%s "
        "resume_from_step=%s task_id=%s",
        validation_run_id,
        user_id,
        resume_from_step,
        self.request.id,
    )

    try:
        if continuation_id is not None:
            from validibot.validations.services.validation_continuation import (
                execute_validation_run_continuation,
            )

            execute_validation_run_continuation(continuation_id)
        else:
            service = ValidationRunService()
            service.execute_workflow_steps(
                validation_run_id=validation_run_id,
                user_id=user_id,
                resume_from_step=resume_from_step,
            )

        logger.info(
            "Celery task: completed validation_run_id=%s task_id=%s",
            validation_run_id,
            self.request.id,
        )
    except ValidationContinuationBusyError as exc:
        logger.info(
            "Celery task: continuation already executing; retrying "
            "validation_run_id=%s continuation_id=%s",
            validation_run_id,
            continuation_id,
        )
        raise self.retry(exc=exc, countdown=5) from exc
    except RETRYABLE_EXCEPTIONS as exc:
        # Transient infrastructure errors are safe to retry. A durable
        # continuation releases its execution claim before reaching here.
        logger.exception(
            "Celery task: transient error (will retry) validation_run_id=%s "
            "task_id=%s retry=%s/%s error_type=%s",
            validation_run_id,
            self.request.id,
            self.request.retries,
            self.max_retries,
            type(exc).__name__,
        )
        raise self.retry(exc=exc) from exc
    except Exception as exc:
        # Permanent errors - mark ValidationRun as FAILED and don't retry.
        # This ensures users see a final status even if the error occurred
        # outside of execute_workflow_steps (e.g., ValidationRun not found).
        logger.exception(
            "Celery task: permanent failure (no retry) validation_run_id=%s "
            "task_id=%s error_type=%s",
            validation_run_id,
            self.request.id,
            type(exc).__name__,
        )
        _mark_validation_run_failed(
            validation_run_id=validation_run_id,
            error_message=TASK_FAILURE_ERROR,
            exception=exc,
        )
        # Re-raise so Celery marks the task as failed
        raise


def _mark_validation_run_failed(
    validation_run_id: str,
    error_message: str,
    exception: Exception,
) -> None:
    """
    Mark a ValidationRun as FAILED due to a task-level error.

    This is called when an error occurs outside of ValidationRunService
    (e.g., ValidationRun not found, database errors). It ensures the user
    sees a final FAILED status rather than being stuck in PENDING/RUNNING.

    This function catches all exceptions to ensure the calling code can
    still re-raise the original exception.
    """
    from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
    from validibot.validations.constants import ValidationRunErrorCategory
    from validibot.validations.constants import ValidationRunStatus
    from validibot.validations.models import ValidationRun

    try:
        run = ValidationRun.objects.filter(id=validation_run_id).first()
        if not run:
            logger.warning(
                "Cannot mark ValidationRun as failed - not found: %s",
                validation_run_id,
            )
            return

        # Only update if not already in a terminal state. Use the
        # canonical set so TIMED_OUT (and any future terminal states)
        # are correctly recognised — without this, a timed-out run
        # would be silently re-marked as FAILED, losing its real
        # outcome.
        if run.status in VALIDATION_RUN_TERMINAL_STATUSES:
            logger.info(
                "ValidationRun already in terminal state %s, not updating: %s",
                run.status,
                validation_run_id,
            )
            return

        run.status = ValidationRunStatus.FAILED
        run.error = error_message
        run.error_category = ValidationRunErrorCategory.SYSTEM_ERROR
        run.ended_at = timezone.now()
        run.save(update_fields=["status", "error", "error_category", "ended_at"])

        logger.info(
            "Marked ValidationRun as FAILED due to task error: %s",
            validation_run_id,
        )
    except Exception:
        # Don't let this helper cause additional failures - just log
        logger.exception(
            "Failed to mark ValidationRun as FAILED: %s",
            validation_run_id,
        )
