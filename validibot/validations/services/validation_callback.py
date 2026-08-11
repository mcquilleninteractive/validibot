"""
Service layer for processing validator callbacks on the worker service.

Validator containers (EnergyPlus, FMU) POST a minimal callback payload to the
worker-only callback endpoint when they complete. The callback handler:

1. Validates the payload shape (Pydantic model from validibot_shared)
2. Authenticates the callback against its durable execution attempt
3. Downloads the full output envelope from cloud storage
4. Delegates to ValidationStepProcessor.complete_from_callback() for step completion
5. Atomically records a durable continuation (when steps remain) or finalizes
   the run

The public API view should be a thin wrapper around this service.

NOTE: Assertion evaluation and finding persistence are handled by the processor,
not by this service. The processor calls validator.post_execute_validate() which
handles all assertion types (CEL, etc.) for the output stage.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError
from rest_framework import status
from rest_framework.response import Response
from validibot_shared.validations.envelopes import ValidationCallback
from validibot_shared.validations.envelopes import ValidationStatus

from validibot.core.models import CallbackReceiptStatus
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import CallbackReceipt
from validibot.validations.models import ExecutionAttempt
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.attempt_paths import validate_attempt_gcs_uri
from validibot.validations.services.cloud_run.gcs_client import download_envelope
from validibot.validations.services.output_envelope_verifier import (
    OutputEnvelopeVerificationError,
)
from validibot.validations.services.output_envelope_verifier import (
    build_expected_output_envelope,
)
from validibot.validations.services.output_envelope_verifier import (
    output_envelope_sha256,
)
from validibot.validations.services.output_envelope_verifier import (
    verify_output_envelope,
)
from validibot.validations.services.validation_run import ValidationRunService

if TYPE_CHECKING:
    from validibot_shared.validations.envelopes import ValidationOutputEnvelope

    from validibot.validations.services.step_processor.advanced import (
        AdvancedValidationProcessor,
    )

logger = logging.getLogger(__name__)

# ── Allowlisted GCS prefix for callback result URIs ───────────────────
#
# The worker callback endpoint receives ``result_uri`` as a free-form string
# from the (OIDC-authenticated) validator container's POST body, then fetches
# and trusts that object as the run's output envelope. Without an allowlist a
# compromised or misbehaving container could point ``result_uri`` at ANY object
# the worker service account can read (cross-org outputs, secrets bundles,
# unrelated buckets) — an arbitrary GCS read / result-substitution vector.
#
# The launcher writes each bundle below the active execution attempt. We rebuild
# that exact prefix from trusted database relationships and require the callback
# URI to remain inside it before touching GCS.


# ── Exception for early-exit error responses ──────────────────────────
#
# Callback processing delegates to several helper methods that each validate
# preconditions (active step run exists, envelope downloads successfully,
# envelope IDs match the expected run/validator).  Rather than threading
# Response objects back through return values, helpers raise this exception
# and the top-level method catches it once and converts it to a Response.


class _CallbackProcessingError(Exception):
    """Raised by helpers when callback processing cannot continue.

    Carries the HTTP status code and response body so the public entry point
    can convert it to a DRF Response in one place.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Result container for step completion ──────────────────────────────


@dataclass(frozen=True)
class _StepCompletionResult:
    """Step outcome consumed by run finalization or continuation staging."""

    step_run: ValidationStepRun
    step_status: StepStatus
    step_error: str
    output_envelope: Any  # typed as Any to avoid coupling to envelope base class


@dataclass(frozen=True, slots=True)
class _CallbackRequest:
    """Normalized data shared by external callback and trusted recovery paths."""

    run_id: str
    callback_id: str
    callback_nonce: str | None = field(repr=False)
    status: ValidationStatus
    result_uri: str


@dataclass(frozen=True, slots=True)
class _CallbackProcessingClaim:
    """Ownership of external callback work between two short transactions."""

    receipt_id: uuid.UUID
    processing_token: uuid.UUID
    attempt_id: uuid.UUID


# ── Helpers ───────────────────────────────────────────────────────────


def _coerce_finished_at(finished_at_candidate) -> datetime:
    """Normalize finished_at to an aware datetime in UTC."""
    if finished_at_candidate is None:
        return datetime.now(tz=UTC)
    if isinstance(finished_at_candidate, datetime):
        dt_value = finished_at_candidate
    elif isinstance(finished_at_candidate, str):
        # Handle common ISO strings, including trailing Z
        iso_value = finished_at_candidate.replace("Z", "+00:00")
        try:
            dt_value = datetime.fromisoformat(iso_value)
        except ValueError:
            logger.warning(
                "Could not parse finished_at string '%s', defaulting to now",
                finished_at_candidate,
            )
            return datetime.now(tz=UTC)
    else:
        logger.warning(
            "Unexpected finished_at type %s, defaulting to now",
            type(finished_at_candidate),
        )
        return datetime.now(tz=UTC)

    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=UTC)
    return dt_value.astimezone(UTC)


