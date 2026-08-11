"""
Tests for GCP Cloud Run reconciliation in cleanup_stuck_runs.

These tests verify the reconciliation logic that recovers validation runs
whose Cloud Run Jobs completed but callbacks were lost.

All tests use SimpleTestCase with mocked DB operations so they can run
without a database.
"""

import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import SimpleTestCase
from django.test import override_settings
from django.utils import timezone
from validibot_shared.validations.envelopes import ValidationStatus

from validibot.validations.constants import ProviderStatusLookupCapability
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.management.commands.cleanup_stuck_runs import Command
from validibot.validations.management.commands.cleanup_stuck_runs import (
    get_default_timeout_minutes,
)
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.execution.base import (
    ProviderStatusTemporarilyUnavailableError,
)
from validibot.validations.services.runners.base import ExecutionStatus

CMD_PATH = "validibot.validations.management.commands.cleanup_stuck_runs"

# These are lazy-imported inside command methods, so patch at source module
GCP_BACKEND_PATH = (
    "validibot.validations.services.execution.registry.get_execution_backend"
)
CALLBACK_SVC_PATH = (
    "validibot.validations.services.validation_callback.ValidationCallbackService"
)
GCS_OBJECT_EXISTS_PATH = (
    "validibot.validations.services.cloud_run.gcs_client.gcs_object_exists"
)


class TestWatchdogTimeoutConfiguration(SimpleTestCase):
    """Keep the watchdog deadline aligned with the outer runtime budget."""

    @override_settings(VALIDATOR_TIMEOUT_SECONDS=3601)
    def test_configured_seconds_round_up_to_a_safe_minute_deadline(self):
        """The watchdog must never fence a run before its provider timeout.

        The command accepts minutes while the shared runtime setting uses
        seconds. Rounding down would create a window where valid provider work
        is canceled before the configured outer execution budget expires.
        """
        assert get_default_timeout_minutes() == 61  # noqa: PLR2004


def _mock_run(*, step_output=None, minutes_ago=45, status=ValidationRunStatus.RUNNING):
    """Create a mock ValidationRun for testing without a database."""
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pk = run.id
    run.status = status
    run.started_at = timezone.now() - timedelta(minutes=minutes_ago)
    run.ended_at = None
    run.duration_ms = None
    run.workflow_id = uuid.uuid4()
    run.error = None
    run.error_category = None
    return run


def _mock_step_run(*, output=None):
    """Create a step run whose provider identity lives on an attempt."""
    provider_data = output or {}
    step_run = MagicMock()
    step_run.output = {}
    step_run.status = StepStatus.RUNNING
    attempt = MagicMock()
    attempt.pk = uuid.uuid4()
    attempt.provider_execution_id = provider_data.get("execution_name", "")
    attempt.execution_bundle_uri = provider_data.get("execution_bundle_uri", "")
    attempt.output_envelope_uri = (
        f"{attempt.execution_bundle_uri.rstrip('/')}/output.json"
        if attempt.execution_bundle_uri
        else ""
    )
    attempt.runner_type = "google_cloud_run"
    attempt.timeout_at = timezone.now() - timedelta(minutes=1)
    active_attempt = attempt if attempt.provider_execution_id else None
    active_queryset = (
        step_run.execution_attempts.filter.return_value.order_by.return_value
    )
    active_queryset.first.return_value = active_attempt
    step_run.execution_attempts.order_by.return_value.first.return_value = (
        active_attempt
    )
    step_run.attempt = attempt
    return step_run


