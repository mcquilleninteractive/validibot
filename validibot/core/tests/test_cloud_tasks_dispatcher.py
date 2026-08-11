"""Tests for security and delivery bounds in the Cloud Tasks dispatcher.

Cloud Tasks authenticates the worker request with an explicit service account
and must stop waiting well before its platform maximum. These tests keep both
configuration failures visible rather than relying on provider defaults.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test.utils import override_settings

from validibot.core.tasks.dispatch import TaskDispatchRequest
from validibot.core.tasks.dispatch.google_cloud_tasks import GoogleCloudTasksDispatcher

TEST_DISPATCH_DEADLINE_SECONDS = 420


def test_get_invoker_service_account_uses_explicit_setting():
    """Use the explicit CLOUD_TASKS_SERVICE_ACCOUNT when configured."""
    dispatcher = GoogleCloudTasksDispatcher()

    with override_settings(
        CLOUD_TASKS_SERVICE_ACCOUNT="invoker@example.com",
    ):
        assert dispatcher._get_invoker_service_account() == "invoker@example.com"


def test_get_invoker_service_account_requires_setting():
    """Raise a clear error when CLOUD_TASKS_SERVICE_ACCOUNT is not set."""
    dispatcher = GoogleCloudTasksDispatcher()

    with (
        override_settings(CLOUD_TASKS_SERVICE_ACCOUNT=""),
        pytest.raises(
            ValueError,
            match="CLOUD_TASKS_SERVICE_ACCOUNT must be set",
        ),
    ):
        dispatcher._get_invoker_service_account()


def test_dispatch_deadline_uses_explicit_setting():
    """Deployments may tune the short HTTP orchestration request deliberately."""
    with override_settings(
        CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS=TEST_DISPATCH_DEADLINE_SECONDS
    ):
        assert (
            GoogleCloudTasksDispatcher._get_dispatch_deadline_seconds()
            == TEST_DISPATCH_DEADLINE_SECONDS
        )


@pytest.mark.parametrize("deadline", [14, 1801])
def test_dispatch_deadline_rejects_values_outside_cloud_tasks_bounds(deadline):
    """Invalid queue settings should fail locally instead of during delivery."""
    with (
        override_settings(CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS=deadline),
        pytest.raises(ValueError, match="between 15 and 1800"),
    ):
        GoogleCloudTasksDispatcher._get_dispatch_deadline_seconds()


@patch(
    "validibot.core.tasks.dispatch.google_cloud_tasks.create_http_task",
    return_value=SimpleNamespace(task_name="queues/q/tasks/durable"),
)
def test_durable_continuation_supplies_deterministic_cloud_task_name(create_task):
    """Ambiguous producer retries must converge on the same provider task."""
    request = TaskDispatchRequest(
        validation_run_id="run-1",
        user_id=None,
        resume_from_step=20,
        continuation_id="continuation-1",
        task_id="validation-continuation-stable",
    )

    with override_settings(
        GCP_PROJECT_ID="project",
        GCP_REGION="region",
        GCS_TASK_QUEUE_NAME="queue",
        WORKER_URL="https://worker.example",
        CLOUD_TASKS_SERVICE_ACCOUNT="invoker@example.com",
    ):
        response = GoogleCloudTasksDispatcher().dispatch(request)

    assert response.error is None
    assert response.task_id == "queues/q/tasks/durable"
    assert create_task.call_args.kwargs["task_id"] == request.task_id
    assert create_task.call_args.kwargs["payload"]["continuation_id"] == (
        request.continuation_id
    )
