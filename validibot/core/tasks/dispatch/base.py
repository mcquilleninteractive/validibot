"""
Base class for task dispatchers.

This module defines the abstract interface that all task dispatchers must implement.
The interface supports multiple deployment targets for enqueueing validation work.

## Design Principles

1. **Encapsulate dispatch mechanism**: Dispatchers handle the full lifecycle of
   creating and submitting tasks. Callers don't need to know implementation details.

2. **Clear sync vs async distinction**: Some dispatchers (test, local dev) run
   tasks synchronously, while others (Celery, Cloud Tasks) are truly async.

3. **Platform-agnostic**: Each dispatcher uses its own task queue mechanism
   internally. The interface works with simple request objects.

4. **Follows existing patterns**: Mirrors the ExecutionBackend pattern in
   validations/services/execution/ for consistency.
"""

from __future__ import annotations

import logging
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class TaskDispatchRequest:
    """
    Request to dispatch a validation run task.

    Contains all the information needed to enqueue a validation run for
    execution, regardless of which dispatcher is used.
    """

    validation_run_id: UUID | str
    """ID of the ValidationRun to execute."""

    user_id: int | None
    """ID of the initiating user, or None for a system-created run."""

    resume_from_step: int | None = None
    """Step order of the last completed step (None for initial execution).

    Despite the name, this is NOT the step to start from — the orchestrator
    uses ``order__gt`` to skip this step and begin with the next one. The
    value is always a real WorkflowStep.order from the database, never a
    fabricated ``order + 1``.
    """

    continuation_id: UUID | str | None = None
    """Durable continuation record authorizing this resume, when applicable."""

    task_id: str | None = None
    """Optional deterministic transport identity supplied by durable work."""

    def to_payload(self) -> dict:
        """Convert to JSON-serializable payload for task queues."""
        return {
            "validation_run_id": str(self.validation_run_id),
            "user_id": self.user_id,
            "resume_from_step": self.resume_from_step,
            "continuation_id": (
                str(self.continuation_id) if self.continuation_id is not None else None
            ),
        }


@dataclass
class TaskDispatchResponse:
    """
    Response from dispatching a task.

    For synchronous dispatchers, the task has already completed.
    For asynchronous dispatchers, contains the task identifier for tracking.
    """

    task_id: str | None
    """
    Identifier for the dispatched task.

    - Cloud Tasks: full task resource name
    - Celery: task ID (UUID)
    - Test/Local: None (no external task created)
    """

    is_sync: bool
    """Whether the task was executed synchronously (already complete)."""

    error: str | None = None
    """Error message if dispatch failed."""


class TaskDispatcher(ABC):
    """
    Abstract base class for task dispatchers.

    A task dispatcher handles enqueueing validation run tasks to the appropriate
    backend for a given deployment target:

    - Test: synchronous inline execution
    - Local dev: direct HTTP call to worker
    - Docker Compose: Celery with Redis broker
    - Google Cloud: Cloud Tasks queue
    - AWS: TBD (SQS, Step Functions, etc.)

    ## Implementation Notes

    Subclasses must implement:
    - `dispatcher_name`: Human-readable name
    - `is_sync`: Property indicating if execution is synchronous
    - `dispatch()`: Main dispatch method
    - `is_available()`: Check if dispatcher is ready

    The dispatch() method should not raise exceptions for transient failures;
    instead, return a TaskDispatchResponse with the error field set.
    """

    @property
    @abstractmethod
    def dispatcher_name(self) -> str:
        """Human-readable name for this dispatcher (e.g., 'cloud_tasks', 'celery')."""

    @property
    @abstractmethod
    def is_sync(self) -> bool:
        """
        Whether this dispatcher executes tasks synchronously.

        Returns:
            True if dispatch() blocks until task completes.
            False if dispatch() returns immediately with a task ID.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if this dispatcher is available and ready for use.

        Returns:
            True if the dispatcher can accept tasks.
        """

    @abstractmethod
    def dispatch(self, request: TaskDispatchRequest) -> TaskDispatchResponse:
        """
        Dispatch a validation run task.

        This is the main entry point for enqueueing work. The method:
        1. Prepares the task payload
        2. Submits to the appropriate queue/executor
        3. Returns a response with task ID or error

        Args:
            request: Task dispatch request.

        Returns:
            TaskDispatchResponse with task ID (async) or completion status (sync).

        Note:
            This method should not raise exceptions for dispatch failures.
            Instead, return a response with the error field populated.
            This allows callers to handle failures gracefully.
        """
