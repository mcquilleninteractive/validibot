"""End-to-end tests for promoted output injection and fail-fast signal resolution.

These tests cover two runtime paths that are critical to the signal mapping
ADR but were not previously exercised at the integration level:

1. **Promoted output injection** (``_inject_promotions`` in base.py):
   When a ``StepIODefinition`` with ``direction=OUTPUT`` has a non-empty
   ``promoted_signal_name``, that output's value should appear in the ``s.*`` CEL
   namespace for downstream steps.

2. **Fail-fast on_missing=error** (``_resolve_workflow_signals`` in
   step_processor/base.py → ``resolve_workflow_signals`` service):
   When a ``WorkflowSignalMapping`` with ``on_missing="error"`` cannot
   resolve its source path, the run should fail immediately with a
   ``SignalResolutionError`` — not silently proceed with missing data.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import StepIODefinition
from validibot.validations.models import ValidationRun
from validibot.validations.services.custom_validator_contracts import (
    sync_configured_io_contract,
)
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base import AssertionStats
from validibot.validations.validators.base import ValidationResult
from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

# ── Promoted output injection ────────────────────────────────────────────
# A validator output with signal_name set should appear in the s.*
# namespace for downstream steps.  This exercises _inject_promotions
# through the full pipeline (not just unit-level).


class TestPromotedOutputInjection(TestCase):
    """Verify promoted validator outputs appear in the s.* CEL namespace.

    This test creates a two-step workflow:
    - Step 1: a fake validator that produces output ``site_eui = 87.5``
      with its ``StepIODefinition`` setting ``signal_name = "eui"``
    - Step 2: a Basic validator with a CEL assertion ``s.eui < 100``

    If promotion works, step 2's CEL context should contain ``s.eui = 87.5``
    and the assertion should pass.  If promotion is broken, ``s.eui`` will be
    missing and the assertion will fail with a CEL evaluation error.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        grant_role(cls.user, cls.org, RoleCode.EXECUTOR)
        cls.user.set_current_org(cls.org)

    def test_promoted_output_available_in_s_namespace(self):
        """A promoted output from step 1 must be accessible as s.<signal_name>
        in step 2's CEL assertions.

        This is the core promise of output promotion: an author names
        a validator output as a signal, and downstream steps reference it
        by that name without knowing which step produced it.
        """
        workflow = WorkflowFactory(
            org=self.org,
            user=self.user,
            is_active=True,
        )

        # Step 1: produces output "site_eui" promoted to signal "eui"
        step1_validator = ValidatorFactory(
            org=self.org,
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        step1_ruleset = Ruleset.objects.create(
            org=self.org,
            user=self.user,
            name="Step 1 ruleset",
            ruleset_type=RulesetType.BASIC,
            version="1.0",
        )
        step1 = WorkflowStepFactory(
            workflow=workflow,
            validator=step1_validator,
            ruleset=step1_ruleset,
            order=10,
            step_key="energy_step",
        )

        # Create a promoted output value definition.
        # StepIODefinition has a XOR constraint: set either validator OR
        # workflow_step, not both.  For step-level promotion, use
        # workflow_step only.
        StepIODefinition.objects.create(
            workflow_step=step1,
            contract_key="site_eui",
            direction=StepIODirection.OUTPUT,
            promoted_signal_name="eui",
        )

        # Step 2: references the promoted output via s.eui
        step2_validator = ValidatorFactory(
            org=self.org,
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        step2_ruleset = Ruleset.objects.create(
            org=self.org,
            user=self.user,
            name="Step 2 ruleset",
            ruleset_type=RulesetType.BASIC,
            version="1.0",
        )
        WorkflowStepFactory(
            workflow=workflow,
            validator=step2_validator,
            ruleset=step2_ruleset,
            order=20,
            step_key="check_step",
        )
        RulesetAssertion.objects.create(
            ruleset=step2_ruleset,
            order=10,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_data_path="",
            severity=Severity.ERROR,
            rhs={"expr": "s.eui < 100"},
        )

        # Fake step 1's validator to return the promoted output
        step1_output_values = {"site_eui": 87.5}

        class FakeValidator:
            """Stub validator that produces a known output value."""

            def validate(self, **_kwargs):
                return ValidationResult(
                    passed=True,
                    issues=[],
                    assertion_stats=AssertionStats(),
                    output_values=step1_output_values,
                    stats={},
                )

        original_get = None

        def patched_get_validator(validation_type):
            # Return fake for step 1's type, real for step 2
            if validation_type == step1_validator.validation_type:
                return FakeValidator
            return original_get(validation_type)

        from validibot.validations.validators.base.config import get_validator_class

        original_get = get_validator_class

        submission = SubmissionFactory(
            org=self.org,
            project=workflow.project,
            user=self.user,
            workflow=workflow,
            content=json.dumps({"placeholder": True}),
        )
        validation_run = ValidationRun.objects.create(
            org=self.org,
            workflow=workflow,
            submission=submission,
            project=submission.project,
            user=self.user,
            status=ValidationRunStatus.PENDING,
        )

        service = ValidationRunService()
        with patch(
            "validibot.validations.validators.base.config.get_validator_class",
            side_effect=patched_get_validator,
        ):
            service.execute_workflow_steps(
                validation_run_id=validation_run.id,
                user_id=self.user.id,
            )

        validation_run.refresh_from_db()

        # Step 1 should pass and store its output in the summary
        step_runs = list(validation_run.step_runs.order_by("step_order"))
        self.assertEqual(len(step_runs), 2)
        self.assertEqual(step_runs[0].status, StepStatus.PASSED)

        # Step 2 should pass because s.eui = 87.5 < 100
        self.assertEqual(step_runs[1].status, StepStatus.PASSED)
        self.assertEqual(validation_run.status, ValidationRunStatus.SUCCEEDED)


# ── Fail-fast on_missing=error ───────────────────────────────────────────
# When a WorkflowSignalMapping with on_missing="error" cannot resolve its
# source path, the run should fail before any step executes.


class TestFailFastOnMissingError(TestCase):
    """Verify that on_missing=error signal mappings cause immediate run failure.

    When a required signal cannot be resolved from the submission data,
    the run must fail with a clear error message.  This prevents silent
    data quality issues where a CEL expression evaluates against missing
    data and produces misleading pass/fail results.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        grant_role(cls.user, cls.org, RoleCode.EXECUTOR)
        cls.user.set_current_org(cls.org)

    def test_missing_required_signal_fails_run(self):
        """A WorkflowSignalMapping with on_missing=error whose source_path
        doesn't exist in the submission must cause the validation run to
        fail before any step executes.

        If this doesn't fail-fast, the CEL expression would either crash
        with a confusing 'undefined variable' error or (worse) silently
        skip the assertion, producing a false pass.
        """
        workflow = WorkflowFactory(
            org=self.org,
            user=self.user,
            is_active=True,
        )

        # Create a required signal mapping pointing to a non-existent path
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="target_eui",
            source_path="energy.target_eui",
            on_missing="error",
            position=0,
        )

        # A basic step with a simple assertion that would pass if the
        # signal existed — but should never execute.
        validator = ValidatorFactory(
            org=self.org,
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        sync_configured_io_contract(validator=validator)
        ruleset = Ruleset.objects.create(
            org=self.org,
            user=self.user,
            name="Test ruleset",
            ruleset_type=RulesetType.BASIC,
            version="1.0",
        )
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
        )
        ensure_step_input_bindings(step)
        RulesetAssertion.objects.create(
            ruleset=ruleset,
            order=10,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_data_path="",
            severity=Severity.ERROR,
            rhs={"expr": "s.target_eui < 100"},
        )

        # Submit data that does NOT have the "energy.target_eui" path
        submission = SubmissionFactory(
            org=self.org,
            project=workflow.project,
            user=self.user,
            workflow=workflow,
            content=json.dumps({"temperature": 22.5}),
        )
        validation_run = ValidationRun.objects.create(
            org=self.org,
            workflow=workflow,
            submission=submission,
            project=submission.project,
            user=self.user,
            status=ValidationRunStatus.PENDING,
        )

        service = ValidationRunService()
        service.execute_workflow_steps(
            validation_run_id=validation_run.id,
            user_id=self.user.id,
        )

        validation_run.refresh_from_db()

        # The run should have failed due to the missing required signal
        self.assertEqual(validation_run.status, ValidationRunStatus.FAILED)

    def test_missing_nullable_signal_does_not_fail(self):
        """A WorkflowSignalMapping with on_missing=null whose source_path
        doesn't exist should inject null and let the run proceed.

        This is the expected behavior when an author marks a signal as
        optional.  The CEL expression can guard with
        ``s.target_eui != null`` or simply accept null values.
        """
        workflow = WorkflowFactory(
            org=self.org,
            user=self.user,
            is_active=True,
        )

        # Create an optional signal mapping pointing to a non-existent path
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="target_eui",
            source_path="energy.target_eui",
            on_missing="null",
            position=0,
        )

        # A basic step with an assertion that handles null
        validator = ValidatorFactory(
            org=self.org,
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        sync_configured_io_contract(validator=validator)
        ruleset = Ruleset.objects.create(
            org=self.org,
            user=self.user,
            name="Test ruleset",
            ruleset_type=RulesetType.BASIC,
            version="1.0",
        )
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
        )
        ensure_step_input_bindings(step)
        RulesetAssertion.objects.create(
            ruleset=ruleset,
            order=10,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_data_path="",
            severity=Severity.ERROR,
            rhs={"expr": "s.target_eui == null"},
        )

        # Submit data that does NOT have the "energy.target_eui" path
        submission = SubmissionFactory(
            org=self.org,
            project=workflow.project,
            user=self.user,
            workflow=workflow,
            content=json.dumps({"temperature": 22.5}),
        )
        validation_run = ValidationRun.objects.create(
            org=self.org,
            workflow=workflow,
            submission=submission,
            project=submission.project,
            user=self.user,
            status=ValidationRunStatus.PENDING,
        )

        service = ValidationRunService()
        service.execute_workflow_steps(
            validation_run_id=validation_run.id,
            user_id=self.user.id,
        )

        validation_run.refresh_from_db()

        # The run should succeed — null was injected and the assertion
        # checks for null equality.
        self.assertEqual(validation_run.status, ValidationRunStatus.SUCCEEDED)
