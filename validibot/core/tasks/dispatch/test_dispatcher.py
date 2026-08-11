"""
Test environment task dispatcher.

Executes validation tasks synchronously inline, bypassing task queues and HTTP.
This is the expected behavior for tests - immediate execution in the same process.
"""

from __future__ import annotations

import logging

from validibot.core.tasks.dispatch.base import TaskDispatcher
from validibot.core.tasks.dispatch.base import TaskDispatchRequest
from validibot.core.tasks.dispatch.base import TaskDispatchResponse

logger = logging.getLogger(__name__)


class TestDispatcher(TaskDispatcher):
    """
    Test environment dispatcher - synchronous inline execution.

    Bypasses task queues and HTTP entirely, calling execute_workflow_steps()
    directly. This is the behavior tests expect - synchronous execution within
    the same process, without needing to mock HTTP calls or task queues.
    """

    @property
    def dispatcher_name(self) -> str:
        return "test"

    @property
    def is_sync(self) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def dispatch(self, request: TaskDispatchRequest) -> TaskDispatchResponse:
        """Execute validation run synchronously inline."""
        from validibot.validations.services.validation_run import ValidationRunService

        logger.info(
            "Test dispatcher: executing synchronously for validation_run_id=%s",
            request.validation_run_id,
        )

        try:
            if request.continuation_id is not None:
                from validibot.validations.services.validation_continuation import (
                    execute_validation_run_continuation,
                )

                execute_validation_run_continuation(request.continuation_id)
            else:
                service = ValidationRunService()
                service.execute_workflow_steps(
                    validation_run_id=str(request.validation_run_id),
                    user_id=request.user_id,
                    resume_from_step=request.resume_from_step,
                )
            return TaskDispatchResponse(task_id=None, is_sync=True)
        except Exception as exc:
            logger.exception(
                "Test dispatcher: execution failed for validation_run_id=%s",
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=None,
                is_sync=True,
                error=str(exc),
            )
