"""Use-case tests for the SysMLv2 radiator workflow example.

These tests exercise the production-style workflow shape used by the SysMLv2
radiator example:

1. JSON Schema validation for the submitted model artifact
2. Basic assertions with CEL expressions referencing workflow-level signals
3. FMU simulation as the final advanced-validator step

The radiator model stores key values such as ``emissivity`` and ``mass``
inside arrays of named elements (the SysML v2 pattern).  Workflow-level
signal mappings (``WorkflowSignalMapping``) use JSONPath filter expressions
to resolve these values into the ``s.*`` CEL namespace — e.g., a mapping
with source_path ``ownedMember[?@.name=='RadiatorPanel']...defaultValue``
and name ``emissivity`` makes ``s.emissivity`` available in CEL.
"""

from __future__ import annotations

from pathlib import Path

from django.test import TestCase

from tests.helpers.workflows import create_workflow_step_with_default_bindings
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.tests.factories import WorkflowFactory

ASSET_DIR = (
    Path(__file__).resolve().parents[1] / "assets" / "sysml_v2" / "radiator_example"
)

STEP_TWO_BINDINGS = {
    "panelArea": (
        "ownedMember[?@.name=='RadiatorPanel']"
        ".ownedAttribute[?@.name=='panelArea'].defaultValue"
    ),
    "emissivity": (
        "ownedMember[?@.name=='RadiatorPanel']"
        ".ownedAttribute[?@.name=='emissivity'].defaultValue"
    ),
    "absorptivity": (
        "ownedMember[?@.name=='RadiatorPanel']"
        ".ownedAttribute[?@.name=='absorptivity'].defaultValue"
    ),
    "mass": (
        "ownedMember[?@.name=='RadiatorPanel']"
        ".ownedAttribute[?@.name=='mass'].defaultValue"
    ),
    "solarIrradiance": (
        "ownedMember[?@.name=='ThermalEnvironment']"
        ".ownedAttribute[?@.name=='solarIrradiance'].defaultValue"
    ),
    "end": "ownedMember[?@.name=='ThermalCoupling'].end",
    "satisfiedRequirement": (
        'ownedMember[?@["@type"]=="sysml:SatisfyRequirementUsage"].satisfiedRequirement'
    ),
}

STEP_TWO_ASSERTIONS = (
    ("emissivity", "s.emissivity > 0.0 && s.emissivity <= 1.0"),
    ("absorptivity", "s.absorptivity >= 0.0 && s.absorptivity <= 1.0"),
    ("panelArea", "s.panelArea > 0.0"),
    ("mass", "s.mass > 0.0"),
    ("solarIrradiance", "s.solarIrradiance >= 0.0"),
    ("end", "size(s.end) == 2"),
    ("satisfiedRequirement", "s.satisfiedRequirement != null"),
)


