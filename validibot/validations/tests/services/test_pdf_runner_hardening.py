"""Tests for the Docker sandbox applied to untrusted PDF parsing.

The PDF backend intentionally uses a narrow qpdf/pikepdf feature set, but the
native parser remains an attacker-controlled-code boundary. These tests pin
the controls that must survive global runner configuration: no network, no
executable scratch space, no IPC, a reduced PID ceiling, child reaping, and an
optional fail-closed stronger runtime.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.test import override_settings

from validibot.validations.services.runners.docker import PDF_PARSER_PIDS_LIMIT
from validibot.validations.services.runners.docker import DockerValidatorRunner
from validibot.validations.services.runners.docker import _apply_pdf_parser_hardening


def _baseline_config() -> dict:
    """Return a deliberately networked baseline that the helper must tighten."""
    return {
        "network": "globally-enabled-validator-network",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "user": "1000:1000",
        "pids_limit": 512,
        "tmpfs": {"/tmp": "size=2g,mode=1777"},  # noqa: S108
    }


def _workspace() -> SimpleNamespace:
    """Provide the attempt-scoped mount contract without touching Docker."""
    return SimpleNamespace(
        host_input_dir=Path("/test/attempt/input"),
        host_output_dir=Path("/test/attempt/output"),
        container_input_dir="/validibot/input",
        container_output_dir="/validibot/output",
    )


class TestPdfParserHardening:
    """Prove the helper cannot inherit weaker global container settings."""

    @override_settings(
        VALIDATOR_PDF_CONTAINER_RUNTIME="",
        VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX=False,
    )
    def test_forces_the_parser_specific_process_and_egress_boundary(self) -> None:
        """A global validator network must never reach the native PDF parser."""
        hardened = _apply_pdf_parser_hardening(_baseline_config())

        assert "network" not in hardened
        assert hardened["network_mode"] == "none"
        assert hardened["ipc_mode"] == "none"
        assert hardened["pids_limit"] == PDF_PARSER_PIDS_LIMIT
        assert hardened["init"] is True
        assert hardened["read_only"] is True
        assert hardened["cap_drop"] == ["ALL"]
        assert hardened["security_opt"] == ["no-new-privileges:true"]
        assert hardened["user"] == "1000:1000"
        assert hardened["tmpfs"]["/tmp"] == (  # noqa: S108
            "rw,noexec,nosuid,nodev,size=2g,mode=1777"
        )

    @override_settings(
        VALIDATOR_PDF_CONTAINER_RUNTIME="runsc",
        VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX=True,
    )
    def test_selects_an_installed_stronger_runtime(self) -> None:
        """A configured gVisor runtime must be passed unchanged to Docker."""
        hardened = _apply_pdf_parser_hardening(_baseline_config())

        assert hardened["runtime"] == "runsc"

    @override_settings(
        VALIDATOR_PDF_CONTAINER_RUNTIME="",
        VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX=False,
    )
    def test_preserves_any_stricter_existing_security_options(self) -> None:
        """PDF defaults must compose with, not erase, an operator seccomp rule."""
        baseline = _baseline_config()
        baseline["security_opt"].append("seccomp=/operator/pdf-seccomp.json")

        hardened = _apply_pdf_parser_hardening(baseline)

        assert hardened["security_opt"] == [
            "no-new-privileges:true",
            "seccomp=/operator/pdf-seccomp.json",
        ]

    @override_settings(
        VALIDATOR_PDF_CONTAINER_RUNTIME="",
        VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX=True,
    )
    def test_fails_closed_when_a_required_runtime_is_missing(self) -> None:
        """Public deployments can prohibit fallback to the host's runc kernel."""
        with pytest.raises(RuntimeError, match="stronger container runtime"):
            _apply_pdf_parser_hardening(_baseline_config())

    @override_settings(
        VALIDATOR_PDF_CONTAINER_RUNTIME="",
        VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX=False,
        VALIDATOR_BACKEND_IMAGE_POLICY="tag",
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=False,
    )
    def test_runner_applies_profile_for_the_execution_backend_slug(self) -> None:
        """The composed runner's short ``pdf`` slug must activate hardening."""
        runner = DockerValidatorRunner(network="globally-enabled-network")
        captured: dict = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            container = MagicMock()
            container.short_id = "pdf-test"
            container.wait.return_value = {"StatusCode": 0}
            container.logs.return_value = b""
            container.image.attrs = {}
            container.attrs = {"Image": "sha256:" + "0" * 64}
            return container

        mock_client = MagicMock()
        mock_client.containers.run.side_effect = fake_run
        with patch.object(runner, "_get_client", return_value=mock_client):
            runner.run(
                container_image="pdf-validator:test",
                input_uri="file:///validibot/input/envelope.json",
                output_uri="file:///validibot/output/envelope.json",
                validator_slug="pdf",
                workspace=_workspace(),
            )

        assert captured["network_mode"] == "none"
        assert "network" not in captured
        assert captured["ipc_mode"] == "none"
        assert captured["init"] is True
        assert "noexec" in captured["tmpfs"]["/tmp"]  # noqa: S108

    @override_settings(
        VALIDATOR_PDF_CONTAINER_RUNTIME="",
        VALIDATOR_PDF_REQUIRE_STRONG_SANDBOX=False,
        VALIDATOR_BACKEND_IMAGE_POLICY="tag",
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=False,
    )
    def test_non_pdf_runner_keeps_explicit_network_configuration(self) -> None:
        """The parser profile must not silently change unrelated validators."""
        runner = DockerValidatorRunner(network="required-validator-network")
        captured: dict = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            container = MagicMock()
            container.short_id = "other-test"
            container.wait.return_value = {"StatusCode": 0}
            container.logs.return_value = b""
            container.image.attrs = {}
            container.attrs = {"Image": "sha256:" + "0" * 64}
            return container

        mock_client = MagicMock()
        mock_client.containers.run.side_effect = fake_run
        with patch.object(runner, "_get_client", return_value=mock_client):
            runner.run(
                container_image="energyplus:test",
                input_uri="file:///validibot/input/envelope.json",
                output_uri="file:///validibot/output/envelope.json",
                validator_slug="energyplus",
                workspace=_workspace(),
            )

        assert captured["network"] == "required-validator-network"
        assert "network_mode" not in captured
        assert "ipc_mode" not in captured
