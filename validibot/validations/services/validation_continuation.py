"""Durable delivery and execution of validation-run continuations.

An asynchronous validator callback can complete one workflow step while later
steps still remain. The decision to resume is domain state, so it is committed
to PostgreSQL with the callback result before any queue is contacted. Celery or
Cloud Tasks then provides at-least-once transport for that durable decision.

This module deliberately implements one operation-specific work record. It is
not a generic event bus: the payload, idempotency key, legal transitions, repair
query, and execution semantics all belong to validation-run continuation.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from validibot.core.textsafety import sanitize_plain_text
from validibot.validations.constants import ValidationContinuationState
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRunContinuation
from validibot.validations.services.models import ValidationRunTaskResult

if TYPE_CHECKING:
    from uuid import UUID

    from validibot.validations.models import CallbackReceipt
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun

logger = logging.getLogger(__name__)

DEFAULT_DISPATCH_STALE_SECONDS = 5 * 60
DEFAULT_EXECUTION_STALE_SECONDS = 35 * 60
MAX_STORED_ERROR_LENGTH = 2000


class ContinuationDispatchOutcome(StrEnum):
    """Bounded result of one continuation dispatch attempt."""

    DISPATCHED = "dispatched"
    ALREADY_DISPATCHED = "already_dispatched"
    BUSY = "busy"
    NOT_REQUIRED = "not_required"
    FAILED = "failed"


class ValidationContinuationBusyError(RuntimeError):
    """A live worker still owns the continuation execution claim."""


@dataclass(frozen=True, slots=True)
class _DispatchClaim:
    """Immutable data needed after releasing the dispatch row lock."""

    continuation_id: UUID
    token: UUID
    validation_run_id: UUID
    user_id: int | None
    resume_from_step: int
    task_id: str


@dataclass(frozen=True, slots=True)
class _ExecutionClaim:
    """Immutable workflow arguments protected by an execution token."""

    continuation_id: UUID
    token: UUID
    validation_run_id: UUID
    user_id: int | None
    resume_from_step: int


@dataclass(frozen=True, slots=True)
class ContinuationRepairReport:
    """Operator-facing summary from one bounded continuation repair pass."""

    examined: int
    dispatched: int
    already_dispatched: int
    busy: int
    not_required: int
    failed: int


def _bounded_error(exc: BaseException) -> str:
    """Return safe, bounded diagnostics suitable for durable operator state."""
    return sanitize_plain_text(str(exc))[:MAX_STORED_ERROR_LENGTH]


def _dispatch_stale_before(now):
    seconds = getattr(
        settings,
        "VALIDATION_CONTINUATION_DISPATCH_STALE_SECONDS",
        DEFAULT_DISPATCH_STALE_SECONDS,
    )
    return now - timedelta(seconds=max(1, int(seconds)))


def _execution_stale_before(now):
    seconds = getattr(
        settings,
        "VALIDATION_CONTINUATION_EXECUTION_STALE_SECONDS",
        DEFAULT_EXECUTION_STALE_SECONDS,
    )
    return now - timedelta(seconds=max(1, int(seconds)))


def stage_validation_run_continuation(
    *,
    validation_run: ValidationRun,
    completed_step_run: ValidationStepRun,
    callback_receipt: CallbackReceipt,
) -> ValidationRunContinuation:
    """Commit one resume decision and schedule best-effort immediate delivery.

    The caller must already be inside the callback-completion transaction. This
    guarantees that a continuation can never exist for a rolled-back step and
    that a committed step can always be rediscovered if the process dies before
    the post-commit dispatcher runs.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "Validation continuations must be staged inside the completion transaction"
        )
    if completed_step_run.validation_run_id != validation_run.pk:
        raise ValueError("Completed step does not belong to the validation run")
    if callback_receipt.validation_run_id != validation_run.pk:
        raise ValueError("Callback receipt does not belong to the validation run")

    continuation, created = ValidationRunContinuation.objects.get_or_create(
        completed_step_run=completed_step_run,
        defaults={
            "validation_run": validation_run,
            "callback_receipt": callback_receipt,
            "resume_from_step": completed_step_run.step_order,
        },
    )
    if not created and (
        continuation.validation_run_id != validation_run.pk
        or continuation.callback_receipt_id != callback_receipt.pk
        or continuation.resume_from_step != completed_step_run.step_order
    ):
        raise RuntimeError(
            "Existing validation continuation conflicts with the completed step"
        )

    transaction.on_commit(
        partial(dispatch_validation_run_continuation, continuation.pk),
        robust=True,
    )
    return continuation


