"""Tests for the one-command managed-validator acceptance feature.

The suite protects the operator-facing simplicity as well as the safety
contract behind it: reports must fail closed, live route checks must pin the
requested release, measurements must not blend revisions, production assets
must actually ship, and the GCP recipe must always restore maintenance mode.
Provider-specific HTTP and immutable-I/O behavior remains covered by its
lower-level conformance suites; these tests cover their acceptance orchestration.
"""

from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.acceptance import BACKENDS
from validibot.validations.acceptance import ROUTINE_ACCEPTANCE_ATTEMPTS
from validibot.validations.acceptance import AcceptanceFixtureBuilder
from validibot.validations.acceptance import AcceptanceReport
from validibot.validations.acceptance import AcceptanceScenario
from validibot.validations.acceptance import ValidatorAcceptanceRunner
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import Ruleset
from validibot.validations.models import Validator
from validibot.validations.services.execution.deployments import (
    ExecutionDeploymentResolutionError,
)

EXPECTED_START_TIMING_SUMMARY = {
    "minimum": 2.0,
    "median": 3.0,
    "maximum": 4.0,
}
EXPECTED_SCALE_TO_ZERO_START_MEDIAN = 30.0
EXPECTED_ENERGYPLUS_U_FACTOR = 2.0
EXPECTED_FMU_INPUT = 42.0
EXPECTED_PORTFOLIO_MANAGER_PROPERTY_ID = "9876543"
EXPECTED_MANAGED_BACKEND_COUNT = 6
COMPATIBLE_SEMANTIC_VALIDATOR_COUNT = 2
SHA256_HEX_LENGTH = 64
ACCEPTANCE_BACKEND = "shacl"
ACCEPTANCE_RELEASE_TAG = "shacl-v1.2.3"


@override_settings(VALIDIBOT_STAGE="staging")
def test_report_is_machine_readable_and_fails_closed():
    """One failed check must make the retained top-level verdict false."""
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )
    report.add("VA-OK", "passed", "A prerequisite passed.")
    report.add("VA-NO", "failed", "A canary failed.", error="bounded")
    report.finish()

    document = report.as_dict()

    assert document["schema_version"] == "validibot.validator-acceptance.v2"
    assert document["stage"] == "staging"
    assert document["backend"] == ACCEPTANCE_BACKEND
    assert document["source_release_tag"] == ACCEPTANCE_RELEASE_TAG
    assert document["backend_release"] == "1.2.3"
    assert document["passed"] is False
    assert [check["id"] for check in document["checks"]] == ["VA-OK", "VA-NO"]
    json.dumps(document)


def test_runner_defaults_to_small_concurrent_release_smoke():
    """Routine certification must test concurrency without becoming load testing."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
    )

    assert runner.attempts_per_backend == ROUTINE_ACCEPTANCE_ATTEMPTS


@pytest.mark.parametrize("release_tag", ["1.2.3", "latest", "v1.2"])
def test_runner_rejects_mutable_or_malformed_release_identity(release_tag):
    """Acceptance evidence is meaningless unless it names an immutable release."""
    with pytest.raises(ValueError, match=r"vX\.Y\.Z"):
        ValidatorAcceptanceRunner(
            backend=ACCEPTANCE_BACKEND,
            release_tag=release_tag,
        )


@pytest.mark.parametrize("attempts", [0, 21])
def test_runner_bounds_operator_requested_burst_size(attempts):
    """A typo must not create an unbounded provider load or a zero-run pass."""
    with pytest.raises(ValueError, match="between 1 and 20"):
        ValidatorAcceptanceRunner(
            backend=ACCEPTANCE_BACKEND,
            release_tag=ACCEPTANCE_RELEASE_TAG,
            attempts_per_backend=attempts,
        )


def test_acceptance_can_only_be_recorded_after_the_job_only_pass():
    """Service canaries alone must not certify the retained fallback route."""
    with pytest.raises(ValueError, match="successful job-only pass"):
        ValidatorAcceptanceRunner(
            backend=ACCEPTANCE_BACKEND,
            release_tag=ACCEPTANCE_RELEASE_TAG,
            routing_mode="normal",
            record_acceptance=True,
        )


def test_preflight_failure_stops_before_any_canary_is_created():
    """A drifted route must fail without adding workload to an unsafe release."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        run_storage_probe=False,
    )
    with (
        patch.object(runner, "_check_deployments", return_value=False),
        patch.object(runner, "_check_storage") as storage_check,
        patch(
            "validibot.validations.acceptance.AcceptanceFixtureBuilder"
        ) as fixture_builder,
    ):
        report = runner.run()

    storage_check.assert_called_once()
    fixture_builder.assert_not_called()
    assert report.passed is False
    assert report.checks[-1].check_id == "VA-SMOKE-ABORTED"


