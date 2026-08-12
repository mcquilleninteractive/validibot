"""
Tests for assertion success message functionality.

This module tests the success message feature that allows passed assertions
to emit positive feedback messages. Success messages can be configured at
the assertion level (custom message) or step level (show all success messages).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from django.test import TestCase

from validibot.actions.protocols import RunContext
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import resolved_file_input
from validibot.validations.validators.basic import BasicValidator as _BasicValidator


class BasicValidator(_BasicValidator):
    """Bind focused success-message tests to the declared document input."""

    def validate(
        self,
        validator,
        submission,
        ruleset,
        run_context=None,
    ):
        """Supply the JSON document through a typed port before evaluation."""
        context = run_context or RunContext()
        context.resolved_file_inputs["document"] = resolved_file_input(
            contract_key="document",
            content=submission.content,
            file_type=SubmissionFileType.JSON,
        )
        return super().validate(
            validator,
            submission,
            ruleset,
            run_context=context,
        )


class SuccessMessageTests(TestCase):
    """
    Tests for assertion success message generation.

    Verifies that passed assertions emit SUCCESS severity issues when:
    1. The assertion has a custom success_message defined
    2. The step has show_success_messages=True
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            org=cls.org,
            is_system=False,
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.BASIC,
        )

    def test_passed_assertion_no_success_message_by_default(self):
        """
        Passed assertions should NOT emit success issues by default.

        When neither a custom success_message nor show_success_messages is set,
        passed assertions should be silent.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            operator=AssertionOperator.LT,
            target_data_path="price",
            rhs={"value": 100},
            options={},
            message_template="",
            success_message="",  # No custom success message
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
        )
        submission.content = json.dumps({"price": 50})  # Passes: 50 < 100
        submission.save(update_fields=["content"])
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)  # No issues, including no success

    def test_passed_assertion_with_custom_success_message(self):
        """
        Passed assertions with custom success_message should emit SUCCESS issue.

        When an assertion has a success_message defined, passing that assertion
        should create a SUCCESS severity issue with the custom message.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            operator=AssertionOperator.LT,
            target_data_path="price",
            rhs={"value": 100},
            options={},
            message_template="",
            success_message="Price is within budget!",
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
        )
        submission.content = json.dumps({"price": 50})  # Passes: 50 < 100
        submission.save(update_fields=["content"])
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 1)
        issue = result.issues[0]
        self.assertEqual(issue.severity, Severity.SUCCESS)
        self.assertEqual(issue.message, "Price is within budget!")
        self.assertEqual(issue.code, "assertion_passed")

    def test_passed_assertion_with_show_success_messages_enabled(self):
        """
        Passed assertions should emit SUCCESS when step.show_success_messages=True.

        When the step has show_success_messages enabled, all passed assertions
        should emit a default success message even without a custom message.
        """
        assertion = RulesetAssertionFactory(
            ruleset=self.ruleset,
            operator=AssertionOperator.LT,
            target_data_path="price",
            rhs={"value": 100},
            options={},
            message_template="",
            success_message="",  # No custom message
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
        )
        submission.content = json.dumps({"price": 50})  # Passes: 50 < 100
        submission.save(update_fields=["content"])

        # Create mock step with show_success_messages=True
        mock_step = MagicMock()
        mock_step.show_success_messages = True
        run_context = RunContext(step=mock_step)

        engine = BasicValidator()
        engine.run_context = run_context

        result = engine.validate(
            self.validator,
            submission,
            self.ruleset,
            run_context=run_context,
        )

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 1)
        issue = result.issues[0]
        self.assertEqual(issue.severity, Severity.SUCCESS)
        self.assertIn("Assertion passed", issue.message)
        self.assertEqual(issue.code, "assertion_passed")
        self.assertEqual(issue.assertion_id, assertion.id)

    def test_failed_assertion_no_success_message(self):
        """
        Failed assertions should NOT emit success messages.

        Even with custom success_message defined, failing assertions should
        only emit failure messages, not success messages.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            operator=AssertionOperator.LT,
            target_data_path="price",
            rhs={"value": 100},
            options={},
            message_template="Price too high",
            success_message="Price is within budget!",
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
        )
        submission.content = json.dumps({"price": 150})  # Fails: 150 >= 100
        submission.save(update_fields=["content"])
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        issue = result.issues[0]
        self.assertEqual(issue.severity, Severity.ERROR)
        self.assertNotEqual(issue.message, "Price is within budget!")


class SeverityBadgeTests(TestCase):
    """Tests for SUCCESS severity badge class mapping."""

    def test_success_severity_badge_class(self):
        """SUCCESS severity should map to text-bg-success badge class."""
        from validibot.core.templatetags.core_tags import finding_badge_class

        class MockFinding:
            severity = Severity.SUCCESS

        result = finding_badge_class(MockFinding())
        self.assertEqual(result, "text-bg-success")

    def test_success_severity_string_badge_class(self):
        """SUCCESS severity as string should map to text-bg-success badge class."""
        from validibot.core.templatetags.core_tags import finding_badge_class

        class MockFinding:
            severity = "SUCCESS"

        result = finding_badge_class(MockFinding())
        self.assertEqual(result, "text-bg-success")