def _claim_dispatch(
    continuation_id: UUID | str,
) -> tuple[_DispatchClaim | None, ContinuationDispatchOutcome]:
    """Claim queue contact in a short transaction, including stale takeover."""
    now = timezone.now()
    with transaction.atomic():
        continuation = (
            ValidationRunContinuation.objects.select_for_update()
            .select_related("validation_run")
            .get(pk=continuation_id)
        )

        if continuation.state in {
            ValidationContinuationState.DISPATCHED,
            ValidationContinuationState.COMPLETED,
        }:
            return None, ContinuationDispatchOutcome.ALREADY_DISPATCHED
        if continuation.state == ValidationContinuationState.NOT_REQUIRED:
            return None, ContinuationDispatchOutcome.NOT_REQUIRED
        if continuation.validation_run.status != ValidationRunStatus.RUNNING:
            continuation.state = ValidationContinuationState.NOT_REQUIRED
            continuation.dispatch_token = None
            continuation.dispatch_started_at = None
            continuation.execution_token = None
            continuation.execution_started_at = None
            continuation.last_error_code = "run_not_running_before_continuation"
            continuation.last_error = ""
            continuation.save(
                update_fields=[
                    "state",
                    "dispatch_token",
                    "dispatch_started_at",
                    "execution_token",
                    "execution_started_at",
                    "last_error_code",
                    "last_error",
                    "modified",
                ]
            )
            return None, ContinuationDispatchOutcome.NOT_REQUIRED

        dispatch_is_active = (
            continuation.state == ValidationContinuationState.DISPATCHING
            and continuation.dispatch_started_at is not None
            and continuation.dispatch_started_at > _dispatch_stale_before(now)
        )
        execution_is_active = (
            continuation.state == ValidationContinuationState.EXECUTING
            and continuation.execution_started_at is not None
            and continuation.execution_started_at > _execution_stale_before(now)
        )
        if dispatch_is_active or execution_is_active:
            return None, ContinuationDispatchOutcome.BUSY

        token = uuid.uuid4()
        continuation.state = ValidationContinuationState.DISPATCHING
        continuation.dispatch_token = token
        continuation.dispatch_started_at = now
        continuation.dispatch_attempts += 1
        continuation.execution_token = None
        continuation.execution_started_at = None
        continuation.last_error_code = ""
        continuation.last_error = ""
        continuation.save(
            update_fields=[
                "state",
                "dispatch_token",
                "dispatch_started_at",
                "dispatch_attempts",
                "execution_token",
                "execution_started_at",
                "last_error_code",
                "last_error",
                "modified",
            ]
        )
        return (
            _DispatchClaim(
                continuation_id=continuation.pk,
                token=token,
                validation_run_id=continuation.validation_run_id,
                user_id=continuation.validation_run.user_id,
                resume_from_step=continuation.resume_from_step,
                task_id=continuation.task_id,
            ),
            ContinuationDispatchOutcome.DISPATCHED,
        )


def _record_dispatch_success(claim: _DispatchClaim, task_id: str | None) -> None:
    """Commit queue acceptance only if this caller still owns the claim."""
    now = timezone.now()
    updated = ValidationRunContinuation.objects.filter(
        pk=claim.continuation_id,
        state=ValidationContinuationState.DISPATCHING,
        dispatch_token=claim.token,
    ).update(
        state=ValidationContinuationState.DISPATCHED,
        dispatch_token=None,
        dispatch_started_at=None,
        transport_task_id=task_id or claim.task_id,
        dispatched_at=now,
        last_error_code="",
        last_error="",
        modified=now,
    )
    if updated == 0:
        logger.info(
            "Continuation delivery advanced before dispatch bookkeeping completed",
            extra={"continuation_id": str(claim.continuation_id)},
        )


