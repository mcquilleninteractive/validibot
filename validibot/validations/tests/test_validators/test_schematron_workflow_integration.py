"""End-to-end test of the Peppol pre-flight workflow (ADR-2026-07-01 item 5).

This is the COMPLETE authoring-to-findings scenario the feature exists for,
exercised exactly the way a user experiences it:

1. the author adds Schematron steps to a workflow and **uploads their
   rules** through the real step-config form (``save_workflow_step``);
2. a submitter sends an XML invoice;
3. the run flows through ``ValidationRunService.execute_workflow_steps`` —
   orchestrator, advanced processor, inline-rules envelope resolution,
   findings persistence, and the ``o.*`` CEL gate.

Only ONE piece is substituted: the container. ``LxmlContainerBackend``
receives the rules exactly as the real container does — inline in the typed
envelope (``SchematronInputs.schematron_text``) — and swaps Saxon for
``lxml.isoschematron``, which is why the fixture rules use the default
XSLT-1.0 query binding. Its output is a genuine ``SchematronOutputEnvelope``
parsed by the canonical shared SVRL parser (the Saxon runtime itself is
covered by the backend repo's layer-C tests).

The three scenarios mirror the fixture invoices:

- valid        → both rule steps pass, run SUCCEEDED
- bad totals   → step 1's rules fail VB-CO-15 and fail-fast stops the run
- no ProfileID → step 1 passes, step 2's rules fail VB-PEPPOL-R001 —
                 proving two steps with different uploaded rules layer
                 exactly as the D7 workflow intends

"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import Validator
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.validators.base.config import get_config
from validibot.workflows.forms import SchematronStepConfigForm
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.views_helpers import save_workflow_step

ASSETS = Path("tests/assets/schematron")


def _system_schematron_validator() -> Validator:
    """Return the installed Schematron validator with its declared file port."""
    call_command("sync_validators", stdout=StringIO(), stderr=StringIO())
    config = get_config(ValidationType.SCHEMATRON)
    return Validator.objects.get(slug=config.slug, version=config.version)


class LxmlContainerBackend:
    """Sync execution backend emulating the Schematron container with lxml.

    The rules arrive exactly as in production — inline in the typed
    envelope, resolved from the step's ruleset by the real envelope builder
    — and only the XSLT engine differs (lxml.isoschematron instead of
    Saxon's compile-and-run). The result is a genuine
    ``SchematronOutputEnvelope`` built via the canonical shared SVRL parser.
    """

    backend_name = "lxml-container-test"

    @property
    def is_async(self) -> bool:
        return False

    def is_available(self) -> bool:
        return True

    def execute(self, request) -> ExecutionResponse:
        from lxml import etree
        from lxml import isoschematron
        from validibot_shared.schematron.envelopes import SchematronFinding
        from validibot_shared.schematron.envelopes import SchematronOutputEnvelope
        from validibot_shared.schematron.envelopes import SchematronOutputs
        from validibot_shared.schematron.svrl import parse_svrl
        from validibot_shared.validations.envelopes import ValidationStatus
        from validibot_shared.validations.envelopes import ValidatorType

        # The REAL Django-side resolution: the same envelope the production
        # dispatchers build, carrying the step's uploaded rules inline.
        from validibot.validations.validators.schematron.launch import (
            resolve_schematron_inputs,
        )

        step = request.run.current_step_run.workflow_step
        inputs = resolve_schematron_inputs(
            validator=request.validator,
            ruleset=step.ruleset,
        )

        # "The container": compile the inline rules, run, parse the SVRL.
        schematron = isoschematron.Schematron(
            etree.fromstring(inputs.schematron_text.encode("utf-8")),
            store_report=True,
        )
        content = request.submission.get_content()
        raw = content.encode("utf-8") if isinstance(content, str) else content
        schematron.validate(etree.ElementTree(etree.fromstring(raw)))
        summary = parse_svrl(etree.tostring(schematron.validation_report))

        outputs = SchematronOutputs(
            engine_status="ok",
            passed=summary.passed,
            error_count=summary.error_count,
            warning_count=summary.warning_count,
            info_count=summary.info_count,
            fired_rule_count=summary.fired_rule_count,
            finding_rule_ids_by_severity=summary.finding_rule_ids_by_severity,
            findings=[
                SchematronFinding(
                    rule_id=f.rule_id,
                    message=f.message,
                    severity=f.severity,
                    location_xpath=f.location,
                    flag=f.flag,
                    role=f.role,
                )
                for f in summary.findings
            ],
            schematron_sha256=inputs.schematron_sha256,
            query_binding="xslt1",
            engine="lxml.isoschematron (test stand-in)",
        )
        envelope = SchematronOutputEnvelope(
            run_id=str(request.run.id),
            step_run_id=str(request.run.current_step_run.pk),
            execution_attempt_id="attempt-1",
            attempt_contract_version="validibot.attempt.v2",
            input_envelope_sha256="a" * 64,
            output_uri="file:///tmp/output.json",
            validator={
                "id": str(request.validator.id),
                "type": ValidatorType.SCHEMATRON,
                "version": str(request.validator.version),
            },
            status=(
                ValidationStatus.SUCCESS
                if summary.passed
                else ValidationStatus.FAILED_VALIDATION
            ),
            timing={},
            messages=[],
            outputs=outputs,
        )
        return ExecutionResponse(
            execution_id=f"lxml-test-{step.id}",
            is_complete=True,
            output_envelope=envelope,
        )


class TestPeppolPreflightWorkflow(TestCase):
    """The D7 two-layer Peppol pre-flight, authored and run end to end."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.user = UserFactory()
        grant_role(self.user, self.org, RoleCode.EXECUTOR)
        self.user.set_current_org(self.org)

        # ONE Schematron validator (the engine); the two steps differ only
        # by the rules their authors uploaded — the user's mental model.
        self.validator = _system_schematron_validator()
        self.workflow = WorkflowFactory(org=self.org)

        # Author step 1 through the REAL form: the EN 16931-like rules.
        self.en_step = self._author_step(
            "EN 16931 rules",
            (ASSETS / "en16931_subset.sch").read_text(),
        )
        # Author step 2: the Peppol-layer rules.
        self.peppol_step = self._author_step(
            "Peppol BIS rules",
            (ASSETS / "peppol_bis_subset.sch").read_text(),
        )

        # D7's optional policy gate as a step-level CEL assertion on step 2
        # — assertions attach to the same ruleset that carries the rules,
        # exactly like assertions on an XSD step.
        RulesetAssertion.objects.create(
            ruleset=self.peppol_step.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "o.error_count == 0"},
            cel_cache="o.error_count == 0",
            severity=Severity.ERROR,
            order=0,
            message_template="Peppol rules reported errors.",
        )

    def _author_step(self, name: str, rules_text: str):
        """Create a step the way the UI does: form → save_workflow_step."""
        form = SchematronStepConfigForm(
            data={"name": name, "schematron_text": rules_text},
        )
        assert form.is_valid(), form.errors
        return save_workflow_step(self.workflow, self.validator, form)

    def _execute(self, invoice_filename: str) -> ValidationRun:
        """Submit one fixture invoice and run the workflow to completion."""
        submission = SubmissionFactory(
            org=self.org,
            project=self.workflow.project,
            user=self.user,
            workflow=self.workflow,
            content=(ASSETS / invoice_filename).read_text(),
            file_type=SubmissionFileType.XML,
            original_filename=invoice_filename,
        )
        run = ValidationRun.objects.create(
            org=self.org,
            workflow=self.workflow,
            submission=submission,
            project=submission.project,
            user=self.user,
            status=ValidationRunStatus.PENDING,
        )
        with patch(
            "validibot.validations.services.execution.get_execution_backend",
            return_value=LxmlContainerBackend(),
        ):
            ValidationRunService().execute_workflow_steps(
                validation_run_id=run.id,
                user_id=self.user.id,
            )
        run.refresh_from_db()
        return run

    def test_valid_invoice_passes_both_rule_steps(self):
        """A conforming invoice sails through both uploaded rule sets.

        The whole point of the pre-flight: a clean invoice produces a
        SUCCEEDED run with both Schematron steps PASSED, zero ERROR
        findings, and the ``o.error_count == 0`` CEL gate satisfied.
        """
        run = self._execute("peppol_invoice_valid.xml")

        self.assertEqual(run.status, ValidationRunStatus.SUCCEEDED)
        step_runs = list(run.step_runs.order_by("step_order"))
        self.assertEqual(len(step_runs), 2)
        self.assertEqual(step_runs[0].status, StepStatus.PASSED)
        self.assertEqual(step_runs[1].status, StepStatus.PASSED)
        self.assertFalse(
            ValidationFinding.objects.filter(
                validation_run=run,
                severity=Severity.ERROR,
            ).exists(),
        )

    def test_bad_totals_fail_fast_at_the_first_rule_step(self):
        """The seeded totals defect fails step 1 with its native rule id.

        This is the flagship capability: an XSD-valid invoice violating an
        arithmetic rule fails with ``code=VB-CO-15`` (D10 — findings are
        queryable by the rule's own id), and fail-fast means step 2's rules
        never run on an invoice that already failed step 1.
        """
        run = self._execute("peppol_invoice_invalid.xml")

        self.assertEqual(run.status, ValidationRunStatus.FAILED)
        step_runs = list(run.step_runs.order_by("step_order"))
        self.assertEqual(step_runs[0].status, StepStatus.FAILED)
        self.assertFalse(
            any(sr.status == StepStatus.PASSED for sr in step_runs[1:]),
        )

        finding = ValidationFinding.objects.get(
            validation_run=run,
            code="VB-CO-15",
        )
        self.assertEqual(finding.severity, Severity.ERROR)
        self.assertIn("LegalMonetaryTotal", finding.meta.get("location_xpath", ""))

    def test_missing_profile_fails_only_the_second_rule_step(self):
        """An invoice passing step 1's rules can still fail step 2's.

        Proves the layering the D7 workflow intends: two steps of the SAME
        validator, each with different uploaded rules, report independently
        under their own native rule ids — and the CEL gate fails alongside.
        """
        run = self._execute("peppol_invoice_missing_profile.xml")

        self.assertEqual(run.status, ValidationRunStatus.FAILED)
        step_runs = list(run.step_runs.order_by("step_order"))
        self.assertEqual(len(step_runs), 2)
        self.assertEqual(step_runs[0].status, StepStatus.PASSED)
        self.assertEqual(step_runs[1].status, StepStatus.FAILED)

        finding = ValidationFinding.objects.get(
            validation_run=run,
            code="VB-PEPPOL-R001",
        )
        self.assertEqual(finding.severity, Severity.ERROR)
        self.assertFalse(
            ValidationFinding.objects.filter(
                validation_run=run,
                code="VB-CO-15",
            ).exists(),
        )


