"""Regression tests for validator release management-command interfaces.

Django's ``BaseCommand`` reserves global options such as ``--version``.
Release lifecycle commands must therefore use distinct, explicit options for
backend identity and destructive lifecycle exceptions, or their parsers fail
before any operator action can run. These tests construct the real parsers so
option collisions and missing safety flags cannot hide behind service-level
test coverage.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from validibot.validations.acceptance import ROUTINE_ACCEPTANCE_ATTEMPTS
from validibot.validations.management.commands import activate_validator_backend_release
from validibot.validations.management.commands import record_validator_provider_deletion
from validibot.validations.management.commands import retire_validator_backend_release


def test_activation_parser_accepts_release_version_without_shadowing_django():
    """Activation must keep Django's flag while parsing release identity."""
    parser = activate_validator_backend_release.Command().create_parser(
        "manage.py",
        "activate_validator_backend_release",
    )

    options = parser.parse_args(
        [
            "--backend",
            "energyplus",
            "--release-version",
            "0.15.4",
        ]
    )

    assert options.release_version == "0.15.4"


def test_retirement_parser_accepts_release_version_without_shadowing_django():
    """Retirement must remain callable when its exact backend version is supplied."""
    parser = retire_validator_backend_release.Command().create_parser(
        "manage.py",
        "retire_validator_backend_release",
    )

    options = parser.parse_args(
        [
            "--backend",
            "energyplus",
            "--release-version",
            "0.15.4",
            "--reason",
            "Verified drain completed.",
            "--immediate",
            "--allow-unaccepted-candidate",
        ]
    )

    assert options.release_version == "0.15.4"
    assert options.immediate is True
    assert options.allow_unaccepted_candidate is True


def test_provider_deletion_parser_requires_explicit_superseded_deactivation():
    """Latest-only repair must be opt-in rather than routine cleanup behavior."""
    parser = record_validator_provider_deletion.Command().create_parser(
        "manage.py",
        "record_validator_provider_deletion",
    )

    routine = parser.parse_args(["--resource", "projects/example/services/old"])
    latest_only = parser.parse_args(
        [
            "--resource",
            "projects/example/services/old",
            "--deactivate-superseded",
        ]
    )

    assert routine.deactivate_superseded is False
    assert latest_only.deactivate_superseded is True


def test_certification_runs_both_acceptance_shapes_in_one_command():
    """One remote execution must retain both Service and Job evidence bursts."""
    nested_calls = []

    def _record(command_name, *args, **options):
        nested_calls.append((command_name, options))

    with patch(
        "validibot.validations.management.commands."
        "certify_validator_backend_release.call_command",
        side_effect=_record,
    ):
        call_command(
            "certify_validator_backend_release",
            backend="energyplus",
            release_tag="energyplus-v0.15.5",
            job_name="validibot-val-energyplus-v0-15-5",
            service_name="validibot-vals-energyplus-v0-15-5",
            outgoing_version="0.15.4",
            outgoing_job_name="validibot-val-energyplus-v0-15-4",
            outgoing_service_name="validibot-vals-energyplus-v0-15-4",
            final_mode="inactive",
            stdout=StringIO(),
            stderr=StringIO(),
        )

    assert [name for name, _options in nested_calls] == [
        "sync_gcp_validator_deployments",
        "sync_gcp_validator_services",
        "activate_validator_backend_release",
        "run_validator_acceptance",
        "activate_validator_backend_release",
        "run_validator_acceptance",
        "sync_gcp_validator_deployments",
        "sync_gcp_validator_services",
        "activate_validator_backend_release",
        "activate_validator_backend_release",
        "activate_validator_backend_release",
    ]
    acceptance_options = [
        options for name, options in nested_calls if name == "run_validator_acceptance"
    ]
    assert [options["routing_mode"] for options in acceptance_options] == [
        "normal",
        "job-only",
    ]
    assert all(
        options["require_persisted_report"] is True
        and options["ambient_isolation_verified"] is True
        and options["attempts"] == ROUTINE_ACCEPTANCE_ATTEMPTS
        for options in acceptance_options
    )
    assert acceptance_options[1]["record_acceptance"] is True
    assert acceptance_options[1]["skip_storage_probe"] is True
    assert [options["mode"] for name, options in nested_calls[-3:]] == [
        "normal",
        "normal",
        "inactive",
    ]
    assert nested_calls[-1][1]["mode"] == "inactive"
