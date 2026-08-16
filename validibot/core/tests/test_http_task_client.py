"""Tests for the shared low-level Cloud Tasks HTTP transport.

The worker and validator backend queues intentionally keep separate policy. These
tests cover only the reusable transport guarantee: exact OIDC/deadline fields,
deterministic task names, and ``AlreadyExists`` convergence without another
identity.
"""

import json
from unittest.mock import patch

import pytest
from google.api_core.exceptions import AlreadyExists

from validibot.core.tasks.dispatch.http_task_client import (
    clear_cloud_tasks_client_cache,
)
from validibot.core.tasks.dispatch.http_task_client import create_http_task

PROJECT_ID = "validibot-prod"
REGION = "australia-southeast1"
QUEUE_NAME = "validator-provider-prod"
TASK_ID = "11111111-1111-4111-8111-111111111111"
DEADLINE_SECONDS = 1800
EXPECTED_DISPATCH_COUNT = 2


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """Keep patched client constructors isolated between transport tests."""
    clear_cloud_tasks_client_cache()
    yield
    clear_cloud_tasks_client_cache()


@patch("google.cloud.tasks_v2.CloudTasksClient")
def test_deterministic_http_task_contains_exact_transport_policy(client_class):
    """The provider delivery must carry its stable identity, OIDC, and deadline."""
    expected_name = (
        f"projects/{PROJECT_ID}/locations/{REGION}/queues/{QUEUE_NAME}/tasks/{TASK_ID}"
    )
    client_class.return_value.create_task.return_value.name = expected_name

    result = create_http_task(
        project_id=PROJECT_ID,
        region=REGION,
        queue_name=QUEUE_NAME,
        task_id=TASK_ID,
        endpoint_url="https://validator.example/v1/execute",
        payload={"attempt_id": TASK_ID},
        oidc_service_account="invoker@validibot-prod.iam.gserviceaccount.com",
        oidc_audience="https://validator.example",
        dispatch_deadline_seconds=DEADLINE_SECONDS,
    )

    assert result.task_name == expected_name
    assert result.created is True
    request = client_class.return_value.create_task.call_args.kwargs["request"]
    assert request.task.name == expected_name
    assert request.task.dispatch_deadline.seconds == DEADLINE_SECONDS
    assert json.loads(request.task.http_request.body) == {"attempt_id": TASK_ID}
    assert request.task.http_request.oidc_token.audience == (
        "https://validator.example"
    )


@patch("google.cloud.tasks_v2.CloudTasksClient")
def test_already_exists_returns_same_deterministic_task_identity(client_class):
    """A duplicate application delivery must bind to the original provider task."""
    client_class.return_value.create_task.side_effect = AlreadyExists("duplicate")

    result = create_http_task(
        project_id=PROJECT_ID,
        region=REGION,
        queue_name=QUEUE_NAME,
        task_id=TASK_ID,
        endpoint_url="https://validator.example/v1/execute",
        payload={"attempt_id": TASK_ID},
        oidc_service_account="invoker@validibot-prod.iam.gserviceaccount.com",
        oidc_audience="https://validator.example",
        dispatch_deadline_seconds=DEADLINE_SECONDS,
    )

    assert result.created is False
    assert result.task_name.endswith(f"/tasks/{TASK_ID}")


@patch("google.cloud.tasks_v2.CloudTasksClient")
def test_http_task_dispatch_reuses_one_process_client(client_class):
    """Repeated dispatches should share one channel and credential cache."""
    client_class.return_value.create_task.return_value.name = "provider-task"
    common = {
        "project_id": PROJECT_ID,
        "region": REGION,
        "queue_name": QUEUE_NAME,
        "endpoint_url": "https://validator.example/v1/execute",
        "payload": {"attempt_id": TASK_ID},
        "oidc_service_account": ("invoker@validibot-prod.iam.gserviceaccount.com"),
        "oidc_audience": "https://validator.example",
        "dispatch_deadline_seconds": DEADLINE_SECONDS,
    }

    create_http_task(**common)
    create_http_task(**common)

    client_class.assert_called_once_with()
    assert client_class.return_value.create_task.call_count == EXPECTED_DISPATCH_COUNT
