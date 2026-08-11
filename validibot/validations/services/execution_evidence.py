"""Build URI-free evidence snapshots from strict validator input envelopes.

The input envelope is the authoritative launch contract, but it also contains
private storage URIs and, for asynchronous execution, a raw callback nonce.
Persisting the envelope wholesale would retain both. This module instead
projects only the evidence-safe fields required by ADR-2026-07-10: the strict
file identities, attempt-contract version, and explicit relationships between
original source bytes and the bytes sent across the execution boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from validibot_shared.evidence.execution import ManifestExecutionInput
from validibot_shared.evidence.execution import ManifestInputRelationship

if TYPE_CHECKING:
    from validibot_shared.validations.envelopes import ValidationInputEnvelope

    from validibot.submissions.models import Submission
    from validibot.workflows.models import WorkflowStep

_PRIMARY_INPUT_PORT_KEYS = frozenset(
    {
        "data_graph",
        "pdf_document",
        "portfolio_manager_report",
        "primary_model",
        "xml_document",
    },
)
_SHA256_HEX_LENGTH = 64
_TEMPLATE_TRANSFORMATION = "energyplus-template-substitution.v1"
_TEXT_MATERIALIZATION_TRANSFORMATION = "submission-text-materialization.v1"


def build_input_evidence_snapshot(
    envelope: ValidationInputEnvelope,
    *,
    submission: Submission,
    step: WorkflowStep,
) -> dict[str, Any]:
    """Return a JSON-safe, URI-free projection of one committed input envelope.

    Args:
        envelope: The validated input envelope about to be committed and launched.
        submission: The run submission. Persisted metadata supplies the original
            digest even when preprocessing replaced in-memory content.
        step: The workflow step, used to identify trusted template resources.

    Returns:
        A JSON-safe dictionary suitable for ``ExecutionAttempt`` storage.
    """
    input_files = [
        _input_file_record(item).model_dump(mode="json")
        for item in envelope.input_files
    ]
    resource_files = [
        _resource_file_record(item).model_dump(mode="json")
        for item in envelope.resource_files
    ]
    all_files = input_files + resource_files
    relationships = _build_input_relationships(
        input_files=input_files,
        submission=submission,
        step=step,
    )
    return {
        "attempt_contract_version": str(envelope.context.attempt_contract_version),
        "input_files": all_files,
        "input_relationships": [
            relationship.model_dump(mode="json") for relationship in relationships
        ],
    }


def _input_file_record(item) -> ManifestExecutionInput:
    """Project one submitted/generated input item without its storage URI."""
    return ManifestExecutionInput(
        channel="input_files",
        name=item.name,
        role=item.role or "",
        port_key=item.port_key,
        media_type=_enum_value(item.mime_type),
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        storage_version=item.storage_version,
    )


def _resource_file_record(item) -> ManifestExecutionInput:
    """Project one workflow resource item without its storage URI."""
    return ManifestExecutionInput(
        channel="resource_files",
        name=item.name,
        resource_type=item.type,
        port_key=item.port_key,
        resource_id=item.id,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        storage_version=item.storage_version,
    )


def _build_input_relationships(
    *,
    input_files: list[dict[str, Any]],
    submission: Submission,
    step: WorkflowStep,
) -> list[ManifestInputRelationship]:
    """Describe how original submission/template bytes relate to execution."""
    source = _persisted_submission_metadata(submission)
    source_sha256 = str(source.get("checksum_sha256") or "")
    target = _select_primary_input(input_files, source_sha256)
    if target is None or not _is_sha256(source_sha256):
        return []

    template_source = _template_source_metadata(step)
    source_matches_target = source_sha256 == target["sha256"]
    if source_matches_target:
        relationship = "identical"
        transformation = ""
    else:
        relationship = "transformed"
        transformation = (
            _TEMPLATE_TRANSFORMATION
            if template_source is not None
            else _TEXT_MATERIALIZATION_TRANSFORMATION
        )

    relationships = [
        ManifestInputRelationship(
            source_kind="submission",
            source_id=str(submission.pk),
            source_name=str(source.get("original_filename") or ""),
            source_size_bytes=_nonnegative_int_or_none(source.get("size_bytes")),
            source_sha256=source_sha256,
            target_channel="input_files",
            target_name=str(target["name"]),
            target_port_key=str(target.get("port_key") or ""),
            target_sha256=str(target["sha256"]),
            relationship=relationship,
            transformation=transformation,
        ),
    ]

    if template_source is not None:
        relationships.append(
            ManifestInputRelationship(
                source_kind="workflow_resource",
                source_id=template_source["source_id"],
                source_name=template_source["source_name"],
                source_size_bytes=template_source["source_size_bytes"],
                source_sha256=template_source["source_sha256"],
                target_channel="input_files",
                target_name=str(target["name"]),
                target_port_key=str(target.get("port_key") or ""),
                target_sha256=str(target["sha256"]),
                relationship="transformed",
                transformation=_TEMPLATE_TRANSFORMATION,
            ),
        )
    return relationships


def _persisted_submission_metadata(submission: Submission) -> dict[str, Any]:
    """Read original metadata rather than preprocessing-mutated object fields."""
    if submission.pk:
        persisted = (
            submission.__class__.objects.filter(pk=submission.pk)
            .values("checksum_sha256", "original_filename", "size_bytes")
            .first()
        )
        if persisted is not None:
            return persisted
    return {
        "checksum_sha256": submission.checksum_sha256,
        "original_filename": submission.original_filename,
        "size_bytes": submission.size_bytes,
    }


def _select_primary_input(
    input_files: list[dict[str, Any]],
    submission_sha256: str,
) -> dict[str, Any] | None:
    """Select the envelope item representing the primary submitted content."""
    primary = [
        item for item in input_files if item.get("port_key") in _PRIMARY_INPUT_PORT_KEYS
    ]
    for item in primary:
        if item.get("sha256") == submission_sha256:
            return item
    if len(primary) == 1:
        return primary[0]
    return None


def _template_source_metadata(step: WorkflowStep) -> dict[str, Any] | None:
    """Return immutable identity for an EnergyPlus template resource, if any."""
    from validibot.workflows.models import WorkflowStepResource

    resource = (
        step.step_resources.select_related("validator_resource_file")
        .filter(role=WorkflowStepResource.MODEL_TEMPLATE)
        .first()
    )
    if resource is None:
        return None

    if resource.is_catalog_reference:
        catalog = resource.validator_resource_file
        source_sha256 = catalog.content_hash
        source_name = catalog.filename
        source_file = catalog.file
    else:
        source_sha256 = resource.content_hash
        source_name = resource.filename
        source_file = resource.step_resource_file
    if not _is_sha256(source_sha256):
        return None
    return {
        "source_id": str(resource.pk),
        "source_name": source_name or "",
        "source_size_bytes": _field_file_size(source_file),
        "source_sha256": source_sha256,
    }


def _field_file_size(field_file) -> int | None:
    """Return stored file size when the storage backend can provide it."""
    try:
        return _nonnegative_int_or_none(field_file.size)
    except (FileNotFoundError, OSError, ValueError):
        return None


def _nonnegative_int_or_none(value: object) -> int | None:
    """Coerce a durable size value while rejecting missing/negative values."""
    if value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _is_sha256(value: str) -> bool:
    """Return whether a value is lowercase hexadecimal SHA-256."""
    return len(value) == _SHA256_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def _enum_value(value: object) -> str:
    """Return an enum's wire value without coupling to its concrete type."""
    return str(getattr(value, "value", value))
