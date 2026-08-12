"""Opaque reference helpers for legacy Community and Cloud agent adapters.

Encodes the minimum routing data the HTTP adapter needs (org slug + workflow
slug, or org slug + run id) into URL-safe base64 JSON strings. The MCP
contract never exposes these routing details to agents — tools round-trip
the opaque handle and the helper API resolves it back to concrete rows.

Cloud's x402 run refs live in ``validibot_cloud.agents.refs`` because they
carry the payer wallet address instead of an org slug. Both formats share
the ``run_`` prefix but decode to different payload shapes; the ``kind``
field inside the payload disambiguates.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow

WORKFLOW_REF_PREFIX = "wf_"
RUN_REF_PREFIX = "run_"
RUN_REF_MEMBER_KIND = "member"

# This codec remains here while Community and Cloud compatibility adapters share
# it. Embedded MCP uses encrypted references from ``validibot.mcp_server``.


def build_workflow_ref(workflow: Workflow) -> str:
    """Return a stable MCP workflow reference for a workflow family.

    The reference is anchored to ``(org.slug, workflow.slug)`` rather than the
    workflow row UUID so it remains stable when a new workflow version becomes
    the latest active member of the family.
    """

    payload = {
        "org": workflow.org.slug,
        "workflow": workflow.slug,
    }
    encoded = _encode_ref_payload(payload)
    return f"{WORKFLOW_REF_PREFIX}{encoded}"


def parse_workflow_ref(workflow_ref: str) -> tuple[str, str]:
    """Decode a workflow reference back to ``(org_slug, workflow_slug)``."""

    if not workflow_ref.startswith(WORKFLOW_REF_PREFIX):
        msg = "Workflow reference must start with 'wf_'."
        raise ValueError(msg)

    payload = _decode_ref_payload(workflow_ref.removeprefix(WORKFLOW_REF_PREFIX))
    org_slug = str(payload.get("org", "")).strip()
    workflow_slug = str(payload.get("workflow", "")).strip()
    if not org_slug or not workflow_slug:
        msg = "Workflow reference is missing org or workflow data."
        raise ValueError(msg)
    return org_slug, workflow_slug


def build_member_run_ref(*, org_slug: str, run_id: str) -> str:
    """Return the opaque run reference for a member-access validation run."""

    payload = {
        "kind": RUN_REF_MEMBER_KIND,
        "org": org_slug,
        "run_id": run_id,
    }
    encoded = _encode_ref_payload(payload)
    return f"{RUN_REF_PREFIX}{encoded}"


def parse_member_run_ref(run_ref: str) -> tuple[str, str]:
    """Decode a member ``run_ref`` back to ``(org_slug, run_id)``."""

    if not run_ref.startswith(RUN_REF_PREFIX):
        msg = "Run reference must start with 'run_'."
        raise ValueError(msg)

    payload = _decode_ref_payload(run_ref.removeprefix(RUN_REF_PREFIX))
    run_kind = str(payload.get("kind", "")).strip()
    org_slug = str(payload.get("org", "")).strip()
    run_id = str(payload.get("run_id", "")).strip()
    if run_kind != RUN_REF_MEMBER_KIND:
        msg = "Run reference is not a member-access run."
        raise ValueError(msg)
    if not org_slug or not run_id:
        msg = "Run reference is missing org or run data."
        raise ValueError(msg)
    return org_slug, run_id


def _encode_ref_payload(payload: dict[str, str]) -> str:
    """Encode a small JSON payload as URL-safe base64 without padding."""

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_ref_payload(encoded_payload: str) -> dict[str, object]:
    """Decode a URL-safe base64 JSON payload used in MCP references."""

    try:
        padding = "=" * (-len(encoded_payload) % 4)
        raw = base64.urlsafe_b64decode(f"{encoded_payload}{padding}".encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        msg = "Reference payload is not valid base64url JSON."
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = "Reference payload must decode to a JSON object."
        raise TypeError(msg)
    return payload
