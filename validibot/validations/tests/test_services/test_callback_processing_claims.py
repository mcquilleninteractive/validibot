"""Concurrency tests for callback processing claims.

Callback output lives in external storage and may take time to download. These
tests ensure database locks protect only claim/application transitions while a
token fences duplicate deliveries across the lock-free storage interval.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.db import close_old_connections
from rest_framework import status
from rest_framework.response import Response

from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.execution_attempts import build_attempt_callback_id
from validibot.validations.services.execution_attempts import (
    build_callback_nonce_verifier,
)
from validibot.validations.services.validation_callback import ValidationCallbackService
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationStepRunFactory

CALLBACK_NONCE = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def _callback_scenario():
    """Create one active attempt and its authenticated callback payload."""
    step_run = ValidationStepRunFactory(
        validation_run__status=ValidationRunStatus.RUNNING,
        status=StepStatus.RUNNING,
    )
    attempt = ExecutionAttemptFactory(
        step_run=step_run,
        state=ExecutionAttemptState.RUNNING,
        callback_nonce_hash=build_callback_nonce_verifier(CALLBACK_NONCE),
        output_envelope_uri="gs://bucket/output.json",
    )
    return (
        step_run,
        attempt,
        {
            "run_id": str(step_run.validation_run_id),
            "callback_id": build_attempt_callback_id(attempt),
            "callback_nonce": CALLBACK_NONCE,
            "status": "success",
            "result_uri": attempt.output_envelope_uri,
        },
    )


@pytest.mark.django_db(transaction=True)
def test_storage_download_does_not_hold_the_callback_database_lock():
    """A duplicate delivery must get a prompt conflict during a slow download."""
    _step_run, _attempt, payload = _callback_scenario()
    download_started = threading.Event()
    release_download = threading.Event()
    output_envelope = MagicMock(name="verified_output_envelope")

    def slow_download(*_args, **_kwargs):
        """Keep the first processor in external work until its rival responds."""
        download_started.set()
        if not release_download.wait(timeout=5):
            raise TimeoutError("test did not release callback download")
        return output_envelope

    def deliver_callback():
        """Give each thread an independent Django connection lifecycle."""
        close_old_connections()
        try:
            return ValidationCallbackService().process(payload=payload)
        finally:
            close_old_connections()

    with (
        patch.object(
            ValidationCallbackService,
            "_download_and_validate_envelope",
            side_effect=slow_download,
        ) as download,
        patch.object(
            ValidationCallbackService,
            "_apply_callback_claim",
            return_value=Response(status=status.HTTP_200_OK),
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(deliver_callback)
        assert download_started.wait(timeout=5)
        second = executor.submit(deliver_callback)
        second_response = second.result(timeout=5)
        release_download.set()
        first_response = first.result(timeout=5)

    assert first_response.status_code == status.HTTP_200_OK
    assert second_response.status_code == status.HTTP_409_CONFLICT
    assert second_response.data["retry"] is True
    download.assert_called_once()
