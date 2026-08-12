from __future__ import annotations

import json
import logging
from datetime import timedelta
from itertools import cycle

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.models import Submission
from validibot.tracking.services import TrackingEventService
from validibot.users.models import User
from validibot.users.models import ensure_default_project
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationRunSummary
from validibot.validations.models import ValidationStepRun
from validibot.validations.models import ValidationStepRunSummary
from validibot.validations.models import Validator
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)

DEFAULT_USERNAME = "admin"
EXAMPLE_RUN_SUMMARY_MARKER = {"example_run": True}
MAX_INDEX = 10  # Number of ERROR findings; the rest will be WARNING


class Command(BaseCommand):
    help = (
        "Create an example workflow, populate it with assertions, "
        "and generate a sample failed run."
    )

    workflow_slug = "example-custom-validation-workflow"
    validator_slug = "example-custom-validator"
    ruleset_name = "Example Custom Validator Ruleset"

    def handle(self, *args, **options):
        with transaction.atomic():
            user = User.objects.filter(username=DEFAULT_USERNAME).first()
            org = user.get_current_org()
            project = ensure_default_project(organization=org)

            workflow = self._build_workflow(org=org, user=user, project=project)
            step_payloads = self._ensure_steps_and_assertions(
                workflow=workflow,
                org=org,
                user=user,
            )

            run = self._ensure_example_run(
                workflow=workflow,
                step_payloads=step_payloads,
                org=org,
                project=project,
                user=user,
            )

            logger.info(f"Created example workflow {workflow.pk} with run {run.pk}")

        path = f"/app/workflows/{workflow.pk}/launch/run/latest/"
        self.stdout.write(
            self.style.SUCCESS(
                (
                    "Example workflow and run created.\n"
                    f"Workflow ID: {workflow.pk}\n"
                    f"View the latest run here: {path}"
                ),
            ),
        )

    def _build_workflow(self, *, org, user, project):
        workflow, _ = Workflow.objects.get_or_create(
            org=org,
            slug=self.workflow_slug,
            version=1,
            defaults={
                "name": "Example Custom Validation Workflow",
                "user": user,
                "project": project,
            },
        )
        changed = False
        if workflow.name != "Example Custom Validation Workflow":
            workflow.name = "Example Custom Validation Workflow"
            changed = True
        if workflow.user_id != user.id:
            workflow.user = user
            changed = True
        if workflow.project_id != project.id:
            workflow.project = project
            changed = True
        if workflow.allowed_file_types != [SubmissionFileType.JSON]:
            workflow.allowed_file_types = [SubmissionFileType.JSON]
            changed = True
        if not workflow.is_active:
            workflow.is_active = True
            changed = True
        if changed:
            workflow.save()
        return workflow

    def _ensure_steps_and_assertions(self, *, workflow, org, user):
        step_payloads: list[tuple[WorkflowStep, list[RulesetAssertion]]] = []
        for index in range(1, 4):
            step, assertions = self._ensure_single_step(
                workflow=workflow,
                org=org,
                user=user,
                index=index,
            )
            step_payloads.append((step, assertions))
        return step_payloads

    def _ensure_single_step(self, *, workflow, org, user, index: int):
        step_name = f"Custom Step {index}"
        existing_step = (
            workflow.steps.filter(name=step_name).select_related("ruleset").first()
        )
        if existing_step and existing_step.ruleset:
            return existing_step, list(existing_step.ruleset.assertions.all())

        validator_slug = f"{self.validator_slug}-{index}"
        validator, _ = Validator.objects.get_or_create(
            slug=validator_slug,
            defaults={
                "name": f"Example Custom Validator {index}",
                "description": "Demonstration validator used by the example command.",
                "validation_type": ValidationType.CUSTOM_VALIDATOR,
                "version": 1,
                "is_system": False,
            },
        )
        validator.validation_type = ValidationType.CUSTOM_VALIDATOR
        validator.is_system = False
        validator.save()
        from validibot.submissions.constants import SubmissionDataFormat
        from validibot.validations.services.custom_validator_contracts import (
            sync_custom_validator_input_port,
        )

        sync_custom_validator_input_port(
            validator=validator,
            data_format=SubmissionDataFormat.JSON,
        )

        ruleset_name = f"{self.ruleset_name} {index}"
        ruleset, _ = Ruleset.objects.get_or_create(
            org=org,
            user=user,
            name=ruleset_name,
            defaults={
                "ruleset_type": RulesetType.CUSTOM_VALIDATOR,
                "version": "1.0.0",
            },
        )
        ruleset.ruleset_type = RulesetType.CUSTOM_VALIDATOR
        ruleset.user = user
        ruleset.save()

        if not ruleset.assertions.exists():
            operators = [
                AssertionOperator.LT,
                AssertionOperator.GT,
                AssertionOperator.EQ,
                AssertionOperator.NE,
                AssertionOperator.BETWEEN,
                AssertionOperator.IN,
                AssertionOperator.NOT_IN,
                AssertionOperator.MATCHES,
                AssertionOperator.CONTAINS,
                AssertionOperator.STARTS_WITH,
            ]
            operator_cycle = cycle(operators)
            for idx in range(1, 21):
                operator = next(operator_cycle)
                rhs, options = self._build_assertion_payload(
                    operator, idx + (index * 100)
                )
                RulesetAssertion.objects.create(
                    ruleset=ruleset,
                    order=idx * 10,
                    assertion_type=AssertionType.BASIC,
                    operator=operator,
                    target_data_path=f"payload.step_{index}.field_{idx}",
                    severity=Severity.ERROR,
                    rhs=rhs,
                    options=options,
                    message_template=(
                        f"Step {index}: Field {idx} "
                        f"violated {operator.replace('_', ' ')}."
                    ),
                )

        step, created = WorkflowStep.objects.get_or_create(
            workflow=workflow,
            order=index * 10,
            defaults={
                "validator": validator,
                "ruleset": ruleset,
                "name": step_name,
                "description": "Example step with 20 assertions.",
                "config": {},
            },
        )
        if created is False:
            if step.validator_id != validator.id:
                step.validator = validator
            if step.ruleset_id != ruleset.id:
                step.ruleset = ruleset
            if step.name != step_name:
                step.name = step_name
            step.save()
        return step, list(ruleset.assertions.all())

    def _build_assertion_payload(self, operator: str, index: int):
        if operator in {
            AssertionOperator.LT,
            AssertionOperator.GT,
            AssertionOperator.LE,
            AssertionOperator.GE,
            AssertionOperator.EQ,
            AssertionOperator.NE,
        }:
            return {"value": index * 5}, {}
        if operator == AssertionOperator.BETWEEN:
            return {"min": index, "max": index * 2}, {
                "include_min": True,
                "include_max": False,
            }
        if operator in {AssertionOperator.IN, AssertionOperator.NOT_IN}:
            return {"values": [f"option-{index}", f"alt-{index}"]}, {}
        if operator == AssertionOperator.MATCHES:
            return {"pattern": r"^example"}, {}
        if operator in {
            AssertionOperator.CONTAINS,
            AssertionOperator.STARTS_WITH,
            AssertionOperator.ENDS_WITH,
        }:
            return {"value": f"demo{index}"}, {}
        return {"value": index}, {}

    def _ensure_example_run(
        self,
        *,
        workflow: Workflow,
        step_payloads: list[tuple[WorkflowStep, list[RulesetAssertion]]],
        org,
        project,
        user,
    ):
        existing = ValidationRun.objects.filter(
            workflow=workflow,
            summary__example_run=True,
        ).first()
        if existing:
            return existing

        start_time = timezone.now() - timedelta(minutes=5)
        end_time = start_time + timedelta(seconds=30)

        submission = Submission.objects.create(
            org=org,
            project=project,
            user=user,
            workflow=workflow,
            name="Example payload",
            content=json.dumps({"payload": {"field": "value"}}),
            file_type=SubmissionFileType.JSON,
            original_filename="example.json",
            size_bytes=42,
            metadata={"example_run": True},
        )

        total_assertions = sum(len(assertions) for _, assertions in step_payloads)

        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            project=project,
            user=user,
            submission=submission,
            status=ValidationRunStatus.FAILED,
            started_at=start_time,
            ended_at=end_time,
            duration_ms=int((end_time - start_time).total_seconds() * 1000),
            error=f"{total_assertions} assertions failed for the example payload.",
            summary=EXAMPLE_RUN_SUMMARY_MARKER,
            source=ValidationRunSource.LAUNCH_PAGE,
        )
        submission.latest_run = run
        submission.save(update_fields=["latest_run"])
        tracking_service = TrackingEventService()
        tracking_service.log_validation_run_created(
            run=run,
            recorded_at=start_time,
            channel="web",
        )
        tracking_service.log_validation_run_status(
            run=run,
            status=run.status,
            recorded_at=end_time,
        )

        total_findings = 0
        total_errors = 0
        total_warnings = 0
        step_run_payloads: list[tuple[ValidationStepRun, int, int, int]] = []

        for step, assertions in step_payloads:
            step_run = ValidationStepRun.objects.create(
                validation_run=run,
                workflow_step=step,
                step_order=step.order,
                status=StepStatus.FAILED,
                started_at=start_time,
                ended_at=end_time,
                duration_ms=int((end_time - start_time).total_seconds() * 1000),
                output={"assertion_count": len(assertions)},
                error="Example data failed every assertion.",
            )

            finding_objs: list[ValidationFinding] = []
            step_errors = 0
            step_warnings = 0
            for idx, assertion in enumerate(assertions):
                severity_value = Severity.ERROR if idx < MAX_INDEX else Severity.WARNING
                if severity_value == Severity.ERROR:
                    step_errors += 1
                else:
                    step_warnings += 1
                finding_objs.append(
                    ValidationFinding(
                        validation_run=run,
                        validation_step_run=step_run,
                        ruleset_assertion=assertion,
                        severity=severity_value,
                        code=assertion.operator,
                        message=assertion.message_template
                        or f"{assertion.target_data_path} failed validation.",
                        path=assertion.target_data_path,
                        meta={"rhs": assertion.rhs, "options": assertion.options},
                    ),
                )
            ValidationFinding.objects.bulk_create(finding_objs)
            total_findings += len(finding_objs)
            total_errors += step_errors
            total_warnings += step_warnings
            step_run_payloads.append((step_run, step_errors, step_warnings, 0))

        summary_record = ValidationRunSummary.objects.create(
            run=run,
            status=run.status,
            completed_at=end_time,
            total_findings=total_findings,
            error_count=total_errors,
            warning_count=total_warnings,
            info_count=0,
            assertion_failure_count=total_errors + total_warnings,
            assertion_total_count=total_assertions,
        )

        for step_run, error_count, warning_count, info_count in step_run_payloads:
            ValidationStepRunSummary.objects.create(
                summary=summary_record,
                step_run=step_run,
                step_name=step_run.workflow_step.name,
                step_order=step_run.step_order,
                status=StepStatus.FAILED,
                error_count=error_count,
                warning_count=warning_count,
                info_count=info_count,
            )

        return run