def test_storage_gate_requires_operator_iam_proof():
    """A token probe alone must not conceal unverified ambient runtime IAM."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
    )
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )

    with patch(
        "validibot.validations.acceptance.probe_attempt_gcs_runtime_capability"
    ) as provider_probe:
        runner._check_storage(report)

    provider_probe.assert_not_called()
    assert report.checks[-1].status == "failed"
    assert "not verified" in report.checks[-1].summary


@override_settings(
    GCS_VALIDATION_BUCKET="private-bucket",
    GCP_PROJECT_ID="validibot-test",
)
def test_operator_iam_proof_allows_storage_acceptance():
    """The offline recipe's Policy Troubleshooter proof unlocks the live probe."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        ambient_isolation_verified=True,
    )
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )
    provider_result = SimpleNamespace(passed=True, checks=[])

    with patch(
        "validibot.validations.acceptance.probe_attempt_gcs_runtime_capability",
        return_value=provider_result,
    ):
        runner._check_storage(report)

    assert report.checks[-1].status == "passed"
    assert report.checks[-1].details["ambient_storage_access_verified"] is True


def test_route_preflight_accepts_only_requested_service_and_ready_job():
    """A green route check must prove both candidate identity and rollback path."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
    )
    validator = SimpleNamespace(pk="validator-1")
    service = SimpleNamespace(
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        readiness_state=ExecutionDeploymentReadiness.READY,
        emergency_blocked=False,
        last_verification_succeeded=True,
        backend_release_identity="1.2.3",
        backend_image_digest="sha256:" + "a" * 64,
    )
    job = SimpleNamespace(
        routing_role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
        readiness_state=ExecutionDeploymentReadiness.READY,
    )
    config = SimpleNamespace(slug="shacl", version="1")
    pair = SimpleNamespace(service=service, job=job)

    with (
        patch("validibot.validations.acceptance.get_config", return_value=config),
        patch(
            "validibot.validations.acceptance.Validator.objects.get",
            return_value=validator,
        ),
        patch(
            "validibot.validations.acceptance.resolve_backend_release_pair",
            return_value=pair,
        ) as resolve_pair,
    ):
        resolved = runner._accepted_routes(BACKENDS[2], "1.2.3")

    assert resolved == (validator, service, job)
    resolve_pair.assert_called_once_with(
        validator=validator,
        backend_slug="shacl",
        backend_release_identity="1.2.3",
    )


def test_route_preflight_propagates_release_pair_resolution_failure():
    """A stale or incomplete release pair must fail before canaries can start."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
    )

    with (
        patch(
            "validibot.validations.acceptance.get_config",
            return_value=SimpleNamespace(slug="shacl", version="1"),
        ),
        patch(
            "validibot.validations.acceptance.Validator.objects.get",
            return_value=SimpleNamespace(pk="validator-1"),
        ),
        patch(
            "validibot.validations.acceptance.resolve_backend_release_pair",
            side_effect=ExecutionDeploymentResolutionError(
                "No verified pair exists for shacl 1.2.3."
            ),
        ),
        pytest.raises(ExecutionDeploymentResolutionError, match=r"1\.2\.3"),
    ):
        runner._accepted_routes(BACKENDS[2], "1.2.3")


