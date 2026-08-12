"""Authenticated opaque references used by the MCP tool contract.

References are deterministic so model clients can safely pass them between
tools, but encrypted so they do not reveal database identifiers or routing
fields. They intentionally have no compatibility path for the retired
base64-JSON references because Validibot has no MCP users to migrate.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from django.conf import settings

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun
    from validibot.workflows.models import Workflow

WORKFLOW_REFERENCE_PREFIX = "wf_"
RUN_REFERENCE_PREFIX = "run_"
_REFERENCE_CONTEXT = b"validibot:mcp-reference:v1"


def build_workflow_reference(workflow: Workflow) -> str:
    """Return a stable reference for a workflow family."""

    return _encrypt_reference(
        prefix=WORKFLOW_REFERENCE_PREFIX,
        payload={"org": workflow.org.slug, "workflow": workflow.slug},
        kind=b"workflow",
    )


def parse_workflow_reference(reference: str) -> tuple[str, str]:
    """Resolve an authenticated workflow reference to its family key."""

    payload = _decrypt_reference(
        reference=reference,
        prefix=WORKFLOW_REFERENCE_PREFIX,
        kind=b"workflow",
    )
    org_slug = payload.get("org", "").strip()
    workflow_slug = payload.get("workflow", "").strip()
    if not org_slug or not workflow_slug:
        raise ValueError("Workflow reference is invalid.")
    return org_slug, workflow_slug


def build_run_reference(validation_run: ValidationRun) -> str:
    """Return an opaque reference for one validation run."""

    return _encrypt_reference(
        prefix=RUN_REFERENCE_PREFIX,
        payload={"run": str(validation_run.pk)},
        kind=b"run",
    )


def parse_run_reference(reference: str) -> str:
    """Resolve an authenticated run reference to its internal identifier."""

    payload = _decrypt_reference(
        reference=reference,
        prefix=RUN_REFERENCE_PREFIX,
        kind=b"run",
    )
    run_id = payload.get("run", "").strip()
    if not run_id:
        raise ValueError("Run reference is invalid.")
    return run_id


def _reference_cipher() -> AESSIV:
    """Build a deterministic authenticated cipher from Django's secret key."""

    key = hashlib.sha512(
        f"{settings.SECRET_KEY}:mcp-reference-v1".encode(),
    ).digest()
    return AESSIV(key)


def _encrypt_reference(*, prefix: str, payload: dict[str, str], kind: bytes) -> str:
    """Encrypt and authenticate a compact reference payload."""

    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    encrypted = _reference_cipher().encrypt(
        plaintext,
        [_REFERENCE_CONTEXT, kind],
    )
    encoded = base64.urlsafe_b64encode(encrypted).decode().rstrip("=")
    return f"{prefix}{encoded}"


def _decrypt_reference(*, reference: str, prefix: str, kind: bytes) -> dict[str, str]:
    """Authenticate and decrypt a reference without leaking parse details."""

    if not reference.startswith(prefix):
        raise ValueError("Reference is invalid.")
    encoded = reference.removeprefix(prefix)
    try:
        encrypted = _decode_canonical_reference(encoded)
        plaintext = _reference_cipher().decrypt(
            encrypted,
            [_REFERENCE_CONTEXT, kind],
        )
        payload = json.loads(plaintext)
    except (
        InvalidTag,
        ValueError,
        TypeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("Reference is invalid.") from exc
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise ValueError("Reference is invalid.")
    return payload


def _decode_canonical_reference(encoded: str) -> bytes:
    """Decode one Base64URL value while rejecting alternate spellings."""

    padding = "=" * (-len(encoded) % 4)
    encrypted = base64.b64decode(
        f"{encoded}{padding}",
        altchars=b"-_",
        validate=True,
    )
    canonical = base64.urlsafe_b64encode(encrypted).decode().rstrip("=")
    if encoded != canonical:
        raise ValueError("Reference encoding is not canonical.")
    return encrypted
