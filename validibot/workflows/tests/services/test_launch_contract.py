"""Tests for the LaunchContract service.

Phase 2 of ADR-2026-04-27 (trust-boundary): the LaunchContract is the
single decision point for "can this workflow be launched with this
payload?". These tests pin down the contract's behavior in isolation,
independent of any of the four call paths (web / API / MCP / x402).

Why isolate the contract test from the path tests
=================================================

Each launch path will get its own integration test that asserts the
path produces the expected error envelope on each violation. Those
tests are integration-shaped — they go through HTTP / serializers /
exception mapping. They're slower and harder to debug.

This file tests the contract function in isolation: given a workflow
fixture and inputs, does ``LaunchContract.validate`` return the
expected violation? When a future ADR adds a new precondition (e.g.
quota check), it lands here as one new test class. The path tests
need only verify the new violation code maps to the right error
envelope on each path, not that the underlying decision logic is
correct — that's the contract's job.

Why test on the violation code, not the message
================================================

The ``code`` field of :class:`LaunchContractViolation` is the load-
bearing contract — operators, integrations, support tooling all
filter on the code. The ``message`` is human-readable and translatable
and may evolve. Tests assert on ``code``; we sample ``message``
content only loosely (substring match) to catch egregious regressions
in messaging quality without breaking on every wording tweak.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import TestCase

from validibot.workflows.services.launch_contract import DEFAULT_MAX_PAYLOAD_BYTES
from validibot.workflows.services.launch_contract import LaunchContract
from validibot.workflows.services.launch_contract import LaunchContractViolation
from validibot.workflows.services.launch_contract import ViolationCode


def _make_workflow_mock(
    *,
    is_active: bool = True,
    has_steps: bool = True,
    first_unavailable_step: object | None = None,
    supports_file_type: bool = True,
    allowed_file_type_labels: list[str] | None = None,
) -> MagicMock:
    """Build a minimal Workflow mock for contract tests.

    Why a mock instead of a real Django Workflow fixture: the
    contract's job is to call a small set of model methods and
    branch on the results. Using a real Workflow means standing up
    org/user/step rows for every test, which is slow and noisy.
    Mocking lets each test set up exactly the behaviour it needs.

    Real Workflow integration is exercised by the path-level tests
    (web / API / MCP / x402), which use real fixtures end-to-end.
    """
    workflow = MagicMock()
    # The relational artifact-dependency launch guard queries by workflow
    # identity. A stable impossible primary key keeps this unit-test double
    # isolated from persisted integration fixtures while exercising the guard.
    workflow.pk = -1
    workflow.is_active = is_active
    # ``workflow.steps.exists()`` is what the contract calls.
    workflow.steps.exists.return_value = has_steps
    workflow.first_unavailable_validator_step.return_value = first_unavailable_step
    workflow.supports_file_type.return_value = supports_file_type
    workflow.allowed_file_type_labels.return_value = allowed_file_type_labels or [
        "JSON",
        "XML",
    ]
    return workflow


class LaunchContractAlwaysAllowedTests(TestCase):
    """Baseline: a healthy workflow + healthy payload -> no violation.

    If this test fails, every other test below is also probably wrong.
    Catches accidental regressions in the default-allow path.
    """

    def test_active_workflow_with_steps_no_payload_passes(self):
        """A minimal call (no file_type, no payload size) returns None.

        Documents the most permissive valid call: just a workflow.
        Some callers (rare) want to check workflow readiness without
        file-type information, e.g. when listing launchable workflows
        in a UI.
        """
        workflow = _make_workflow_mock()
        result = LaunchContract.validate(workflow=workflow)
        self.assertIsNone(result)

    def test_active_workflow_with_supported_file_passes(self):
        """A workflow that supports the file type and has compat steps passes."""
        workflow = _make_workflow_mock()
        result = LaunchContract.validate(workflow=workflow, file_type="JSON")
        self.assertIsNone(result)

    def test_unavailable_validator_blocks_launch(self):
        """A workflow step whose validator code is missing is not launchable.

        Dynamic validator types mean the DB can contain rows for plugins that
        are not installed in this process. The launch contract must catch that
        before a run is created.
        """
        validator = MagicMock()
        validator.name = "Cloud-only Validator"
        validator.runtime_unavailable_reason.return_value = "plugin missing"
        step = MagicMock()
        step.validator = validator
        step.step_number_display = "2"

        workflow = _make_workflow_mock(first_unavailable_step=step)
        result = LaunchContract.validate(workflow=workflow)

        self.assertIsInstance(result, LaunchContractViolation)
        self.assertEqual(result.code, ViolationCode.VALIDATOR_UNAVAILABLE)
        self.assertEqual(result.detail, "plugin missing")

    def test_active_workflow_with_reasonable_payload_passes(self):
        """A 1 MiB payload (well under the 10 MiB default) passes."""
        workflow = _make_workflow_mock()
        result = LaunchContract.validate(
            workflow=workflow,
            file_type="JSON",
            payload_size_bytes=1024 * 1024,
        )
        self.assertIsNone(result)


class LaunchContractWorkflowStateTests(TestCase):
    """Workflow-state preconditions: active + has steps."""

    def test_inactive_workflow_returns_workflow_inactive(self):
        """Inactive workflows can't be launched, regardless of payload.

        This covers the case where an admin disabled a workflow but
        an in-flight client still tries to use it. The error must be
        unambiguous so the client knows to stop retrying.
        """
        workflow = _make_workflow_mock(is_active=False)
        result = LaunchContract.validate(workflow=workflow, file_type="JSON")
        self.assertIsNotNone(result)
        self.assertEqual(result.code, ViolationCode.WORKFLOW_INACTIVE)
        self.assertIn("not currently active", result.message.lower())

    def test_workflow_without_steps_returns_no_steps(self):
        """A workflow with no steps is a half-built workflow.

        Same shape as inactive — the operator hasn't finished setting
        up the workflow. Distinct error code so support can route it
        to "finish your workflow setup" docs vs. "your workflow is
        disabled".
        """
        workflow = _make_workflow_mock(has_steps=False)
        result = LaunchContract.validate(workflow=workflow, file_type="JSON")
        self.assertIsNotNone(result)
        self.assertEqual(result.code, ViolationCode.NO_STEPS)
        self.assertIn("no steps", result.message.lower())

    def test_inactive_workflow_takes_precedence_over_no_steps(self):
        """When both apply, inactive wins.

        An inactive workflow's step list might be stale, so we report
        the more authoritative violation (inactive) rather than
        cascading. Documents the check order.
        """
        workflow = _make_workflow_mock(is_active=False, has_steps=False)
        result = LaunchContract.validate(workflow=workflow, file_type="JSON")
        self.assertEqual(result.code, ViolationCode.WORKFLOW_INACTIVE)


class LaunchContractFileTypeTests(TestCase):
    """File-type and step-compatibility checks.

    These were the bulk of the duplication before Phase 2: each path
    had its own copy of "does the workflow accept this file type?".
    """

    def test_unsupported_file_type_returns_unsupported_file_type(self):
        """Workflow doesn't accept this file type at all."""
        workflow = _make_workflow_mock(
            supports_file_type=False,
            allowed_file_type_labels=["JSON"],
        )
        result = LaunchContract.validate(
            workflow=workflow,
            file_type="binary",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.code, ViolationCode.UNSUPPORTED_FILE_TYPE)
        # Message should communicate what IS allowed.
        self.assertIn("JSON", result.message)

    def test_step_input_contract_is_not_an_admission_gate(self):
        """Concrete step/source compatibility belongs to run execution.

        Admission checks only the workflow's allowed primary file types. This
        permits a multi-type workflow to accept a file that a particular step
        may later reject with a structured input-contract finding.
        """
        workflow = _make_workflow_mock(supports_file_type=True)
        result = LaunchContract.validate(workflow=workflow, file_type="JSON")
        self.assertIsNone(result)

    def test_no_file_type_skips_file_type_checks(self):
        """When file_type is None, the contract doesn't check it.

        Some paths (rare) call the contract for workflow-readiness
        verification before the file type is known.
        """
        workflow = _make_workflow_mock(supports_file_type=False)
        # file_type=None should NOT trigger UNSUPPORTED_FILE_TYPE
        # because the check is gated on file_type being provided.
        result = LaunchContract.validate(workflow=workflow)
        self.assertIsNone(result)