def _record_dispatch_failure(claim: _DispatchClaim, exc: BaseException) -> None:
    """Release a known-failed claim so immediate or scheduled repair may retry."""
    now = timezone.now()
    ValidationRunContinuation.objects.filter(
        pk=claim.continuation_id,
        state=ValidationContinuationState.DISPATCHING,
        dispatch_token=claim.token,
    ).update(
        state=ValidationContinuationState.PENDING,
        dispatch_token=None,
        dispatch_started_at=None,
        last_error_code="transport_dispatch_failed",
        last_error=_bounded_error(exc),
        modified=now,
    )


def dispatch_validation_run_continuation(
    continuation_id: UUID | str,
) -> ContinuationDispatchOutcome:
    """Deliver one committed continuation with deterministic queue identity."""
    claim, outcome = _claim_dispatch(continuation_id)
    if claim is None:
        return outcome

    from validibot.core.tasks import enqueue_validation_run

    try:
        task_id = enqueue_validation_run(
            validation_run_id=claim.validation_run_id,
            user_id=claim.user_id,
            resume_from_step=claim.resume_from_step,
            continuation_id=claim.continuation_id,
            task_id=claim.task_id,
        )
    except Exception as exc:
        _record_dispatch_failure(claim, exc)
        logger.warning(
            "Validation continuation dispatch failed; durable repair will retry",
            extra={"continuation_id": str(claim.continuation_id)},
            exc_info=True,
        )
        return ContinuationDispatchOutcome.FAILED

    _record_dispatch_success(claim, task_id)
    return ContinuationDispatchOutcome.DISPATCHED


def _claim_execution(continuation_id: UUID | str) -> _ExecutionClaim | None:
    """Fence duplicate worker deliveries before workflow code can run."""
    now = timezone.now()
    with transaction.atomic():
        continuation = (
            ValidationRunContinuation.objects.select_for_update()
            .select_related("validation_run")
            .get(pk=continuation_id)
        )
        if continuation.state in {
            ValidationContinuationState.COMPLETED,
            ValidationContinuationState.NOT_REQUIRED,
        }:
            return None
        if continuation.validation_run.status != ValidationRunStatus.RUNNING:
            continuation.state = ValidationContinuationState.NOT_REQUIRED
            continuation.execution_token = None
            continuation.execution_started_at = None
            continuation.last_error_code = "run_not_running_at_continuation"
            continuation.last_error = ""
            continuation.save(
                update_fields=[
                    "state",
                    "execution_token",
                    "execution_started_at",
                    "last_error_code",
                    "last_error",
                    "modified",
                ]
            )
            return None
        if (
            continuation.state == ValidationContinuationState.EXECUTING
            and continuation.execution_started_at is not None
            and continuation.execution_started_at > _execution_stale_before(now)
        ):
            raise ValidationContinuationBusyError(
                f"Continuation {continuation.pk} is already executing"
            )

        token = uuid.uuid4()
        continuation.state = ValidationContinuationState.EXECUTING
        continuation.dispatch_token = None
        continuation.dispatch_started_at = None
        continuation.dispatched_at = continuation.dispatched_at or now
        continuation.execution_token = token
        continuation.execution_started_at = now
        continuation.execution_attempts += 1
        continuation.last_error_code = ""
        continuation.last_error = ""
        continuation.save(
            update_fields=[
                "state",
                "dispatch_token",
                "dispatch_started_at",
                "dispatched_at",
                "execution_token",
                "execution_started_at",
                "execution_attempts",
                "last_error_code",
                "last_error",
                "modified",
            ]
        )
        return _ExecutionClaim(
            continuation_id=continuation.pk,
            token=token,
            validation_run_id=continuation.validation_run_id,
            user_id=continuation.validation_run.user_id,
            resume_from_step=continuation.resume_from_step,
        )


