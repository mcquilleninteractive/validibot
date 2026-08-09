"""Resolve declared validator file inputs for every execution path.

The immutable submission and earlier run artifacts remain separate evidence
objects. This service resolves either source into one bounded descriptor whose
bytes and identity have been verified. In-process validators consume the bytes;
isolated adapters can consume the same descriptor's identity fields when they
build backend envelopes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import StepIODirection
from validibot.validations.models import Artifact
from validibot.validations.models import ResolvedInputTrace
from validibot.validations.models import StepInputBinding
from validibot.validations.services import artifact_ports
from validibot.validations.services.artifact_bindings import effective_artifact_ports
from validibot.validations.services.artifacts import build_artifact_ref
from validibot.validations.services.file_identity import FileIdentity

DEFAULT_IN_PROCESS_FILE_LIMIT_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResolvedFileInput:
    """One exact file supplied to a validator input contract."""

    contract_key: str
    name: str
    source_scope: str
    data_format: str
    media_type: str
    identity: FileIdentity
    content: bytes
    artifact_id: str = ""
    producer_step_key: str = ""
    producer_output_key: str = ""


def resolve_file_inputs(
    *,
    run,
    step,
    step_run=None,
    max_bytes: int | None = None,
) -> dict[str, ResolvedFileInput]:
    """Resolve and verify every declared singleton artifact input on ``step``."""
    byte_limit = max_bytes or int(
        getattr(
            settings,
            "IN_PROCESS_ARTIFACT_INPUT_MAX_BYTES",
            DEFAULT_IN_PROCESS_FILE_LIMIT_BYTES,
        )
    )
    queryset = StepInputBinding.objects.filter(workflow_step=step).select_related(
        "io_definition",
        "source_step",
        "source_output_io_definition",
    )
    bindings = {binding.io_definition_id: binding for binding in queryset}
    resolved: dict[str, ResolvedFileInput] = {}
    errors: list[str] = []
    traces: list[ResolvedInputTrace] = []

    for port in effective_artifact_ports(step, direction=StepIODirection.INPUT):
        binding = bindings.get(port.pk)
        if binding is None:
            if port.min_items:
                errors.append(
                    f"Required artifact port '{port.contract_key}' has no binding."
                )
            continue
        try:
            artifact_ports.validate_source_scope(port, binding.source_scope)
            if binding.source_scope == BindingSourceScope.SUBMISSION_FILE:
                item = _resolve_submission(
                    run=run,
                    port=port,
                    binding=binding,
                    max_bytes=byte_limit,
                )
            elif binding.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT:
                item = _resolve_upstream_artifact(
                    run=run,
                    step=step,
                    port=port,
                    binding=binding,
                    max_bytes=byte_limit,
                )
            else:
                errors.append(
                    f"File source '{binding.source_scope}' is not available to "
                    "an in-process validator."
                )
                continue
            resolved[port.contract_key] = item
            if step_run is not None:
                traces.append(
                    _trace(
                        step_run=step_run,
                        port=port,
                        binding=binding,
                        resolved=True,
                        value_snapshot={
                            "name": item.name,
                            "source": item.source_scope,
                            "size_bytes": item.identity.size_bytes,
                            "sha256": item.identity.sha256,
                            "storage_version": item.identity.storage_version,
                            "artifact_id": item.artifact_id,
                            "producer_step_key": item.producer_step_key,
                            "producer_output_key": item.producer_output_key,
                        },
                    )
                )
        except ValueError as exc:
            errors.append(str(exc))
            if step_run is not None:
                traces.append(
                    _trace(
                        step_run=step_run,
                        port=port,
                        binding=binding,
                        resolved=False,
                        error_message=str(exc),
                    )
                )

    if traces:
        ResolvedInputTrace.objects.bulk_create(traces)
    if errors:
        raise ValueError("; ".join(errors))
    return resolved


def _resolve_submission(*, run, port, binding, max_bytes: int) -> ResolvedFileInput:
    """Resolve the immutable primary submission through a file port."""
    if binding.source_data_path != "primary":
        raise ValueError(
            f"In-process port '{port.contract_key}' only supports the primary "
            "submitted file."
        )
    submission = run.submission
    content = submission.read_bytes(max_bytes=max_bytes)
    if not content:
        raise ValueError(f"Submitted file for '{port.contract_key}' is unavailable.")
    sha256 = hashlib.sha256(content).hexdigest()
    if submission.size_bytes and submission.size_bytes != len(content):
        raise ValueError("Submitted file size no longer matches its stored identity.")
    if submission.checksum_sha256 and submission.checksum_sha256 != sha256:
        raise ValueError("Submitted file hash no longer matches its stored identity.")
    name = Path(submission.original_filename or "submission").name
    artifact_ports.validate_file_uri(port=port, uri=name)
    identity = FileIdentity(
        uri=_submission_file_uri(submission),
        size_bytes=len(content),
        sha256=sha256,
        storage_version=f"sha256:{sha256}",
    )
    return ResolvedFileInput(
        contract_key=port.contract_key,
        name=name,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        data_format=str(port.data_format or ""),
        media_type=str(port.media_type or ""),
        identity=identity,
        content=content,
    )


def _resolve_upstream_artifact(
    *,
    run,
    step,
    port,
    binding,
    max_bytes: int,
) -> ResolvedFileInput:
    """Resolve one protected upstream relation to its exact run artifact."""
    source_step = binding.source_step
    source_output = binding.source_output_io_definition
    if source_step is None or source_output is None:
        raise ValueError(f"Artifact port '{port.contract_key}' has no producer.")
    if source_step.workflow_id != step.workflow_id or source_step.order >= step.order:
        raise ValueError(
            "The artifact producer is not an earlier step in this workflow."
        )

    try:
        artifact = Artifact.objects.select_related("step_run", "workflow_step").get(
            validation_run=run,
            workflow_step=source_step,
            contract_key=source_output.contract_key,
            item_key="",
        )
    except Artifact.DoesNotExist as exc:
        raise ValueError(
            f"Earlier step '{source_step.name}' did not produce "
            f"'{source_output.contract_key}'."
        ) from exc

    ref = build_artifact_ref(artifact).model_dump(mode="json")
    artifact_ports.validate_artifact_ref(port=port, artifact_ref=ref)
    content = _read_artifact_bytes(artifact=artifact, max_bytes=max_bytes)
    sha256 = hashlib.sha256(content).hexdigest()
    if len(content) != artifact.size_bytes or sha256 != artifact.sha256:
        raise ValueError("Upstream artifact bytes do not match their trusted identity.")
    identity = FileIdentity.from_artifact_ref(ref)
    return ResolvedFileInput(
        contract_key=port.contract_key,
        name=Path(artifact.label or ref["name"]).name,
        source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        data_format=artifact.data_format,
        media_type=artifact.content_type,
        identity=identity,
        content=content,
        artifact_id=str(artifact.pk),
        producer_step_key=source_step.step_key,
        producer_output_key=source_output.contract_key,
    )


def _read_artifact_bytes(*, artifact: Artifact, max_bytes: int) -> bytes:
    """Read bounded local, Django-storage, or generation-pinned GCS bytes."""
    if artifact.size_bytes > max_bytes:
        raise ValueError(
            f"Artifact is {artifact.size_bytes} bytes; limit is {max_bytes} bytes."
        )
    if artifact.file and artifact.file.name:
        with artifact.file.open("rb") as source:
            data = source.read(max_bytes + 1)
    else:
        parsed = urlparse(artifact.storage_uri or "")
        if parsed.scheme == "file":
            from validibot.validations.services.artifact_display import (
                resolve_local_artifact_path,
            )

            path = resolve_local_artifact_path(artifact)
            if path is None:
                raise ValueError(
                    "Local artifact path is outside its attempt workspace."
                )
            with path.open("rb") as source:
                data = source.read(max_bytes + 1)
        elif parsed.scheme == "gs":
            from validibot.validations.services.cloud_run.gcs_client import (
                download_bytes_generation,
            )

            data = download_bytes_generation(
                uri=artifact.storage_uri,
                storage_version=artifact.storage_version,
                max_bytes=max_bytes,
            )
        else:
            raise ValueError("Artifact bytes are unavailable to this validator.")
    if len(data) > max_bytes:
        raise ValueError(f"Artifact exceeds the {max_bytes}-byte input limit.")
    return data


def _submission_file_uri(submission) -> str:
    """Return a stable URI without assuming the storage exposes ``url``."""
    try:
        uri = submission.input_file.url
    except (AttributeError, NotImplementedError, ValueError):
        uri = ""
    return str(uri or f"submission:{submission.pk}")


def _trace(
    *,
    step_run,
    port,
    binding,
    resolved: bool,
    value_snapshot=None,
    error_message: str = "",
) -> ResolvedInputTrace:
    """Build one immutable diagnostic trace for a file resolution decision."""
    return ResolvedInputTrace(
        step_run=step_run,
        io_definition=port,
        input_contract_key=port.contract_key,
        source_scope_used=binding.source_scope,
        source_data_path_used=binding.source_data_path,
        upstream_step_key=(
            binding.source_step.step_key if binding.source_step_id else ""
        ),
        resolved=resolved,
        used_default=False,
        value_snapshot=value_snapshot,
        error_message=error_message,
    )