@pytest.mark.django_db
def test_acceptance_recording_reuses_the_preflight_pair_resolution():
    """Durable acceptance must attach to the exact candidate route just exercised.

    Reusing route resolution prevents the final write from reintroducing an
    assumption that immutable release history contains exactly one Service row.
    """
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        routing_mode="job-only",
        record_acceptance=True,
    )
    validator = SimpleNamespace(pk="validator-1")
    scenario = SimpleNamespace(
        workflow=SimpleNamespace(
            steps=SimpleNamespace(
                get=MagicMock(return_value=SimpleNamespace(validator=validator))
            )
        )
    )
    service = SimpleNamespace(pk="service-2", validator_id=validator.pk)
    job = SimpleNamespace(pk="job-1", validator_id=validator.pk)
    accepted = SimpleNamespace(service=service, job=job)
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=1,
    )

    with (
        patch.object(
            runner,
            "_accepted_routes",
            return_value=(validator, service, job),
        ) as resolve_route,
        patch(
            "validibot.validations.services.execution.deployments."
            "mark_execution_deployment_pair_accepted",
            return_value=accepted,
        ) as mark_accepted,
    ):
        runner._record_pair_acceptance(report, (scenario,))

    resolve_route.assert_called_once_with(
        BACKENDS[2],
        "1.2.3",
        validator=validator,
    )
    mark_accepted.assert_called_once_with(service=service, job=job)
    assert report.checks[-1].status == "passed"
    assert report.checks[-1].details["deployments"][0] == {
        "validator_id": "validator-1",
        "service_deployment_id": "service-2",
        "job_deployment_id": "job-1",
    }


def test_latency_gate_uses_only_attempts_from_the_exact_launched_burst():
    """Old fast samples must not hide missing or slow evidence in this release."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )
    scenario = AcceptanceScenario(
        backend=BACKENDS[2],
        workflow=SimpleNamespace(),
        inline_text="fixture",
        filename="fixture.ttl",
        file_type=SubmissionFileType.TEXT,
        fixture_sha256="a" * 64,
    )
    accepted_at = timezone.now()
    attempts = [
        SimpleNamespace(
            deployment=SimpleNamespace(
                deployment_revision="service-r7",
                minimum_instances=0,
            ),
            provider_accepted_at=accepted_at,
            provider_started_at=accepted_at + timedelta(seconds=2),
            callback_received_at=accepted_at + timedelta(seconds=5),
        ),
        SimpleNamespace(
            deployment=SimpleNamespace(
                deployment_revision="service-r7",
                minimum_instances=0,
            ),
            provider_accepted_at=accepted_at,
            provider_started_at=accepted_at + timedelta(seconds=3),
            callback_received_at=accepted_at + timedelta(seconds=6),
        ),
        SimpleNamespace(
            deployment=SimpleNamespace(
                deployment_revision="service-r7",
                minimum_instances=0,
            ),
            provider_accepted_at=accepted_at,
            provider_started_at=accepted_at + timedelta(seconds=4),
            callback_received_at=accepted_at + timedelta(seconds=8),
        ),
    ]
    querysets = []
    for attempt in attempts:
        queryset = MagicMock()
        queryset.select_related.return_value.order_by.return_value.last.return_value = (
            attempt
        )
        querysets.append(queryset)
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )
    launched = [
        (1, SimpleNamespace(pk="run-1")),
        (2, SimpleNamespace(pk="run-2")),
        (3, SimpleNamespace(pk="run-3")),
    ]

    with patch(
        "validibot.validations.acceptance.ExecutionAttempt.objects.filter",
        side_effect=querysets,
    ):
        runner._record_latency(report, scenario, launched)

    check = report.checks[-1]
    assert check.status == "passed"
    assert (
        check.details["provider_start_summary_seconds"] == EXPECTED_START_TIMING_SUMMARY
    )
    assert check.details["timing_samples"] == [
        {
            "sequence": 1,
            "validation_run_id": "run-1",
            "provider_start_seconds": 2.0,
            "provider_total_seconds": 5.0,
        },
        {
            "sequence": 2,
            "validation_run_id": "run-2",
            "provider_start_seconds": 3.0,
            "provider_total_seconds": 6.0,
        },
        {
            "sequence": 3,
            "validation_run_id": "run-3",
            "provider_start_seconds": 4.0,
            "provider_total_seconds": 8.0,
        },
    ]
    assert check.details["deployment_revisions"] == ["service-r7"]
    assert "provider_start_p95_seconds" not in check.details
    assert "representative_sample" not in check.details


def test_scale_to_zero_startup_latency_is_observed_without_universal_threshold():
    """Startup evidence must not be judged by one target for every backend."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )
    scenario = AcceptanceScenario(
        backend=BACKENDS[2],
        workflow=SimpleNamespace(),
        inline_text="fixture",
        filename="fixture.ttl",
        file_type=SubmissionFileType.TEXT,
        fixture_sha256="a" * 64,
    )
    accepted_at = timezone.now()
    attempts = [
        SimpleNamespace(
            deployment=SimpleNamespace(
                deployment_revision="service-r7",
                minimum_instances=0,
            ),
            provider_accepted_at=accepted_at,
            provider_started_at=accepted_at + timedelta(seconds=20),
            callback_received_at=accepted_at + timedelta(seconds=25),
        ),
        SimpleNamespace(
            deployment=SimpleNamespace(
                deployment_revision="service-r7",
                minimum_instances=0,
            ),
            provider_accepted_at=accepted_at,
            provider_started_at=accepted_at + timedelta(seconds=30),
            callback_received_at=accepted_at + timedelta(seconds=35),
        ),
        SimpleNamespace(
            deployment=SimpleNamespace(
                deployment_revision="service-r7",
                minimum_instances=0,
            ),
            provider_accepted_at=accepted_at,
            provider_started_at=accepted_at + timedelta(seconds=45),
            callback_received_at=accepted_at + timedelta(seconds=50),
        ),
    ]
    querysets = []
    for attempt in attempts:
        queryset = MagicMock()
        queryset.select_related.return_value.order_by.return_value.last.return_value = (
            attempt
        )
        querysets.append(queryset)
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )
    launched = [
        (1, SimpleNamespace(pk="run-1")),
        (2, SimpleNamespace(pk="run-2")),
        (3, SimpleNamespace(pk="run-3")),
    ]

    with patch(
        "validibot.validations.acceptance.ExecutionAttempt.objects.filter",
        side_effect=querysets,
    ):
        runner._record_latency(report, scenario, launched)

    check = report.checks[-1]
    assert check.status == "passed"
    assert check.details["latency_policy"] == (
        "release_smoke_observation_no_percentiles_or_threshold"
    )
    assert (
        check.details["provider_start_summary_seconds"]["median"]
        == EXPECTED_SCALE_TO_ZERO_START_MEDIAN
    )
    assert "provider_start_p95_seconds" not in check.details
    assert "provider_start_target_seconds" not in check.details
    assert (
        check.details["provider_start_measurement"]
        == "provider_accepted_at_to_provider_started_at"
    )


