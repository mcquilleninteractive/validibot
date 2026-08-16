"""Issue and prepare attempt-scoped GCS authority for Cloud Run validators.

The trusted Django service retains its ordinary bucket access. Validator jobs
receive only a short-lived Google Credential Access Boundary token restricted
to one ``runs/.../attempts/<uuid>/`` prefix and to object viewing/creation.
They receive no delete permission, so an existing object cannot be replaced.

Reusable resources and upstream artifacts can live outside that prefix. Before
dispatch, this module copies their exact committed generations into the
attempt bundle and rewrites the typed envelope to those copies. A compromised
runtime therefore has no legitimate reason to address another prefix.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

import google.auth
from google.auth import downscoped
from google.auth.credentials import with_scopes_if_required
from google.auth.transport.requests import Request

from validibot.validations.services.cloud_run.gcs_client import copy_gcs_file_generation
from validibot.validations.services.cloud_run.gcs_client import parse_gcs_uri

if TYPE_CHECKING:
    from pydantic import BaseModel


_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# The initial token must survive validator-backend-queue delay and loading the immutable
# input envelope. Only after that envelope is parsed does the validator have
# the callback-bound proof needed to use the authenticated refresh endpoint.
_MIN_REMAINING_LIFETIME_SECONDS = 300
_SOURCE_TOKEN_ROLLOVER_WAIT_SECONDS = 330
_SOURCE_TOKEN_ROLLOVER_POLL_SECONDS = 10

logger = logging.getLogger(__name__)


class GCSRuntimeCapabilityError(RuntimeError):
    """Raised when a narrow runtime capability cannot be prepared safely."""


@dataclass(frozen=True, slots=True)
class AttemptGCSRuntimeCapability:
    """One bearer token plus non-secret limits passed directly to Cloud Run."""

    access_token: str = field(repr=False)
    expires_at: datetime
    allowed_prefix: str
    project_id: str
    refresh_url: str

    def as_environment(self) -> dict[str, str]:
        """Return Cloud Run overrides without ever persisting the token."""
        return {
            "VALIDIBOT_GCS_CAPABILITY_REQUIRED": "1",
            "VALIDIBOT_GCS_ACCESS_TOKEN": self.access_token,
            "VALIDIBOT_GCS_ACCESS_TOKEN_EXPIRY": self.expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "VALIDIBOT_GCS_ALLOWED_PREFIX": self.allowed_prefix,
            "VALIDIBOT_GCS_PROJECT_ID": self.project_id,
            "VALIDIBOT_GCS_CAPABILITY_REFRESH_URL": self.refresh_url,
        }


def prepare_envelope_for_attempt_capability(
    envelope: BaseModel,
    *,
    execution_bundle_uri: str,
) -> BaseModel:
    """Copy every external file generation into the attempt and rewrite URIs."""
    allowed_prefix = _normalize_attempt_prefix(execution_bundle_uri)
    input_files = _stage_items(
        getattr(envelope, "input_files", []),
        category="input",
        allowed_prefix=allowed_prefix,
    )
    resource_files = _stage_items(
        getattr(envelope, "resource_files", []),
        category="resource",
        allowed_prefix=allowed_prefix,
    )
    payload = envelope.model_dump(mode="python")
    payload["input_files"] = [item.model_dump(mode="python") for item in input_files]
    payload["resource_files"] = [
        item.model_dump(mode="python") for item in resource_files
    ]
    return type(envelope).model_validate(payload)


def issue_attempt_gcs_runtime_capability(
    *,
    execution_bundle_uri: str,
    project_id: str,
    refresh_url: str,
) -> AttemptGCSRuntimeCapability:
    """Mint a short-lived read/create token for exactly one attempt prefix."""
    if not project_id:
        raise GCSRuntimeCapabilityError("GCP project ID is required")
    if not refresh_url.startswith("https://"):
        raise GCSRuntimeCapabilityError("GCS capability refresh URL must use HTTPS")

    allowed_prefix = _normalize_attempt_prefix(execution_bundle_uri)
    bucket_name, object_prefix = parse_gcs_uri(allowed_prefix)
    resource_prefix = f"projects/_/buckets/{bucket_name}/objects/{object_prefix}"
    rule = downscoped.AccessBoundaryRule(
        available_resource=(
            f"//storage.googleapis.com/projects/_/buckets/{bucket_name}"
        ),
        available_permissions=[
            "inRole:roles/storage.objectViewer",
            "inRole:roles/storage.objectCreator",
        ],
        availability_condition=downscoped.AvailabilityCondition(
            expression=f"resource.name.startsWith({_cel_quote(resource_prefix)})",
            title="validibot-attempt-prefix",
            description="Restrict validator object access to one execution attempt.",
        ),
    )
    boundary = downscoped.CredentialAccessBoundary(rules=[rule])

    source_credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    source_credentials = with_scopes_if_required(
        source_credentials,
        [_CLOUD_PLATFORM_SCOPE],
    )
    request = Request()
    # Force a fresh source token so the derived credential starts with almost
    # the full bounded lifetime instead of inheriting a nearly-expired ADC token.
    deadline = time.monotonic() + _SOURCE_TOKEN_ROLLOVER_WAIT_SECONDS
    waiting_for_rollover = False
    while True:
        source_credentials.refresh(request)
        credentials = downscoped.Credentials(
            source_credentials=source_credentials,
            credential_access_boundary=boundary,
        )
        credentials.refresh(request)

        token = str(credentials.token or "")
        expires_at = _aware_utc(credentials.expiry)
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        if token and remaining >= _MIN_REMAINING_LIFETIME_SECONDS:
            break
        if not token or remaining <= 0 or time.monotonic() >= deadline:
            raise GCSRuntimeCapabilityError(
                "GCS downscoped token is missing or too close to expiry"
            )
        if not waiting_for_rollover:
            logger.info("Waiting for the GCP source token to roll over before dispatch")
            waiting_for_rollover = True
        time.sleep(
            min(
                _SOURCE_TOKEN_ROLLOVER_POLL_SECONDS,
                max(1, remaining),
            )
        )

    return AttemptGCSRuntimeCapability(
        access_token=token,
        expires_at=expires_at,
        allowed_prefix=allowed_prefix,
        project_id=project_id,
        refresh_url=refresh_url,
    )


def _stage_items(items, *, category: str, allowed_prefix: str) -> list:
    """Return validated items whose GCS objects all live under one prefix."""
    staged = []
    for index, item in enumerate(items):
        source_uri = str(item.uri)
        if source_uri.startswith(allowed_prefix):
            staged.append(item)
            continue
        if not source_uri.startswith("gs://"):
            raise GCSRuntimeCapabilityError(
                "Cloud Run attempt capabilities require every file to use GCS"
            )

        destination_uri = (
            f"{allowed_prefix}capability-inputs/{category}/"
            f"{index:03d}-{item.sha256[:12]}-{item.name}"
        )
        identity = copy_gcs_file_generation(
            source_uri=source_uri,
            source_generation=str(item.storage_version),
            destination_uri=destination_uri,
            expected_size_bytes=int(item.size_bytes),
            expected_sha256=str(item.sha256),
        )
        payload = item.model_dump(mode="python")
        payload.update(identity.envelope_fields())
        staged.append(type(item).model_validate(payload))
    return staged


def _normalize_attempt_prefix(execution_bundle_uri: str) -> str:
    """Return a syntactically valid GCS bundle prefix ending in one slash."""
    if not execution_bundle_uri.startswith("gs://"):
        raise GCSRuntimeCapabilityError(
            "Attempt-scoped GCS authority requires a gs:// execution bundle"
        )
    prefix = f"{execution_bundle_uri.rstrip('/')}/"
    parse_gcs_uri(prefix)
    return prefix


def _aware_utc(value: datetime | None) -> datetime:
    """Normalize google-auth expiry values to timezone-aware UTC."""
    if value is None:
        raise GCSRuntimeCapabilityError("GCS downscoped token has no expiry")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _cel_quote(value: str) -> str:
    """Return a safe single-quoted CEL string literal."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"
