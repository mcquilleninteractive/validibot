"""
Task dispatch for validation run execution.

This module provides the main entry point for dispatching validation run
execution tasks. It wraps the TaskDispatcher abstraction with a simple
function interface for backward compatibility.

For the underlying dispatcher architecture, see `validibot.core.tasks.dispatch`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from validibot.core.tasks.dispatch import TaskDispatchRequest
from validibot.core.tasks.dispatch import get_task_dispatcher

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)


def enqueue_validation_run(
    validation_run_id: UUID | str,
    user_id: int | None,
    resume_from_step: int | None = None,
    continuation_id: UUID | str | None = None,
    task_id: str | None = None,
) -> str | None:
    """
    Dispatch a validation run execution task.

    Routes the task to the appropriate backend based on the deployment environment.
    This is the main entry point for starting validation execution.

    The underlying dispatcher is selected automatically based on environment:
    - Test: Execute synchronously inline
    - Local dev: Call worker via HTTP
    - Docker Compose: Enqueue via Celery
    - Google Cloud: Enqueue via Cloud Tasks

    Args:
        validation_run_id: ID of the ValidationRun to execute.
        user_id: ID of the user who initiated the run, if one exists.
        resume_from_step: Step order of the last completed step. The
            orchestrator uses ``order__gt`` to skip it and start from the
            next one. None for initial execution.
        continuation_id: Durable continuation authorizing a callback resume.
        task_id: Optional deterministic queue identity for idempotent dispatch.

    Returns:
        Task identifier if applicable, None for sync execution.

    Raises:
        RuntimeError: If task dispatch fails.
    """
    request = TaskDispatchRequest(
        validation_run_id=validation_run_id,
        user_id=user_id,
        resume_from_step=resume_from_step,
        continuation_id=continuation_id,
        task_id=task_id,
    )

    dispatcher = get_task_dispatcher()

    logging.debug(
        f"Dispatching validation run {validation_run_id} "
        f"using '{dispatcher.dispatcher_name}' dispatcher"
    )
    response = dispatcher.dispatch(request)

    if response.error:
        raise RuntimeError(f"Task dispatch failed: {response.error}")

    return response.task_id
