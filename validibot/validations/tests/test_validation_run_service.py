"""Tests for validation-run launch, orchestration, and summary behavior.

The launch tests pin transaction ordering; the remaining tests protect the
service facade's established run and finding behavior while lifecycle
internals evolve behind it.
"""

from collections import Counter

import pytest
from rest_framework import status
from rest_framework.test import APIRequestFactory

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationRunSummary
from validibot.validations.services.artifact_bindings import set_artifact_input_binding
from validibot.validations.services.step_orchestrator import StepOrchestrator
from validibot.validations.services.step_processor.result import StepProcessingResult
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


@pytest.mark.django_db
def test_launch_commits_run_before_enqueue(monkeypatch):
    """A launch must persist an unreleased definition user before dispatch."""
    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.EXECUTOR)
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    WorkflowStepFactory(workflow=workflow)  # ensure workflow has a validator step
    submission = SubmissionFactory(
        org=org,
        project=workflow.project,
        user=user,
        workflow=workflow,
    )

    factory = APIRequestFactory()
    request = factory.post("/api/v1/workflows/start/")
    request.user = user
    observed_release_markers: list[object] = []

    def observe_enqueued_run(*, validation_run_id, user_id):
        """Record the durable fence at the exact worker-dispatch boundary."""

        del user_id
        admitted_run = ValidationRun.objects.get(pk=validation_run_id)
        observed_release_markers.append(admitted_run.definition_released_at)

    monkeypatch.setattr(
        "validibot.core.tasks.enqueue_validation_run",
        observe_enqueued_run,
    )

    service = ValidationRunService()
    response = service.launch(
        request=request,
        org=org,
        workflow=workflow,
        submission=submission,
        user_id=user.id,
        metadata=None,
    )

    validation_run = response.validation_run
    validation_run.refresh_from_db()
    assert validation_run.pk
    assert observed_release_markers == [None]
    assert validation_run.definition_released_at is None
    assert response.status in {status.HTTP_202_ACCEPTED, status.HTTP_201_CREATED}


@pytest.mark.django_db
def test_launch_enqueue_failure_releases_definition_use(monkeypatch):
    """A run that never reaches a worker must not strand Mutable editing."""

    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.EXECUTOR)
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    WorkflowStepFactory(workflow=workflow)
    submission = SubmissionFactory(
        org=org,
        project=workflow.project,
        user=user,
        workflow=workflow,
    )
    request = APIRequestFactory().post("/api/v1/workflows/start/")
    request.user = user

    def fail_enqueue(**kwargs):
        """Represent an unavailable execution queue after admission commits."""

        del kwargs
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr("validibot.core.tasks.enqueue_validation_run", fail_enqueue)

    response = ValidationRunService().launch(
        request=request,
        org=org,
        workflow=workflow,
        submission=submission,
        user_id=user.id,
    )

    run = response.validation_run
    run.refresh_from_db()
    assert run.status == ValidationRunStatus.FAILED
    assert run.definition_released_at is not None


@pytest.mark.django_db
def test_launch_rejects_submission_after_retention_purge():
    """A purge tombstone must prevent a later run from launching without input."""

    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.EXECUTOR)
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    WorkflowStepFactory(workflow=workflow)
    submission = SubmissionFactory(
        org=org,
        project=workflow.project,
        user=user,
        workflow=workflow,
    )
    submission.purge_content()

    request = APIRequestFactory().post("/api/v1/workflows/start/")
    request.user = user

    with pytest.raises(
        ValueError,
        match="Submission content is no longer available",
    ):
        ValidationRunService().launch(
            request=request,
            org=org,
            workflow=workflow,
            submission=submission,
            user_id=user.id,
        )

    assert not ValidationRun.objects.filter(submission=submission).exists()


@pytest.mark.django_db
def test_execute_fails_gracefully_when_validator_missing():
    """When a validator can't be loaded, the step fails gracefully.

    The refactored ValidatorStepHandler returns a failed StepResult with
    a descriptive error rather than raising an exception. This results in
    the run failing with "One or more validation steps failed" and the
    specific error recorded in the ValidationFinding.
    """
    from unittest.mock import patch

    org = OrganizationFactory()
    user = UserFactory()
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    validator = ValidatorFactory(
        org=org,
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        is_system=False,
    )
    WorkflowStepFactory(workflow=workflow, validator=validator)
    submission = SubmissionFactory(
        org=org,
        project=workflow.project,
        user=user,
        workflow=workflow,
    )
    validation_run = ValidationRun.objects.create(
        org=org,
        workflow=workflow,
        submission=submission,
        project=submission.project,
        user=user,
        status=ValidationRunStatus.PENDING,
    )

    # Simulate missing validator by making the registry raise KeyError
    with patch(
        "validibot.validations.validators.base.config.get_validator_class",
        side_effect=KeyError("CUSTOM_VALIDATOR"),
    ):
        service = ValidationRunService()
        service.execute_workflow_steps(
            validation_run_id=validation_run.id,
            user_id=user.id,
        )

    validation_run.refresh_from_db()
    assert validation_run.status == ValidationRunStatus.FAILED
    assert validation_run.definition_released_at is not None
    # Step failure is now graceful - results in "steps failed" message
    assert "failed" in validation_run.error.lower()
    # The specific validator error is recorded in the findings
    finding = ValidationFinding.objects.filter(
        validation_run=validation_run,
    ).first()
    assert finding is not None
    assert "failed to load" in finding.message.lower()


