import json
from uuid import uuid4

import factory
from factory.django import DjangoModelFactory

from validibot.core.models import CallbackReceiptStatus
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CustomValidatorType
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationContinuationState
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Artifact
from validibot.validations.models import CallbackReceipt
from validibot.validations.models import CustomValidator
from validibot.validations.models import ExecutionAttempt
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationRunContinuation
from validibot.validations.models import ValidationStepRun
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorResourceFile
from validibot.workflows.tests.factories import WorkflowStepFactory


class RulesetFactory(DjangoModelFactory):
    class Meta:
        model = Ruleset

    org = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Test Ruleset {n}")
    ruleset_type = RulesetType.JSON_SCHEMA
    version = 1

    @factory.lazy_attribute
    def rules_text(self):
        if self.ruleset_type == RulesetType.JSON_SCHEMA:
            return json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {},
                }
            )
        if self.ruleset_type == RulesetType.XML_SCHEMA:
            return "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'/>"
        return ""

    @factory.lazy_attribute
    def metadata(self):
        if self.ruleset_type == RulesetType.JSON_SCHEMA:
            return {
                "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            }
        if self.ruleset_type == RulesetType.XML_SCHEMA:
            return {
                "schema_type": XMLSchemaType.XSD.value,
            }
        return {}


class ValidatorFactory(DjangoModelFactory):
    class Meta:
        model = Validator
        skip_postgeneration_save = True

    slug = factory.LazyFunction(lambda: f"test-validator-{uuid4().hex[:12]}")
    description = factory.Faker("text", max_nb_chars=200)
    name = factory.Sequence(lambda n: f"Test Validator {n}")
    validation_type = ValidationType.JSON_SCHEMA
    version = 1
    default_ruleset = None
    org = None
    is_system = True
    allow_custom_assertion_targets = False
    has_processor = factory.LazyAttribute(
        lambda obj: (
            obj.validation_type
            in (
                ValidationType.ENERGYPLUS,
                ValidationType.FMU,
                ValidationType.PORTFOLIO_MANAGER,
            )
        )
    )
    supports_assertions = factory.LazyAttribute(
        lambda obj: (
            obj.validation_type
            in (
                ValidationType.BASIC,
                ValidationType.JSON_SCHEMA,
                ValidationType.XML_SCHEMA,
                ValidationType.ENERGYPLUS,
                ValidationType.FMU,
                ValidationType.AI_ASSIST,
                ValidationType.CUSTOM_VALIDATOR,
                ValidationType.PORTFOLIO_MANAGER,
                ValidationType.SHACL,
                ValidationType.THERM,
            )
        )
    )

    @factory.post_generation
    def ensure_defaults(self, create, extracted, **kwargs):
        """Align flags with the chosen validation type."""
        desired = self.allow_custom_assertion_targets
        if self.validation_type == ValidationType.BASIC:
            desired = True
        if desired != self.allow_custom_assertion_targets:
            self.allow_custom_assertion_targets = desired
            if create:
                self.save(update_fields=["allow_custom_assertion_targets"])


class StepIODefinitionFactory(DjangoModelFactory):
    """Factory for the unified StepIODefinition model.

    Creates step I/O definitions with sensible defaults. Typically owned
    by a validator (library contract) — set workflow_step instead for
    step-specific definitions.
    """

    class Meta:
        model = "validations.StepIODefinition"

    contract_key = factory.Sequence(lambda n: f"io_value_{n}")
    native_name = factory.LazyAttribute(lambda o: o.contract_key)
    direction = "input"
    data_type = "number"
    origin_kind = "catalog"
    source_kind = "payload_path"
    is_path_editable = True
    validator = factory.SubFactory(ValidatorFactory)
    order = factory.Sequence(lambda n: n)


class StepInputBindingFactory(DjangoModelFactory):
    """Factory for per-step input bindings.

    Links a StepIODefinition to a WorkflowStep with binding configuration
    (source_data_path, default_value, is_required).
    """

    class Meta:
        model = "validations.StepInputBinding"

    workflow_step = factory.SubFactory(
        "validibot.workflows.tests.factories.WorkflowStepFactory",
    )
    io_definition = factory.SubFactory(StepIODefinitionFactory)
    source_scope = "submission_payload"
    source_data_path = ""
    is_required = True


class DerivationFactory(DjangoModelFactory):
    """Factory for computed derivation values."""

    class Meta:
        model = "validations.Derivation"

    contract_key = factory.Sequence(lambda n: f"derived_{n}")
    expression = "value_a + value_b"
    data_type = "number"
    validator = factory.SubFactory(ValidatorFactory)
    order = factory.Sequence(lambda n: n)


class CustomValidatorFactory(DjangoModelFactory):
    class Meta:
        model = CustomValidator

    validator = factory.SubFactory(
        ValidatorFactory,
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        is_system=False,
    )
    org = factory.SubFactory(OrganizationFactory)
    custom_type = CustomValidatorType.MODELICA
    base_validation_type = ValidationType.CUSTOM_VALIDATOR
    notes = "Custom validator for tests."
    created_by = factory.SubFactory(UserFactory)


class ValidationRunFactory(DjangoModelFactory):
    """Create validation runs with a tenant-consistent relationship graph."""

    class Meta:
        model = ValidationRun

    @classmethod
    def _generate(cls, strategy, params):
        """Propagate explicit parents into the generated submission graph.

        ``factory_boy`` otherwise creates the submission before evaluating the
        lazy run fields. Supplying only ``org`` or ``workflow`` would therefore
        create unrelated tenant rows, which is invalid in production and makes
        security tests misleading.
        """

        params = dict(params)
        submission = params.get("submission")
        workflow = params.get("workflow")
        org = params.get("org")
        project = params.get("project")
        user = params.get("user")

        if submission is not None:
            params.setdefault("workflow", submission.workflow)
            params.setdefault("org", submission.org)
            params.setdefault("project", submission.project)
            params.setdefault("user", submission.user)
        else:
            if workflow is not None:
                params.setdefault("org", workflow.org)
                params.setdefault("project", workflow.project)
                params.setdefault("submission__workflow", workflow)
                params.setdefault("submission__org", workflow.org)
                params.setdefault("submission__project", workflow.project)
            if org is not None:
                params.setdefault("submission__org", org)
            if project is not None:
                params.setdefault("submission__project", project)
            if user is not None:
                params.setdefault("submission__user", user)

        return super()._generate(strategy, params)

    submission = factory.SubFactory(SubmissionFactory)
    workflow = factory.LazyAttribute(lambda o: o.submission.workflow)
    org = factory.LazyAttribute(lambda o: o.submission.org)
    project = factory.LazyAttribute(lambda o: o.submission.project)
    user = factory.LazyAttribute(lambda o: o.submission.user)
    status = ValidationRunStatus.PENDING
    source = ValidationRunSource.LAUNCH_PAGE


class ValidationStepRunFactory(DjangoModelFactory):
    class Meta:
        model = ValidationStepRun

    validation_run = factory.SubFactory(ValidationRunFactory)
    workflow_step = factory.SubFactory(
        WorkflowStepFactory,
        workflow=factory.SelfAttribute("..validation_run.workflow"),
    )
    step_order = factory.Sequence(lambda n: n * 10)
    status = StepStatus.PENDING
    output = factory.Dict({})


class ExecutionAttemptFactory(DjangoModelFactory):
    """Build a durable attempt attached to its validation step run."""

    class Meta:
        model = ExecutionAttempt

    step_run = factory.SubFactory(
        ValidationStepRunFactory,
    )
    attempt_number = 1
    state = ExecutionAttemptState.PENDING
    runner_type = "docker"
    input_envelope_sha256 = "a" * 64
    output_envelope_uri = "gs://bucket/output.json"


class ValidationFindingFactory(DjangoModelFactory):
    class Meta:
        model = ValidationFinding

    validation_step_run = factory.SubFactory(ValidationStepRunFactory)
    # Ensure run FK matches the step_run's validation_run
    validation_run = factory.LazyAttribute(
        lambda o: o.validation_step_run.validation_run,
    )
    severity = Severity.ERROR
    code = ""
    message = factory.Faker("sentence")
    path = factory.Faker("file_path", depth=3)
    meta = factory.Dict({})
    ruleset_assertion = None


class ArtifactFactory(DjangoModelFactory):
    """Build artifacts whose organization matches their validation run."""

    class Meta:
        model = Artifact

    validation_run = factory.SubFactory(ValidationRunFactory)
    org = factory.SelfAttribute("validation_run.org")
    label = factory.Sequence(lambda n: f"artifact-{n}.txt")
    content_type = "text/plain"
    size_bytes = 0
    sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    storage_version = (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    # file is optional; tests can attach a ContentFile if needed


class RulesetAssertionFactory(DjangoModelFactory):
    class Meta:
        model = RulesetAssertion

    ruleset = factory.SubFactory(RulesetFactory)
    order = factory.Sequence(lambda n: n * 10)
    assertion_type = AssertionType.BASIC
    operator = AssertionOperator.LE
    target_data_path = "facility_electric_demand_w"
    severity = Severity.ERROR
    rhs = factory.Dict({"value": 100})
    options = factory.Dict({})
    message_template = "Peak too high."


class CallbackReceiptFactory(DjangoModelFactory):
    """Factory for CallbackReceipt model used in idempotency tests."""

    class Meta:
        model = CallbackReceipt

    callback_id = factory.Sequence(lambda n: f"cb-uuid-{n}")
    validation_run = factory.SubFactory(ValidationRunFactory)
    execution_attempt = factory.LazyAttribute(
        lambda obj: ExecutionAttemptFactory(
            step_run__validation_run=obj.validation_run,
            state=ExecutionAttemptState.COMPLETED,
        ),
    )
    status = CallbackReceiptStatus.COMPLETED
    result_uri = factory.LazyAttribute(
        lambda o: f"gs://bucket/runs/{o.validation_run.id}/output.json"
    )


class ValidationRunContinuationFactory(DjangoModelFactory):
    """Build a continuation with one internally consistent callback graph."""

    class Meta:
        model = ValidationRunContinuation

    completed_step_run = factory.SubFactory(
        ValidationStepRunFactory,
        status=StepStatus.PASSED,
        validation_run__status=ValidationRunStatus.RUNNING,
    )
    validation_run = factory.LazyAttribute(
        lambda obj: obj.completed_step_run.validation_run,
    )
    callback_receipt = factory.LazyAttribute(
        lambda obj: CallbackReceiptFactory(
            validation_run=obj.validation_run,
            execution_attempt=ExecutionAttemptFactory(
                step_run=obj.completed_step_run,
                state=ExecutionAttemptState.COMPLETED,
            ),
        ),
    )
    resume_from_step = factory.LazyAttribute(
        lambda obj: obj.completed_step_run.step_order,
    )
    state = ValidationContinuationState.PENDING


class ValidatorResourceFileFactory(DjangoModelFactory):
    """Factory for ValidatorResourceFile — catalog-mode resource files.

    Creates an EnergyPlus weather file by default. The ``file`` field uses
    a SimpleUploadedFile so tests don't touch the real filesystem.
    """

    class Meta:
        model = ValidatorResourceFile

    validator = factory.SubFactory(
        ValidatorFactory,
        validation_type=ValidationType.ENERGYPLUS,
    )
    resource_type = ResourceFileType.ENERGYPLUS_WEATHER
    name = factory.Sequence(lambda n: f"Weather File {n}")
    filename = factory.Sequence(lambda n: f"weather-{n}.epw")
    file = factory.django.FileField(filename="weather.epw", data=b"LOCATION,Test")
    is_default = False