def _release_execution(claim: _ExecutionClaim, exc: BaseException) -> None:
    """Make a failed execution claim retryable without reopening terminal work."""
    now = timezone.now()
    ValidationRunContinuation.objects.filter(
        pk=claim.continuation_id,
        state=ValidationContinuationState.EXECUTING,
        execution_token=claim.token,
    ).update(
        state=ValidationContinuationState.DISPATCHED,
        execution_token=None,
        execution_started_at=None,
        last_error_code="continuation_execution_failed",
        last_error=_bounded_error(exc),
        modified=now,
    )


def _complete_execution(claim: _ExecutionClaim) -> None:
    """Close the exact execution generation that performed the continuation."""
    now = timezone.now()
    updated = ValidationRunContinuation.objects.filter(
        pk=claim.continuation_id,
        state=ValidationContinuationState.EXECUTING,
        execution_token=claim.token,
    ).update(
        state=ValidationContinuationState.COMPLETED,
        execution_token=None,
        execution_started_at=None,
        completed_at=now,
        last_error_code="",
        last_error="",
        modified=now,
    )
    if updated == 0:
        logger.warning(
            "Stale continuation execution could not close a newer claim",
            extra={"continuation_id": str(claim.continuation_id)},
        )


def _current_run_result(continuation_id: UUID | str) -> ValidationRunTaskResult:
    """Return the current run outcome for an idempotently consumed delivery."""
    continuation = ValidationRunContinuation.objects.select_related(
        "validation_run"
    ).get(pk=continuation_id)
    run = continuation.validation_run
    return ValidationRunTaskResult(
        run_id=run.pk,
        status=ValidationRunStatus(run.status),
        error=run.error or "",
    )


def execute_validation_run_continuation(
    continuation_id: UUID | str,
) -> ValidationRunTaskResult:
    """Execute one durable continuation and converge duplicate deliveries."""
    claim = _claim_execution(continuation_id)
    if claim is None:
        return _current_run_result(continuation_id)

    from validibot.validations.services.validation_run import ValidationRunService

    try:
        result = ValidationRunService().execute_workflow_steps(
            validation_run_id=claim.validation_run_id,
            user_id=claim.user_id,
            resume_from_step=claim.resume_from_step,
        )
    except Exception as exc:
        _release_execution(claim, exc)
        raise

    _complete_execution(claim)
    return result


def pending_validation_continuation_ids(*, limit: int) -> list[UUID]:
    """Return a bounded repair batch without holding locks during dispatch."""
    now = timezone.now()
    candidates = ValidationRunContinuation.objects.filter(
        Q(state=ValidationContinuationState.PENDING)
        | Q(
            state=ValidationContinuationState.DISPATCHING,
            dispatch_started_at__lte=_dispatch_stale_before(now),
        )
        | Q(
            state=ValidationContinuationState.EXECUTING,
            execution_started_at__lte=_execution_stale_before(now),
        )
    ).order_by("created")
    return list(candidates.values_list("pk", flat=True)[: max(1, limit)])


def repair_validation_run_continuations(
    *,
    limit: int = 100,
    dry_run: bool = False,
) -> ContinuationRepairReport:
    """Rediscover and redeliver committed continuation work in one bounded pass."""
    continuation_ids = pending_validation_continuation_ids(limit=limit)
    if dry_run:
        count = len(continuation_ids)
        return ContinuationRepairReport(
            examined=count,
            dispatched=0,
            already_dispatched=0,
            busy=0,
            not_required=0,
            failed=0,
        )

    counts = dict.fromkeys(ContinuationDispatchOutcome, 0)
    for continuation_id in continuation_ids:
        outcome = dispatch_validation_run_continuation(continuation_id)
        counts[outcome] += 1
    return ContinuationRepairReport(
        examined=len(continuation_ids),
        dispatched=counts[ContinuationDispatchOutcome.DISPATCHED],
        already_dispatched=counts[ContinuationDispatchOutcome.ALREADY_DISPATCHED],
        busy=counts[ContinuationDispatchOutcome.BUSY],
        not_required=counts[ContinuationDispatchOutcome.NOT_REQUIRED],
        failed=counts[ContinuationDispatchOutcome.FAILED],
    )
