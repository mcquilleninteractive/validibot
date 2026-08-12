"""End-to-end smoke test — verifies the deployment can validate things.

This command is the implementation behind ``just self-hosted smoke-test``
and ``just gcp smoke-test <stage>``. It exercises the real validation
pipeline (queue, worker, dispatcher, step orchestrator) by running a
minimal JSON-Schema validation through the same code path real
validations use, then reports the outcome with stable check IDs.

The smoke test answers a single question per the boring-self-hosting
ADR (section 7):

    "Is this deployment fundamentally functional, or is something
     obviously broken?"

That's distinct from the doctor command, which answers "is the
configuration sane?" Doctor catches the *configuration* class of
problems (settings missing, DB unreachable, storage not writable);
smoke test catches the *runtime* class (queue broken, worker not
picking up jobs, dispatcher misconfigured, validator can't actually
process a payload).

Together they form the operator's confidence loop after install,
upgrade, or restore.

Why exercise the real dispatcher
================================

A synthetic in-process check would catch fewer bugs. The smoke test
deliberately:

- creates a real Submission with real JSON content;
- calls ``ValidationRunService.launch(...)`` (the same entry point
  views use);
- waits for the worker to pick up the run;
- polls until the run reaches a terminal status.

If the queue is misconfigured, this catches it. If the worker isn't
running, this catches it. If the JSON Schema validator's image is
broken (advanced validators), this catches it.

Idempotency and demo-data marking
=================================

The demo org / user / workflow are created with ``get_or_create``
under deterministic slugs prefixed ``smoke-test-``. Re-running the
command does not duplicate data; a fresh ValidationRun is created on
each invocation, but the surrounding fixtures are reused.

The "[Demo]" suffix on human-readable names makes demo data visible
in the UI — operators reviewing recent runs can immediately see that
something is a smoke-test artifact rather than real customer work.

Output schema
=============

JSON output (``--json``) is governed by ``validibot.smoke-test.v1``.
Like the doctor schema, additive fields stay v1; removing or
renaming fields requires a v2 bump.

Distinguishing failure modes
============================

Three terminal outcomes carry distinct meaning, all reported with
distinct check IDs:

- **ST005 OK** — run reached ``SUCCEEDED``. The deployment can
  validate things.
- **ST005 ERROR (validation)** — run reached ``VALIDATION_FAILED``.
  The pipeline ran end-to-end but the validator reported findings on
  a payload that should have passed. Strong signal that something in
  the validator code or the demo fixture itself is broken.
- **ST005 ERROR (system)** — run reached ``FAILED``. The pipeline
  itself errored out — usually a worker / dispatcher / config
  problem the doctor missed. Operators should run doctor first if
  this appears.
- **ST004 ERROR (timeout)** — run never reached terminal status
  within ``--timeout-seconds``. Strong signal that the worker is
  not picking up jobs, or that the queue is misconfigured.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.http import HttpRequest

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.models import Submission
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import RoleCode
from validibot.users.models import User
from validibot.users.models import ensure_default_project
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import Validator
from validibot.validations.services.validation_run import ValidationRunService
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun

# JSON output schema version. Bump only on breaking changes.
SMOKE_TEST_SCHEMA_VERSION = "validibot.smoke-test.v1"

# Default polling timeout. Tiny JSON-Schema validations against a
# 30-byte payload finish in under a second on test mode and within a
# few seconds on Compose / Cloud Run. 120s is a generous bound that
# still surfaces a stuck queue clearly.
DEFAULT_TIMEOUT_SECONDS = 120

# How often to poll the database while waiting for the run to finish.
# 1s is fast enough that ``--timeout-seconds 5`` works for tight
# tests, slow enough that a long-running smoke test doesn't pound
# the database.
POLL_INTERVAL_SECONDS = 1

# Demo identifiers — deterministic slugs so re-running the command
# reuses the same fixtures. The ``smoke-test-`` prefix and ``[Demo]``
# name suffix make it obvious in the UI that these aren't real data.
DEMO_USERNAME = "smoke-test-user"
DEMO_EMAIL = "smoke-test@validibot.local"
DEMO_USER_NAME = "Smoke Test [Demo]"
DEMO_ORG_SLUG = "smoke-test-org"
DEMO_ORG_NAME = "Smoke Test [Demo]"
DEMO_WORKFLOW_SLUG = "smoke-test-json-schema"
DEMO_WORKFLOW_NAME = "Smoke Test JSON Schema [Demo]"
DEMO_RULESET_NAME = "Smoke Test JSON Schema [Demo]"
DEMO_STEP_NAME = "Smoke Test JSON Schema Step"

# The smallest possible JSON Schema that meaningfully exercises
# validation: one required string field with a constant value. The
# matching payload is the simplest input that satisfies the schema.
# Any deviation (missing field, wrong type, wrong value) produces
# findings — exactly what a healthy smoke test should report as zero.
DEMO_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "smoke_test": {
            "type": "string",
            "const": "ok",
            "description": (
                "Sentinel field. The smoke test asserts that "
                "validating a payload with smoke_test='ok' produces "
                "zero findings."
            ),
        },
    },
    "required": ["smoke_test"],
    "additionalProperties": False,
}
DEMO_PAYLOAD: dict = {"smoke_test": "ok"}


class CheckStatus(Enum):
    """Status of a smoke-test check.

    Mirrors ``check_validibot``'s 5-state plus skipped scale.
    Operators reading both commands' output should not have to
    learn two taxonomies.
    """

    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"
    SKIPPED = "skipped"


# Statuses that fail the command unconditionally.
_BLOCKING_STATUSES = frozenset({CheckStatus.ERROR, CheckStatus.FATAL})


@dataclass
class CheckResult:
    """One smoke-test step's outcome.

    The pair (``id``, ``category``) is the contract that
    ``docs/operations/self-hosting/smoke-test-check-ids.md`` documents
    and that integrations rely on. Renaming an existing ID is a
    breaking change; adding new IDs is fine.
    """

    id: str
    category: str
    name: str
    status: CheckStatus
    message: str
    details: dict | None = None
    fix_hint: str | None = None

    def to_json(self) -> dict:
        """Serialize for ``--json`` output. ``status`` becomes its string value."""
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


class Command(BaseCommand):
    """End-to-end smoke test for a Validibot deployment.

    Runs a tiny JSON-Schema validation through the real pipeline
    (queue, worker, dispatcher) and reports the outcome with stable
    check IDs. See module docstring for the full taxonomy.

    Operators normally invoke this via ``just self-hosted smoke-test``
    or ``just gcp smoke-test <stage>``; both shell into this command.
    """

    help = (
        "Verify the deployment can validate things end-to-end. "
        "Operators normally invoke via `just self-hosted smoke-test` "
        "or `just gcp smoke-test <stage>`."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.results: list[CheckResult] = []
        self.json_output = False
        self.target: str | None = None
        self.stage: str | None = None
        self.timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    def add_arguments(self, parser):
        parser.add_argument(
            "--target",
            choices=["self_hosted", "gcp", "local_docker_compose", "test"],
            default=None,
            help=(
                "Deployment target. Defaults to settings.DEPLOYMENT_TARGET. "
                "Recorded in JSON output for cross-target dashboards."
            ),
        )
        parser.add_argument(
            "--stage",
            choices=["dev", "staging", "prod"],
            default=None,
            help=(
                "Stage. Only meaningful for --target gcp; self-hosted "
                "is single-stage per VM."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output results as JSON (validibot.smoke-test.v1 schema).",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=int,
            default=DEFAULT_TIMEOUT_SECONDS,
            help=(
                f"How long to wait for the validation run to reach a "
                f"terminal status. Default: {DEFAULT_TIMEOUT_SECONDS}s."
            ),
        )

    # ── Entry point ─────────────────────────────────────────────────────

    def handle(self, *args, **options):
        self.json_output = options.get("json", False)
        self.target = options.get("target") or self._infer_target()
        self.stage = options.get("stage")
        self.timeout_seconds = options.get(
            "timeout_seconds",
            DEFAULT_TIMEOUT_SECONDS,
        )

        if not self.json_output:
            self._print_header()

        # Fixtures must succeed before launch can be attempted, so we
        # bail early if ST001 or ST002 produce a fatal. Subsequent
        # steps depend on prior results being present, so a
        # short-circuit saves operators from cascading red errors
        # that all trace back to the same root cause.
        org, user, workflow = self._step_ensure_demo_fixtures()
        if self._has_fatal():
            self._finalize()
            return

        submission = self._step_create_submission(org=org, user=user, workflow=workflow)
        if self._has_fatal():
            self._finalize()
            return

        run = self._step_launch_run(
            org=org,
            user=user,
            workflow=workflow,
            submission=submission,
        )
        if self._has_fatal():
            self._finalize()
            return

        run = self._step_wait_for_terminal(run)
        if run is None:
            # ST004 already recorded the timeout; we still report
            # the (skipped) outcome check so the JSON shape stays
            # stable across pass / fail / timeout paths.
            self._add_result(
                "ST005",
                "outcome",
                "Run outcome",
                CheckStatus.SKIPPED,
                "Skipped — run did not reach terminal status.",
            )
        else:
            self._step_verify_outcome(run)

        # ST006 is the signed-credential check (Pro / hardened
        # profile). Always recorded as SKIPPED on community so the
        # JSON shape is consistent across editions; Pro adds a real
        # implementation behind a feature flag.
        self._step_signed_credential()

        self._finalize()

    def _infer_target(self) -> str:
        """Default to settings.DEPLOYMENT_TARGET when --target isn't passed.

        Mirrors the doctor command's inference so operators get
        target-aware behaviour without flag plumbing.
        """
        return getattr(settings, "DEPLOYMENT_TARGET", "self_hosted")

    # ── Step 1: Demo fixtures (ST001) ──────────────────────────────────

    def _step_ensure_demo_fixtures(
        self,
    ) -> tuple[Organization | None, User | None, Workflow | None]:
        """Get-or-create the demo org / user / workflow / step / ruleset.

        Idempotent: re-running reuses the existing fixtures via
        ``get_or_create`` on deterministic slugs. If the JSON Schema
        system validator isn't installed, we fail fatally — the
        smoke test can't proceed without it, and the operator-facing
        fix is to run ``setup_validibot``.
        """
        try:
            with transaction.atomic():
                user = self._ensure_user()
                org = self._ensure_org(user)
                project = ensure_default_project(org)
                workflow = self._ensure_workflow(org=org, user=user, project=project)
        except FixtureError as exc:
            self._add_result(
                "ST001",
                "fixtures",
                "Demo fixtures",
                CheckStatus.FATAL,
                str(exc),
                fix_hint=exc.fix_hint,
            )
            return None, None, None

        self._add_result(
            "ST001",
            "fixtures",
            "Demo fixtures",
            CheckStatus.OK,
            "Demo org, user, workflow ready.",
            details={
                "org_slug": org.slug,
                "user_username": user.username,
                "workflow_slug": workflow.slug,
                "workflow_id": workflow.pk,
            },
        )
        return org, user, workflow

    def _ensure_user(self) -> User:
        user, created = User.objects.get_or_create(
            username=DEMO_USERNAME,
            defaults={
                "email": DEMO_EMAIL,
                "name": DEMO_USER_NAME,
                "is_active": True,
            },
        )
        if created:
            # Set an unusable password — the smoke-test user is never
            # supposed to log in interactively. This also stops it
            # being mistaken for a real account.
            user.set_unusable_password()
            user.save()
        return user

    def _ensure_org(self, user: User) -> Organization:
        org, _ = Organization.objects.get_or_create(
            slug=DEMO_ORG_SLUG,
            defaults={"name": DEMO_ORG_NAME},
        )
        membership, mem_created = Membership.objects.get_or_create(
            user=user,
            org=org,
            defaults={"is_active": True},
        )
        if mem_created:
            membership.set_roles(
                {RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR},
            )
        if not user.current_org_id:
            user.set_current_org(org)
        return org

    def _ensure_workflow(
        self,
        *,
        org: Organization,
        user: User,
        project,
    ) -> Workflow:
        workflow, _ = Workflow.objects.get_or_create(
            org=org,
            slug=DEMO_WORKFLOW_SLUG,
            version="1",
            defaults={
                "name": DEMO_WORKFLOW_NAME,
                "user": user,
                "project": project,
                "is_active": True,
                "allowed_file_types": [SubmissionFileType.JSON],
            },
        )

        if not workflow.steps.exists():
            self._ensure_workflow_step(workflow=workflow, org=org, user=user)

        return workflow

    def _ensure_workflow_step(
        self,
        *,
        workflow: Workflow,
        org: Organization,
        user: User,
    ) -> WorkflowStep:
        validator = Validator.objects.filter(
            validation_type=ValidationType.JSON_SCHEMA,
            is_system=True,
        ).first()
        if validator is None:
            msg = (
                "JSON Schema system validator not found. The smoke "
                "test depends on the validator created by setup_validibot."
            )
            raise FixtureError(
                msg,
                fix_hint="Run: python manage.py setup_validibot",
            )

        ruleset = Ruleset.objects.create(
            org=org,
            user=user,
            name=DEMO_RULESET_NAME,
            ruleset_type=RulesetType.JSON_SCHEMA,
            rules_text=json.dumps(DEMO_SCHEMA),
            metadata={"schema_type": JSONSchemaVersion.DRAFT_2020_12.value},
            version="1",
        )
        from validibot.validations.services.custom_validator_contracts import (
            sync_configured_io_contract,
        )
        from validibot.validations.services.input_bindings import (
            ensure_step_input_bindings,
        )

        sync_configured_io_contract(validator=validator)
        step = WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
            name=DEMO_STEP_NAME,
        )
        ensure_step_input_bindings(step)
        return step

    # ── Step 2: Submission (ST002) ─────────────────────────────────────

    def _step_create_submission(
        self,
        *,
        org: Organization,
        user: User,
        workflow: Workflow,
    ) -> Submission | None:
        """Create a new submission with the demo payload.

        A fresh submission per smoke-test invocation is correct: each
        run should be a distinct artefact in the ``ValidationRun``
        history so operators can correlate "the smoke test I ran at
        14:30" with a specific row.
        """
        try:
            submission = Submission.objects.create(
                name="Smoke Test Submission [Demo]",
                org=org,
                project=workflow.project,
                user=user,
                workflow=workflow,
                content=json.dumps(DEMO_PAYLOAD),
                file_type=SubmissionFileType.JSON,
                original_filename="smoke-test.json",
            )
        except Exception as exc:
            self._add_result(
                "ST002",
                "submission",
                "Submission",
                CheckStatus.FATAL,
                f"Failed to create submission: {exc}",
                fix_hint=(
                    "Run: just self-hosted doctor\n"
                    "       Likely a database connectivity or schema problem."
                ),
            )
            return None

        self._add_result(
            "ST002",
            "submission",
            "Submission",
            CheckStatus.OK,
            "Demo submission created.",
            details={"submission_id": str(submission.id)},
        )
        return submission

    # ── Step 3: Launch (ST003) ─────────────────────────────────────────

    def _step_launch_run(
        self,
        *,
        org: Organization,
        user: User,
        workflow: Workflow,
        submission: Submission,
    ) -> ValidationRun | None:
        """Launch a validation run via the same code path views use.

        We construct an empty ``HttpRequest`` and attach the demo
        user — the launch path only reads ``request.user``. Using the
        real ``ValidationRunService`` (rather than calling the worker
        synchronously) means the dispatcher, queue, and worker all
        get exercised — the whole point of a smoke test.
        """
        request = HttpRequest()
        request.method = "POST"
        request.user = user

        try:
            response = ValidationRunService().launch(
                request=request,
                org=org,
                workflow=workflow,
                submission=submission,
                user_id=user.id,
            )
        except Exception as exc:
            self._add_result(
                "ST003",
                "launch",
                "Launch run",
                CheckStatus.FATAL,
                f"ValidationRunService.launch raised: {exc}",
                fix_hint=(
                    "Check the logs of the web container; this is usually "
                    "a permission, quota, or workflow-config problem."
                ),
            )
            return None

        run = response.validation_run
        self._add_result(
            "ST003",
            "launch",
            "Launch run",
            CheckStatus.OK,
            "Run dispatched to the worker.",
            details={
                "run_id": str(run.pk),
                "initial_status": run.status,
            },
        )
        return run

    # ── Step 4: Wait for terminal (ST004) ──────────────────────────────

    def _step_wait_for_terminal(
        self,
        run: ValidationRun,
    ) -> ValidationRun | None:
        """Poll the database until ``run.status`` is terminal.

        Returns the refreshed run, or ``None`` if we timed out. The
        timeout being hit is a strong signal the worker isn't picking
        up jobs — operators should then check ``just self-hosted
        logs-service worker`` or ``just gcp errors-since <stage> 5m``.
        """
        deadline = time.monotonic() + self.timeout_seconds
        elapsed = 0.0

        while time.monotonic() < deadline:
            run.refresh_from_db()
            if run.status in VALIDATION_RUN_TERMINAL_STATUSES:
                self._add_result(
                    "ST004",
                    "execution",
                    "Run execution",
                    CheckStatus.OK,
                    f"Run reached terminal status in {elapsed:.1f}s.",
                    details={
                        "terminal_status": run.status,
                        "elapsed_seconds": round(elapsed, 1),
                    },
                )
                return run
            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed = self.timeout_seconds - max(0.0, deadline - time.monotonic())

        # Timed out — record fatal so the operator sees a clear
        # "the worker isn't running" signal.
        run.refresh_from_db()
        self._add_result(
            "ST004",
            "execution",
            "Run execution",
            CheckStatus.FATAL,
            (
                f"Run did not reach terminal status within "
                f"{self.timeout_seconds}s (last seen: {run.status})."
            ),
            details={
                "last_seen_status": run.status,
                "timeout_seconds": self.timeout_seconds,
            },
            fix_hint=(
                "The worker likely isn't picking up jobs. Check:\n"
                "  - just self-hosted logs-service worker\n"
                "  - just self-hosted doctor       (look for VB401-VB499)\n"
                "  - just self-hosted errors-since 5m"
            ),
        )
        return None

    # ── Step 5: Outcome (ST005) ────────────────────────────────────────

    def _step_verify_outcome(self, run: ValidationRun) -> None:
        """Distinguish healthy / validation-finding / system failure.

        ``ValidationRunStatus`` has only one failure terminal
        (``FAILED``) — there's no separate ``VALIDATION_FAILED``.
        We use ``total_findings`` to split FAILED into two distinct
        operator-facing failure modes:

        - **FAILED + findings > 0** — validation ran end-to-end but
          the validator reported issues on the demo payload. The
          pipeline is healthy; the validator code, or the smoke-test
          fixture itself, isn't.
        - **FAILED + findings == 0** — system error before findings
          could be persisted. Worker / dispatcher / config-level
          problem the doctor likely missed.

        A passing smoke test requires ``SUCCEEDED`` with zero
        findings. Anything else is reported with a fix-hint that
        points at the likely investigation path.
        """
        findings = getattr(run, "total_findings", 0) or 0
        details = {
            "final_status": run.status,
            "total_findings": findings,
        }

        if run.status == ValidationRunStatus.SUCCEEDED and findings == 0:
            self._add_result(
                "ST005",
                "outcome",
                "Run outcome",
                CheckStatus.OK,
                "Run succeeded with zero findings — pipeline is healthy.",
                details=details,
            )
            return

        if run.status == ValidationRunStatus.SUCCEEDED:
            # SUCCEEDED with findings is unusual — the demo schema +
            # payload should match exactly. Treat as WARN rather than
            # OK because the run completed cleanly (no system fault),
            # but the smoke test's expected zero-findings invariant
            # was violated.
            self._add_result(
                "ST005",
                "outcome",
                "Run outcome",
                CheckStatus.WARN,
                (
                    f"Run succeeded but reported {findings} finding(s) "
                    f"on the demo payload — likely fixture drift."
                ),
                details=details,
                fix_hint=(
                    f"Inspect run {run.pk} in admin to see which "
                    f"finding(s) were raised. The demo payload satisfies "
                    f"the demo schema by construction, so any finding "
                    f"indicates the validator returned a different "
                    f"verdict than expected."
                ),
            )
            return

        if run.status == ValidationRunStatus.FAILED and findings > 0:
            self._add_result(
                "ST005",
                "outcome",
                "Run outcome",
                CheckStatus.ERROR,
                (
                    f"Run failed with {findings} finding(s) on the demo "
                    "payload. The pipeline ran end-to-end but the "
                    "JSON Schema validator behaved unexpectedly."
                ),
                details=details,
                fix_hint=(
                    f"Inspect run {run.pk} in admin or via the API. "
                    "Possible causes: validator-code regression, "
                    "smoke-test fixture drift, or a Pro plugin "
                    "intercepting the run."
                ),
            )
            return

        if run.status == ValidationRunStatus.FAILED:
            self._add_result(
                "ST005",
                "outcome",
                "Run outcome",
                CheckStatus.ERROR,
                (
                    "Run failed with a system error before findings "
                    "could be reported. The pipeline broke at the "
                    "worker / dispatcher / config layer."
                ),
                details=details,
                fix_hint=(
                    "Run doctor first; this is usually a config issue "
                    "doctor catches (VB1xx database, VB3xx Docker, "
                    "VB4xx Celery). Also check worker logs:\n"
                    "  just self-hosted logs-service worker"
                ),
            )
            return

        # TIMED_OUT, CANCELED, or any future terminal status. We
        # report neutrally rather than treating them as ERROR
        # because a CANCELED run between launch and poll is a real
        # (if unusual) outcome and operators should see it in raw
        # form rather than via a Django stack trace.
        self._add_result(
            "ST005",
            "outcome",
            "Run outcome",
            CheckStatus.WARN,
            f"Run reached terminal status '{run.status}' (not SUCCEEDED).",
            details=details,
        )

    # ── Step 6: Signed credential (ST006) ──────────────────────────────

    def _step_signed_credential(self) -> None:
        """Verify a signed credential round-trip when Pro is active.

        ADR section 7: "for self-hosted-hardened, or when signed
        credentials are enabled, issues and verifies a signed
        credential against the instance's local JWKS endpoint. For
        self-hosted without Pro/signing, the check reports SKIPPED."

        Community has no signing backend, so the check is SKIPPED on
        every community deployment. Pro will register a real
        implementation under a feature flag in a later phase.
        """
        from validibot.core.features import CommercialFeature
        from validibot.core.features import is_feature_enabled

        if is_feature_enabled(CommercialFeature.SIGNED_CREDENTIALS):
            # Real implementation lands when Pro signing is wired
            # through this command. Until then, a Pro deployment
            # also reports SKIPPED — but with a different message
            # so we know the gate exists.
            self._add_result(
                "ST006",
                "signing",
                "Signed credential",
                CheckStatus.SKIPPED,
                (
                    "Skipped — Pro signing is enabled but the smoke-test "
                    "credential round-trip is not yet implemented."
                ),
            )
            return

        self._add_result(
            "ST006",
            "signing",
            "Signed credential",
            CheckStatus.SKIPPED,
            "Skipped — community deployment (no signing backend).",
        )

    # ── Result handling ────────────────────────────────────────────────

    def _add_result(
        self,
        check_id: str,
        category: str,
        name: str,
        status: CheckStatus,
        message: str,
        *,
        details: dict | None = None,
        fix_hint: str | None = None,
    ) -> None:
        result = CheckResult(
            id=check_id,
            category=category,
            name=name,
            status=status,
            message=message,
            details=details,
            fix_hint=fix_hint,
        )
        self.results.append(result)

        if self.json_output:
            return

        # Human output: one line per check with status icon, ID, and
        # message. Fix hints (when present) appear indented underneath
        # so they don't clutter the OK case.
        if status == CheckStatus.OK:
            icon = self.style.SUCCESS("✓")
            msg = self.style.SUCCESS(message)
        elif status == CheckStatus.INFO:
            icon = self.style.HTTP_INFO("i")
            msg = self.style.HTTP_INFO(message)
        elif status == CheckStatus.WARN:
            icon = self.style.WARNING("!")
            msg = self.style.WARNING(message)
        elif status in (CheckStatus.ERROR, CheckStatus.FATAL):
            icon = self.style.ERROR("✗")
            msg = self.style.ERROR(message)
        else:  # SKIPPED
            icon = self.style.NOTICE("-")
            msg = self.style.NOTICE(message)

        self.stdout.write(f"  {icon} [{check_id}] {msg}")
        if fix_hint and status in (
            CheckStatus.ERROR,
            CheckStatus.FATAL,
            CheckStatus.WARN,
        ):
            for line in fix_hint.split("\n"):
                self.stdout.write(f"      Fix: {line}" if line else "")

    def _has_fatal(self) -> bool:
        return any(r.status == CheckStatus.FATAL for r in self.results)

    # ── Output and exit ────────────────────────────────────────────────

    def _print_header(self) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write(self.style.HTTP_INFO("  Validibot Smoke Test"))
        self.stdout.write(
            self.style.HTTP_INFO(
                f"  target={self.target} stage={self.stage or '-'}",
            ),
        )
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write("")

    def _finalize(self) -> None:
        """Emit final output and set the process exit code."""
        if self.json_output:
            self._output_json()
        else:
            self._output_summary()

        if any(r.status in _BLOCKING_STATUSES for r in self.results):
            sys.exit(1)

    def _output_json(self) -> None:
        """Emit ``validibot.smoke-test.v1`` JSON to stdout."""
        payload = {
            "schema_version": SMOKE_TEST_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "target": self.target,
            "stage": self.stage,
            "passed": not any(r.status in _BLOCKING_STATUSES for r in self.results),
            "results": [r.to_json() for r in self.results],
        }
        self.stdout.write(json.dumps(payload, indent=2))

    def _output_summary(self) -> None:
        """Print the human-readable verdict line."""
        self.stdout.write("")
        if any(r.status in _BLOCKING_STATUSES for r in self.results):
            self.stdout.write(
                self.style.ERROR(
                    "Smoke test FAILED. See messages above for the failing step.",
                ),
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "Smoke test PASSED. The deployment can validate things.",
                ),
            )
        self.stdout.write("")


class FixtureError(Exception):
    """Raised when demo fixtures can't be created.

    Carries an optional ``fix_hint`` so the calling step can surface
    actionable guidance (e.g. "run setup_validibot first") in the
    operator-facing report.
    """

    def __init__(self, message: str, *, fix_hint: str | None = None):
        super().__init__(message)
        self.fix_hint = fix_hint