def test_latency_evidence_rejects_a_nonzero_validator_service_minimum():
    """Acceptance must protect the zero-minimum cost policy from drift."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=1,
    )
    scenario = AcceptanceScenario(
        backend=BACKENDS[2],
        workflow=SimpleNamespace(),
        inline_text="fixture",
        filename="fixture.ttl",
        file_type=SubmissionFileType.TEXT,
        fixture_sha256="a" * 64,
    )
    accepted_at = timezone.now()
    attempt = SimpleNamespace(
        deployment=SimpleNamespace(
            deployment_revision="service-r7",
            minimum_instances=1,
        ),
        provider_accepted_at=accepted_at,
        provider_started_at=accepted_at + timedelta(seconds=1),
        callback_received_at=accepted_at + timedelta(seconds=2),
    )
    queryset = MagicMock()
    queryset.select_related.return_value.order_by.return_value.last.return_value = (
        attempt
    )
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=1,
    )

    with patch(
        "validibot.validations.acceptance.ExecutionAttempt.objects.filter",
        return_value=queryset,
    ):
        runner._record_latency(
            report,
            scenario,
            [(1, SimpleNamespace(pk="run-1"))],
        )

    check = report.checks[-1]
    assert check.status == "failed"
    assert check.details["error"] == (
        "validator Service minimum instances must remain zero"
    )


def test_smoke_verdict_requires_matching_immutable_attempt_provenance():
    """A successful run is not accepted when its observed image differs."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
    )
    scenario = AcceptanceScenario(
        backend=BACKENDS[2],
        workflow=SimpleNamespace(),
        inline_text="fixture",
        filename="fixture.ttl",
        file_type=SubmissionFileType.TEXT,
        fixture_sha256="a" * SHA256_HEX_LENGTH,
    )
    image_digest = "sha256:" + "a" * SHA256_HEX_LENGTH
    deployment = SimpleNamespace(
        pk="deployment-1",
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        deployment_revision="service-r7",
        backend_release_identity="1.2.3",
        backend_image_digest=image_digest,
    )
    now = timezone.now()
    attempt = SimpleNamespace(
        pk="attempt-1",
        state="COMPLETED",
        deployment_id="deployment-1",
        deployment=deployment,
        deployment_snapshot={
            "deployment_id": "deployment-1",
            "deployment_revision": "service-r7",
            "backend_image_digest": image_digest,
        },
        backend_image_digest="sha256:" + "b" * SHA256_HEX_LENGTH,
        input_envelope_sha256="c" * SHA256_HEX_LENGTH,
        input_evidence_snapshot={"files": [{"sha256": "d" * SHA256_HEX_LENGTH}]},
        output_envelope_sha256="e" * SHA256_HEX_LENGTH,
        provider_accepted_at=now,
        provider_started_at=now + timedelta(seconds=1),
        provider_finished_at=now + timedelta(seconds=2),
        callback_received_at=now + timedelta(seconds=3),
    )
    queryset = MagicMock()
    queryset.select_related.return_value.order_by.return_value.last.return_value = (
        attempt
    )
    run = SimpleNamespace(
        pk="run-1",
        status=ValidationRunStatus.SUCCEEDED,
        error="",
    )
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=1,
    )

    with patch(
        "validibot.validations.acceptance.ExecutionAttempt.objects.filter",
        return_value=queryset,
    ):
        runner._record_run(report, scenario, 1, run)

    assert report.checks[-1].status == "failed"
    assert report.checks[-1].details["error"] == (
        "attempt observed a different backend image"
    )


