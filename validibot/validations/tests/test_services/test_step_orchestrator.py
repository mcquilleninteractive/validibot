"""Tests for StepOrchestrator internal methods.

Covers step lifecycle (_start_step_run) idempotency and canonical persistence
of action results by _record_step_result.
"""

from __future__ import annotations

import json
from collections import Counter
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import ActionFailureMode
from validibot.actions.constants import CredentialActionType
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.credential_issuance import CredentialIssuanceError
from validibot.validations.services.credential_issuance import (
    register_credential_issuer,
)
from validibot.validations.services.credential_issuance import reset_credential_issuer
from validibot.validations.services.step_orchestrator import StepOrchestrator
from validibot.validations.services.step_orchestrator import _StepRunDisposition
from validibot.validations.services.step_processor.result import StepProcessingResult
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base import ValidationResult
from validibot.workflows.tests.factories import WorkflowStepFactory

DELIVERY_MS = 12

# ---------- _start_step_run ----------


@pytest.mark.django_db
class TestStartStepRun:
    """Test _start_step_run idempotency and retry behavior."""

    def setup_method(self):
        self.orchestrator = StepOrchestrator()
        self.run = ValidationRunFactory()
        self.wf_step = WorkflowStepFactory(workflow=self.run.workflow)

    def test_new_step_creates_running_step_run(self):
        """First call creates a new step run with RUNNING status."""
        admission = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        step_run = admission.step_run
        assert admission.disposition is _StepRunDisposition.EXECUTE
        assert step_run.status == StepStatus.RUNNING
        assert step_run.started_at is not None
        assert step_run.step_order == (self.wf_step.order or 0)

    @pytest.mark.parametrize(
        "terminal_status",
        [
            StepStatus.PASSED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
        ],
    )
    def test_terminal_step_skips_execution(self, terminal_status):
        """A step that already finished returns should_execute=False."""
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=terminal_status,
        )

        admission = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        step_run = admission.step_run
        assert admission.disposition is _StepRunDisposition.TERMINAL
        assert step_run.id == existing.id
        # Status should not have changed
        step_run.refresh_from_db()
        assert step_run.status == terminal_status

    def test_running_step_preserves_active_ownership(self):
        """A duplicate delivery must not reinterpret active work as abandoned."""
        old_time = timezone.now() - timezone.timedelta(minutes=5)
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=StepStatus.RUNNING,
            started_at=old_time,
        )

        admission = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        step_run = admission.step_run
        assert admission.disposition is _StepRunDisposition.IN_PROGRESS
        assert step_run.id == existing.id
        step_run.refresh_from_db()
        assert step_run.started_at == old_time

    def test_running_step_preserves_partial_findings(self):
        """Duplicate admission cannot erase evidence owned by the live worker."""
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=StepStatus.RUNNING,
        )
        # Simulate partial findings from a crashed prior attempt
        ValidationFinding.objects.create(
            validation_run=self.run,
            validation_step_run=existing,
            severity=Severity.ERROR,
            message="stale finding from crashed attempt",
        )
        assert (
            ValidationFinding.objects.filter(
                validation_step_run=existing,
            ).count()
            == 1
        )

        admission = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert admission.disposition is _StepRunDisposition.IN_PROGRESS
        assert (
            ValidationFinding.objects.filter(
                validation_step_run=admission.step_run,
            ).count()
            == 1
        )

    def test_idempotent_on_second_new_call(self):
        """Two calls for the same (run, step) return the same step run."""
        first = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )
        second = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert first.disposition is _StepRunDisposition.EXECUTE
        assert second.disposition is _StepRunDisposition.IN_PROGRESS
        assert first.step_run.id == second.step_run.id
        # Only one row should exist
        assert (
            ValidationStepRun.objects.filter(
                validation_run=self.run,
                workflow_step=self.wf_step,
            ).count()
            == 1
        )

    @patch.object(StepOrchestrator, "_execute_validator_step")
    def test_duplicate_resume_stops_at_active_step_without_skipping(self, execute):
        """At-least-once delivery cannot repeat or advance beyond active work."""
        self.run.status = ValidationRunStatus.RUNNING
        self.run.save(update_fields=["status"])
        first = self.wf_step
        first.order = 10
        first.save(update_fields=["order"])
        second = WorkflowStepFactory(
            workflow=self.run.workflow,
            validator=ValidatorFactory(),
            order=20,
        )
        active = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=first,
            step_order=first.order,
            status=StepStatus.RUNNING,
        )

        result = self.orchestrator.execute_workflow_steps(
            validation_run_id=self.run.pk,
            user_id=self.run.user_id,
            resume_from_step=0,
        )

        assert result.status == ValidationRunStatus.RUNNING
        execute.assert_not_called()
        active.refresh_from_db()
        assert active.status == StepStatus.RUNNING
        assert not ValidationStepRun.objects.filter(
            validation_run=self.run,
            workflow_step=second,
        ).exists()


@pytest.mark.django_db
class TestRecordActionStepResult:
    """Verify action values are durable execution state, separate from stats."""

    def test_action_output_values_are_persisted_canonically(self):
        """Handler outputs must survive for downstream steps and audit hashes."""
        orchestrator = StepOrchestrator()
        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            status=StepStatus.RUNNING,
        )

        orchestrator._record_step_result(
            validation_run=run,
            step_run=step_run,
            validation_result=ValidationResult(
                passed=True,
                issues=[],
                output_values={"credential_id": "credential-123"},
                stats={"delivery_ms": DELIVERY_MS},
            ),
        )

        step_run.refresh_from_db()
        assert step_run.output_values == {"credential_id": "credential-123"}
        assert step_run.output["delivery_ms"] == DELIVERY_MS