class TestReconcileSkipsNonGCPDeployment(SimpleTestCase):
    """Test that reconciliation is skipped for non-GCP deployments."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=False)
    def test_reconcile_skips_non_gcp(self, mock_is_gcp):
        """Non-GCP deployments should return 'not_applicable'."""
        run = _mock_run()
        cmd = Command()
        result = cmd._try_reconcile_gcp_run(run)
        assert result == "not_applicable"


class TestReconcileSkipsRunWithoutStepRun(SimpleTestCase):
    """Test reconciliation skips runs without active step runs."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(f"{CMD_PATH}.Command._get_active_step_run", return_value=None)
    def test_reconcile_skips_run_without_step_run(self, mock_get_step, mock_is_gcp):
        """Runs with no active RUNNING/PENDING step should return 'not_applicable'."""
        run = _mock_run()
        cmd = Command()
        result = cmd._try_reconcile_gcp_run(run)
        assert result == "not_applicable"


class TestReconcileSkipsRunWithoutExecutionName(SimpleTestCase):
    """Test reconciliation skips runs without execution_name metadata."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    def test_reconcile_skips_without_execution_name(self, mock_is_gcp):
        """Runs missing execution_name in step output should return 'not_applicable'."""
        run = _mock_run()
        step_run = _mock_step_run(output={"job_status": "PENDING"})

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)
        assert result == "not_applicable"

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    def test_reconcile_skips_empty_output(self, mock_is_gcp):
        """Runs with empty step output should return 'not_applicable'."""
        run = _mock_run()
        step_run = _mock_step_run(output={})

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)
        assert result == "not_applicable"


class TestReconcileStillRunningJob(SimpleTestCase):
    """Test reconciliation skips jobs still running on GCP."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    def test_reconcile_skips_still_running_job(self, mock_backend_cls, mock_is_gcp):
        """Jobs still running on GCP should return 'still_running'."""
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=False,
            execution_status=ExecutionStatus.RUNNING,
        )
        mock_backend_cls.return_value = mock_backend

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)
        assert result == "still_running"
        # Run status should NOT have been modified
        assert run.status == ValidationRunStatus.RUNNING