class TestPurchaseOrderPreflightWorkflow(TestCase):
    """The same pipeline on a NEUTRAL (non-invoice) pack, end to end.

    Reuses the exact production path — real step-config form, real inline-rules
    resolution, real ``ValidationRunService`` orchestration, real findings
    persistence, only the container's XSLT engine swapped for
    ``LxmlContainerBackend``. The point is to prove two things the invoice
    scenarios don't:

    1. **A warnings-only run SUCCEEDS end to end** (D3). Non-ERROR findings are
       persisted and visible, but the run is not failed by them — the
       "warnings are advisory" contract, proven through the whole stack rather
       than at the parser.
    2. The engine handles a domain with no invoice semantics at all, so nothing
       invoice-specific is quietly baked into the pipeline.
    """

    def setUp(self):
        self.org = OrganizationFactory()
        self.user = UserFactory()
        grant_role(self.user, self.org, RoleCode.EXECUTOR)
        self.user.set_current_org(self.org)

        self.validator = _system_schematron_validator()
        self.workflow = WorkflowFactory(org=self.org)

        # One Schematron step carrying the neutral purchase-order pack.
        form = SchematronStepConfigForm(
            data={
                "name": "Purchase-order rules",
                "schematron_text": (
                    ASSETS / "purchase_order" / "purchase_order.sch"
                ).read_text(),
            },
        )
        assert form.is_valid(), form.errors
        self.step = save_workflow_step(self.workflow, self.validator, form)

    def _execute(self, fixture_filename: str) -> ValidationRun:
        submission = SubmissionFactory(
            org=self.org,
            project=self.workflow.project,
            user=self.user,
            workflow=self.workflow,
            content=(ASSETS / "purchase_order" / fixture_filename).read_text(),
            file_type=SubmissionFileType.XML,
            original_filename=fixture_filename,
        )
        run = ValidationRun.objects.create(
            org=self.org,
            workflow=self.workflow,
            submission=submission,
            project=submission.project,
            user=self.user,
            status=ValidationRunStatus.PENDING,
        )
        with patch(
            "validibot.validations.services.execution.get_execution_backend",
            return_value=LxmlContainerBackend(),
        ):
            ValidationRunService().execute_workflow_steps(
                validation_run_id=run.id,
                user_id=self.user.id,
            )
        run.refresh_from_db()
        return run

    def test_warnings_only_order_succeeds_with_findings_persisted(self):
        """A warnings/info-only order run SUCCEEDS, and the findings persist.

        The order reconciles arithmetically (no ERROR) but trips a deprecated
        audit-status ``report`` and a missing description (WARNING) plus a
        missing note (INFO). The run must SUCCEED and the step PASS, while all
        three findings are stored under their native ids at their mapped
        severities — advisory findings surfaced without blocking.
        """
        run = self._execute("purchase_order_warnings_only.xml")

        self.assertEqual(run.status, ValidationRunStatus.SUCCEEDED)
        step_run = run.step_runs.get()
        self.assertEqual(step_run.status, StepStatus.PASSED)

        self.assertFalse(
            ValidationFinding.objects.filter(
                validation_run=run,
                severity=Severity.ERROR,
            ).exists(),
        )
        warning_codes = set(
            ValidationFinding.objects.filter(
                validation_run=run,
                severity=Severity.WARNING,
            ).values_list("code", flat=True),
        )
        self.assertEqual(warning_codes, {"VBPO-LEGACY-01", "VBPO-DESC-01"})
        self.assertTrue(
            ValidationFinding.objects.filter(
                validation_run=run,
                code="VBPO-NOTE-01",
                severity=Severity.INFO,
            ).exists(),
        )

    def test_bad_math_order_fails_on_the_cross_field_arithmetic_rule(self):
        """A cross-field arithmetic defect fails the run end to end.

        ``grandTotal`` does not equal the sum of the line totals, so the
        order-level ``VBPO-MATH-02`` fires as an ERROR and the run FAILS —
        proving the neutral pack's fatal path (a constraint no grammar can
        express) flows through the whole pipeline under its native rule id.
        """
        run = self._execute("purchase_order_bad_math.xml")

        self.assertEqual(run.status, ValidationRunStatus.FAILED)
        step_run = run.step_runs.get()
        self.assertEqual(step_run.status, StepStatus.FAILED)

        finding = ValidationFinding.objects.get(
            validation_run=run,
            code="VBPO-MATH-02",
        )
        self.assertEqual(finding.severity, Severity.ERROR)
