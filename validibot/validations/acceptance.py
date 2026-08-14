"""Repeatable live acceptance for managed validator deployments.

This module deliberately keeps the production acceptance surface small.  It
reuses the normal workflow launcher, durable execution-attempt records, the
existing GCS capability probe, and source-controlled fixtures.  The GCP
operator recipe owns the temporary maintenance window; this module owns only
application-level preparation, execution, and a secret-free JSON report.

The acceptance workflows live in a dedicated internal organization and use an
operator account with an unusable password.  They are reused between runs so a
production acceptance does not create a growing collection of fixture
definitions.  Each invocation still creates fresh submissions, runs, attempts,
and immutable evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from google.cloud import storage

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.models import Submission
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import RoleCode
from validibot.users.models import User
from validibot.users.models import ensure_default_project
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionRoutingMode
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.models import ExecutionAttempt
from validibot.validations.models import Ruleset
from validibot.validations.models import StepInputBinding
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.services.cloud_run.gcs_capability_probe import (
    probe_attempt_gcs_runtime_capability,
)
from validibot.validations.services.execution.deployments import (
    resolve_backend_release_pair,
)
from validibot.validations.services.fmu import build_introspection_metadata
from validibot.validations.services.fmu import introspect_fmu
from validibot.validations.services.fmu_step_io import sync_step_fmu_io_definitions
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.services.template_step_io import (
    sync_step_template_io_definitions,
)
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.validators.base.config import get_config
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.models import WorkflowStepResource

if TYPE_CHECKING:
    from datetime import datetime

    from validibot.projects.models import Project

ACCEPTANCE_SCHEMA_VERSION = "validibot.validator-acceptance.v2"
ACCEPTANCE_FIXTURE_VERSION = 1
ACCEPTANCE_ORG_SLUG = "validibot-validator-acceptance"
ACCEPTANCE_USERNAME = "validibot-validator-acceptance"
ACCEPTANCE_WEATHER_FILENAME = "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"
RELEASE_TAG_PATTERN = re.compile(
    r"^(?P<backend>[a-z][a-z0-9_]*)-v"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$"
)
MAX_ATTEMPTS_PER_BACKEND = 20
ROUTINE_ACCEPTANCE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """Identity and fixture metadata for one managed validator backend."""

    key: str
    validation_type: str
    ruleset_type: str | None


BACKENDS = (
    BackendSpec(
        "energyplus",
        ValidationType.ENERGYPLUS,
        RulesetType.ENERGYPLUS,
    ),
    BackendSpec("fmu", ValidationType.FMU, RulesetType.FMU),
    BackendSpec("shacl", ValidationType.SHACL, RulesetType.SHACL),
    BackendSpec(
        "schematron",
        ValidationType.SCHEMATRON,
        RulesetType.SCHEMATRON,
    ),
    BackendSpec(
        "portfolio_manager",
        ValidationType.PORTFOLIO_MANAGER,
        RulesetType.PORTFOLIO_MANAGER,
    ),
    BackendSpec("pdf", ValidationType.PDF, None),
)
BACKENDS_BY_KEY = {spec.key: spec for spec in BACKENDS}


def _compatible_validators(spec: BackendSpec) -> list[Validator]:
    """Return every semantic Validator that this backend release must support."""
    validators = list(
        Validator.objects.filter(
            execution_backend_slug=spec.key,
            validation_type=spec.validation_type,
            is_system=True,
            is_enabled=True,
            release_state=ValidatorReleaseState.PUBLISHED,
            availability_state=ValidatorAvailabilityState.AVAILABLE,
        ).order_by("slug", "version", "pk")
    )
    if not validators:
        raise ValueError(
            f"No published compatible Validator declares backend {spec.key}"
        )
    return validators


@dataclass(frozen=True, slots=True)
class AcceptanceScenario:
    """A reusable workflow plus the exact submission used to exercise it."""

    backend: BackendSpec
    workflow: Workflow
    inline_text: str | bytes
    filename: str
    file_type: str
    fixture_sha256: str
    validator_id: str = ""
    validator_slug: str = ""
    validator_version: str = ""


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    """One stable, secret-free acceptance verdict."""

    check_id: str
    status: str
    summary: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Project the check to the versioned report format."""
        return {
            "id": self.check_id,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


class AcceptanceReport:
    """Accumulate verdicts and produce the single operator-facing report."""

    def __init__(
        self,
        *,
        backend: str,
        release_tag: str,
        attempts_per_backend: int,
    ) -> None:
        timestamp = timezone.now()
        suffix = uuid.uuid4().hex[:8]
        self.acceptance_id = (
            f"va-{timestamp:%Y%m%dT%H%M%SZ}-{backend}-"
            f"{release_tag.rsplit('-v', 1)[1]}-{suffix}"
        )
        self.backend = backend
        self.release_tag = release_tag
        self.attempts_per_backend = attempts_per_backend
        self.started_at = timestamp
        self.finished_at: datetime | None = None
        self.checks: list[AcceptanceCheck] = []

    def add(
        self,
        check_id: str,
        status: str,
        summary: str,
        **details: Any,
    ) -> None:
        """Append one check while keeping status vocabulary constrained."""
        if status not in {"passed", "failed", "skipped"}:
            raise ValueError(f"Unknown acceptance status: {status}")
        self.checks.append(
            AcceptanceCheck(
                check_id=check_id,
                status=status,
                summary=summary,
                details=details,
            )
        )

    @property
    def passed(self) -> bool:
        """Return true only when the report has checks and none failed."""
        return bool(self.checks) and all(
            check.status != "failed" for check in self.checks
        )

    def finish(self) -> None:
        """Freeze the completion time used by the report projection."""
        self.finished_at = timezone.now()

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON-safe acceptance document."""
        finished_at = self.finished_at or timezone.now()
        return {
            "schema_version": ACCEPTANCE_SCHEMA_VERSION,
            "acceptance_id": self.acceptance_id,
            "stage": str(getattr(settings, "VALIDIBOT_STAGE", "") or "unknown"),
            "backend": self.backend,
            "source_release_tag": self.release_tag,
            "backend_release": self.release_tag.rsplit("-v", 1)[1],
            "attempts_per_backend": self.attempts_per_backend,
            "started_at": self.started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }


class AcceptanceFixtureBuilder:
    """Create or reuse the private managed-validator acceptance workflows."""

    def __init__(self) -> None:
        self.user, self.org, self.project = self._ensure_actor()

    def build_all(self) -> dict[str, AcceptanceScenario]:
        """Return deterministic scenarios for every managed backend."""
        with transaction.atomic():
            return {spec.key: self._build_scenario(spec) for spec in BACKENDS}

    def build(self, spec: BackendSpec) -> AcceptanceScenario:
        """Return the deterministic scenario for one selected backend."""
        with transaction.atomic():
            return self._build_scenario(spec)

    def build_compatible(self, spec: BackendSpec) -> tuple[AcceptanceScenario, ...]:
        """Build one scenario for every executable published semantic Validator."""
        with transaction.atomic():
            validators = _compatible_validators(spec)
            return tuple(
                self._build_scenario(spec, validator=validator)
                for validator in validators
            )

    def _ensure_actor(self) -> tuple[User, Organization, Project]:
        """Create a non-login operator identity that bypasses tenant quotas."""
        user, created = User.objects.get_or_create(
            username=ACCEPTANCE_USERNAME,
            defaults={
                "email": "validator-acceptance@localhost.invalid",
                "name": "Validator Acceptance Operator",
                "is_active": True,
                "is_superuser": True,
                "is_staff": False,
            },
        )
        required_updates: list[str] = []
        if not user.is_active:
            user.is_active = True
            required_updates.append("is_active")
        if not user.is_superuser:
            user.is_superuser = True
            required_updates.append("is_superuser")
        if user.is_staff:
            user.is_staff = False
            required_updates.append("is_staff")
        if created or user.has_usable_password():
            user.set_unusable_password()
            required_updates.append("password")
        if required_updates:
            user.save(update_fields=sorted(set(required_updates)))

        org, _ = Organization.objects.get_or_create(
            slug=ACCEPTANCE_ORG_SLUG,
            defaults={"name": "Validibot Validator Acceptance"},
        )
        membership, membership_created = Membership.objects.get_or_create(
            user=user,
            org=org,
            defaults={"is_active": True},
        )
        if not membership.is_active:
            membership.is_active = True
            membership.save(update_fields=["is_active"])
        if membership_created or not membership.has_role(RoleCode.EXECUTOR):
            membership.set_roles({RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR})
        if user.current_org_id != org.pk:
            user.set_current_org(org)
        return user, org, ensure_default_project(org)

    def _build_scenario(
        self,
        spec: BackendSpec,
        *,
        validator: Validator | None = None,
    ) -> AcceptanceScenario:
        """Create one backend-specific workflow and submission definition."""
        validator = validator or self._current_validator(spec)
        existing = self._existing_workflow(spec, validator)
        if existing is not None:
            return self._scenario_for_existing(spec, existing, validator)
        if spec.validation_type == ValidationType.ENERGYPLUS:
            return self._create_energyplus(spec, validator)
        if spec.validation_type == ValidationType.FMU:
            return self._create_fmu(spec, validator)
        if spec.validation_type == ValidationType.SHACL:
            return self._create_shacl(spec, validator)
        if spec.validation_type == ValidationType.SCHEMATRON:
            return self._create_schematron(spec, validator)
        if spec.validation_type == ValidationType.PORTFOLIO_MANAGER:
            return self._create_portfolio_manager(spec, validator)
        if spec.validation_type == ValidationType.PDF:
            return self._create_pdf(spec, validator)
        raise ValueError(f"Unsupported acceptance backend: {spec.key}")

    def _current_validator(self, spec: BackendSpec) -> Validator:
        """Resolve the exact current system-validator contract."""
        config = get_config(spec.validation_type)
        if config is None:
            raise ValueError(f"No registered validator config for {spec.key}")
        validator = Validator.objects.filter(
            slug=config.slug,
            version=config.version,
            validation_type=spec.validation_type,
            is_system=True,
            is_enabled=True,
            availability_state=ValidatorAvailabilityState.AVAILABLE,
        ).first()
        if validator is None:
            raise ValueError(
                f"Current {spec.key} system validator is missing; run sync_validators"
            )
        return validator

    def _workflow_slug(self, spec: BackendSpec, validator: Validator) -> str:
        """Version fixture identity without mutating workflows already in use."""
        semantic_key = hashlib.sha256(
            f"{validator.slug}:{validator.version}".encode()
        ).hexdigest()[:8]
        return (
            f"validator-acceptance-{spec.key}-f{ACCEPTANCE_FIXTURE_VERSION}-"
            f"{semantic_key}"
        )

    def _existing_workflow(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> Workflow | None:
        """Reuse an immutable fixture workflow only when its contract matches."""
        workflow = Workflow.objects.filter(
            org=self.org,
            slug=self._workflow_slug(spec, validator),
            version="1",
        ).first()
        if workflow is None:
            return None
        step = workflow.steps.order_by("order", "pk").first()
        if step is None or step.validator_id != validator.pk:
            raise ValueError(f"Existing {spec.key} acceptance workflow has drifted")
        return workflow

    def _scenario_for_existing(
        self,
        spec: BackendSpec,
        workflow: Workflow,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Rebuild source-controlled submission metadata for a reused workflow."""
        content, filename, file_type = self._submission_fixture(spec)
        return AcceptanceScenario(
            backend=spec,
            workflow=workflow,
            inline_text=content,
            filename=filename,
            file_type=file_type,
            fixture_sha256=_sha256_content(content),
            validator_id=str(validator.pk),
            validator_slug=validator.slug,
            validator_version=str(validator.version),
        )

    def _create_workflow(
        self,
        spec: BackendSpec,
        validator: Validator,
        *,
        allowed_file_types: list[str],
        rules_text: str = "",
        rules_metadata: dict[str, Any] | None = None,
        step_config: dict[str, Any] | None = None,
    ) -> tuple[Workflow, WorkflowStep]:
        """Create the common one-step workflow structure."""
        workflow_slug = self._workflow_slug(spec, validator)
        workflow = Workflow.objects.create(
            org=self.org,
            slug=workflow_slug,
            version="1",
            name=f"Validator acceptance: {spec.key}",
            user=self.user,
            project=self.project,
            is_active=True,
            allowed_file_types=allowed_file_types,
        )
        ruleset = (
            self._ensure_ruleset(
                spec,
                workflow_slug=workflow_slug,
                rules_text=rules_text,
                rules_metadata=rules_metadata,
            )
            if spec.ruleset_type is not None
            else None
        )
        step = WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
            name=f"{spec.key.title()} acceptance canary",
            config=step_config or {},
        )
        return workflow, step

    def _ensure_ruleset(
        self,
        spec: BackendSpec,
        *,
        workflow_slug: str,
        rules_text: str,
        rules_metadata: dict[str, Any] | None,
    ) -> Ruleset:
        """Create the deterministic ruleset or reclaim its orphaned record.

        Workflows are operational data and may be removed independently of
        their reusable rulesets. A later acceptance run must therefore reclaim
        the unreferenced deterministic ruleset instead of violating its unique
        identity constraint. An attached ruleset indicates genuine fixture
        drift and fails closed so acceptance never mutates another workflow.
        """
        expected_values = {
            "user": self.user,
            "rules_text": rules_text,
            "metadata": rules_metadata or {},
        }
        if spec.ruleset_type is None:
            raise ValueError(f"{spec.key} acceptance does not use a ruleset")
        ruleset, created = Ruleset.objects.get_or_create(
            org=self.org,
            name=f"{workflow_slug}-rules",
            ruleset_type=spec.ruleset_type,
            version="1",
            defaults=expected_values,
        )
        if created:
            return ruleset
        if WorkflowStep.objects.filter(ruleset=ruleset).exists():
            raise ValueError(
                f"Existing {spec.key} acceptance ruleset is attached to "
                "another workflow"
            )
        ruleset.user = self.user
        ruleset.rules_file = ""
        ruleset.rules_text = rules_text
        ruleset.metadata = rules_metadata or {}
        ruleset.save()
        return ruleset

    def _create_energyplus(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create the small parameterised EnergyPlus canary."""
        template = self._asset_text("idf/window_glazing_template.idf")
        weather = ValidatorResourceFile.objects.filter(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            org__isnull=True,
            filename=ACCEPTANCE_WEATHER_FILENAME,
        ).first()
        if weather is None:
            raise ValueError(
                f"Acceptance weather file {ACCEPTANCE_WEATHER_FILENAME} is missing"
            )
        variables = [
            {"name": "U_FACTOR", "variable_type": "number"},
            {"name": "SHGC", "variable_type": "number"},
            {"name": "VISIBLE_TRANSMITTANCE", "variable_type": "number"},
        ]
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.JSON],
            step_config={"run_simulation": True, "case_sensitive": True},
        )
        sync_step_template_io_definitions(step, variables)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=ContentFile(
                template.encode("utf-8"),
                name="window_glazing_template.idf",
            ),
            filename="window_glazing_template.idf",
            resource_type="energyplus_model_template",
        )
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=weather,
        )
        ensure_step_input_bindings(step)
        return self._scenario_for_existing(spec, workflow, validator)

    def _create_fmu(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create a system-FMU workflow using the tiny Feedthrough fixture."""
        fmu_payload = self._asset_bytes("fmu/Feedthrough.fmu")
        result = introspect_fmu(fmu_payload, "Feedthrough.fmu")
        sim = result.simulation_defaults
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.JSON],
            step_config={
                "fmu_simulation": {
                    "start_time": sim.start_time,
                    "stop_time": sim.stop_time,
                    "step_size": sim.step_size,
                    "tolerance": sim.tolerance,
                },
                "fmu_introspection": build_introspection_metadata(result),
            },
        )
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.FMU_MODEL,
            step_resource_file=ContentFile(fmu_payload, name="Feedthrough.fmu"),
            filename="Feedthrough.fmu",
            resource_type="fmu",
        )
        variables = [
            {
                "name": variable.name,
                "causality": variable.causality,
                "variability": variable.variability,
                "value_reference": variable.value_reference,
                "value_type": variable.value_type,
                "unit": variable.unit,
                "description": variable.description,
                "label": "",
            }
            for variable in result.variables
        ]
        sync_step_fmu_io_definitions(step, variables)
        ensure_step_input_bindings(step)
        # The sync creates payload bindings for FMU inputs.  Keep their blank
        # paths: the resolver intentionally falls back to each contract key.
        StepInputBinding.objects.filter(
            workflow_step=step,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
        ).update(is_required=True)
        return self._scenario_for_existing(spec, workflow, validator)

    def _create_shacl(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create the minimal conforming RDF/SHACL canary."""
        shapes = self._asset_text("shacl/example_person_shapes.ttl")
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.TEXT],
            rules_text=shapes,
            rules_metadata={"submission_format": "turtle"},
        )
        ensure_step_input_bindings(step)
        return self._scenario_for_existing(spec, workflow, validator)

    def _create_schematron(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create the valid calibration-certificate Schematron canary."""
        rules = self._asset_text("schematron/calibration/calibration-rules-demo.sch")
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.XML],
            rules_text=rules,
        )
        ensure_step_input_bindings(step)
        return self._scenario_for_existing(spec, workflow, validator)

    def _create_portfolio_manager(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create a single-report XML canary that exercises metric extraction."""
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.XML],
            step_config={
                "submission_structure": "single_report",
                "default_euit_kbtu_ft2_yr": "40",
                "compare_to_euit": True,
                "near_target_percent": "10",
            },
        )
        ensure_step_input_bindings(step)
        StepInputBinding.objects.filter(
            workflow_step=step,
            io_definition__contract_key="default_euit_kbtu_ft2_yr",
        ).update(default_value="40")
        return self._scenario_for_existing(spec, workflow, validator)

    def _create_pdf(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create the fixed-policy positive PDF package canary."""
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.PDF],
            step_config={
                "policy": "static_text_package_v1",
                "execution_timeout_seconds": 300,
            },
        )
        ensure_step_input_bindings(step)
        return self._scenario_for_existing(spec, workflow, validator)

    def _submission_fixture(self, spec: BackendSpec) -> tuple[str | bytes, str, str]:
        """Return exact source-controlled input bytes for one backend."""
        if spec.validation_type == ValidationType.ENERGYPLUS:
            return (
                json.dumps(
                    {
                        "U_FACTOR": 2.0,
                        "SHGC": 0.4,
                        "VISIBLE_TRANSMITTANCE": 0.6,
                    },
                    sort_keys=True,
                ),
                "energyplus-acceptance.json",
                SubmissionFileType.JSON,
            )
        if spec.validation_type == ValidationType.FMU:
            return (
                json.dumps(
                    {
                        "real_continuous_in": 42.0,
                        "real_discrete_in": 7.0,
                        "int_in": 7,
                        "bool_in": True,
                    },
                    sort_keys=True,
                ),
                "fmu-acceptance.json",
                SubmissionFileType.JSON,
            )
        if spec.validation_type == ValidationType.SHACL:
            return (
                self._asset_text("shacl/valid_person.ttl"),
                "valid-person.ttl",
                SubmissionFileType.TEXT,
            )
        if spec.validation_type == ValidationType.SCHEMATRON:
            return (
                self._asset_text(
                    "schematron/calibration/calibration-certificate-valid.xml"
                ),
                "calibration-certificate-valid.xml",
                SubmissionFileType.XML,
            )
        if spec.validation_type == ValidationType.PORTFOLIO_MANAGER:
            return (
                self._asset_text("portfolio_manager/property-report-valid.xml"),
                "property-report-valid.xml",
                SubmissionFileType.XML,
            )
        if spec.validation_type == ValidationType.PDF:
            return (
                self._asset_bytes("pdf/aec-issue-package-clean.pdf"),
                "aec-issue-package-clean.pdf",
                SubmissionFileType.PDF,
            )
        raise ValueError(f"Unsupported acceptance backend: {spec.key}")

    def _asset_bytes(self, relative_path: str) -> bytes:
        """Read a shipped fixture and fail clearly when images omit tests."""
        path = Path(settings.BASE_DIR) / "tests" / "assets" / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"Validator acceptance fixture missing: {path}")
        return path.read_bytes()

    def _asset_text(self, relative_path: str) -> str:
        """Read one UTF-8 fixture without silently replacing invalid bytes."""
        return self._asset_bytes(relative_path).decode("utf-8")