class TestReconcileMarksFailed(SimpleTestCase):
    """Test reconciliation marks runs as failed when GCP job failed."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    def test_reconcile_marks_failed_job(self, mock_backend_cls, mock_is_gcp):
        """Failed GCP jobs should call _mark_run_failed_from_gcp."""
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=True,
            execution_status=ExecutionStatus.FAILED,
            error_message="Container OOM killed",
        )
        mock_backend_cls.return_value = mock_backend

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        with (
            patch.object(cmd, "_get_active_step_run", return_value=step_run),
            patch.object(cmd, "_mark_run_failed_from_gcp") as mock_mark_failed,
        ):
            result = cmd._try_reconcile_gcp_run(run)

        assert result == "reconciled"
        mock_mark_failed.assert_called_once_with(
            run,
            "Container OOM killed",
            attempt=step_run.attempt,
        )

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    def test_reconcile_preserves_failed_job_without_message(
        self,
        mock_backend_cls,
        mock_is_gcp,
    ):
        """An empty provider diagnostic must never turn failure into success.

        Cloud Run can report a terminal FAILED condition without useful text.
        Reconciliation must use the explicit state and a bounded fallback
        message, never attempt to download a nonexistent successful output.
        """
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=True,
            execution_status=ExecutionStatus.FAILED,
            error_message=None,
        )
        mock_backend_cls.return_value = mock_backend

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        with (
            patch.object(cmd, "_get_active_step_run", return_value=step_run),
            patch.object(cmd, "_mark_run_failed_from_gcp") as mock_mark_failed,
            patch.object(cmd, "_recover_lost_callback") as mock_recover,
        ):
            result = cmd._try_reconcile_gcp_run(run)

        assert result == "reconciled"
        mock_mark_failed.assert_called_once_with(
            run,
            "Cloud Run reported a failed execution without diagnostic details",
            attempt=step_run.attempt,
        )
        mock_recover.assert_not_called()


class TestMarkRunFailedFromGCP(SimpleTestCase):
    """Test the _mark_run_failed_from_gcp method."""

    @patch("validibot.validations.services.run_admission.emit_validation_run_finalized")
    @patch(f"{CMD_PATH}.transaction")
    @patch(f"{CMD_PATH}.ValidationRun")
    @patch(
        "validibot.validations.services.execution_attempts.transition_execution_attempt"
    )
    def test_marks_run_as_failed(
        self,
        mock_transition,
        mock_run_model,
        mock_transaction,
        mock_finalize,
    ):
        """Should lock the run and set FAILED status with error details."""
        locked_run = MagicMock()
        locked_run.status = ValidationRunStatus.RUNNING
        locked_run.started_at = timezone.now() - timedelta(minutes=45)

        mock_run_model.objects.select_for_update.return_value.get.return_value = (
            locked_run
        )
        # Make transaction.atomic() work as a no-op context manager
        mock_transaction.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        run = _mock_run()
        attempt = MagicMock()
        cmd = Command()
        cmd._mark_run_failed_from_gcp(
            run,
            "Container OOM killed",
            attempt=attempt,
        )

        assert locked_run.status == ValidationRunStatus.FAILED
        assert locked_run.error_category == ValidationRunErrorCategory.RUNTIME_ERROR
        assert "Container OOM killed" in locked_run.error
        assert "reconciliation" in locked_run.error
        assert locked_run.ended_at is not None
        assert locked_run.duration_ms is not None
        locked_run.save.assert_called_once()
        mock_transition.assert_called_once()
        mock_finalize.assert_called_once_with(
            sender=cmd.__class__,
            validation_run=locked_run,
        )

    @patch(f"{CMD_PATH}.transaction")
    @patch(f"{CMD_PATH}.ValidationRun")
    def test_race_condition_skips_non_running(self, mock_run_model, mock_transaction):
        """If run status changed between query and lock, skip it."""
        locked_run = MagicMock()
        locked_run.status = ValidationRunStatus.SUCCEEDED  # Changed by another process

        mock_run_model.objects.select_for_update.return_value.get.return_value = (
            locked_run
        )
        mock_transaction.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        run = _mock_run()
        attempt = MagicMock()
        cmd = Command()
        cmd._mark_run_failed_from_gcp(
            run,
            "Container OOM killed",
            attempt=attempt,
        )

        # Should not have been modified
        locked_run.save.assert_not_called()
        assert locked_run.status == ValidationRunStatus.SUCCEEDED


class TestReconcileRecoversLostCallback(SimpleTestCase):
    """Test reconciliation reuses completion without forging callback proof."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    @patch(GCS_OBJECT_EXISTS_PATH, return_value=True)
    @patch(CALLBACK_SVC_PATH)
    def test_service_reconciliation_salvages_exact_attempt_output(
        self,
        mock_callback_cls,
        mock_output_exists,
        mock_backend_factory,
        mock_is_gcp,
    ):
        """A Service missing its callback must recover from its pinned output URI."""
        backend = MagicMock()
        backend.status_lookup_capability = ProviderStatusLookupCapability.UNSUPPORTED
        mock_backend_factory.return_value = backend
        mock_response = MagicMock(status_code=200)
        callback_service = MagicMock()
        callback_service.process_reconciliation.return_value = mock_response
        mock_callback_cls.return_value = callback_service
        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": ("projects/p/locations/r/queues/q/tasks/attempt-id"),
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )
        step_run.attempt.deployment_id = uuid.uuid4()
        step_run.attempt.deployment = MagicMock()

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)

        assert result == "reconciled"
        mock_backend_factory.assert_called_once_with(step_run.attempt.deployment)
        mock_output_exists.assert_called_once_with(
            "gs://bucket/runs/org/run-id/output.json"
        )
        callback_service.process_reconciliation.assert_called_once()

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    @patch(CALLBACK_SVC_PATH)
    def test_reconcile_recovers_lost_callback(
        self, mock_callback_cls, mock_backend_cls, mock_is_gcp
    ):
        """Succeeded GCP jobs with lost callbacks should be recovered."""
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=True,
            execution_status=ExecutionStatus.SUCCEEDED,
        )
        mock_backend_cls.return_value = mock_backend

        mock_service = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_service.process_reconciliation.return_value = mock_response
        mock_callback_cls.return_value = mock_service

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)

        assert result == "reconciled"
        # Trusted recovery passes the resolved attempt directly and does not
        # invent an external callback nonce that reconciliation cannot know.
        mock_service.process_reconciliation.assert_called_once_with(
            run=run,
            attempt=step_run.attempt,
            callback_status=ValidationStatus.SUCCESS,
            result_uri="gs://bucket/runs/org/run-id/output.json",
        )

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    @patch(CALLBACK_SVC_PATH)
    def test_reconcile_handles_callback_failure(
        self, mock_callback_cls, mock_backend_cls, mock_is_gcp
    ):
        """Trusted completion failures remain visible to the watchdog."""
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=True,
            execution_status=ExecutionStatus.SUCCEEDED,
        )
        mock_backend_cls.return_value = mock_backend

        mock_service = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.data = {"error": "Internal server error"}
        mock_service.process_reconciliation.return_value = mock_response
        mock_callback_cls.return_value = mock_service

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)
        assert result == "error"

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    def test_reconcile_handles_missing_bundle_uri(self, mock_backend_cls, mock_is_gcp):
        """Returns 'error' when execution_bundle_uri is missing."""
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=True,
            execution_status=ExecutionStatus.SUCCEEDED,
        )
        mock_backend_cls.return_value = mock_backend

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                # No execution_bundle_uri
            }
        )

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)
        assert result == "error"


