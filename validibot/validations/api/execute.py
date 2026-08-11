"""
API endpoint for executing validation runs.

This endpoint is called by task dispatchers to process validation runs on the
worker instance. It receives a validation_run_id and optional resume_from_step,
then delegates to ValidationRunService.execute_workflow_steps().

Architecture varies by deployment target:

    Local Dev:
        HTTP POST (direct) -> This View -> ValidationRunService.execute_workflow_steps()

    Docker Compose:
        Celery Worker -> HTTP POST -> This View -> ValidationRunService.
            execute_workflow_steps()

    Google Cloud:
        Cloud Tasks -> Cloud Run Worker -> This View -> ValidationRunService.
            execute_workflow_steps()
        (Authentication via Cloud Run IAM with OIDC tokens)

    AWS:
        TBD (not yet implemented)

The WorkerOnlyAPIView base class ensures this endpoint is only accessible
on worker instances, not on the public-facing API server.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response

from validibot.core.api.worker import WorkerOnlyAPIView
from validibot.validations.services.validation_continuation import (
    ValidationContinuationBusyError,
)
from validibot.validations.services.validation_continuation import (
    execute_validation_run_continuation,
)
from validibot.validations.services.validation_run import ValidationRunService

logger = logging.getLogger(__name__)


class ExecuteValidationRunView(WorkerOnlyAPIView):
    """
    Execute a validation run from a task dispatch.

    This endpoint is the worker-side receiver for validation run execution tasks.
    It's called by the task dispatcher (HTTP, Celery, Cloud Tasks, etc.) with
    a JSON payload containing:
    - validation_run_id: ID of the ValidationRun to execute
    - user_id: ID of the user who initiated the run
    - resume_from_step: (optional) Step order to resume from

    Authentication:
        - Google Cloud: Cloud Run IAM performs authentication via OIDC token
        - Docker Compose: Worker-only access enforced by WorkerOnlyAPIView
        - Local dev: No authentication (direct HTTP calls)

    Error Handling:
        Returns 200 OK for all business logic outcomes (success, failure,
        idempotent skip) so that task queues don't retry. Returns 500 for
        infrastructure errors (database connection, etc.) which should
        trigger a retry.

    URL: POST /api/v1/execute-validation-run/
    """

    def post(self, request):
        """
        Process a validation run execution task.

        Request body (JSON):
            {
                "validation_run_id": "00000000-0000-0000-0000-000000000000",
                "user_id": 456,
                "resume_from_step": null  // or int for resume
            }

        Returns:
            200 OK: Task completed (validation succeeded, failed, or was skipped)
            500 Internal Server Error: Infrastructure error (triggers task retry)
        """
        # Parse request data
        validation_run_id = request.data.get("validation_run_id")
        user_id = request.data.get("user_id")
        resume_from_step = request.data.get("resume_from_step")
        continuation_id = request.data.get("continuation_id")

        if not validation_run_id:
            logger.error("Missing validation_run_id in request")
            return Response(
                {"error": "validation_run_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.info(
            "Received execute-validation-run task: validation_run_id=%s "
            "user_id=%s resume_from_step=%s",
            validation_run_id,
            user_id,
            resume_from_step,
        )

        try:
            if continuation_id is not None:
                result = execute_validation_run_continuation(continuation_id)
            else:
                service = ValidationRunService()
                result = service.execute_workflow_steps(
                    validation_run_id=validation_run_id,
                    user_id=user_id,
                    resume_from_step=resume_from_step,
                )

            logger.info(
                "Validation run %s execution completed: status=%s",
                validation_run_id,
                result.status,
            )

            # Always return 200 for business logic outcomes
            # This tells Cloud Tasks the task was processed successfully
            return Response(
                {
                    "validation_run_id": validation_run_id,
                    "status": result.status,
                    "error": result.error or "",
                },
                status=status.HTTP_200_OK,
            )

        except ValidationContinuationBusyError:
            logger.info(
                "Validation continuation %s already has an active worker",
                continuation_id,
            )
            return Response(
                {
                    "validation_run_id": validation_run_id,
                    "error": "continuation is already executing",
                },
                status=status.HTTP_409_CONFLICT,
            )
        except Exception:
            # Log the exception and return 500 to trigger task retry
            logger.exception(
                "Failed to execute validation run %s",
                validation_run_id,
            )
            return Response(
                {
                    "validation_run_id": validation_run_id,
                    # Don't leak exception text. ``logger.exception``
                    # above captures the traceback; this endpoint is
                    # called by Cloud Tasks, which only consumes the
                    # status code.
                    "error": "internal error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
