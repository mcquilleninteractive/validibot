"""Resolve declared validator file inputs for every execution path.

The immutable submission, earlier run artifacts, and managed resources remain
separate evidence objects. This service turns any of those sources into the
same read-only descriptor. In-process validators ask it to verify and return
bounded bytes; isolated adapters pass their container-visible identities and
use the descriptor to build backend envelopes without reading large files into
the Django process.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Collection
    from collections.abc import Mapping

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
    content: bytes | None
    source_data_path: str = ""
    role: str = ""
    envelope_channel: str = ""
    artifact_id: str = ""
    producer_step_key: str = ""
    producer_output_key: str = ""
    resource_id: str = ""
    resource_type: str = ""


def resolve_file_inputs(
    *,
    run,
    step,
    step_run=None,
    max_bytes: int | None = None,
    load_content: bool = True,
    materialized_file_identities: Mapping[str, FileIdentity] | None = None,
    resource_identity_overrides: Mapping[str, FileIdentity] | None = None,
    system_file_identities: Mapping[str, FileIdentity] | None = None,
    contract_keys: Collection[str] | None = None,
) -> dict[str, ResolvedFileInput]:
    """Resolve every selected singleton artifact input on ``step``.

    ``load_content`` separates the two execution modes without creating two
    source resolvers. Inline validators keep the default and receive verified,
    bounded bytes. Isolated adapters set it to ``False`` and provide immutable
    identities for files already materialized into an attempt workspace.

    ``contract_keys`` narrows resolution for adapters whose domain configuration
    deliberately disables an optional port, such as EnergyPlus preflight mode.
    Required-port checks still apply to every selected contract.
    """
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

    selected_contracts = set(contract_keys) if contract_keys is not None else None
    for port in effective_artifact_ports(step, direction=StepIODirection.INPUT):
        if (
            selected_contracts is not None
            and port.contract_key not in selected_contracts
        ):
            continue
        binding = bindings.get(port.pk)
        if binding is None:
            if port.min_items:
                errors.append(
                    f"Required artifact port '{port.contract_key}' has no binding."
                )
            continue
        try:
            artifact_ports.validate_source_scope(port, binding.source_scope)
            item: ResolvedFileInput | None
            if binding.source_scope == BindingSourceScope.SUBMISSION_FILE:
                item = _resolve_submission(
                    run=run,
                    port=port,
                    binding=binding,
                    max_bytes=byte_limit,
                    load_content=load_content,
                    materialized_file_identities=materialized_file_identities,
                )
            elif binding.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT:
                item = _resolve_upstream_artifact(
                    run=run,
                    step=step,
                    port=port,
                    binding=binding,
                    max_bytes=byte_limit,
                    load_content=load_content,
                )
            elif binding.source_scope == BindingSourceScope.WORKFLOW_RESOURCE:
                item = _resolve_workflow_resource(
                    step=step,
                    port=port,
                    binding=binding,
                    max_bytes=byte_limit,
                    load_content=load_content,
                    resource_identity_overrides=resource_identity_overrides,
                    materialized_file_identities=materialized_file_identities,
                )
            elif binding.source_scope == BindingSourceScope.SYSTEM:
                item = _resolve_system_file(
                    port=port,
                    binding=binding,
                    load_content=load_content,
                    system_file_identities=system_file_identities,
                )
            else:
                errors.append(
                    f"File source '{binding.source_scope}' cannot be materialized "
                    f"for artifact port '{port.contract_key}'."
                )
                continue
            if item is None:
                continue
            resolved[port.contract_key] = item
            if step_run is not None:
                traces.append(
                    _trace(
                        step_run=step_run,
                        port=port,
                        binding=binding,
                        resolved=True,
                        value_snapshot=_trace_value_snapshot(item),
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


def _trace_value_snapshot(item: ResolvedFileInput) -> dict[str, object]:
    """Describe the resolved evidence without unrelated empty source fields."""
    snapshot: dict[str, object] = {
        "name": item.name,
        "source": item.source_scope,
        "size_bytes": item.identity.size_bytes,
        "sha256": item.identity.sha256,
        "storage_version": item.identity.storage_version,
    }
    if item.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT:
        snapshot.update(
            artifact_id=item.artifact_id,
            producer_step_key=item.producer_step_key,
            producer_output_key=item.producer_output_key,
        )
    elif item.source_scope == BindingSourceScope.WORKFLOW_RESOURCE:
        snapshot.update(
            resource_id=item.resource_id,
            resource_type=item.resource_type,
        )
    return snapshot


def _resolve_submission(
    *,
    run,
    port,
    binding,
    max_bytes: int,
    load_content: bool,
    materialized_file_identities: Mapping[str, FileIdentity] | None,
) -> ResolvedFileInput:
    """Resolve the immutable primary submission through a file port."""
    if load_content and binding.source_data_path != "primary":
        raise ValueError(
            f"In-process port '{port.contract_key}' only supports the primary "
            "submitted file."
        )
    if load_content:
        submission = run.submission
        content = submission.read_bytes(max_bytes=max_bytes)
        if not content:
            raise ValueError(
                f"Submitted file for '{port.contract_key}' is unavailable."
            )
        sha256 = hashlib.sha256(content).hexdigest()
        if submission.size_bytes and submission.size_bytes != len(content):
            raise ValueError(
                "Submitted file size no longer matches its stored identity."
            )
        if submission.checksum_sha256 and submission.checksum_sha256 != sha256:
            raise ValueError(
                "Submitted file hash no longer matches its stored identity."
            )
        name = Path(submission.original_filename or "submission").name
        identity = FileIdentity(
            uri=_submission_file_uri(submission),
            size_bytes=len(content),
            sha256=sha256,
            storage_version=f"sha256:{sha256}",
        )
    else:
        identity = _materialized_submission_identity(
            port=port,
            binding=binding,
            identities=materialized_file_identities,
        )
        content = None
        name = _filename_from_uri(identity.uri) or "submission"
    # Inline submissions can have an opaque evidence URI (``submission:<uuid>``)
    # because no storage object exists.  The sanitized original filename is the
    # carrier identity in that case and is therefore what the extension
    # contract must validate.  Materialized execution paths still validate the
    # concrete workspace/cloud URI supplied by the adapter.
    validation_uri = name if load_content else identity.uri or name
    artifact_ports.validate_file_uri(port=port, uri=validation_uri)
    return ResolvedFileInput(
        contract_key=port.contract_key,
        name=name,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        data_format=str(port.data_format or ""),
        media_type=str(port.media_type or ""),
        identity=identity,
        content=content,
        source_data_path=binding.source_data_path,
        role=str(port.role or ""),
        envelope_channel=str(port.envelope_channel or ""),
    )


def _resolve_upstream_artifact(
    *,
    run,
    step,
    port,
    binding,
    max_bytes: int,
    load_content: bool,
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
    content = None
    if load_content:
        content = _read_artifact_bytes(artifact=artifact, max_bytes=max_bytes)
        sha256 = hashlib.sha256(content).hexdigest()
        if len(content) != artifact.size_bytes or sha256 != artifact.sha256:
            raise ValueError(
                "Upstream artifact bytes do not match their trusted identity."
            )
    identity = FileIdentity.from_artifact_ref(ref)
    return ResolvedFileInput(
        contract_key=port.contract_key,
        name=Path(artifact.label or ref["name"]).name,
        source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        data_format=artifact.data_format,
        media_type=artifact.content_type,
        identity=identity,
        content=content,
        source_data_path=binding.source_data_path,
        role=str(port.role or ""),
        envelope_channel=str(port.envelope_channel or ""),
        artifact_id=str(artifact.pk),
        producer_step_key=source_step.step_key,
        producer_output_key=source_output.contract_key,
    )


def _resolve_workflow_resource(
    *,
    step,
    port,
    binding,
    max_bytes: int,
    load_content: bool,
    resource_identity_overrides: Mapping[str, FileIdentity] | None,
    materialized_file_identities: Mapping[str, FileIdentity] | None,
) -> ResolvedFileInput | None:
    """Resolve one managed or step-owned resource selected for a file port."""
    expected_type = binding.source_data_path or port.resource_type or port.data_format
    resource_rows = list(
        step.step_resources.select_related("validator_resource_file"),
    )
    matches = [
        row for row in resource_rows if _step_resource_metadata(row)[1] == expected_type
    ]
    artifact_ports.validate_cardinality(
        port=port,
        count=len(matches),
        source_description=(
            f"workflow resource type '{expected_type}' on step {step.id}"
        ),
    )
    if not matches:
        return None

    resource = matches[0]
    resource_id, resource_type, name = _step_resource_metadata(resource)
    identity = _materialized_contract_identity(
        port=port,
        identities=materialized_file_identities,
    )
    if identity is None:
        identity = (resource_identity_overrides or {}).get(resource_id)
    if identity is None:
        identity = _stored_workflow_resource_identity(resource)
    artifact_ports.validate_file_uri(port=port, uri=identity.uri or name)

    content = None
    if load_content:
        content = _read_workflow_resource_bytes(resource=resource, max_bytes=max_bytes)
        sha256 = hashlib.sha256(content).hexdigest()
        if len(content) != identity.size_bytes or sha256 != identity.sha256:
            raise ValueError(
                "Workflow resource bytes do not match their trusted identity."
            )

    return ResolvedFileInput(
        contract_key=port.contract_key,
        name=Path(name).name,
        source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
        source_data_path=binding.source_data_path,
        data_format=str(port.data_format or resource_type),
        media_type=str(port.media_type or ""),
        role=str(port.role or ""),
        envelope_channel=str(port.envelope_channel or ""),
        identity=identity,
        content=content,
        resource_id=resource_id,
        resource_type=resource_type,
    )


def _resolve_system_file(
    *,
    port,
    binding,
    load_content: bool,
    system_file_identities: Mapping[str, FileIdentity] | None,
) -> ResolvedFileInput:
    """Resolve a system-owned file whose identity is supplied by its adapter."""
    identity = (system_file_identities or {}).get(port.contract_key)
    if identity is None:
        raise ValueError(
            f"System file for artifact port '{port.contract_key}' is unavailable."
        )
    if load_content:
        raise ValueError(
            f"System file source for '{port.contract_key}' has no in-process "
            "byte reader."
        )
    artifact_ports.validate_file_uri(port=port, uri=identity.uri)
    return ResolvedFileInput(
        contract_key=port.contract_key,
        name=_filename_from_uri(identity.uri) or port.contract_key,
        source_scope=BindingSourceScope.SYSTEM,
        source_data_path=binding.source_data_path,
        data_format=str(port.data_format or ""),
        media_type=str(port.media_type or ""),
        role=str(port.role or ""),
        envelope_channel=str(port.envelope_channel or ""),
        identity=identity,
        content=None,
    )


def _materialized_submission_identity(
    *,
    port,
    binding,
    identities: Mapping[str, FileIdentity] | None,
) -> FileIdentity:
    """Choose a staged submission identity from generic contract metadata."""
    candidates = [
        binding.source_data_path,
        port.role,
        port.contract_key,
        f"{port.contract_key}_uri",
    ]
    if binding.source_data_path == "primary":
        candidates.append("primary_file_uri")
    if identities is not None:
        for key in candidates:
            if not key:
                continue
            identity = identities.get(key)
            if identity is not None:
                return identity
    raise ValueError(
        f"Required artifact port '{port.contract_key}' could not resolve a "
        "submitted file identity from runtime materialization keys "
        f"{', '.join(key for key in candidates if key)}."
    )


def _materialized_contract_identity(
    *,
    port,
    identities: Mapping[str, FileIdentity] | None,
) -> FileIdentity | None:
    """Return an adapter-staged identity for a non-submission contract, if any."""
    if identities is not None:
        for key in (port.contract_key, f"{port.contract_key}_uri"):
            identity = identities.get(key)
            if identity is not None:
                return identity
    return None


def _step_resource_metadata(step_resource) -> tuple[str, str, str]:
    """Return a workflow resource's stable ID, type, and display filename."""
    if step_resource.is_catalog_reference:
        resource = step_resource.validator_resource_file
        return str(resource.id), resource.resource_type, resource.filename
    return (
        str(step_resource.pk),
        step_resource.resource_type,
        step_resource.filename or Path(step_resource.step_resource_file.name).name,
    )