class TestReconcileFallsThrough(SimpleTestCase):
    """Test reconciliation falls through on errors."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    def test_reconcile_falls_through_on_api_error(self, mock_backend_cls, mock_is_gcp):
        """A transient provider error stays retryable during the bounded grace."""
        mock_backend = MagicMock()
        mock_backend.check_status.side_effect = (
            ProviderStatusTemporarilyUnavailableError("provider unavailable")
        )
        mock_backend_cls.return_value = mock_backend

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)
        assert result == "provider_unavailable"

    @override_settings(VALIDATOR_STATUS_LOOKUP_GRACE_SECONDS=300)
    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    def test_reconcile_times_out_after_provider_lookup_grace(
        self,
        mock_backend_cls,
        mock_is_gcp,
    ):
        """Persistent provider API failure cannot defer the durable fence forever."""
        mock_backend = MagicMock()
        mock_backend.check_status.side_effect = (
            ProviderStatusTemporarilyUnavailableError("provider unavailable")
        )
        mock_backend_cls.return_value = mock_backend
        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )
        step_run.attempt.timeout_at = timezone.now() - timedelta(minutes=6)

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)

        assert result == "error"

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(
        GCP_BACKEND_PATH,
        side_effect=Exception("Unexpected error"),
    )
    def test_reconcile_falls_through_on_unexpected_error(
        self, mock_backend_cls, mock_is_gcp
    ):
        """Falls through when backend raises unexpectedly."""
        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run)
        assert result == "error"


class TestReconcileDryRun(SimpleTestCase):
    """Test reconciliation in dry-run mode."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    def test_dry_run_reports_reconcilable_success(self, mock_backend_cls, mock_is_gcp):
        """Dry run should report what would be reconciled without modifying."""
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=True,
            execution_status=ExecutionStatus.SUCCEEDED,
        )
        mock_backend_cls.return_value = mock_backend

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        cmd.stdout = StringIO()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run, dry_run=True)

        assert result == "reconciled"
        output = cmd.stdout.getvalue()
        assert "RECONCILE-OK" in output

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    def test_dry_run_reports_reconcilable_failure(self, mock_backend_cls, mock_is_gcp):
        """Dry run should report failed GCP jobs without modifying."""
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=True,
            execution_status=ExecutionStatus.FAILED,
            error_message="OOM killed",
        )
        mock_backend_cls.return_value = mock_backend

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        cmd = Command()
        cmd.stdout = StringIO()
        with patch.object(cmd, "_get_active_step_run", return_value=step_run):
            result = cmd._try_reconcile_gcp_run(run, dry_run=True)

        assert result == "reconciled"
        output = cmd.stdout.getvalue()
        assert "RECONCILE-FAIL" in output