@pytest.mark.django_db
class TestDeferredSignedCredentialIssuance:
    """Verify signed credentials are issued after run finalization."""

    @pytest.fixture(autouse=True)
    def _isolate_credential_issuer(self):
        """Keep the process-wide provider registry deterministic per test."""
        reset_credential_issuer()
        yield
        reset_credential_issuer()

    def test_signed_credential_issues_after_run_is_succeeded(self):
        """Credential issuance must happen after the run reaches SUCCEEDED."""
        orchestrator = StepOrchestrator()
        run = ValidationRunFactory(status=ValidationRunStatus.PENDING)
        validator = ValidatorFactory()
        WorkflowStepFactory(
            workflow=run.workflow,
            order=10,
            validator=validator,
        )
        credential_definition = ActionDefinition.objects.create(
            slug="signed-credential",
            name="Signed credential",
            action_category=ActionCategoryType.CREDENTIAL,
            type=CredentialActionType.SIGNED_CREDENTIAL,
            is_active=True,
        )
        credential_action = Action.objects.create(
            definition=credential_definition,
            slug="credential-action",
            name="Credential action",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        credential_step = WorkflowStepFactory(
            workflow=run.workflow,
            order=20,
            validator=None,
            action=credential_action,
        )

        def fake_execute_validator_step(*, validation_run, step_run):
            finalized = orchestrator._finalize_step_run(
                step_run=step_run,
                status=StepStatus.PASSED,
                stats={},
                error=None,
            )
            return StepProcessingResult(
                passed=True,
                step_run=finalized,
                severity_counts=Counter(),
                total_findings=0,
                assertion_failures=0,
                assertion_total=0,
            )

        issued_id = uuid4()

        def fake_issue(step_run):
            """Assert the permanent manifest exists before Pro is invoked."""

            artifact = step_run.validation_run.evidence_artifact
            assert artifact.manifest_path
            assert artifact.manifest_hash
            return str(issued_id)

        fake_issue_credential = Mock(side_effect=fake_issue)
        register_credential_issuer(
            fake_issue_credential,
            provider_name="test.credential_issuer",
        )
        with patch.object(
            orchestrator,
            "_execute_validator_step",
            side_effect=fake_execute_validator_step,
        ):
            result = orchestrator.execute_workflow_steps(run.id, run.user_id)

        run.refresh_from_db()
        step_run = ValidationStepRun.objects.get(
            validation_run=run,
            workflow_step=credential_step,
        )
        assert result.status == ValidationRunStatus.SUCCEEDED
        assert run.status == ValidationRunStatus.SUCCEEDED
        fake_issue_credential.assert_called_once_with(step_run)
        assert step_run.status == StepStatus.PASSED
        assert step_run.output["credential_issuance"] == "issued"
        assert step_run.output["credential_id"] == str(issued_id)

    @pytest.mark.parametrize(
        ("failure_mode", "expected_status", "expected_severity"),
        [
            pytest.param(
                ActionFailureMode.ADVISORY,
                ValidationRunStatus.SUCCEEDED,
                Severity.WARNING,
                id="advisory",
            ),
            pytest.param(
                ActionFailureMode.BLOCKING,
                ValidationRunStatus.FAILED,
                Severity.ERROR,
                id="blocking",
            ),
        ],
    )
    def test_credential_failure_is_reflected_in_the_final_manifest(
        self,
        failure_mode,
        expected_status,
        expected_severity,
    ):
        """The permanent receipt must record the final post-issuance outcome."""
        orchestrator = StepOrchestrator()
        run = ValidationRunFactory(status=ValidationRunStatus.PENDING)
        validator = ValidatorFactory()
        WorkflowStepFactory(
            workflow=run.workflow,
            order=10,
            validator=validator,
        )
        credential_definition = ActionDefinition.objects.create(
            slug="signed-credential-advisory",
            name="Signed credential",
            action_category=ActionCategoryType.CREDENTIAL,
            type=CredentialActionType.SIGNED_CREDENTIAL,
            is_active=True,
        )
        credential_action = Action.objects.create(
            definition=credential_definition,
            slug="credential-action-advisory",
            name="Credential action",
            failure_mode=failure_mode,
        )
        credential_step = WorkflowStepFactory(
            workflow=run.workflow,
            order=20,
            validator=None,
            action=credential_action,
        )

        def fake_execute_validator_step(*, validation_run, step_run):
            finalized = orchestrator._finalize_step_run(
                step_run=step_run,
                status=StepStatus.PASSED,
                stats={},
                error=None,
            )
            return StepProcessingResult(
                passed=True,
                step_run=finalized,
                severity_counts=Counter(),
                total_findings=0,
                assertion_failures=0,
                assertion_total=0,
            )

        fake_issue_credential = Mock(
            side_effect=CredentialIssuanceError(
                "Signing backend is not configured.",
            ),
        )
        register_credential_issuer(
            fake_issue_credential,
            provider_name="test.failing_credential_issuer",
        )
        with patch.object(
            orchestrator,
            "_execute_validator_step",
            side_effect=fake_execute_validator_step,
        ):
            result = orchestrator.execute_workflow_steps(run.id, run.user_id)

        run.refresh_from_db()
        step_run = ValidationStepRun.objects.get(
            validation_run=run,
            workflow_step=credential_step,
        )
        finding = ValidationFinding.objects.get(validation_step_run=step_run)
        artifact = run.evidence_artifact
        with artifact.manifest_path.open("rb") as manifest_file:
            manifest = json.load(manifest_file)

        assert result.status == expected_status
        assert run.status == expected_status
        assert step_run.status == StepStatus.FAILED
        assert step_run.output["credential_issuance"] == "failed"
        assert finding.severity == expected_severity
        assert finding.code == "credential_issuance_failed"
        assert manifest["status"] == expected_status