class LaunchContractPayloadSizeTests(TestCase):
    """Payload-size checks (Phase 2 generalisation).

    Phase 0 added a 10 MiB cap on x402 to bound paid-orphan risk.
    Phase 2 generalises the cap so it applies on all paths — an
    oversized JSON envelope can't bypass via the CLI / API after
    being rejected on x402.
    """

    def test_empty_payload_returns_payload_empty(self):
        """Zero bytes is empty, distinct from too-large."""
        workflow = _make_workflow_mock()
        result = LaunchContract.validate(
            workflow=workflow,
            file_type="JSON",
            payload_size_bytes=0,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.code, ViolationCode.PAYLOAD_EMPTY)

    def test_negative_payload_treated_as_empty(self):
        """A negative size is nonsensical; treat as empty.

        Defensive — some upstream callers might compute size as
        ``len(content) - some_offset`` and end up negative. Better
        to surface that as PAYLOAD_EMPTY than crash.
        """
        workflow = _make_workflow_mock()
        result = LaunchContract.validate(
            workflow=workflow,
            file_type="JSON",
            payload_size_bytes=-1,
        )
        self.assertEqual(result.code, ViolationCode.PAYLOAD_EMPTY)

    def test_payload_at_default_limit_passes(self):
        """Exactly the default limit is OK; one byte over is too large.

        Edge case — confirms the comparison is ``>``, not ``>=``.
        """
        workflow = _make_workflow_mock()
        result_at_limit = LaunchContract.validate(
            workflow=workflow,
            file_type="JSON",
            payload_size_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
        )
        self.assertIsNone(result_at_limit)

        result_over_limit = LaunchContract.validate(
            workflow=workflow,
            file_type="JSON",
            payload_size_bytes=DEFAULT_MAX_PAYLOAD_BYTES + 1,
        )
        self.assertIsNotNone(result_over_limit)
        self.assertEqual(
            result_over_limit.code,
            ViolationCode.PAYLOAD_TOO_LARGE,
        )

    def test_custom_max_payload_bytes_overrides_default(self):
        """Operators can pass a smaller limit per call.

        Useful for: x402 path may want a stricter limit than the
        general default once we surface payment-cost considerations.
        Or a test environment may want to verify behavior at a
        smaller threshold without needing a 10 MiB test fixture.
        """
        workflow = _make_workflow_mock()
        # 1024 byte limit; 2048 byte payload should violate.
        result = LaunchContract.validate(
            workflow=workflow,
            file_type="JSON",
            payload_size_bytes=2048,
            max_payload_bytes=1024,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.code, ViolationCode.PAYLOAD_TOO_LARGE)

    def test_no_payload_size_skips_payload_check(self):
        """When payload_size_bytes is None, no payload check runs.

        The web view may not know the exact decoded payload size up-
        front (it uses Django's request.FILES streaming). It can
        skip the contract's payload check and rely on Django's own
        upload-size limits. Documents that None is a valid input.
        """
        workflow = _make_workflow_mock()
        # No payload_size_bytes argument
        result = LaunchContract.validate(workflow=workflow, file_type="JSON")
        self.assertIsNone(result)