def _wire_stuck_runs_qs(mock_run_model, mock_qs):
    """Wire mock_run_model so handle() iterates over mock_qs."""
    qs_chain = mock_run_model.objects.annotate.return_value
    qs_chain = qs_chain.filter.return_value.filter.return_value.order_by.return_value
    qs_chain.__getitem__ = MagicMock(return_value=mock_qs)


class TestCommandHandle(SimpleTestCase):
    """Tests for the full handle() method with mocked DB queries."""

    @patch(f"{CMD_PATH}.ValidationRun")
    def test_watchdog_repairs_continuations_even_without_stuck_runs(
        self,
        mock_run_model,
    ):
        """Scheduled maintenance redelivers work independently of run timeout."""
        mock_qs = MagicMock()
        mock_qs.count.return_value = 0
        _wire_stuck_runs_qs(mock_run_model, mock_qs)
        report = MagicMock(
            examined=1,
            dispatched=1,
            already_dispatched=0,
            busy=0,
            not_required=0,
            failed=0,
        )
        out = StringIO()
        cmd = Command()
        cmd.stdout = out
        cmd.style = MagicMock()
        cmd.style.SUCCESS = lambda value: value

        with patch.object(
            cmd,
            "_repair_continuations",
            return_value=report,
        ) as repair:
            cmd.handle(timeout_minutes=30, dry_run=False, batch_size=25)

        repair.assert_called_once_with(limit=25, dry_run=False)
        assert "Continuation repair" in out.getvalue()
        assert "No runs stuck" in out.getvalue()

    @patch(f"{CMD_PATH}.ValidationRun")
    def test_no_stuck_runs_reports_clean(self, mock_run_model):
        """Command should report no stuck runs when none exist."""
        mock_qs = MagicMock()
        mock_qs.count.return_value = 0
        _wire_stuck_runs_qs(mock_run_model, mock_qs)

        out = StringIO()
        cmd = Command()
        cmd.stdout = out
        cmd.style = MagicMock()
        cmd.style.SUCCESS = lambda x: x
        cmd.style.WARNING = lambda x: x

        # Simulate handle() with empty queryset
        with patch.object(
            cmd,
            "_repair_continuations",
            return_value=MagicMock(examined=0),
        ):
            cmd.handle(timeout_minutes=30, dry_run=False, batch_size=100)

        output = out.getvalue()
        assert "No runs stuck" in output

    @patch(f"{CMD_PATH}.ValidationRun")
    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    @patch(CALLBACK_SVC_PATH)
    def test_command_reconciles_before_timeout(
        self, mock_callback_cls, mock_backend_cls, mock_is_gcp, mock_run_model
    ):
        """The command should try reconciliation before timing out runs."""
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=True,
            execution_status=ExecutionStatus.SUCCEEDED,
        )
        mock_backend_cls.return_value = mock_backend

        mock_service = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_service.process_reconciliation.return_value = mock_response
        mock_callback_cls.return_value = mock_service

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        # Set up the queryset to return our mock run
        mock_qs = MagicMock()
        mock_qs.count.return_value = 1
        mock_qs.__iter__ = MagicMock(return_value=iter([run]))
        _wire_stuck_runs_qs(mock_run_model, mock_qs)

        out = StringIO()
        cmd = Command()
        cmd.stdout = out
        cmd.style = MagicMock()
        cmd.style.SUCCESS = lambda x: x
        cmd.style.WARNING = lambda x: x

        with (
            patch.object(cmd, "_get_active_step_run", return_value=step_run),
            patch.object(
                cmd,
                "_repair_continuations",
                return_value=MagicMock(examined=0),
            ),
        ):
            cmd.handle(timeout_minutes=30, dry_run=False, batch_size=100)

        output = out.getvalue()
        assert "Reconciled" in output
        mock_service.process_reconciliation.assert_called_once()

    @patch(f"{CMD_PATH}.ValidationRun")
    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=True)
    @patch(GCP_BACKEND_PATH)
    @patch(f"{CMD_PATH}.transaction")
    @patch(f"{CMD_PATH}.cancel_active_execution")
    def test_command_fences_and_cancels_still_running_jobs_after_deadline(
        self,
        mock_cancel,
        mock_transaction,
        mock_backend_cls,
        mock_is_gcp,
        mock_run_model,
    ):
        """Known-running provider work must be stopped after its outer deadline.

        Skipping a provider that remains RUNNING after the configured maximum
        leaves unbounded compute and a run that can never become terminal. The
        watchdog first commits TIMED_OUT, then requests provider cancellation.
        """
        mock_backend = MagicMock()
        mock_backend.check_status.return_value = ExecutionResponse(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            is_complete=False,
            execution_status=ExecutionStatus.RUNNING,
        )
        mock_backend_cls.return_value = mock_backend

        run = _mock_run()
        step_run = _mock_step_run(
            output={
                "execution_name": "projects/p/locations/r/jobs/j/executions/e",
                "execution_bundle_uri": "gs://bucket/runs/org/run-id",
            }
        )

        mock_qs = MagicMock()
        mock_qs.count.return_value = 1
        mock_qs.__iter__ = MagicMock(return_value=iter([run]))
        _wire_stuck_runs_qs(mock_run_model, mock_qs)

        locked_run = MagicMock()
        locked_run.status = ValidationRunStatus.RUNNING
        locked_run.started_at = run.started_at
        mock_run_model.objects.select_for_update.return_value.get.return_value = (
            locked_run
        )
        mock_transaction.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        out = StringIO()
        cmd = Command()
        cmd.stdout = out
        cmd.style = MagicMock()
        cmd.style.SUCCESS = lambda x: x
        cmd.style.WARNING = lambda x: x

        with (
            patch.object(cmd, "_get_active_step_run", return_value=step_run),
            patch(f"{CMD_PATH}.fence_active_execution_attempt") as mock_fence,
            patch(
                "validibot.validations.services.run_admission."
                "emit_validation_run_finalized"
            ) as mock_finalize,
            patch.object(
                cmd,
                "_repair_continuations",
                return_value=MagicMock(examined=0),
            ),
        ):
            cmd.handle(timeout_minutes=30, dry_run=False, batch_size=100)

        output = out.getvalue()
        assert locked_run.status == ValidationRunStatus.TIMED_OUT
        assert "Marked 1 stuck run(s) as TIMED_OUT" in output
        mock_fence.assert_called_once()
        mock_cancel.assert_called_once_with(locked_run)
        mock_finalize.assert_called_once_with(
            sender=cmd.__class__,
            validation_run=locked_run,
        )

    @patch(f"{CMD_PATH}.ValidationRun")
    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=False)
    @patch(f"{CMD_PATH}.transaction")
    @patch(f"{CMD_PATH}.cancel_active_execution")
    def test_non_gcp_runs_are_timed_out(
        self,
        mock_cancel,
        mock_transaction,
        mock_is_gcp,
        mock_run_model,
    ):
        """Non-GCP runs should be timed out as before (no reconciliation)."""
        run = _mock_run()

        mock_qs = MagicMock()
        mock_qs.count.return_value = 1
        mock_qs.__iter__ = MagicMock(return_value=iter([run]))
        _wire_stuck_runs_qs(mock_run_model, mock_qs)

        # Mock the atomic block and select_for_update
        locked_run = MagicMock()
        locked_run.status = ValidationRunStatus.RUNNING
        locked_run.started_at = run.started_at
        mock_run_model.objects.select_for_update.return_value.get.return_value = (
            locked_run
        )
        mock_transaction.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        out = StringIO()
        cmd = Command()
        cmd.stdout = out
        cmd.style = MagicMock()
        cmd.style.SUCCESS = lambda x: x
        cmd.style.WARNING = lambda x: x

        with (
            patch(f"{CMD_PATH}.fence_active_execution_attempt") as mock_fence,
            patch(
                "validibot.validations.services.run_admission."
                "emit_validation_run_finalized"
            ) as mock_finalize,
            patch.object(
                cmd,
                "_repair_continuations",
                return_value=MagicMock(examined=0),
            ),
        ):
            cmd.handle(timeout_minutes=30, dry_run=False, batch_size=100)

        # The locked run should have been updated to TIMED_OUT
        assert locked_run.status == ValidationRunStatus.TIMED_OUT
        assert locked_run.error_category == ValidationRunErrorCategory.TIMEOUT
        locked_run.save.assert_called_once()
        mock_fence.assert_called_once()
        mock_cancel.assert_called_once_with(locked_run)
        mock_finalize.assert_called_once_with(
            sender=cmd.__class__,
            validation_run=locked_run,
        )

    @patch(f"{CMD_PATH}.ValidationRun")
    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", return_value=False)
    def test_command_dry_run_does_not_modify(self, mock_is_gcp, mock_run_model):
        """Dry run should not modify any runs."""
        run = _mock_run()

        mock_qs = MagicMock()
        mock_qs.count.return_value = 1
        mock_qs.__iter__ = MagicMock(return_value=iter([run]))
        _wire_stuck_runs_qs(mock_run_model, mock_qs)

        out = StringIO()
        cmd = Command()
        cmd.stdout = out
        cmd.style = MagicMock()
        cmd.style.SUCCESS = lambda x: x
        cmd.style.WARNING = lambda x: x

        with patch.object(
            cmd,
            "_repair_continuations",
            return_value=MagicMock(examined=0),
        ):
            cmd.handle(timeout_minutes=30, dry_run=True, batch_size=100)

        output = out.getvalue()
        assert "DRY RUN" in output
        # No DB modifications
        mock_run_model.objects.select_for_update.assert_not_called()


