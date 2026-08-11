"""Use-case coverage for custom BASIC validators and namespaced CEL payloads.

The validator is intentionally organization-owned and has no file ports. The
workflow helper still enforces the production binding postcondition so future
inputs added to this scenario cannot be silently omitted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers.workflows import create_workflow_step_with_default_bindings
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.basic import BasicValidator
from validibot.workflows.tests.factories import WorkflowFactory


@pytest.mark.django_db(transaction=True)
class TestCelAssertion:
    """
    Exercises CEL assertions in the BASIC validator when custom assertion
    targets are allowed. Ensures the validator builds a CEL context from JSON
    payloads, evaluates the assertion successfully, and records a passing
    ValidationRun with no findings.
    """

    def test_cel_assertion_with_custom_targets_passes(self):
        """
        Verify that a BASIC validator with ``allow_custom_assertion_targets`` set
        can expose payload fields directly to CEL expressions, execute the rule,
        and complete the workflow run without findings.
        """
        org = OrganizationFactory()
        user = UserFactory()
        grant_role(user, org, RoleCode.EXECUTOR)

        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.BASIC,
            allow_custom_assertion_targets=True,
        )
        ruleset = RulesetFactory(org=org, user=user)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'p.price > 0 && p.rating >= 90 && "mini" in p.tags'},
        )

        workflow = WorkflowFactory(org=org, user=user, is_active=True)
        step = create_workflow_step_with_default_bindings(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
        )

        payload = Path("tests/assets/json/example_product.json").read_text()
        payload_data = json.loads(payload)

        engine = BasicValidator()
        assert validator.allow_custom_assertion_targets is True
        context = engine._build_cel_context(payload_data, validator)
        # Under the namespaced CEL design, raw payload data is always
        # accessed via the p.* namespace — never as bare top-level keys.
        assert context["p"]["price"] == payload_data["price"]
        assert context["p"]["rating"] == payload_data["rating"]
        assert context["p"]["tags"] == payload_data["tags"]

        result = engine.evaluate_assertions_for_stage(
            ruleset=ruleset,
            validator=validator,
            payload=payload_data,
            stage="input",
        )
        assert result.issues == []

        submission = SubmissionFactory(
            org=org,
            project=workflow.project,
            user=user,
            workflow=workflow,
            content=payload,
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
        result = service.execute_workflow_steps(
            validation_run_id=validation_run.id,
            user_id=user.id,
        )

        validation_run.refresh_from_db()
        assert result.status == ValidationRunStatus.SUCCEEDED
        assert validation_run.status == ValidationRunStatus.SUCCEEDED
        assert validation_run.findings.count() == 0
        step_runs = validation_run.step_runs.all()
        assert step_runs.count() == 1
        assert step_runs.first().workflow_step == step