class SysmlV2RadiatorWorkflowTests(TestCase):
    """Use-case coverage for the SysMLv2 radiator workflow."""

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        grant_role(cls.user, cls.org, RoleCode.EXECUTOR)
        cls.user.set_current_org(cls.org)
        cls.project = ProjectFactory(org=cls.org)
        cls.workflow = cls._build_workflow()

    @classmethod
    def _asset_text(cls, name: str) -> str:
        """Load a test asset from the radiator example directory."""
        return (ASSET_DIR / name).read_text(encoding="utf-8")

    @classmethod
    def _build_workflow(cls):
        """Create the three-step radiator workflow used in the regression tests."""
        workflow = WorkflowFactory(
            org=cls.org,
            user=cls.user,
            project=cls.project,
            is_active=True,
            allowed_file_types=[SubmissionFileType.JSON],
            name="SysMLv2 Radiator Workflow",
            slug="sysmlv2-radiator-workflow",
        )

        cls._add_schema_step(workflow)
        cls._add_cel_step(workflow)
        cls._add_fmu_step(workflow)

        return workflow

    @classmethod
    def _add_schema_step(cls, workflow):
        """Add step 1: JSON Schema validation for the radiator model."""
        validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
            is_system=False,
            org=cls.org,
            supports_assertions=False,
        )
        ruleset = Ruleset.objects.create(
            org=cls.org,
            user=cls.user,
            name="SysMLv2 radiator schema",
            ruleset_type=RulesetType.JSON_SCHEMA,
            version="1.0",
            rules_text=cls._asset_text("thermal_radiator_schema.json"),
            metadata={"schema_type": JSONSchemaVersion.DRAFT_2020_12.value},
        )
        create_workflow_step_with_default_bindings(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
            name="Check SysMLv2 file schema",
            config={},
        )

    @classmethod
    def _add_cel_step(cls, workflow):
        """Add step 2: CEL assertions over bound SysML values."""
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            org=cls.org,
        )
        ruleset = Ruleset.objects.create(
            org=cls.org,
            user=cls.user,
            name="SysMLv2 radiator CEL rules",
            ruleset_type=RulesetType.BASIC,
            version="1.0",
        )
        create_workflow_step_with_default_bindings(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=20,
            name="Check domain constraints",
            config={},
        )

        # Under the namespaced CEL design, values for CEL expressions come
        # from workflow-level signal mappings (the s.* namespace), not from
        # step-bound input bindings.  Create WorkflowSignalMapping rows so
        # the resolution service populates s.emissivity, s.mass, etc.
        for position, (name, source_path) in enumerate(STEP_TWO_BINDINGS.items()):
            WorkflowSignalMapping.objects.create(
                workflow=workflow,
                name=name,
                source_path=source_path,
                on_missing="null",
                position=position,
            )

        for order, (_target_key, expression) in enumerate(STEP_TWO_ASSERTIONS, start=1):
            RulesetAssertion.objects.create(
                ruleset=ruleset,
                order=order * 10,
                assertion_type=AssertionType.CEL_EXPRESSION,
                operator=AssertionOperator.CEL_EXPR,
                target_data_path="",
                severity=Severity.ERROR,
                rhs={"expr": expression},
            )

    @classmethod
    def _add_fmu_step(cls, workflow):
        """Add step 3: placeholder FMU step to mirror the production flow.

        The regression tests below fail before this step executes, but we still
        create a real FMU validator from the radiator example asset so the
        workflow shape matches production more closely.
        """
        from django.core.files.uploadedfile import SimpleUploadedFile

        from validibot.validations.services.fmu import create_fmu_validator

        upload = SimpleUploadedFile(
            "ThermalRadiator.fmu",
            (ASSET_DIR / "ThermalRadiator.fmu").read_bytes(),
            content_type="application/octet-stream",
        )
        validator = create_fmu_validator(
            org=cls.org,
            project=workflow.project,
            name="Thermal Radiator FMU",
            upload=upload,
        )
        ruleset = Ruleset.objects.create(
            org=cls.org,
            user=cls.user,
            name="SysMLv2 radiator FMU step",
            ruleset_type=RulesetType.FMU,
            version="1.0",
        )
        create_workflow_step_with_default_bindings(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=30,
            name="Run thermal radiation simulation",
            config={},
        )

    def _run_submission(self, asset_name: str) -> ValidationRun:
        """Create a submission from an asset and execute the workflow."""
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            workflow=self.workflow,
            file_type=SubmissionFileType.JSON,
            content=self._asset_text(asset_name),
        )
        validation_run = ValidationRun.objects.create(
            org=self.org,
            workflow=self.workflow,
            submission=submission,
            project=self.project,
            user=self.user,
            status=ValidationRunStatus.PENDING,
        )
        service = ValidationRunService()
        service.execute_workflow_steps(
            validation_run_id=validation_run.id,
            user_id=self.user.id,
        )
        validation_run.refresh_from_db()
        return validation_run

    def test_schema_failure_stops_before_domain_checks(self):
        """A schema-invalid SysML submission should fail on step 1 only."""
        validation_run = self._run_submission(
            "invalid_thermal_radiator_model_schema_fail.json",
        )

        self.assertEqual(validation_run.status, ValidationRunStatus.FAILED)

        step_runs = list(validation_run.step_runs.order_by("step_order"))
        self.assertEqual(len(step_runs), 1)
        self.assertEqual(step_runs[0].workflow_step.name, "Check SysMLv2 file schema")
        self.assertEqual(step_runs[0].status, "FAILED")
        self.assertGreater(step_runs[0].findings.count(), 0)

    def test_one_basic_assertion_failure(self):
        """A single invalid value (negative emissivity) should fail step 2.

        The 'one fail' asset has only one domain violation: emissivity = -0.5.
        All other values are valid.  The workflow should fail on step 2 with
        exactly one assertion finding.
        """
        validation_run = self._run_submission(
            "invalid_thermal_radiator_model_one_basic_assertion_fail.json",
        )

        self.assertEqual(validation_run.status, ValidationRunStatus.FAILED)

        step_runs = list(validation_run.step_runs.order_by("step_order"))
        self.assertEqual(len(step_runs), 2)
        self.assertEqual(step_runs[0].status, "PASSED")
        self.assertEqual(step_runs[1].workflow_step.name, "Check domain constraints")
        self.assertEqual(step_runs[1].status, "FAILED")

        findings = list(step_runs[1].findings.order_by("id"))
        # Only the emissivity > 0.0 assertion should fail
        error_findings = [f for f in findings if f.severity == Severity.ERROR]
        self.assertEqual(len(error_findings), 1)

    def test_many_basic_assertion_failures(self):
        """Multiple invalid values should produce multiple assertion failures.

        The 'many fails' asset has four domain violations: negative emissivity,
        negative mass, one-ended interface, and a dangling requirement reference.
        Step input bindings use JSONPath filter expressions to resolve values from
        the nested SysML v2 structure, so no 'undefined name' or timeout errors
        should appear.
        """
        validation_run = self._run_submission(
            "invalid_thermal_radiator_model_many_basic_assertion_fails.json",
        )

        self.assertEqual(validation_run.status, ValidationRunStatus.FAILED)

        step_runs = list(validation_run.step_runs.order_by("step_order"))
        self.assertEqual(len(step_runs), 2)
        self.assertEqual(step_runs[0].status, "PASSED")
        self.assertEqual(step_runs[1].workflow_step.name, "Check domain constraints")
        self.assertEqual(step_runs[1].status, "FAILED")

        findings = list(step_runs[1].findings.order_by("id"))
        error_findings = [f for f in findings if f.severity == Severity.ERROR]
        self.assertGreaterEqual(len(error_findings), 3)

        messages = [f.message for f in findings]
        self.assertTrue(
            all("undefined name" not in msg for msg in messages),
            f"Unexpected 'undefined name' errors: {messages}",
        )
        self.assertTrue(
            all("timed out" not in msg.lower() for msg in messages),
            f"Unexpected timeout errors: {messages}",
        )
