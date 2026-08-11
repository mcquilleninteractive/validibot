"""Tests for issuing and staging attempt-scoped Cloud Storage authority.

Credential Access Boundaries protect confidentiality only when every object a
validator needs is inside one attempt prefix. These tests pin the exact
read/create role ceiling, secret-safe value object, and immutable staging of
external input/resource generations into that prefix.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from pydantic import BaseModel
from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import SupportedMimeType

from validibot.validations.services.cloud_run import gcs_runtime_capabilities
from validibot.validations.services.file_identity import FileIdentity

EXPECTED_TOKEN_ISSUANCE_ATTEMPTS = 2


class _TestEnvelope(BaseModel):
    """Minimal common file-bearing envelope used to exercise staging."""

    input_files: list[InputFileItem]
    resource_files: list[ResourceFileItem]


def _input_item(*, uri: str, generation: str = "17") -> InputFileItem:
    """Build one strict file item with deterministic immutable identity."""
    return InputFileItem(
        name="model.idf",
        mime_type=SupportedMimeType.ENERGYPLUS_IDF,
        role="primary-model",
        port_key="primary_model",
        uri=uri,
        size_bytes=12,
        sha256="a" * 64,
        storage_version=generation,
    )


def _resource_item(*, uri: str, generation: str = "18") -> ResourceFileItem:
    """Build one reusable resource whose source lives outside the attempt."""
    return ResourceFileItem(
        id="weather-1",
        name="weather.epw",
        type="weather",
        port_key="weather_file",
        uri=uri,
        size_bytes=34,
        sha256="b" * 64,
        storage_version=generation,
    )


def test_prepare_envelope_copies_only_external_generations(monkeypatch):
    """Already-local inputs stay put while reusable resources get exact copies."""
    allowed_prefix = "gs://validation/runs/org/run/attempts/attempt/"
    envelope = _TestEnvelope(
        input_files=[_input_item(uri=f"{allowed_prefix}model.idf")],
        resource_files=[
            _resource_item(uri="gs://validation/validator-assets/weather.epw")
        ],
    )
    captured: dict[str, object] = {}

    def _copy(**kwargs):
        """Return the destination identity while recording copy preconditions."""
        captured.update(kwargs)
        return FileIdentity(
            uri=str(kwargs["destination_uri"]),
            size_bytes=int(kwargs["expected_size_bytes"]),
            sha256=str(kwargs["expected_sha256"]),
            storage_version="99",
        )

    monkeypatch.setattr(
        gcs_runtime_capabilities,
        "copy_gcs_file_generation",
        _copy,
    )

    prepared = gcs_runtime_capabilities.prepare_envelope_for_attempt_capability(
        envelope,
        execution_bundle_uri=allowed_prefix.rstrip("/"),
    )

    assert prepared.input_files[0].uri == f"{allowed_prefix}model.idf"
    staged_resource = prepared.resource_files[0]
    assert str(staged_resource.uri).startswith(
        f"{allowed_prefix}capability-inputs/resource/000-"
    )
    assert staged_resource.storage_version == "99"
    assert captured["source_generation"] == "18"
    assert captured["expected_sha256"] == "b" * 64


def test_issue_capability_limits_token_to_read_and_create_roles(monkeypatch):
    """The token can inspect inputs and create outputs but cannot delete them."""
    captured: dict[str, object] = {}

    class _SourceCredentials:
        """Fresh ADC stand-in held only by the trusted Django service."""

        requires_scopes = False

        def refresh(self, request):
            """Record that issuance forces a fresh source access token."""
            captured["source_refreshed"] = request
            captured["source_refresh_count"] = (
                int(captured.get("source_refresh_count", 0)) + 1
            )

    class _DownscopedCredentials:
        """Return a deterministic token while exposing the tested boundary."""

        def __init__(self, *, source_credentials, credential_access_boundary):
            """Capture the source and access boundary passed by the issuer."""
            captured["source"] = source_credentials
            captured["boundary"] = credential_access_boundary
            self.token = None
            self.expiry = None

        def refresh(self, request):
            """Simulate Google's STS exchange for a bounded token."""
            captured["downscoped_refreshed"] = request
            refresh_count = int(captured.get("downscoped_refresh_count", 0)) + 1
            captured["downscoped_refresh_count"] = refresh_count
            self.token = "attempt-token-secret"  # noqa: S105 - test fixture
            # The metadata server may return a still-valid cached source token
            # inside its final five minutes. The issuer waits for rollover so
            # the initial token can survive queueing and input-envelope load.
            lifetime = 2 if refresh_count == 1 else 50
            self.expiry = datetime.now(UTC) + timedelta(minutes=lifetime)

    source = _SourceCredentials()
    monkeypatch.setattr(
        gcs_runtime_capabilities.google.auth,
        "default",
        lambda **kwargs: (source, "project"),
    )
    monkeypatch.setattr(
        gcs_runtime_capabilities.downscoped,
        "Credentials",
        _DownscopedCredentials,
    )
    monkeypatch.setattr(gcs_runtime_capabilities.time, "sleep", lambda _seconds: None)

    capability = gcs_runtime_capabilities.issue_attempt_gcs_runtime_capability(
        execution_bundle_uri="gs://validation/runs/org/run/attempts/attempt",
        project_id="validibot-project",
        refresh_url=(
            "https://worker.example/api/v1/validation-storage-capabilities/refresh/"
        ),
    )

    boundary = captured["boundary"].to_json()["accessBoundary"]
    rule = boundary["accessBoundaryRules"][0]
    assert rule["availablePermissions"] == [
        "inRole:roles/storage.objectViewer",
        "inRole:roles/storage.objectCreator",
    ]
    assert (
        "runs/org/run/attempts/attempt/" in rule["availabilityCondition"]["expression"]
    )
    assert capability.allowed_prefix == (
        "gs://validation/runs/org/run/attempts/attempt/"
    )
    assert "attempt-token-secret" not in repr(capability)
    assert capability.as_environment()["VALIDIBOT_GCS_CAPABILITY_REQUIRED"] == "1"
    assert captured["source_refresh_count"] == EXPECTED_TOKEN_ISSUANCE_ATTEMPTS
    assert captured["downscoped_refresh_count"] == EXPECTED_TOKEN_ISSUANCE_ATTEMPTS