def _stored_workflow_resource_identity(step_resource) -> FileIdentity:
    """Resolve and verify the durable identity of a managed workflow resource."""
    from urllib.parse import unquote

    from validibot.validations.services.file_identity import local_file_identity

    expected_sha256 = (
        step_resource.validator_resource_file.content_hash
        if step_resource.is_catalog_reference
        else step_resource.content_hash
    )
    uri = step_resource.get_storage_uri()
    if uri.startswith("gs://"):
        from validibot.validations.services.cloud_run.gcs_client import (
            get_gcs_file_identity,
        )

        return get_gcs_file_identity(uri=uri, sha256=expected_sha256)
    if uri.startswith("file://"):
        identity = local_file_identity(
            path=Path(unquote(urlparse(uri).path)),
            uri=uri,
        )
        if expected_sha256 and identity.sha256 != expected_sha256:
            raise ValueError(
                f"Managed resource bytes no longer match their stored digest: {uri}"
            )
        return identity
    raise ValueError(f"Unsupported managed-resource URI for immutable input: {uri}")


def _read_workflow_resource_bytes(*, resource, max_bytes: int) -> bytes:
    """Read a workflow resource through its authoritative Django file field."""
    field_file = (
        resource.validator_resource_file.file
        if resource.is_catalog_reference
        else resource.step_resource_file
    )
    with field_file.open("rb") as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"Resource exceeds the {max_bytes}-byte input limit.")
    return data


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


def _filename_from_uri(uri: str) -> str:
    """Return a safe basename from a local, cloud, or opaque storage URI."""
    parsed = urlparse(uri)
    return Path(parsed.path or uri).name


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