class ValidatorAcceptanceRunner:
    """Run preflight, storage, and end-to-end canaries for one release."""

    def __init__(
        self,
        *,
        backend: str,
        release_tag: str,
        attempts_per_backend: int = ROUTINE_ACCEPTANCE_ATTEMPTS,
        timeout_seconds: int = 1200,
        poll_interval_seconds: float = 2.0,
        run_storage_probe: bool = True,
        ambient_isolation_verified: bool = False,
        routing_mode: ExecutionRoutingMode | str = ExecutionRoutingMode.NORMAL,
        record_acceptance: bool = False,
    ) -> None:
        if backend not in BACKENDS_BY_KEY:
            allowed = ", ".join(sorted(BACKENDS_BY_KEY))
            raise ValueError(f"backend must be one of: {allowed}")
        match = RELEASE_TAG_PATTERN.fullmatch(release_tag)
        if match is None:
            raise ValueError("release_tag must be <backend>-vX.Y.Z")
        if match.group("backend") != backend:
            raise ValueError("release_tag backend must match the selected backend")
        if not 1 <= attempts_per_backend <= MAX_ATTEMPTS_PER_BACKEND:
            raise ValueError(
                f"attempts_per_backend must be between 1 and {MAX_ATTEMPTS_PER_BACKEND}"
            )
        self.backend = backend
        self.spec = BACKENDS_BY_KEY[backend]
        self.release_tag = release_tag
        self.release_version = match.group("version")
        self.attempts_per_backend = attempts_per_backend
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.run_storage_probe = run_storage_probe
        self.ambient_isolation_verified = ambient_isolation_verified
        try:
            self.routing_mode = ExecutionRoutingMode(routing_mode)
        except ValueError as exc:
            raise ValueError("routing_mode must be normal or job-only") from exc
        if self.routing_mode not in {
            ExecutionRoutingMode.NORMAL,
            ExecutionRoutingMode.JOB_ONLY,
        }:
            raise ValueError("routing_mode must be normal or job-only")
        if record_acceptance and self.routing_mode != ExecutionRoutingMode.JOB_ONLY:
            raise ValueError("record_acceptance requires the successful job-only pass")
        self.record_acceptance = record_acceptance

    def run(self) -> AcceptanceReport:
        """Execute the complete non-destructive application acceptance suite."""
        report = AcceptanceReport(
            backend=self.backend,
            release_tag=self.release_tag,
            attempts_per_backend=self.attempts_per_backend,
        )
        deployments_ok = self._check_deployments(report)
        self._check_storage(report)
        if not deployments_ok:
            report.add(
                "VA-SMOKE-ABORTED",
                "failed",
                "Canaries were not started because deployment preflight failed.",
            )
            report.finish()
            return report

        try:
            scenarios = AcceptanceFixtureBuilder().build_compatible(self.spec)
        except Exception as exc:
            report.add(
                "VA-FIXTURES",
                "failed",
                "Acceptance fixtures could not be prepared.",
                error=_safe_error(exc),
            )
            report.finish()
            return report

        report.add(
            "VA-FIXTURES",
            "passed",
            f"The {self.backend} semantic acceptance workflows are ready.",
            fixture_version=ACCEPTANCE_FIXTURE_VERSION,
            validators=[
                {
                    "validator_id": scenario.validator_id,
                    "slug": scenario.validator_slug,
                    "version": scenario.validator_version,
                    "fixture_sha256": scenario.fixture_sha256,
                }
                for scenario in scenarios
            ],
        )
        launched_by_scenario = {
            _scenario_key(scenario): self._launch_backend(report, scenario)
            for scenario in scenarios
        }
        self._wait_for_runs(launched_by_scenario)
        for scenario in scenarios:
            launched = launched_by_scenario[_scenario_key(scenario)]
            for sequence, run in launched:
                run.refresh_from_db()
                self._record_run(report, scenario, sequence, run)
            self._record_latency(report, scenario, launched)
        if report.passed and self.record_acceptance:
            self._record_pair_acceptance(report, scenarios)
        report.finish()
        return report

    def _check_deployments(self, report: AcceptanceReport) -> bool:
        """Require the candidate pair to have the requested routing mode."""
        try:
            validators = _compatible_validators(self.spec)
            routes = [
                self._accepted_routes(
                    self.spec,
                    self.release_version,
                    validator=validator,
                )
                for validator in validators
            ]
            report.add(
                f"VA-{self.spec.key.upper()}-ROUTE",
                "passed",
                (
                    "Candidate Service and Job have the requested private "
                    f"{self.routing_mode.value} routing."
                ),
                backend=self.backend,
                source_release_tag=self.release_tag,
                routing_mode=self.routing_mode.value,
                validators=[
                    {
                        "validator_id": str(validator.pk),
                        "slug": validator.slug,
                        "version": validator.version,
                        "service_deployment_id": str(service.pk),
                        "service_revision": service.deployment_revision,
                        "service_image_digest": service.backend_image_digest,
                        "service_minimum_instances": service.minimum_instances,
                        "service_maximum_instances": service.maximum_instances,
                        "job_deployment_id": str(job.pk),
                        "job_revision": job.deployment_revision,
                    }
                    for validator, service, job in routes
                ],
            )
        except Exception as exc:
            report.add(
                f"VA-{self.spec.key.upper()}-ROUTE",
                "failed",
                "Managed deployment route failed acceptance preflight.",
                error=_safe_error(exc),
            )
            return False
        return True

    def _record_pair_acceptance(
        self,
        report: AcceptanceReport,
        scenarios: tuple[AcceptanceScenario, ...],
    ) -> None:
        """Persist acceptance atomically for every semantic Validator exercised."""
        from validibot.validations.services.execution.deployments import (
            mark_execution_deployment_pair_accepted,
        )

        accepted_ids: list[dict[str, str]] = []
        try:
            pairs = []
            for scenario in scenarios:
                validator = scenario.workflow.steps.get().validator
                _, service, job = self._accepted_routes(
                    self.spec,
                    self.release_version,
                    validator=validator,
                )
                pairs.append((service, job))
            with transaction.atomic():
                for service, job in pairs:
                    accepted = mark_execution_deployment_pair_accepted(
                        service=service,
                        job=job,
                    )
                    accepted_ids.append(
                        {
                            "validator_id": str(accepted.service.validator_id),
                            "service_deployment_id": str(accepted.service.pk),
                            "job_deployment_id": str(accepted.job.pk),
                        }
                    )
        except Exception as exc:
            report.add(
                f"VA-{self.backend.upper()}-ACCEPTED",
                "failed",
                "The successful canaries could not be attached to the exact pair.",
                error=_safe_error(exc),
            )
            return
        report.add(
            f"VA-{self.backend.upper()}-ACCEPTED",
            "passed",
            "The exact backend release pair has a durable acceptance time.",
            deployments=accepted_ids,
        )

    def _accepted_routes(
        self,
        spec: BackendSpec,
        expected_release: str,
        *,
        validator: Validator | None = None,
    ) -> tuple[Validator, ValidatorExecutionDeployment, ValidatorExecutionDeployment]:
        """Resolve and validate the two routes required for a safe canary."""
        if validator is None:
            config = get_config(spec.validation_type)
            if config is None:
                raise ValueError("validator config is not registered")
            validator = Validator.objects.get(
                slug=config.slug,
                version=config.version,
                is_system=True,
            )
        pair = resolve_backend_release_pair(
            validator=validator,
            backend_slug=spec.key,
            backend_release_identity=expected_release,
        )
        service = pair.service
        job = pair.job
        if self.routing_mode == ExecutionRoutingMode.NORMAL and (
            service.routing_role != ExecutionDeploymentRoutingRole.PRIMARY
            or job.routing_role != ExecutionDeploymentRoutingRole.LONG_RUNNING
        ):
            raise ValueError(
                "normal mode requires Service PRIMARY and Job LONG_RUNNING"
            )
        if self.routing_mode == ExecutionRoutingMode.JOB_ONLY and (
            service.routing_role != ExecutionDeploymentRoutingRole.INACTIVE
            or job.routing_role != ExecutionDeploymentRoutingRole.PRIMARY
        ):
            raise ValueError("job-only mode requires Service INACTIVE and Job PRIMARY")
        return validator, service, job

    def _check_storage(self, report: AcceptanceReport) -> None:
        """Require IAM denial proof and exercise the real downscoped token."""
        if not self.run_storage_probe:
            report.add(
                "VA-STORAGE-CAPABILITY",
                "skipped",
                "Live GCS capability probe was disabled for this invocation.",
            )
            return
        if not self.ambient_isolation_verified:
            report.add(
                "VA-STORAGE-CAPABILITY",
                "failed",
                "Ambient validator storage isolation was not verified by the "
                "operator recipe.",
            )
            return
        try:
            result = probe_attempt_gcs_runtime_capability(
                bucket_name=str(getattr(settings, "GCS_VALIDATION_BUCKET", "")),
                project_id=str(getattr(settings, "GCP_PROJECT_ID", "")),
            )
            report.add(
                "VA-STORAGE-CAPABILITY",
                "passed" if result.passed else "failed",
                (
                    "Attempt-scoped GCS operations matched the accepted boundary."
                    if result.passed
                    else "One or more attempt-scoped GCS operations were unsafe."
                ),
                checks=[check.as_dict() for check in result.checks],
                ambient_storage_access_verified=True,
            )
        except Exception as exc:
            report.add(
                "VA-STORAGE-CAPABILITY",
                "failed",
                "The live attempt-scoped GCS probe could not complete.",
                error=_safe_error(exc),
            )

    def _launch_backend(
        self,
        report: AcceptanceReport,
        scenario: AcceptanceScenario,
    ):
        """Launch one semantic-validator burst without serialising its attempts."""
        launched = []
        for sequence in range(1, self.attempts_per_backend + 1):
            try:
                run = self._launch(scenario, report.acceptance_id)
                launched.append((sequence, run))
            except Exception as exc:
                report.add(
                    _scenario_check_id(scenario, f"SMOKE-{sequence:02d}"),
                    "failed",
                    "The acceptance run could not be launched.",
                    error=_safe_error(exc),
                )
        return launched

    def _wait_for_runs(self, launched_by_backend) -> None:
        """Wait once for every concurrently launched semantic-validator burst."""
        deadline = time.monotonic() + self.timeout_seconds
        pending = {
            str(run.pk): run
            for launched in launched_by_backend.values()
            for _sequence, run in launched
        }
        while pending and time.monotonic() < deadline:
            for run_id, run in list(pending.items()):
                run.refresh_from_db()
                if run.status in VALIDATION_RUN_TERMINAL_STATUSES:
                    pending.pop(run_id)
            if pending:
                time.sleep(self.poll_interval_seconds)

    def _record_latency(
        self,
        report: AcceptanceReport,
        scenario: AcceptanceScenario,
        launched,
    ) -> None:
        """Retain timing observations for one immutable release smoke burst.

        Correctness, complete timing evidence, and immutable revision identity
        remain release gates. Provider-start and provider-total latency are
        retained as individual observations, but a three-attempt release smoke
        does not claim statistically meaningful percentiles or compare timings
        with a universal startup target. The measured provider-start interval
        can include provider queueing, Cloud Run provisioning, container
        startup, and backend setup, so it is not a portable cold-start SLO.
        """
        provider_start_samples = []
        provider_total_samples = []
        timing_samples = []
        revisions = set()
        minimum_instances = set()
        service_minimum_instances = set()
        for sequence, run in launched:
            timing_sample: dict[str, int | str | float | None] = {
                "sequence": sequence,
                "validation_run_id": str(run.pk),
                "provider_start_seconds": None,
                "provider_total_seconds": None,
            }
            attempt = (
                ExecutionAttempt.objects.filter(step_run__validation_run=run)
                .select_related("deployment")
                .order_by("attempt_number")
                .last()
            )
            if attempt is None or attempt.deployment is None:
                timing_samples.append(timing_sample)
                continue
            revisions.add(attempt.deployment.deployment_revision)
            observed_minimum = attempt.deployment.minimum_instances
            minimum_instances.add(observed_minimum)
            if getattr(attempt.deployment, "deployment_kind", "") in {
                ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
                "",
            }:
                service_minimum_instances.add(observed_minimum)
            if (
                attempt.provider_accepted_at
                and attempt.provider_started_at
                and attempt.provider_started_at >= attempt.provider_accepted_at
            ):
                provider_start_seconds = round(
                    (
                        attempt.provider_started_at - attempt.provider_accepted_at
                    ).total_seconds(),
                    3,
                )
                provider_start_samples.append(provider_start_seconds)
                timing_sample["provider_start_seconds"] = provider_start_seconds
            if (
                attempt.provider_accepted_at
                and attempt.callback_received_at
                and attempt.callback_received_at >= attempt.provider_accepted_at
            ):
                provider_total_seconds = round(
                    (
                        attempt.callback_received_at - attempt.provider_accepted_at
                    ).total_seconds(),
                    3,
                )
                provider_total_samples.append(provider_total_seconds)
                timing_sample["provider_total_seconds"] = provider_total_seconds
            timing_samples.append(timing_sample)

        details = {
            "samples": len(provider_start_samples),
            "required_samples": self.attempts_per_backend,
            "timing_samples": timing_samples,
            "deployment_revisions": sorted(revisions),
            "minimum_instances": sorted(minimum_instances),
            "service_minimum_instances": sorted(service_minimum_instances),
            "latency_policy": ("release_smoke_observation_no_percentiles_or_threshold"),
            "provider_start_measurement": (
                "provider_accepted_at_to_provider_started_at"
            ),
            "provider_total_measurement": (
                "provider_accepted_at_to_callback_received_at"
            ),
        }
        failure = ""
        if len(provider_start_samples) != self.attempts_per_backend:
            failure = "one or more provider-start samples is missing"
        elif len(provider_total_samples) != self.attempts_per_backend:
            failure = "one or more provider-total samples is missing"
        elif len(revisions) != 1:
            failure = "the burst did not use exactly one immutable revision"
        elif service_minimum_instances and service_minimum_instances != {0}:
            failure = "validator Service minimum instances must remain zero"
        else:
            details.update(
                {
                    "provider_start_summary_seconds": _timing_summary(
                        provider_start_samples
                    ),
                    "provider_total_summary_seconds": _timing_summary(
                        provider_total_samples
                    ),
                }
            )
        check_id = _scenario_check_id(scenario, "LATENCY")
        if failure:
            report.add(
                check_id,
                "failed",
                (
                    "The exact acceptance burst did not produce complete "
                    "timing observations."
                ),
                error=failure,
                **details,
            )
        else:
            report.add(
                check_id,
                "passed",
                (
                    "The exact acceptance burst produced complete timing "
                    "observations; this release smoke does not claim "
                    "performance percentiles or use a universal threshold."
                ),
                **details,
            )

    def _launch(self, scenario: AcceptanceScenario, acceptance_id: str):
        """Create a fresh submission and use the normal application launcher."""
        submission = Submission(
            name=f"{acceptance_id}: {scenario.backend.key}",
            org=scenario.workflow.org,
            project=scenario.workflow.project,
            user=scenario.workflow.user,
            workflow=scenario.workflow,
            metadata={
                "validator_acceptance_id": acceptance_id,
                "fixture_sha256": scenario.fixture_sha256,
            },
        )
        if isinstance(scenario.inline_text, bytes):
            submission.set_content(
                uploaded_file=ContentFile(
                    scenario.inline_text,
                    name=scenario.filename,
                ),
                filename=scenario.filename,
                file_type=scenario.file_type,
            )
        else:
            submission.set_content(
                inline_text=scenario.inline_text,
                filename=scenario.filename,
                file_type=scenario.file_type,
            )
        submission.save()
        request = HttpRequest()
        request.method = "POST"
        request.user = scenario.workflow.user
        response = ValidationRunService().launch(
            request=request,
            org=scenario.workflow.org,
            workflow=scenario.workflow,
            submission=submission,
            user_id=scenario.workflow.user_id,
            metadata={"validator_acceptance_id": acceptance_id},
            source=ValidationRunSource.SCHEDULE,
        )
        return response.validation_run

    def _record_run(
        self,
        report: AcceptanceReport,
        scenario: AcceptanceScenario,
        sequence: int,
        run,
    ) -> None:
        """Verify the terminal run used the exact release and selected route."""
        check_id = _scenario_check_id(scenario, f"SMOKE-{sequence:02d}")
        attempt = (
            ExecutionAttempt.objects.filter(step_run__validation_run=run)
            .select_related("deployment")
            .order_by("attempt_number")
            .last()
        )
        details: dict[str, Any] = {
            "run_id": str(run.pk),
            "run_status": run.status,
            "fixture_sha256": scenario.fixture_sha256,
        }
        failure = ""
        if run.status not in VALIDATION_RUN_TERMINAL_STATUSES:
            failure = f"run did not finish within {self.timeout_seconds} seconds"
        elif run.status != ValidationRunStatus.SUCCEEDED:
            failure = f"run finished as {run.status}"
        elif attempt is None:
            failure = "run has no durable execution attempt"
        else:
            deployment = attempt.deployment
            deployment_snapshot = (
                attempt.deployment_snapshot
                if isinstance(attempt.deployment_snapshot, dict)
                else {}
            )
            details.update(
                {
                    "attempt_id": str(attempt.pk),
                    "attempt_state": attempt.state,
                    "deployment_id": str(attempt.deployment_id or ""),
                    "deployment_kind": (
                        deployment.deployment_kind if deployment is not None else ""
                    ),
                    "deployment_revision": (
                        deployment.deployment_revision if deployment is not None else ""
                    ),
                    "backend_image_digest": attempt.backend_image_digest,
                    "deployment_snapshot_revision": deployment_snapshot.get(
                        "deployment_revision",
                        "",
                    ),
                    "input_envelope_sha256": attempt.input_envelope_sha256,
                    "input_evidence_item_count": len(
                        attempt.input_evidence_snapshot.get("files", [])
                        if isinstance(attempt.input_evidence_snapshot, dict)
                        else []
                    ),
                    "output_envelope_sha256": attempt.output_envelope_sha256,
                    "provider_accepted_at": _iso(attempt.provider_accepted_at),
                    "provider_started_at": _iso(attempt.provider_started_at),
                    "provider_finished_at": _iso(attempt.provider_finished_at),
                    "callback_received_at": _iso(attempt.callback_received_at),
                }
            )
            if deployment is None:
                failure = "attempt has no pinned managed deployment"
            elif attempt.state != ExecutionAttemptState.COMPLETED:
                failure = f"attempt finished in unexpected state {attempt.state}"
            elif deployment.deployment_kind != self._expected_deployment_kind:
                failure = (
                    "attempt did not use the expected "
                    f"{self._expected_deployment_kind} route"
                )
            elif deployment.backend_release_identity != self.release_version:
                failure = "attempt used a different backend release"
            elif deployment_snapshot.get("deployment_id") != str(deployment.pk):
                failure = "attempt snapshot names a different deployment"
            elif (
                deployment_snapshot.get("deployment_revision")
                != deployment.deployment_revision
            ):
                failure = "attempt snapshot names a different Service revision"
            elif (
                deployment_snapshot.get("backend_image_digest")
                != deployment.backend_image_digest
            ):
                failure = "attempt snapshot names a different backend image"
            elif attempt.backend_image_digest != deployment.backend_image_digest:
                failure = "attempt observed a different backend image"
            elif not attempt.input_envelope_sha256:
                failure = "attempt did not retain an input-envelope digest"
            elif not attempt.input_evidence_snapshot:
                failure = "attempt did not retain immutable input evidence"
            elif not attempt.output_envelope_sha256:
                failure = "attempt did not retain an output-envelope digest"
            elif attempt.provider_accepted_at is None:
                failure = "provider acceptance time was not recorded"
            elif attempt.provider_started_at is None:
                failure = "provider start time was not recorded"
            elif attempt.provider_finished_at is None:
                failure = "provider finish time was not recorded"
            elif attempt.callback_received_at is None:
                failure = "authenticated callback time was not recorded"

        if failure:
            details["error"] = failure
            if run.error:
                details["run_error"] = str(run.error)[:500]
            report.add(
                check_id,
                "failed",
                "End-to-end validator canary failed.",
                **details,
            )
        else:
            report.add(
                check_id,
                "passed",
                "End-to-end validator canary completed on the candidate route.",
                **details,
            )

    @property
    def _expected_deployment_kind(self) -> str:
        """Map acceptance routing mode to the provider kind a run must snapshot."""
        if self.routing_mode == ExecutionRoutingMode.JOB_ONLY:
            return ExecutionDeploymentKind.CLOUD_RUN_JOB
        return ExecutionDeploymentKind.CLOUD_RUN_SERVICE