def test_failed_smoke_report_includes_the_recorded_validation_finding():
    """Acceptance failures must expose their precise finding to the operator."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
    )
    scenario = AcceptanceScenario(
        backend=BACKENDS[4],
        workflow=SimpleNamespace(),
        inline_text="fixture",
        filename="fixture.xml",
        file_type=SubmissionFileType.XML,
        fixture_sha256="a" * SHA256_HEX_LENGTH,
    )
    finding = SimpleNamespace(
        severity="ERROR",
        code="",
        path="",
        message="Required input 'portfolio_manager_report' could not be resolved.",
    )
    run = SimpleNamespace(
        pk="run-1",
        status=ValidationRunStatus.FAILED,
        error="One or more validation steps failed.",
        findings=SimpleNamespace(order_by=lambda _field: [finding]),
    )
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=1,
    )
    queryset = MagicMock()
    queryset.select_related.return_value.order_by.return_value.last.return_value = None

    with patch(
        "validibot.validations.acceptance.ExecutionAttempt.objects.filter",
        return_value=queryset,
    ):
        runner._record_run(report, scenario, 1, run)

    check = report.checks[-1]
    assert check.status == "failed"
    assert check.details["run_error"] == "One or more validation steps failed."
    assert check.details["findings"] == [
        {
            "severity": "ERROR",
            "code": "",
            "path": "",
            "message": (
                "Required input 'portfolio_manager_report' could not be resolved."
            ),
        }
    ]


def test_job_only_smoke_accepts_the_exact_immutable_job_snapshot():
    """The second canary pass must prove dispatch used the fallback Job route."""
    runner = ValidatorAcceptanceRunner(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        routing_mode="job-only",
    )
    scenario = AcceptanceScenario(
        backend=BACKENDS[2],
        workflow=SimpleNamespace(),
        inline_text="fixture",
        filename="fixture.ttl",
        file_type=SubmissionFileType.TEXT,
        fixture_sha256="a" * SHA256_HEX_LENGTH,
    )
    image_digest = "sha256:" + "a" * SHA256_HEX_LENGTH
    deployment = SimpleNamespace(
        pk="deployment-job-1",
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
        deployment_revision="job-v1-20260724",
        backend_release_identity="1.2.3",
        backend_image_digest=image_digest,
    )
    now = timezone.now()
    attempt = SimpleNamespace(
        pk="attempt-1",
        state="COMPLETED",
        deployment_id="deployment-job-1",
        deployment=deployment,
        deployment_snapshot={
            "deployment_id": "deployment-job-1",
            "deployment_revision": "job-v1-20260724",
            "backend_image_digest": image_digest,
        },
        backend_image_digest=image_digest,
        input_envelope_sha256="c" * SHA256_HEX_LENGTH,
        input_evidence_snapshot={"files": [{"sha256": "d" * SHA256_HEX_LENGTH}]},
        output_envelope_sha256="e" * SHA256_HEX_LENGTH,
        provider_accepted_at=now,
        provider_started_at=now + timedelta(seconds=1),
        provider_finished_at=now + timedelta(seconds=2),
        callback_received_at=now + timedelta(seconds=3),
    )
    queryset = MagicMock()
    queryset.select_related.return_value.order_by.return_value.last.return_value = (
        attempt
    )
    run = SimpleNamespace(
        pk="run-1",
        status=ValidationRunStatus.SUCCEEDED,
        error="",
    )
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=1,
    )

    with patch(
        "validibot.validations.acceptance.ExecutionAttempt.objects.filter",
        return_value=queryset,
    ):
        runner._record_run(report, scenario, 1, run)

    assert report.checks[-1].status == "passed"
    assert (
        report.checks[-1].details["deployment_kind"]
        == ExecutionDeploymentKind.CLOUD_RUN_JOB
    )


def test_submission_fixtures_are_deterministic_and_domain_representative():
    """Canaries must exercise real parser/simulation paths, not empty payloads."""
    builder = object.__new__(AcceptanceFixtureBuilder)

    energyplus_text, _, _ = builder._submission_fixture(BACKENDS[0])
    fmu_text, _, _ = builder._submission_fixture(BACKENDS[1])
    shacl_text, _, _ = builder._submission_fixture(BACKENDS[2])
    schematron_text, _, _ = builder._submission_fixture(BACKENDS[3])
    portfolio_manager_text, _, _ = builder._submission_fixture(BACKENDS[4])
    pdf_bytes, pdf_filename, pdf_file_type = builder._submission_fixture(BACKENDS[5])

    assert json.loads(energyplus_text)["U_FACTOR"] == EXPECTED_ENERGYPLUS_U_FACTOR
    assert json.loads(fmu_text)["real_continuous_in"] == EXPECTED_FMU_INPUT
    assert "ValidPerson" in shacl_text or "Person" in shacl_text
    assert "calibration" in schematron_text.lower()
    assert (
        f"<propertyId>{EXPECTED_PORTFOLIO_MANAGER_PROPERTY_ID}</propertyId>"
        in portfolio_manager_text
    )
    assert len(BACKENDS) == EXPECTED_MANAGED_BACKEND_COUNT
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF-2.0")
    assert pdf_filename == "aec-issue-package-clean.pdf"
    assert pdf_file_type == SubmissionFileType.PDF


@pytest.mark.django_db
def test_fixture_builder_creates_and_reuses_one_selected_backend_workflow():
    """A backend release must prepare only its deterministic canary workflow."""
    call_command("sync_validators", stdout=StringIO(), stderr=StringIO())

    first = AcceptanceFixtureBuilder().build(BACKENDS[2])
    second = AcceptanceFixtureBuilder().build(BACKENDS[2])

    assert first.backend.key == ACCEPTANCE_BACKEND
    assert first.workflow.pk == second.workflow.pk
    assert first.workflow.steps.count() == 1
    assert len(first.fixture_sha256) == SHA256_HEX_LENGTH


@pytest.mark.django_db
def test_fixture_builder_reclaims_orphaned_deterministic_ruleset():
    """A system reset must not make the next backend acceptance setup fail.

    Resetting operational data removes acceptance workflows while preserving
    their organization-owned rulesets. Reusing that unreferenced identity is
    required for setup to remain repeatable without weakening the uniqueness
    constraint shared by ordinary user rulesets.
    """
    call_command("sync_validators", stdout=StringIO(), stderr=StringIO())
    spec = BACKENDS[2]
    builder = AcceptanceFixtureBuilder()
    validator = builder._current_validator(spec)
    workflow_slug = builder._workflow_slug(spec, validator)
    orphaned = Ruleset.objects.create(
        org=builder.org,
        name=f"{workflow_slug}-rules",
        ruleset_type=spec.ruleset_type,
        version="1",
        rules_text="stale acceptance fixture",
        metadata={"stale": True},
    )

    scenario = builder.build(spec)

    orphaned.refresh_from_db()
    step = scenario.workflow.steps.get()
    assert step.ruleset_id == orphaned.pk
    assert "stale acceptance fixture" not in orphaned.rules_text
    assert orphaned.metadata == {"submission_format": "turtle"}


@pytest.mark.django_db
def test_portfolio_manager_fixture_exercises_explicit_target_comparison():
    """Acceptance must test authored EUIt policy without a hidden profile."""
    call_command("sync_validators", stdout=StringIO(), stderr=StringIO())

    scenario = AcceptanceFixtureBuilder().build(BACKENDS[4])
    step = scenario.workflow.steps.get()

    assert step.config["compare_to_euit"] is True
    assert step.config["default_euit_kbtu_ft2_yr"] == "40"
    assert "profile" not in step.config


@pytest.mark.django_db
def test_pdf_fixture_exercises_the_fixed_positive_package_policy():
    """Release acceptance must launch the reviewed PDF package through real upload."""
    call_command("sync_validators", stdout=StringIO(), stderr=StringIO())

    scenario = AcceptanceFixtureBuilder().build(BACKENDS[5])
    step = scenario.workflow.steps.get()

    assert step.ruleset is None
    assert step.config == {
        "policy": "static_text_package_v1",
        "execution_timeout_seconds": 300,
    }
    assert scenario.file_type == SubmissionFileType.PDF
    assert isinstance(scenario.inline_text, bytes)
    assert scenario.inline_text.startswith(b"%PDF-2.0")


@pytest.mark.django_db
def test_fixture_builder_covers_every_compatible_semantic_validator_row():
    """Distinct compatible semantic rows must never collide on one workflow."""
    call_command("sync_validators", stdout=StringIO(), stderr=StringIO())
    current = Validator.objects.get(
        validation_type=BACKENDS[2].validation_type,
        is_system=True,
    )
    original_slug = current.slug
    current.pk = None
    current._state.adding = True
    current.slug = f"{original_slug}-alternate"
    current.semantic_digest = "b" * SHA256_HEX_LENGTH
    current.save(force_insert=True)

    scenarios = AcceptanceFixtureBuilder().build_compatible(BACKENDS[2])

    assert {scenario.validator_slug for scenario in scenarios} == {
        original_slug,
        current.slug,
    }
    assert (
        len({scenario.workflow.pk for scenario in scenarios})
        == COMPATIBLE_SEMANTIC_VALIDATOR_COUNT
    )
    assert all(scenario.workflow.steps.count() == 1 for scenario in scenarios)


def test_runtime_image_includes_only_explicit_acceptance_assets():
    """Production must have canaries without accidentally shipping all tests."""
    dockerignore = (Path(settings.BASE_DIR) / ".dockerignore").read_text()

    assert "tests/*" in dockerignore
    assert "!tests/assets/fmu/Feedthrough.fmu" in dockerignore
    assert "!tests/assets/idf/window_glazing_template.idf" in dockerignore
    assert "!tests/assets/pdf/aec-issue-package-clean.pdf" in dockerignore
    assert "!tests/assets/portfolio_manager/property-report-valid.xml" in dockerignore
    assert "!tests/assets/shacl/valid_person.ttl" in dockerignore
    assert (
        "!tests/assets/schematron/calibration/calibration-rules-demo.sch"
        in dockerignore
    )


def test_management_command_persists_and_prints_one_json_result():
    """Automation needs one parseable report location, not copied console notes."""
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )
    report.add("VA-ALL", "passed", "All checks passed.")
    report.finish()
    output = StringIO()

    with (
        patch(
            "validibot.validations.management.commands.run_validator_acceptance."
            "ValidatorAcceptanceRunner"
        ) as runner_class,
        patch(
            "validibot.validations.management.commands.run_validator_acceptance."
            "persist_acceptance_report",
            return_value={"uri": "gs://private/report.json", "sha256": "a" * 64},
        ),
    ):
        runner_class.return_value.run.return_value = report
        call_command(
            "run_validator_acceptance",
            backend=ACCEPTANCE_BACKEND,
            release_tag=ACCEPTANCE_RELEASE_TAG,
            require_persisted_report=True,
            stdout=output,
        )

    document = json.loads(output.getvalue())
    assert document["passed"] is True
    assert document["attempts_per_backend"] == ROUTINE_ACCEPTANCE_ATTEMPTS
    assert (
        runner_class.call_args.kwargs["attempts_per_backend"]
        == ROUTINE_ACCEPTANCE_ATTEMPTS
    )
    assert document["evidence"]["uri"] == "gs://private/report.json"


def test_management_command_fails_when_private_evidence_is_not_configured():
    """Production automation must not turn an unretained console pass into proof."""
    report = AcceptanceReport(
        backend=ACCEPTANCE_BACKEND,
        release_tag=ACCEPTANCE_RELEASE_TAG,
        attempts_per_backend=ROUTINE_ACCEPTANCE_ATTEMPTS,
    )
    report.add("VA-ALL", "passed", "All checks passed.")
    report.finish()

    with (
        patch(
            "validibot.validations.management.commands.run_validator_acceptance."
            "ValidatorAcceptanceRunner"
        ) as runner_class,
        patch(
            "validibot.validations.management.commands.run_validator_acceptance."
            "persist_acceptance_report",
            return_value=None,
        ),
    ):
        runner_class.return_value.run.return_value = report
        with pytest.raises(CommandError, match="persistence is not configured"):
            call_command(
                "run_validator_acceptance",
                backend=ACCEPTANCE_BACKEND,
                release_tag=ACCEPTANCE_RELEASE_TAG,
                require_persisted_report=True,
                stdout=StringIO(),
            )


def test_gcp_recipe_accepts_one_backend_in_both_execution_shapes():
    """Acceptance must exercise Service and Job routing before recording success."""
    recipe = (Path(settings.BASE_DIR) / "just" / "gcp" / "mod.just").read_text()
    start = recipe.index("validator-acceptance name stage release_tag")
    end = recipe.index("# Historical all-backend acceptance", start)
    acceptance_recipe = recipe[start:end]

    assert "_maintenance-assert-offline" in acceptance_recipe
    assert "cleanup_acceptance()" in acceptance_recipe
    assert "just gcp _enforce-maintenance" in acceptance_recipe
    assert "certify_validator_backend_release" in acceptance_recipe
    assert acceptance_recipe.count("just gcp management-cmd") == 1
    assert "--job-name=$JOB_NAME" in acceptance_recipe
    assert "--service-name=$SERVICE_NAME" in acceptance_recipe
    assert "--backend={{name}}" in acceptance_recipe
    assert "gcloud tasks queues resume" in acceptance_recipe
    assert "gcloud tasks queues pause" in acceptance_recipe
    assert "production acceptance requires exactly 3" in acceptance_recipe
    assert "scale-to-zero" in acceptance_recipe
    assert "CANDIDATE_TOUCHED=0" in acceptance_recipe
    assert "ACCEPTANCE_FAILURE allow-unaccepted" in acceptance_recipe
    assert "Parking {{name}} $VERSION as inactive" in acceptance_recipe
    assert "maintenance-off" not in acceptance_recipe

    update_start = recipe.index("validator-update stage backend")
    update_end = recipe.index("# Roll back one backend release", update_start)
    update_recipe = recipe[update_start:update_end]
    assert (
        "Capacity policy: every release-specific validator Service uses min=0."
        in update_recipe
    )
    assert "Restoring each backend to its previously accepted route" in update_recipe
    assert "deactivating candidate $candidate_version" in update_recipe

    service_start = recipe.index("validator-service-deploy name stage")
    service_end = recipe.index("# Provision all managed Services", service_start)
    service_recipe = recipe[service_start:service_end]
    assert "MIN_INSTANCES=0" in service_recipe
    assert "VALIDATOR_SERVICE_MIN_INSTANCES" not in service_recipe