def _coerce_optional_timing_at(timing_candidate) -> datetime | None:
    """Normalize optional provider timing without inventing a latency sample."""
    if timing_candidate is None:
        return None
    if isinstance(timing_candidate, datetime):
        dt_value = timing_candidate
    elif isinstance(timing_candidate, str):
        try:
            dt_value = datetime.fromisoformat(timing_candidate.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Could not parse optional provider timing value")
            return None
    else:
        logger.warning(
            "Unexpected optional provider timing type %s",
            type(timing_candidate),
        )
        return None
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=UTC)
    return dt_value.astimezone(UTC)


class ValidationCallbackService:
    """
    Process container-based validator callbacks.

    This service is invoked by the worker-only callback API endpoint. It handles
    idempotency, envelope download, and run finalization.

    IMPORTANT: This class is only used for async backends (GCP Cloud Run, AWS
    Fargate) where containers POST callbacks when complete. For sync backends
    (Docker Compose), the processor handles completion inline.

    Responsibilities:
    - Validate callback payload and authenticate its attempt nonce
    - Check idempotency after authentication
    - Download output envelope from cloud storage
    - Delegate to ValidationStepProcessor.complete_from_callback()
    - Commit a durable resume request (more steps) or finalize the run

    Finding persistence and assertion evaluation are NOT done here - the
    processor handles all of that via validator.post_execute_validate().
    """

    # ── Public entry point ────────────────────────────────────────────

    def process(self, *, payload: dict, caller_email: str = "") -> Response:
        """
        Validate and process a validator callback payload.

        Args:
            payload: Incoming request body (JSON) containing callback data.

        Returns:
            DRF Response with an appropriate status code and body.
        """
        try:
            # The callback is intentionally minimal — it just says "run X
            # finished with status Y, go fetch the full results from this URI."
            # The actual validation-specific data (findings, outputs) lives in
            # the output.json at result_uri, which Django downloads and
            # processes separately. This keeps the callback contract stable
            # across all validator types — the container doesn't need to
            # serialize its full output into the HTTP POST.
            validated_callback = ValidationCallback.model_validate(payload)
            callback = _CallbackRequest(
                run_id=validated_callback.run_id,
                callback_id=validated_callback.callback_id,
                callback_nonce=validated_callback.callback_nonce,
                status=validated_callback.status,
                result_uri=validated_callback.result_uri,
            )

            logger.info(
                "Received callback for run %s with status %s (callback_id=%s)",
                callback.run_id,
                callback.status,
                callback.callback_id,
            )

            # Get the validation run FIRST — before idempotency check.
            # This ensures we return a clean 404 if the run doesn't exist,
            # rather than an FK error when creating the receipt.
            try:
                run = ValidationRun.objects.get(id=callback.run_id)
            except ValidationRun.DoesNotExist:
                logger.warning("Validation run not found: %s", callback.run_id)
                return Response(
                    {"error": "Validation run not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            return self._process_with_idempotency_guard(
                callback,
                run,
                require_callback_nonce=True,
                caller_email=caller_email,
            )

        except ValidationError:
            logger.warning(
                "Invalid callback payload",
                extra={"event": "validator_callback_failure"},
            )
            return Response(
                {"error": "Invalid callback payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            # Don't leak exception text into the response.
            # ``logger.exception`` captures the full traceback for
            # operators; this endpoint is called by validator workers
            # under OIDC auth and only consumes the status code anyway.
            logger.exception(
                "Unexpected error processing callback",
                extra={"event": "validator_callback_failure"},
            )
            return Response(
                {"error": "Internal server error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def process_reconciliation(
        self,
        *,
        run: ValidationRun,
        attempt: ExecutionAttempt,
        callback_status: ValidationStatus,
        result_uri: str,
    ) -> Response:
        """Recover trusted provider output without fabricating a raw nonce.

        Reconciliation has already authenticated to the provider and resolved
        the durable attempt. It reuses receipt fencing and the full output
        verification pipeline, while only the worker API accepts untrusted
        callback payloads and therefore requires the raw callback credential.
        """
        from validibot.validations.services.execution_attempts import (
            build_attempt_callback_id,
        )

        callback = _CallbackRequest(
            run_id=str(run.pk),
            callback_id=build_attempt_callback_id(attempt),
            callback_nonce=None,
            status=callback_status,
            result_uri=result_uri,
        )
        return self._process_with_idempotency_guard(
            callback,
            run,
            require_callback_nonce=False,
            caller_email="",
        )

    @staticmethod
    def _late_callback_response(
        callback: _CallbackRequest,
        run: ValidationRun,
    ) -> Response:
        """Acknowledge output that arrived after the run became terminal."""
        logger.info(
            "Ignoring late callback for terminal run %s (status=%s, callback_id=%s)",
            run.id,
            run.status,
            callback.callback_id,
        )
        return Response(
            {
                "message": "Run is already terminal",
                "late_callback_ignored": True,
            },
            status=status.HTTP_200_OK,
        )

    # ── Idempotency guard ─────────────────────────────────────────────

    def _process_with_idempotency_guard(
        self,
        callback: _CallbackRequest,
        run: ValidationRun,
        *,
        require_callback_nonce: bool,
        caller_email: str,
    ) -> Response:
        """Authenticate, claim, verify, and atomically apply one callback.

        The processing token fences stale workers without holding a database
        transaction across storage I/O. A duplicate delivery observes terminal
        state, receives a retryable conflict while a live claim exists, or takes
        over a claim whose bounded processing window expired.
        """
        from validibot.validations.services.execution_attempts import (
            resolve_callback_attempt,
        )

        attempt = resolve_callback_attempt(
            callback.callback_id,
            run_id=run.pk,
        )
        if attempt is None:
            logger.warning(
                "Rejected callback that was not bound to an execution attempt",
                extra={"run_id": str(run.pk)},
            )
            return Response(
                {"error": "Callback does not identify a valid execution attempt"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if require_callback_nonce:
            from validibot.validations.services.execution_attempts import (
                verify_attempt_callback_nonce,
            )

            if not verify_attempt_callback_nonce(attempt, callback.callback_nonce):
                logger.warning(
                    "Rejected callback with invalid attempt credentials",
                    extra={
                        "run_id": str(run.pk),
                        "attempt_id": str(attempt.pk),
                    },
                )
                return Response(
                    {"error": "Invalid callback credentials"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if attempt.deployment_id:
                expected_identity = str(
                    attempt.deployment_snapshot.get(
                        "expected_runtime_identity",
                        "",
                    )
                ).lower()
                if not expected_identity or caller_email.lower() != expected_identity:
                    logger.warning(
                        "Rejected callback from runtime identity not pinned to "
                        "the execution attempt",
                        extra={
                            "run_id": str(run.pk),
                            "attempt_id": str(attempt.pk),
                            "caller_email": caller_email,
                            "deployment_id": str(attempt.deployment_id),
                        },
                    )
                    return Response(
                        {"error": "Callback runtime identity does not match attempt"},
                        status=status.HTTP_403_FORBIDDEN,
                    )
        claimed = self._claim_callback_processing(callback, run, attempt)
        if isinstance(claimed, Response):
            return claimed

        try:
            _step_run, validator = self._resolve_attempt_step_run(run, attempt)
            output_envelope = self._download_and_validate_envelope(
                callback,
                run,
                validator,
                attempt,
            )
        except _CallbackProcessingError as exc:
            if status.is_client_error(exc.status_code):
                self._reject_callback_claim(
                    callback=callback,
                    run=run,
                    claim=claimed,
                    error=exc,
                )
            else:
                self._release_callback_claim(
                    claim=claimed,
                    error_code="callback_processing_retryable",
                    error=exc.detail,
                )
            return Response(
                {"error": exc.detail},
                status=exc.status_code,
            )
        return self._apply_callback_claim(
            callback=callback,
            run=run,
            claim=claimed,
            output_envelope=output_envelope,
        )

    @staticmethod
    def _terminal_receipt_response(receipt: CallbackReceipt) -> Response:
        """Return the stable acknowledgement for an already-terminal delivery."""
        was_rejected = receipt.status == CallbackReceiptStatus.REJECTED
        return Response(
            {
                "message": (
                    "Callback already rejected"
                    if was_rejected
                    else "Callback already processed"
                ),
                "idempotent_replayed": True,
                "original_received_at": receipt.received_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _callback_claim_stale_before(now: datetime) -> datetime:
        """Return the takeover boundary for a crashed callback processor."""
        seconds = getattr(
            settings,
            "VALIDATION_CALLBACK_PROCESSING_STALE_SECONDS",
            10 * 60,
        )
        return now - timedelta(seconds=max(1, int(seconds)))

    def _claim_callback_processing(
        self,
        callback: _CallbackRequest,
        run: ValidationRun,
        attempt: ExecutionAttempt,
    ) -> _CallbackProcessingClaim | Response:
        """Claim external callback work without retaining any database lock."""
        now = timezone.now()
        token = uuid.uuid4()
        with transaction.atomic():
            locked_run = ValidationRun.objects.select_for_update().get(pk=run.pk)
            locked_attempt = (
                ExecutionAttempt.objects.select_for_update()
                .select_related("step_run")
                .get(pk=attempt.pk)
            )
            receipt = (
                CallbackReceipt.objects.select_for_update()
                .filter(callback_id=callback.callback_id)
                .first()
            )
            if receipt is None:
                receipt = CallbackReceipt.objects.create(
                    callback_id=callback.callback_id,
                    validation_run=locked_run,
                    execution_attempt=locked_attempt,
                    status=CallbackReceiptStatus.PROCESSING,
                    result_uri=callback.result_uri or "",
                )
            elif (
                receipt.execution_attempt_id != locked_attempt.pk
                or receipt.validation_run_id != locked_run.pk
                or receipt.result_uri != (callback.result_uri or "")
            ):
                logger.error(
                    "Callback receipt identity conflict",
                    extra={
                        "run_id": str(locked_run.pk),
                        "attempt_id": str(locked_attempt.pk),
                        "receipt_id": str(receipt.pk),
                    },
                )
                return Response(
                    {"error": "Callback receipt identity conflict"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if receipt.status != CallbackReceiptStatus.PROCESSING:
                return self._terminal_receipt_response(receipt)
            current_step = locked_run.current_step_run
            if (
                locked_attempt.is_terminal
                or locked_run.status in VALIDATION_RUN_TERMINAL_STATUSES
                or current_step is None
                or current_step.pk != locked_attempt.step_run_id
            ):
                self._complete_receipt(receipt)
                return self._late_callback_response(callback, locked_run)
            if (
                receipt.processing_token is not None
                and receipt.processing_started_at is not None
                and receipt.processing_started_at
                > self._callback_claim_stale_before(now)
            ):
                return Response(
                    {
                        "message": "Callback is being processed by another request",
                        "retry": True,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            receipt.processing_token = token
            receipt.processing_started_at = now
            receipt.delivery_count += 1
            receipt.last_error_code = ""
            receipt.last_error = ""
            receipt.save(
                update_fields=[
                    "processing_token",
                    "processing_started_at",
                    "delivery_count",
                    "last_error_code",
                    "last_error",
                ]
            )
            return _CallbackProcessingClaim(
                receipt_id=receipt.pk,
                processing_token=token,
                attempt_id=locked_attempt.pk,
            )

    @staticmethod
    def _release_callback_claim(
        *,
        claim: _CallbackProcessingClaim,
        error_code: str,
        error: str,
    ) -> None:
        """Release retryable work only when the exact processing token matches."""
        CallbackReceipt.objects.filter(
            pk=claim.receipt_id,
            status=CallbackReceiptStatus.PROCESSING,
            processing_token=claim.processing_token,
        ).update(
            processing_token=None,
            processing_started_at=None,
            last_error_code=error_code[:64],
            last_error=error[:2000],
        )

    def _reject_callback_claim(
        self,
        *,
        callback: _CallbackRequest,
        run: ValidationRun,
        claim: _CallbackProcessingClaim,
        error: _CallbackProcessingError,
    ) -> None:
        """Commit a permanent rejection and fence the producing attempt."""
        from validibot.validations.constants import ExecutionAttemptState
        from validibot.validations.services.execution_attempts import (
            transition_execution_attempt,
        )

        callback_received_at = timezone.now()
        with transaction.atomic():
            ValidationRun.objects.select_for_update().get(pk=run.pk)
            ExecutionAttempt.objects.select_for_update().get(pk=claim.attempt_id)
            receipt = CallbackReceipt.objects.select_for_update().get(
                pk=claim.receipt_id
            )
            if (
                receipt.status != CallbackReceiptStatus.PROCESSING
                or receipt.processing_token != claim.processing_token
            ):
                return
            transition_execution_attempt(
                claim.attempt_id,
                ExecutionAttemptState.FAILED,
                last_error_code="callback_rejected",
                last_error=error.detail,
                callback_received_at=callback_received_at,
            )
            self._reject_receipt(receipt, error.detail)

    def _apply_callback_claim(
        self,
        *,
        callback: _CallbackRequest,
        run: ValidationRun,
        claim: _CallbackProcessingClaim,
        output_envelope: ValidationOutputEnvelope,
    ) -> Response:
        """Atomically apply verified output under the exact processing claim."""
        from validibot.validations.constants import ExecutionAttemptState
        from validibot.validations.services.execution_attempts import (
            transition_execution_attempt,
        )

        callback_received_at = timezone.now()
        try:
            with transaction.atomic():
                locked_run = ValidationRun.objects.select_for_update().get(pk=run.pk)
                locked_attempt = (
                    ExecutionAttempt.objects.select_for_update(of=("self",))
                    .select_related("step_run__workflow_step__validator")
                    .get(pk=claim.attempt_id)
                )
                receipt = CallbackReceipt.objects.select_for_update().get(
                    pk=claim.receipt_id
                )
                if receipt.status != CallbackReceiptStatus.PROCESSING:
                    return self._terminal_receipt_response(receipt)
                if receipt.processing_token != claim.processing_token:
                    return Response(
                        {
                            "message": "Callback processing ownership changed",
                            "retry": True,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                current_step = locked_run.current_step_run
                if (
                    locked_attempt.is_terminal
                    or locked_run.status in VALIDATION_RUN_TERMINAL_STATUSES
                    or current_step is None
                    or current_step.pk != locked_attempt.step_run_id
                ):
                    self._complete_receipt(receipt)
                    return self._late_callback_response(callback, locked_run)

                step_run = locked_attempt.step_run
                result = self._complete_step(
                    locked_run,
                    step_run,
                    output_envelope,
                )
                self._finalize_or_stage_continuation(
                    locked_run,
                    result,
                    receipt,
                )
                transition_execution_attempt(
                    locked_attempt.pk,
                    ExecutionAttemptState.COMPLETED,
                    provider_started_at=_coerce_optional_timing_at(
                        getattr(output_envelope.timing, "started_at", None)
                    ),
                    provider_finished_at=_coerce_optional_timing_at(
                        getattr(output_envelope.timing, "finished_at", None)
                    ),
                    callback_received_at=callback_received_at,
                    output_envelope_uri=callback.result_uri or "",
                    output_envelope_sha256=output_envelope_sha256(output_envelope),
                )
                self._complete_receipt(receipt)
        except Exception as exc:
            self._release_callback_claim(
                claim=claim,
                error_code="callback_apply_failed",
                error=str(exc),
            )
            raise

        logger.info(
            "Processed callback for run %s, step status=%s",
            callback.run_id,
            result.step_status,
        )
        return Response(
            {"message": "Callback processed successfully"},
            status=status.HTTP_200_OK,
        )

    # ── Step 1: Resolve the active step run ───────────────────────────

    @staticmethod
    def _resolve_attempt_step_run(
        run: ValidationRun,
        attempt: ExecutionAttempt,
    ) -> tuple[ValidationStepRun, Any]:
        """Resolve the active step directly from authenticated attempt identity.

        A callback must never select whichever step happens to be current. Its
        producing attempt already names the only logical step whose output it
        may complete.

        Returns:
            (step_run, validator) tuple.

        Raises:
            _CallbackProcessingError: If no active step run or no validator.
        """
        step_run = (
            ValidationStepRun.objects.select_related(
                "workflow_step__validator",
            )
            .filter(
                pk=attempt.step_run_id,
                validation_run=run,
                status__in=[StepStatus.RUNNING, StepStatus.PENDING],
            )
            .first()
        )

        if not step_run:
            logger.warning("No active step run found for run %s", run.id)
            raise _CallbackProcessingError(
                status.HTTP_404_NOT_FOUND,
                "Step run not found",
            )

        validator = step_run.workflow_step.validator
        if not validator:
            logger.error("No validator found for step run: %s", step_run.id)
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                "No validator found for step",
            )

        return step_run, validator

    # ── Step 2: Download and validate the output envelope ─────────────

    @staticmethod
    def _validate_result_uri_allowlist(
        result_uri: str,
        run: ValidationRun,
        attempt,
    ) -> None:
        """
        Reject a callback ``result_uri`` that escapes its attempt's GCS prefix.

        The callback container controls ``result_uri`` and we download+trust
        whatever object it names. To stop that string from pointing at an
        arbitrary object the worker SA can read, we require it to be a
        ``gs://`` URI in ``settings.GCS_VALIDATION_BUCKET`` and under this
        attempt's deterministic prefix
        ``runs/<org_id>/<run_id>/attempts/<attempt_id>/`` — the exact layout
        the launcher writes (see ``cloud_run/launcher.py``).

        When ``GCS_VALIDATION_BUCKET`` is unset (local/sync dev, where the
        container-callback path is not exercised against real GCS and the
        launcher writes to the local filesystem instead), there is no bucket
        to pin against, so the allowlist is skipped — production deployments
        always configure the bucket, which is where the read primitive matters.

        Args:
            result_uri: The ``gs://`` URI supplied in the callback POST body.
            run: The already-resolved ValidationRun this callback belongs to,
                used to derive the expected run prefix.
            attempt: Trusted execution attempt resolved from the callback id.

        Raises:
            _CallbackProcessingError: If ``result_uri`` is not a ``gs://`` URI,
                targets a different bucket, or falls outside this attempt's
                prefix.
        """
        expected_bucket = settings.GCS_VALIDATION_BUCKET
        if not expected_bucket:
            # No bucket configured → local/sync dev path, nothing to pin to.
            return

        try:
            validate_attempt_gcs_uri(
                result_uri,
                expected_bucket=expected_bucket,
                org_id=str(run.org_id),
                run_id=str(run.id),
                attempt_id=str(attempt.pk),
            )
        except ValueError:
            logger.warning(
                "Callback result_uri outside the attempt allowlist for run %s",
                run.id,
            )
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                "result_uri is not permitted for this execution attempt",
            ) from None

    @staticmethod
    def _download_and_validate_envelope(
        callback: _CallbackRequest,
        run: ValidationRun,
        validator,
        attempt,
    ):
        """
        Download the output envelope from GCS and verify it matches expectations.

        Each advanced validator declares its output envelope class in its
        ValidatorConfig; these are resolved at startup and stored in the
        validator registry for O(1) lookups.

        Before touching GCS we pin the callback-supplied ``result_uri`` to this
        attempt's expected bucket+prefix (see
        ``_validate_result_uri_allowlist``) so a misbehaving container cannot
        turn the worker into an arbitrary-object reader.

        Raises:
            _CallbackProcessingError: On allowlist violation, download failure,
                missing envelope class, or validator/run ID mismatch.
        """
        if (callback.result_uri or "") != attempt.output_envelope_uri:
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                "result_uri does not match the execution attempt",
            )

        # Gate the untrusted result_uri BEFORE any GCS access.
        ValidationCallbackService._validate_result_uri_allowlist(
            callback.result_uri or "",
            run,
            attempt,
        )

        try:
            expected = build_expected_output_envelope(
                run=run,
                validator=validator,
                attempt=attempt,
            )
        except OutputEnvelopeVerificationError as exc:
            logger.warning(
                "Cannot verify output for validator type %s: %s",
                validator.validation_type,
                exc.code,
            )
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                exc.detail,
            ) from exc

        try:
            output_envelope = cast(
                "ValidationOutputEnvelope",
                download_envelope(
                    callback.result_uri,
                    expected.envelope_class,
                    max_bytes=getattr(
                        settings,
                        "VALIDATION_RESULT_MAX_BYTES",
                        None,
                    ),
                ),
            )
        except Exception as exc:
            logger.exception("Failed to download output envelope")
            # Return a static message — the raw exception (which can carry the
            # signed result URI, storage paths, or internal state) is captured
            # server-side by logger.exception above and must not reach the
            # caller's response body.
            raise _CallbackProcessingError(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Failed to download output envelope.",
            ) from exc

        try:
            return verify_output_envelope(output_envelope, expected=expected)
        except OutputEnvelopeVerificationError as exc:
            logger.warning(
                "Rejected callback output envelope for run %s: %s",
                run.pk,
                exc.code,
            )
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                exc.detail,
            ) from exc

    # ── Step 3: Complete the step via the processor ───────────────────

    def _complete_step(
        self,
        run: ValidationRun,
        step_run: ValidationStepRun,
        output_envelope,
    ) -> _StepCompletionResult:
        """
        Delegate to ValidationStepProcessor and emit the step-completed signal.

        The processor handles findings persistence, output-stage assertion
        evaluation, and output value storage. After it completes, we refresh the
        step_run from the database to get the authoritative final status
        (which may differ from the envelope status if assertions failed).

        Returns:
            _StepCompletionResult with the refreshed step status and error.
        """
        from validibot.validations.services.step_processor import get_step_processor

        processor = cast(
            "AdvancedValidationProcessor",
            get_step_processor(run, step_run),
        )
        processor.complete_from_callback(output_envelope)

        # Refresh step_run to get the final status set by the processor.
        # The processor's finalize_step() sets step_run.status based on:
        # 1. Container envelope status (authoritative for container execution)
        # 2. Output-stage assertion failures (can fail step even if container
        #    succeeded)
        # We must use this status, NOT output_envelope.status, for run-level
        # decisions.
        step_run.refresh_from_db()

        # Notify listeners that a step completed (e.g., cloud metering for
        # credit deduction). Using send_robust so a failing receiver doesn't
        # break the callback flow.
        from validibot.validations.signals import validation_step_completed

        # ``ran_to_completion`` tells metering receivers whether the validator
        # container actually executed and produced a result, so they can charge
        # compute for runs that "finished but had errors" while NOT charging
        # runs that failed at runtime. It is derived from the ENVELOPE status,
        # not step_run.status, on purpose:
        #   * SUCCESS / FAILED_VALIDATION  -> the container ran to completion
        #     ("finished but had errors") -> ran_to_completion=True (charge)
        #   * FAILED_RUNTIME / CANCELLED   -> no usable result -> False (skip)
        # step_run.status cannot be used here: it collapses FAILED_VALIDATION and
        # FAILED_RUNTIME both to FAILED, and a SUCCESS-but-custom-container-error
        # step is now FAILED too (still ran to completion, so still billable).
        ran_to_completion = output_envelope.status in {
            ValidationStatus.SUCCESS,
            ValidationStatus.FAILED_VALIDATION,
        }

        validation_step_completed.send_robust(
            sender=self.__class__,
            step_run=step_run,
            validation_run=run,
            envelope_status=output_envelope.status.value,
            ran_to_completion=ran_to_completion,
        )

        return _StepCompletionResult(
            step_run=step_run,
            step_status=StepStatus(step_run.status),
            step_error=step_run.error or "",
            output_envelope=output_envelope,
        )

    # ── Step 4: Resume next step or finalize the run ──────────────────

    def _finalize_or_stage_continuation(
        self,
        run: ValidationRun,
        result: _StepCompletionResult,
        receipt: CallbackReceipt,
    ) -> None:
        """Commit durable resume work or finalize the run in this transaction.

        Queue contact is intentionally absent here. The continuation service
        registers an ``on_commit`` fast path and the watchdog repairs any
        committed row lost before that callback can run.
        """
        remaining_steps = run.workflow.steps.filter(
            order__gt=result.step_run.step_order,
        ).exists()

        if remaining_steps and result.step_status == StepStatus.PASSED:
            from validibot.validations.services.validation_continuation import (
                stage_validation_run_continuation,
            )

            stage_validation_run_continuation(
                validation_run=run,
                completed_step_run=result.step_run,
                callback_receipt=receipt,
            )
        else:
            self._finalize_run(run, result)

    def _finalize_run(
        self,
        run: ValidationRun,
        result: _StepCompletionResult,
    ) -> None:
        """
        Finalize the validation run after the last step (or a failed step).

        Sets run status, error category, timing, rebuilds the summary record,
        stamps the evidence hash, and queues a submission purge if the
        retention policy requires it.
        """
        # Map step status to run status. We use the processor's step_status
        # (not envelope status) because the processor accounts for output-stage
        # assertion failures that can fail a step even when the container
        # returned SUCCESS.
        step_to_run_status = {
            StepStatus.PASSED: ValidationRunStatus.SUCCEEDED,
            StepStatus.FAILED: ValidationRunStatus.FAILED,
            StepStatus.SKIPPED: ValidationRunStatus.CANCELED,
        }

        # Determine error category based on envelope status (runtime vs
        # validation) combined with step status (for assertion failures).
        if result.step_status in {StepStatus.PASSED, StepStatus.SKIPPED}:
            error_category = ""
        elif result.output_envelope.status == ValidationStatus.FAILED_RUNTIME:
            error_category = ValidationRunErrorCategory.RUNTIME_ERROR
        else:
            # FAILED_VALIDATION, SUCCESS with assertion failures, or unknown
            error_category = ValidationRunErrorCategory.VALIDATION_FAILED

        finished_at = _coerce_finished_at(
            result.output_envelope.timing.finished_at,
        )

        target_status = step_to_run_status.get(
            result.step_status,
            ValidationRunStatus.FAILED,
        )
        duration_ms = run.duration_ms

        if run.started_at and finished_at:
            delta = finished_at - run.started_at
            duration_ms = int(delta.total_seconds() * 1000)

        # Optimistic terminal fence: callback admission and output processing
        # cannot hold a database lock across storage/provider work. Make the
        # final transition conditional instead, so a concurrent cancel or
        # watchdog timeout wins without a stale model save resurrecting it.
        updated = ValidationRun.objects.filter(
            pk=run.pk,
            status__in=[
                ValidationRunStatus.PENDING,
                ValidationRunStatus.RUNNING,
            ],
        ).update(
            status=target_status,
            error_category=error_category,
            ended_at=finished_at,
            error=result.step_error,
            duration_ms=duration_ms,
        )
        if updated == 0:
            run.refresh_from_db(
                fields=["status", "error_category", "ended_at", "error", "duration_ms"]
            )
            logger.info(
                "Ignored callback finalization for terminal run %s (status=%s)",
                run.id,
                run.status,
            )
            return

        # Keep the model passed to downstream signals/projections aligned with
        # the conditional database update without another query.
        run.status = target_status
        run.error_category = error_category
        run.ended_at = finished_at
        run.error = result.step_error
        run.duration_ms = duration_ms

        ValidationRunService().rebuild_run_summary_record(
            validation_run=run,
        )

        from validibot.validations.services.output_hash import safe_stamp_output_hash

        safe_stamp_output_hash(run)

        # ADR-2026-04-27 Phase 4 Session A: stamp the evidence
        # manifest after the run reaches its terminal state. Best-
        # effort — a manifest-generation failure does not change
        # the run's outcome. Sibling call lives in
        # step_orchestrator.execute_workflow_steps for the sync
        # path; both are idempotent (re-stamping replaces in place).
        from validibot.validations.services.evidence import stamp_evidence_manifest

        stamp_evidence_manifest(run)

        # Retention eligibility starts only after every detailed projection is
        # finalized, preventing a scheduled purge from racing evidence writes.
        from validibot.validations.services.run_admission import (
            emit_validation_run_finalized,
        )

        emit_validation_run_finalized(
            sender=self.__class__,
            validation_run=run,
        )

        logger.info(
            "Finalized run %s with status %s",
            run.id,
            run.status,
        )
        self._queue_purge_if_do_not_store(run)

    # ── Step 5: Receipt bookkeeping ───────────────────────────────────

    @staticmethod
    def _complete_receipt(receipt: CallbackReceipt) -> None:
        """Commit successful callback consumption and release its claim."""
        receipt.status = CallbackReceiptStatus.COMPLETED
        receipt.processing_token = None
        receipt.processing_started_at = None
        receipt.last_error_code = ""
        receipt.last_error = ""
        receipt.save(
            update_fields=[
                "status",
                "processing_token",
                "processing_started_at",
                "last_error_code",
                "last_error",
            ]
        )

    @staticmethod
    def _reject_receipt(receipt: CallbackReceipt, error: str) -> None:
        """Commit a permanent callback rejection and release its claim."""
        receipt.status = CallbackReceiptStatus.REJECTED
        receipt.processing_token = None
        receipt.processing_started_at = None
        receipt.last_error_code = "callback_rejected"
        receipt.last_error = error[:2000]
        receipt.save(
            update_fields=[
                "status",
                "processing_token",
                "processing_started_at",
                "last_error_code",
                "last_error",
            ]
        )

    # ── Submission purge ──────────────────────────────────────────────

    @staticmethod
    def _queue_purge_if_do_not_store(run: ValidationRun) -> None:
        """
        Queue submission purge if the retention policy is DO_NOT_STORE.

        Validator callbacks should be fast and reliable. Instead of purging
        submission content inline (which may require deleting many GCS objects),
        we enqueue a purge record for the scheduled purge worker to process.

        Args:
            run: The ValidationRun that just completed.
        """
        from validibot.submissions.constants import DataRetention
        from validibot.submissions.models import queue_submission_purge

        submission = run.submission
        if not submission:
            return

        if submission.retention_policy != DataRetention.DO_NOT_STORE:
            return

        if submission.content_purged_at:
            return

        try:
            queue_submission_purge(submission)
            logger.info(
                "Queued DO_NOT_STORE submission purge after run completion",
                extra={
                    "submission_id": str(submission.id),
                    "run_id": str(run.id),
                },
            )
        except Exception:
            logger.exception(
                "Failed to queue DO_NOT_STORE submission purge",
                extra={
                    "submission_id": str(submission.id),
                    "run_id": str(run.id),
                },
            )
