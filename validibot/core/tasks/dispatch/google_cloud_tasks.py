"""
Google Cloud Tasks dispatcher.

Enqueues validation tasks to a Cloud Tasks queue, which delivers them to
the Cloud Run worker service via HTTP with OIDC authentication.
"""

from __future__ import annotations

import logging

from django.conf import settings

from validibot.core.tasks.dispatch.base import TaskDispatcher
from validibot.core.tasks.dispatch.base import TaskDispatchRequest
from validibot.core.tasks.dispatch.base import TaskDispatchResponse
from validibot.core.tasks.dispatch.http_task_client import create_http_task

logger = logging.getLogger(__name__)

MIN_DISPATCH_DEADLINE_SECONDS = 15
MAX_DISPATCH_DEADLINE_SECONDS = 30 * 60


class GoogleCloudTasksDispatcher(TaskDispatcher):
    """
    Google Cloud Tasks dispatcher - async task queue.

    Creates tasks that POST to the worker's execute-validation-run endpoint
    with OIDC authentication. The worker is typically a Cloud Run service.

    Required settings:
    - GCP_PROJECT_ID: Google Cloud project ID
    - GCS_TASK_QUEUE_NAME: Cloud Tasks queue name
    - WORKER_URL: Worker service URL (Cloud Run URL)

    Optional settings:
    - GCP_REGION: Region for Cloud Tasks (default: us-west1)
    - CLOUD_TASKS_SERVICE_ACCOUNT: Service account for OIDC token
    """

    @property
    def dispatcher_name(self) -> str:
        return "cloud_tasks"

    @property
    def is_sync(self) -> bool:
        return False

    def is_available(self) -> bool:
        """Check if required settings are configured."""
        return all(
            [
                getattr(settings, "GCP_PROJECT_ID", None),
                getattr(settings, "GCS_TASK_QUEUE_NAME", None),
                getattr(settings, "WORKER_URL", None),
            ]
        )

    def dispatch(self, request: TaskDispatchRequest) -> TaskDispatchResponse:
        """Enqueue task via Google Cloud Tasks."""
        # Validate configuration
        project_id = getattr(settings, "GCP_PROJECT_ID", "")
        queue_name = getattr(settings, "GCS_TASK_QUEUE_NAME", "")
        worker_url = getattr(settings, "WORKER_URL", "")

        if not project_id:
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error="GCP_PROJECT_ID must be set for Cloud Tasks",
            )
        if not queue_name:
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error="GCS_TASK_QUEUE_NAME must be set for Cloud Tasks",
            )
        if not worker_url:
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error="WORKER_URL must be set for Cloud Tasks",
            )

        # Build the full queue path
        region = getattr(settings, "GCP_REGION", "us-west1")
        queue_path = f"projects/{project_id}/locations/{region}/queues/{queue_name}"

        logger.info(
            "Cloud Tasks config: project_id=%s region=%s queue_name=%s queue_path=%s",
            project_id,
            region,
            queue_name,
            queue_path,
        )

        # Human-readable identifier for logging only. Ordinary run launches let
        # Cloud Tasks allocate the name so an explicit later retry remains
        # possible. Durable continuations supply ``request.task_id`` because
        # repeated dispatch of the *same committed work row* must converge.
        if request.resume_from_step is not None:
            task_name = (
                f"validation-run-{request.validation_run_id}"
                f"-step-{request.resume_from_step}"
            )
        else:
            task_name = f"validation-run-{request.validation_run_id}"

        # Build the task URL and payload
        endpoint_url = f"{worker_url.rstrip('/')}/api/v1/execute-validation-run/"
        payload = request.to_payload()

        # Get the service account for OIDC authentication
        service_account = self._get_invoker_service_account()

        logger.info(
            "Enqueueing Cloud Task: queue=%s task_name=%s validation_run_id=%s "
            "resume_from_step=%s",
            queue_name,
            task_name,
            request.validation_run_id,
            request.resume_from_step,
        )

        try:
            created_task = create_http_task(
                project_id=project_id,
                region=region,
                queue_name=queue_name,
                endpoint_url=endpoint_url,
                payload=payload,
                oidc_service_account=service_account,
                oidc_audience=worker_url,
                dispatch_deadline_seconds=self._get_dispatch_deadline_seconds(),
                task_id=request.task_id,
            )
            logger.info(
                "Cloud Task created: %s for validation_run_id=%s",
                created_task.task_name,
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=created_task.task_name,
                is_sync=False,
            )

        except Exception as exc:
            logger.exception(
                "Cloud Tasks dispatcher: failed to create task for "
                "validation_run_id=%s",
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error=str(exc),
            )

    def _get_invoker_service_account(self) -> str:
        """
        Get the service account email to use for OIDC token.

        This should be the service account that has roles/run.invoker
        permission on the worker Cloud Run service.
        """
        service_account = getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", "")
        if not service_account:
            raise ValueError(
                "CLOUD_TASKS_SERVICE_ACCOUNT must be set for Cloud Tasks dispatch. "
                "Set it to the Cloud Run service account email "
                "(e.g. validibot-cloudrun-prod@PROJECT.iam.gserviceaccount.com).",
            )
        return service_account

    @staticmethod
    def _get_dispatch_deadline_seconds() -> int:
        """Return a valid Cloud Tasks HTTP deadline for orchestration work."""
        deadline = int(getattr(settings, "CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS", 600))
        if not (
            MIN_DISPATCH_DEADLINE_SECONDS <= deadline <= MAX_DISPATCH_DEADLINE_SECONDS
        ):
            raise ValueError(
                "CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS must be between "
                "15 and 1800 seconds."
            )
        return deadline