@pytest.mark.django_db
def test_execute_rejects_incompatible_file_type():
    org = OrganizationFactory()
    user = UserFactory()
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    validator = ValidatorFactory(
        org=org,
        validation_type=ValidationType.JSON_SCHEMA,
        is_system=False,
    )
    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.JSON_SCHEMA,
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator, ruleset=ruleset)
    port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="json_document",
        direction=StepIODirection.INPUT,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        data_format=SubmissionDataFormat.JSON,
        accepted_data_formats=[SubmissionDataFormat.JSON],
        accepted_file_types=[SubmissionFileType.JSON],
        accepted_extensions=["json"],
        allowed_source_scopes=[BindingSourceScope.SUBMISSION_FILE],
        min_items=1,
        max_items=1,
    )
    set_artifact_input_binding(
        consumer_step=step,
        consumer_port=port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
    )
    submission = SubmissionFactory(
        org=org,
        project=workflow.project,
        user=user,
        workflow=workflow,
        file_type=SubmissionFileType.XML,
        content="<root />",
    )
    validation_run = ValidationRun.objects.create(
        org=org,
        workflow=workflow,
        submission=submission,
        project=submission.project,
        user=user,
        status=ValidationRunStatus.PENDING,
    )

    service = ValidationRunService()
    result = service.execute_workflow_step(step=step, validation_run=validation_run)

    assert result.passed is False
    assert result.issues
    assert result.issues[0].code == "input_file_type_incompatible"
    assert "requires JSON" in result.issues[0].message


@pytest.mark.django_db
def test_execute_persists_findings_and_summary(monkeypatch):
    """
    Test that findings, summaries, and assertion stats are correctly persisted
    when executing a validation run via the processor architecture.
    """
    org = OrganizationFactory()
    user = UserFactory()
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    validator = ValidatorFactory(
        org=org,
        validation_type=ValidationType.BASIC,
        is_system=False,
    )
    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.BASIC,
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator, ruleset=ruleset)
    assertion = RulesetAssertionFactory(ruleset=ruleset)
    submission = SubmissionFactory(
        org=org,
        project=workflow.project,
        user=user,
        workflow=workflow,
    )
    validation_run = ValidationRun.objects.create(
        org=org,
        workflow=workflow,
        submission=submission,
        project=submission.project,
        user=user,
        status=ValidationRunStatus.PENDING,
    )

    # Create a fake processor execute result
    def mock_execute_validator_step(self, *, validation_run, step_run):
        # Manually create a finding to simulate what the processor does
        ValidationFinding.objects.create(
            validation_run=validation_run,
            validation_step_run=step_run,
            path="price",
            message="Price exceeds limit",
            severity=Severity.ERROR,
            ruleset_assertion_id=assertion.id,
        )
        # Update step_run output with assertion stats (like processor does)
        step_run.output = {
            "assertion_failures": 1,
            "assertion_total": 1,
        }
        step_run.status = StepStatus.FAILED
        step_run.save()

        return StepProcessingResult(
            passed=False,
            step_run=step_run,
            severity_counts=Counter({Severity.ERROR: 1}),
            total_findings=1,
            assertion_failures=1,
            assertion_total=1,
        )

    monkeypatch.setattr(
        StepOrchestrator,
        "_execute_validator_step",
        mock_execute_validator_step,
    )

    service = ValidationRunService()
    result = service.execute_workflow_steps(
        validation_run_id=validation_run.id,
        user_id=user.id,
    )

    validation_run.refresh_from_db()
    assert result.status == ValidationRunStatus.FAILED
    assert validation_run.status == ValidationRunStatus.FAILED
    assert validation_run.step_runs.count() == 1

    findings = ValidationFinding.objects.filter(validation_run=validation_run)
    assert findings.count() == 1
    finding = findings.first()
    assert finding.message == "Price exceeds limit"
    assert finding.ruleset_assertion_id == assertion.id
    assert finding.validation_step_run.workflow_step == step
    assert finding.path == "price"

    summary_record = ValidationRunSummary.objects.get(run=validation_run)
    assert summary_record.error_count == 1
    assert summary_record.total_findings == 1
    assert summary_record.assertion_failure_count == 1
    assert summary_record.assertion_total_count == 1
    assert summary_record.step_summaries.count() == 1