class LaunchContractCheckOrderTests(TestCase):
    """The order of checks matters for diagnostics.

    Workflow state checks come first because they're authoritative:
    an inactive workflow's step list might be stale, so we'd report
    incorrect violations on later checks. File-type checks come
    before payload checks because the file-type result is more
    actionable (operators can change the file; bumping the size
    limit is rare).
    """

    def test_inactive_takes_precedence_over_file_type(self):
        """Inactive wins over unsupported_file_type."""
        workflow = _make_workflow_mock(
            is_active=False,
            supports_file_type=False,
        )
        result = LaunchContract.validate(workflow=workflow, file_type="binary")
        self.assertEqual(result.code, ViolationCode.WORKFLOW_INACTIVE)

    def test_file_type_takes_precedence_over_payload_size(self):
        """Unsupported file type wins over payload-too-large.

        Why this order: if the file type is wrong, fixing the size
        won't help. Reporting the size first would send the operator
        on a wild goose chase.
        """
        workflow = _make_workflow_mock(supports_file_type=False)
        result = LaunchContract.validate(
            workflow=workflow,
            file_type="binary",
            payload_size_bytes=DEFAULT_MAX_PAYLOAD_BYTES * 2,
        )
        self.assertEqual(result.code, ViolationCode.UNSUPPORTED_FILE_TYPE)


class LaunchContractViolationDataclassTests(TestCase):
    """The :class:`LaunchContractViolation` dataclass is the public contract.

    Operators and integrations route on the ``code`` field. These
    tests pin down the dataclass shape so additive changes (new
    optional fields) don't accidentally break consumers that
    construct the dataclass directly.
    """

    def test_violation_is_frozen(self):
        """Frozen dataclass — violations are immutable.

        Once a contract decision is made, callers shouldn't mutate it
        before mapping to their own error envelope. Frozen catches
        that at runtime.
        """
        violation = LaunchContractViolation(
            code=ViolationCode.WORKFLOW_INACTIVE,
            message="test",
        )
        with self.assertRaises(Exception):  # noqa: B017, PT027
            # FrozenInstanceError is the specific exception, but it's
            # a private import path. Just assert "raises something".
            violation.code = ViolationCode.NO_STEPS  # type: ignore[misc]

    def test_violation_code_is_string_enum(self):
        """ViolationCode is a StrEnum — code values serialize as strings.

        This matters for JSON output: ``{"code": "workflow_inactive"}``
        should serialize naturally without needing a custom encoder.
        """
        violation = LaunchContractViolation(
            code=ViolationCode.WORKFLOW_INACTIVE,
            message="test",
        )
        self.assertEqual(str(violation.code), "workflow_inactive")
        # And the enum value comparison works.
        self.assertEqual(violation.code, "workflow_inactive")

    def test_detail_is_optional(self):
        """The detail field defaults to None."""
        violation = LaunchContractViolation(
            code=ViolationCode.WORKFLOW_INACTIVE,
            message="test",
        )
        self.assertIsNone(violation.detail)
