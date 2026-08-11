"""Tests for durable continuation delivery through the Celery dispatcher.

Celery does not provide queue-side deduplication equivalent to Cloud Tasks, so
the durable database claim remains authoritative. The transport must still
preserve the continuation identity and stable task ID on every delivery.
"""

from unittest.mock import patch

from validibot.core.tasks.dispatch import TaskDispatchRequest
from validibot.core.tasks.dispatch.celery_dispatcher import CeleryDispatcher


@patch(
    "validibot.core.tasks.dispatch.celery_dispatcher."
    "execute_validation_run_task.apply_async"
)
def test_celery_preserves_continuation_and_task_id(apply_async):
    """A Celery worker must receive the same durable identity the producer claimed."""
    dispatcher = CeleryDispatcher()
    request = TaskDispatchRequest(
        validation_run_id="run-1",
        user_id=None,
        resume_from_step=20,
        continuation_id="continuation-1",
        task_id="validation-continuation-stable",
    )

    with patch.object(dispatcher, "is_available", return_value=True):
        response = dispatcher.dispatch(request)

    assert response.error is None
    assert response.task_id == request.task_id
    apply_async.assert_called_once_with(
        kwargs={
            "validation_run_id": "run-1",
            "user_id": None,
            "resume_from_step": 20,
            "continuation_id": "continuation-1",
        },
        task_id=request.task_id,
    )