def persist_acceptance_report(report: dict[str, Any]) -> dict[str, str] | None:
    """Create one immutable private GCS report and return its identity."""
    bucket_name = str(getattr(settings, "GCS_VALIDATION_BUCKET", "") or "")
    project_id = str(getattr(settings, "GCP_PROJECT_ID", "") or "")
    if not bucket_name or not project_id:
        return None
    acceptance_id = str(report["acceptance_id"])
    canonical = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    date = timezone.now().date().isoformat()
    object_name = f"operations/validator-acceptance/{date}/{acceptance_id}.json"
    client = storage.Client(project=project_id)
    blob = client.bucket(bucket_name).blob(object_name)
    blob.upload_from_string(
        canonical,
        content_type="application/json",
        if_generation_match=0,
    )
    return {
        "uri": f"gs://{bucket_name}/{object_name}",
        "sha256": digest,
    }


def _sha256_content(value: str | bytes) -> str:
    """Return the fixture identity used in reports and submission metadata."""
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _scenario_key(scenario: AcceptanceScenario) -> str:
    """Return a stable key for concurrent semantic-validator bursts."""
    if scenario.validator_id:
        return scenario.validator_id
    return (
        f"{scenario.backend.key}:{scenario.validator_slug}:{scenario.validator_version}"
    )


def _scenario_check_id(scenario: AcceptanceScenario, suffix: str) -> str:
    """Keep report check identifiers unique across semantic Validator versions."""
    semantic = ""
    if scenario.validator_slug and scenario.validator_version:
        safe_slug = re.sub(r"[^A-Z0-9]+", "-", scenario.validator_slug.upper()).strip(
            "-"
        )
        semantic = f"-{safe_slug}-V{scenario.validator_version}"
    return f"VA-{scenario.backend.key.upper()}{semantic}-{suffix}"


def _timing_summary(values: list[float]) -> dict[str, float]:
    """Summarize a small smoke burst without presenting it as a percentile."""
    return {
        "minimum": min(values),
        "median": round(median(values), 3),
        "maximum": max(values),
    }


def _iso(value) -> str | None:
    """Render optional datetimes consistently in JSON details."""
    return value.isoformat() if value is not None else None


def _safe_error(exc: Exception) -> str:
    """Bound diagnostic text so reports cannot become log or secret dumps."""
    return f"{type(exc).__name__}: {str(exc)[:400]}"