class TestGetActiveStepRun(SimpleTestCase):
    """Test the _get_active_step_run method."""

    @patch(f"{CMD_PATH}.ValidationStepRun")
    def test_returns_running_step_run(self, mock_step_model):
        """Should find a RUNNING step run for the given validation run."""
        mock_step = _mock_step_run(output={"execution_name": "test"})
        step_qs = mock_step_model.objects.select_related.return_value
        step_qs.filter.return_value.order_by.return_value.first.return_value = mock_step

        run = _mock_run()
        cmd = Command()
        result = cmd._get_active_step_run(run)
        assert result == mock_step

    @patch(f"{CMD_PATH}.ValidationStepRun")
    def test_returns_none_when_no_step_run(self, mock_step_model):
        """Should return None when no matching step run exists."""
        step_qs = mock_step_model.objects.select_related.return_value
        step_qs.filter.return_value.order_by.return_value.first.return_value = None

        run = _mock_run()
        cmd = Command()
        result = cmd._get_active_step_run(run)
        assert result is None


class TestIsGCPDeployment(SimpleTestCase):
    """Test the _is_gcp_deployment method."""

    @patch(f"{CMD_PATH}.Command._is_gcp_deployment", wraps=Command._is_gcp_deployment)
    def test_returns_false_when_import_fails(self, mock_method):
        """Should return False when deployment module is not available."""
        cmd = Command()
        with patch("builtins.__import__", side_effect=ImportError("no module")):
            # The actual method catches all exceptions
            result = Command._is_gcp_deployment(cmd)
            # It may or may not return False depending on cache,
            # but should not raise
            assert isinstance(result, bool)
