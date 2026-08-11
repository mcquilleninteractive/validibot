"""
Celery task dispatcher for Docker Compose deployments.

Enqueues validation tasks to a Celery queue with Redis broker.
Workers running `celery -A config worker` process the tasks.

This is the primary dispatcher for Docker Compose production deployments.
"""

from __future__ import annotations

import logging
import uuid

from celery import current_app
from django.conf import settings
from django.db import transaction

from validibot.core.tasks.dispatch.base import TaskDispatcher
from validibot.core.tasks.dispatch.base import TaskDispatchRequest
from validibot.core.tasks.dispatch.base import TaskDispatchResponse

# Import the task from the centralized tasks module.
# This allows the dispatcher to reference the task without defining it here,
# keeping task definitions in one place for Celery autodiscovery.
from validibot.core.tasks.validation_tasks import execute_validation_run_task

logger = logging.getLogger(__name__)


class CeleryDispatcher(TaskDispatcher):
    """
    Celery dispatcher - async task queue with Redis broker.

    Enqueues validation tasks to the Celery broker (Redis).
    Workers process tasks from the queue asynchronously.

    This is the primary dispatcher for Docker Compose production deployments.

    Required settings:
    - CELERY_BROKER_URL must be configured
    - django_celery_beat must be in INSTALLED_APPS (for periodic tasks)
    """

    @property
    def dispatcher_name(self) -> str:
        return "celery"

    @property
    def is_sync(self) -> bool:
        return False

    def is_available(self) -> bool:
        """Check if Celery is configured and broker is reachable."""
        # Check if django_celery_beat is in INSTALLED_APPS
        if "django_celery_beat" not in settings.INSTALLED_APPS:
            return False

        # Check if broker URL is configured
        return bool(getattr(settings, "CELERY_BROKER_URL", None))

    def dispatch(self, request: TaskDispatchRequest) -> TaskDispatchResponse:
        """Enqueue task via Celery."""
        if not self.is_available():
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error=(
                    "Celery not configured "
                    "(django_celery_beat not installed or broker not set)"
                ),
            )

        logger.info(
            "Celery dispatcher: enqueueing validation_run_id=%s user_id=%s",
            request.validation_run_id,
            request.user_id,
        )

        try:
            # Use delay_on_commit to ensure the task is only sent after the
            # current database transaction commits. This prevents race conditions
            # where the worker tries to fetch a ValidationRun that doesn't exist yet.
            # Requires Celery 5.4+
            #
            # If not in a transaction (autocommit mode), this behaves like .delay()
            task_kwargs = {
                "validation_run_id": str(request.validation_run_id),
                "user_id": request.user_id,
                "resume_from_step": request.resume_from_step,
                "continuation_id": (
                    str(request.continuation_id)
                    if request.continuation_id is not None
                    else None
                ),
            }

            # Generate a deterministic task ID upfront so we can return it
            # even when deferring task dispatch until transaction commit.
            # This allows callers to track the task regardless of timing.
            task_id = request.task_id or (
                f"task-{request.validation_run_id}-{uuid.uuid4().hex[:8]}"
            )

            # Check if we're in eager mode (tests)
            if current_app.conf.task_always_eager:
                # In eager mode, delay_on_commit doesn't work properly,
                # so use regular delay
                execute_validation_run_task.apply_async(
                    kwargs=task_kwargs,
                    task_id=task_id,
                )
            elif transaction.get_connection().in_atomic_block:
                # We're in a transaction - use on_commit to defer sending.
                # We pass the pre-generated task_id so the returned ID is valid.
                def send_task():
                    execute_validation_run_task.apply_async(
                        kwargs=task_kwargs,
                        task_id=task_id,
                    )

                transaction.on_commit(send_task)

                logger.info(
                    "Celery dispatcher: task deferred until transaction commit "
                    "task_id=%s validation_run_id=%s",
                    task_id,
                    request.validation_run_id,
                )

                return TaskDispatchResponse(
                    task_id=task_id,
                    is_sync=False,
                )
            else:
                # Not in a transaction - send immediately
                execute_validation_run_task.apply_async(
                    kwargs=task_kwargs,
                    task_id=task_id,
                )

            logger.info(
                "Celery dispatcher: enqueued task_id=%s for validation_run_id=%s",
                task_id,
                request.validation_run_id,
            )

            return TaskDispatchResponse(
                task_id=task_id,
                is_sync=False,
            )

        except Exception as exc:
            logger.exception(
                "Celery dispatcher: failed to enqueue validation_run_id=%s",
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error=str(exc),
            )
