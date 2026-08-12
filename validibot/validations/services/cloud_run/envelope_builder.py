"""
Envelope builder for creating typed validation input envelopes.

This module provides functions to build domain-specific input envelopes
(EnergyPlusInputEnvelope, FMUInputEnvelope, etc.) from Django model instances.

Design: Simple factory functions, not classes. Each validator type gets its own
builder function. This keeps the code straightforward and easy to test.
"""

import json
import logging
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote
from urllib.parse import urlparse

from validibot_shared.energyplus import EnergyPlusIdfCheck
from validibot_shared.energyplus import EnergyPlusReviewProfile
from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope
from validibot_shared.energyplus.envelopes import EnergyPlusInputs
from validibot_shared.fmu.envelopes import FMUInputEnvelope
from validibot_shared.fmu.envelopes import FMUInputs
from validibot_shared.fmu.envelopes import FMUSimulationConfig
from validibot_shared.pdf import PDF_STATIC_TEXT_PROFILE
from validibot_shared.pdf import PdfInputEnvelope
from validibot_shared.pdf import PdfInputs
from validibot_shared.pdf import PdfProcessingLimits
from validibot_shared.portfolio_manager import PortfolioManagerInputs
from validibot_shared.portfolio_manager import build_portfolio_manager_input_envelope
from validibot_shared.portfolio_manager import mime_type_for_portfolio_manager_filename
from validibot_shared.shacl.envelopes import build_shacl_input_envelope
from validibot_shared.shacl.envelopes import mime_type_for_rdf_format
from validibot_shared.validations.envelopes import ATTEMPT_CONTRACT_VERSION
from validibot_shared.validations.envelopes import ExecutionContext
from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import OrganizationInfo
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import SupportedMimeType
from validibot_shared.validations.envelopes import ValidationInputEnvelope
from validibot_shared.validations.envelopes import ValidatorInfo
from validibot_shared.validations.envelopes import ValidatorType
from validibot_shared.validations.envelopes import WorkflowInfo

from validibot.validations.constants import PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import ValidationType
from validibot.validations.services import artifact_ports
from validibot.validations.services.file_identity import FileIdentity
from validibot.validations.services.file_identity import local_file_identity
from validibot.validations.services.resolved_files import resolve_file_inputs

logger = logging.getLogger(__name__)


class ValidatorLike(Protocol):
    """Protocol for validator-like objects (duck typing for easier testing)."""

    id: str
    validation_type: str
    version: int | str


def build_energyplus_input_envelope(
    *,
    run_id: str,
    validator: ValidatorLike,
    org_id: str,
    org_name: str,
    workflow_id: str,
    step_id: str,
    step_name: str | None,
    model_file: FileIdentity | None,
    resource_files: list[ResourceFileItem],
    callback_url: str,
    callback_id: str | None,
    execution_bundle_uri: str,
    execution_attempt_id: str,
    step_run_id: str,
    expected_output_uri: str,
    timestep_per_hour: int = 4,
    idf_checks: list[EnergyPlusIdfCheck] | None = None,
    run_simulation: bool = True,
    review_profile: EnergyPlusReviewProfile = "standard",
    skip_callback: bool = False,
    input_files: list[InputFileItem] | None = None,
    callback_nonce: str | None = None,
    callback_nonce_commitment: str | None = None,
) -> EnergyPlusInputEnvelope:
    """
    Build an EnergyPlusInputEnvelope from Django validation run data.

    This function creates a fully typed input envelope for EnergyPlus validators.
    It takes Django model data and transforms it into the container input format.

    The validator always returns a fixed set of output values defined in its
    catalog - users don't need to specify which outputs they want.

    Args:
        run_id: Validation run UUID
        validator: Validator instance (or validator-like object)
        org_id: Organization UUID
        org_name: Organization name (for logging)
        workflow_id: Workflow UUID
        step_id: Workflow step UUID
        step_name: Human-readable step name
        model_file: Immutable identity of the IDF/epJSON file.
            The file extension determines the envelope metadata: ``.idf`` URIs
            produce ``name="model.idf"`` with ``mime_type=ENERGYPLUS_IDF``;
            ``.epjson`` URIs produce ``name="model.epjson"`` with
            ``mime_type=ENERGYPLUS_EPJSON``.  The runner uses the ``name`` field
            to determine the local filename when downloading, and EnergyPlus
            uses the extension to decide IDF vs epJSON parsing mode.
        resource_files: List of ResourceFileItem objects (weather files, etc.)
        callback_url: Django endpoint to POST results
        callback_id: Unique identifier for idempotent callback processing
        callback_nonce: Per-attempt secret echoed only in the callback.
        callback_nonce_commitment: Public commitment included in canonical
            input-envelope hashing.
        execution_bundle_uri: Directory URI for this run's files
        timestep_per_hour: Timesteps applied to the backend's private model copy.
        idf_checks: Validibot review checks to run before EnergyPlus.
        run_simulation: Run the full simulation; false selects preflight mode.
        review_profile: Evidence/severity profile (``standard`` or ``leed_review``).
        skip_callback: If True, container won't POST callback after completion

    Returns:
        Fully populated EnergyPlusInputEnvelope ready for storage upload

    Example:
        >>> envelope = build_energyplus_input_envelope(
        ...     run_id=str(run.id),
        ...     validator=run.validator,
        ...     org_id=str(run.org.id),
        ...     org_name=run.org.name,
        ...     workflow_id=str(run.workflow.id),
        ...     step_id=str(run.step.id),
        ...     step_name=run.step.name,
        ...     model_file=FileIdentity(
        ...         uri="gs://bucket/model.idf",
        ...         size_bytes=123,
        ...         sha256="a" * 64,
        ...         storage_version="1700000000000000",
        ...     ),
        ...     resource_files=[weather_resource],
        ...     callback_url="https://api.example.com/callbacks/",
        ...     execution_bundle_uri="gs://bucket/runs/abc-123/",
        ...     timestep_per_hour=4,
        ... )
    """
    # Build validator info
    validator_type = ValidatorType(getattr(validator, "validation_type", ""))
    validator_info = ValidatorInfo(
        id=str(validator.id),
        type=validator_type,
        version=str(validator.version),
    )

    # Build organization info
    org_info = OrganizationInfo(
        id=org_id,
        name=org_name,
    )

    # Build workflow info
    workflow_info = WorkflowInfo(
        id=workflow_id,
        step_id=step_id,
        step_name=step_name,
    )

    if input_files is None:
        if model_file is None:
            msg = "EnergyPlus envelope requires a model_file or input_files"
            raise ValueError(msg)
        input_files = [
            _build_energyplus_input_file_item("primary_model", model_file),
        ]

    # Build EnergyPlus-specific inputs
    energyplus_inputs = EnergyPlusInputs(
        timestep_per_hour=timestep_per_hour,
        idf_checks=idf_checks or [],
        run_simulation=run_simulation,
        review_profile=review_profile,
    )

    # Build execution context
    execution_context = ExecutionContext(
        callback_id=callback_id,
        callback_nonce=callback_nonce,
        callback_nonce_commitment=callback_nonce_commitment,
        callback_url=callback_url,
        execution_bundle_uri=execution_bundle_uri,
        execution_attempt_id=execution_attempt_id,
        step_run_id=step_run_id,
        attempt_contract_version=ATTEMPT_CONTRACT_VERSION,
        expected_output_uri=expected_output_uri,
        skip_callback=skip_callback,
    )

    # Build the envelope
    envelope = EnergyPlusInputEnvelope(
        run_id=run_id,
        validator=validator_info,
        org=org_info,
        workflow=workflow_info,
        input_files=input_files,
        resource_files=resource_files,
        inputs=energyplus_inputs,
        context=execution_context,
    )

    return envelope


def _build_energyplus_input_file_item(
    port_key: str,
    file: FileIdentity,
    *,
    role: str = "primary-model",
) -> InputFileItem:
    """Build an EnergyPlus ``InputFileItem`` from a resolved file-port URI."""

    lowered_uri = file.uri.lower()
    if lowered_uri.endswith((".epjson", ".json")):
        name = (
            "model.epjson" if role == "primary-model" else _filename_from_uri(file.uri)
        )
        mime_type = SupportedMimeType.ENERGYPLUS_EPJSON
    elif lowered_uri.endswith(".epw"):
        name = _filename_from_uri(file.uri) or "weather.epw"
        mime_type = SupportedMimeType.ENERGYPLUS_EPW
    else:
        name = "model.idf" if role == "primary-model" else _filename_from_uri(file.uri)
        mime_type = SupportedMimeType.ENERGYPLUS_IDF

    return InputFileItem(
        name=name or "input-file",
        mime_type=mime_type,
        role=role,
        port_key=port_key,
        **file.envelope_fields(),
    )


def _filename_from_uri(uri: str) -> str:
    """Return the final path component from a storage URI."""

    parsed = urlparse(uri)
    path = parsed.path or uri
    return Path(unquote(path)).name


def _resolve_energyplus_file_port_items(
    *,
    run,
    step,
    step_config: dict,
    input_file_uris: dict[str, FileIdentity] | None,
    resource_uri_overrides: dict[str, FileIdentity] | None,
) -> tuple[list[InputFileItem], list[ResourceFileItem]]:
    """Resolve declared EnergyPlus artifact input ports into envelope items."""

    from validibot.validations.constants import StepIODirection
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition

    ports = {
        port.contract_key: port
        for port in StepIODefinition.objects.filter(
            validator_id=step.validator_id,
            direction=StepIODirection.INPUT,
            io_medium=StepIOMedium.ARTIFACT,
        )
    }
    if not ports:
        msg = f"EnergyPlus validator {step.validator_id} has no declared file ports"
        raise ValueError(msg)

    bindings = {
        binding.io_definition.contract_key: binding
        for binding in StepInputBinding.objects.filter(
            workflow_step=step,
            io_definition__in=ports.values(),
        ).select_related("io_definition")
    }

    input_files: list[InputFileItem] = []
    resource_files: list[ResourceFileItem] = []
    for contract_key in ("primary_model", "weather_file"):
        if contract_key == "weather_file" and not step_config.get(
            "run_simulation",
            True,
        ):
            # Conversion-only preflight neither requires nor consumes weather.
            # Skipping the optional port also avoids materializing a stale/default
            # EPW that has no bearing on the result.
            continue
        port = ports.get(contract_key)
        if port is None:
            continue
        binding = bindings.get(contract_key)
        if binding is None:
            msg = (
                f"Required artifact port '{contract_key}' on step {step.id} "
                "has no StepInputBinding."
            )
            _record_artifact_input_trace(
                run=run,
                port=port,
                source_scope="",
                source_data_path="",
                resolved=False,
                error_message=msg,
            )
            raise ValueError(msg)

        try:
            artifact_ports.validate_source_scope(port, binding.source_scope)
        except ValueError as exc:
            _record_artifact_input_trace(
                run=run,
                port=port,
                source_scope=binding.source_scope,
                source_data_path=binding.source_data_path,
                resolved=False,
                error_message=str(exc),
            )
            raise
        if port.envelope_channel == EnvelopeChannel.RESOURCE_FILES:
            if binding.source_scope == BindingSourceScope.WORKFLOW_RESOURCE:
                try:
                    resolved_resources = _resolve_workflow_resource_port(
                        run=run,
                        step=step,
                        port=port,
                        binding=binding,
                        resource_uri_overrides=resource_uri_overrides,
                    )
                except ValueError as exc:
                    _record_artifact_input_trace(
                        run=run,
                        port=port,
                        source_scope=binding.source_scope,
                        source_data_path=binding.source_data_path,
                        resolved=False,
                        error_message=str(exc),
                    )
                    raise
                resource_files.extend(resolved_resources)
                _record_artifact_input_trace(
                    run=run,
                    port=port,
                    source_scope=binding.source_scope,
                    source_data_path=binding.source_data_path,
                    resolved=True,
                    value_snapshot=[
                        _resource_file_item_snapshot(item)
                        for item in resolved_resources
                    ],
                )
                continue

            identity, value_snapshot = (
                _resolve_artifact_or_submission_file_identity_with_trace(
                    run=run,
                    step=step,
                    step_config=step_config,
                    input_file_uris=input_file_uris,
                    port=port,
                    binding=binding,
                )
            )
            item = _build_energyplus_input_file_item(
                port.contract_key,
                identity,
                role=port.role or "weather",
            )
            try:
                artifact_ports.validate_input_file_item(port=port, item=item)
            except ValueError as exc:
                _record_and_raise_artifact_resolution_error(
                    run=run,
                    port=port,
                    binding=binding,
                    error_message=str(exc),
                )
            input_files.append(item)
            _record_artifact_input_trace(
                run=run,
                port=port,
                source_scope=binding.source_scope,
                source_data_path=binding.source_data_path,
                resolved=True,
                value_snapshot=value_snapshot,
            )
            continue

        identity, value_snapshot = (
            _resolve_artifact_or_submission_file_identity_with_trace(
                run=run,
                step=step,
                step_config=step_config,
                input_file_uris=input_file_uris,
                port=port,
                binding=binding,
            )
        )
        item = _build_energyplus_input_file_item(
            port.contract_key,
            identity,
            role=port.role or "primary-model",
        )
        try:
            artifact_ports.validate_input_file_item(port=port, item=item)
        except ValueError as exc:
            _record_and_raise_artifact_resolution_error(
                run=run,
                port=port,
                binding=binding,
                error_message=str(exc),
            )
        input_files.append(item)
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope=binding.source_scope,
            source_data_path=binding.source_data_path,
            resolved=True,
            value_snapshot=value_snapshot,
        )

    return input_files, resource_files


def _resolve_workflow_resource_port(
    *,
    run,
    step,
    port,
    binding,
    resource_uri_overrides: dict[str, FileIdentity] | None,
) -> list[ResourceFileItem]:
    """Resolve a workflow-resource port through the shared file descriptor."""

    resolved = resolve_file_inputs(
        run=run,
        step=step,
        load_content=False,
        resource_identity_overrides=resource_uri_overrides,
        contract_keys={port.contract_key},
    ).get(port.contract_key)
    if resolved is None:
        return []
    item = ResourceFileItem(
        id=resolved.resource_id,
        name=resolved.name,
        type=resolved.resource_type,
        port_key=resolved.contract_key,
        **resolved.identity.envelope_fields(),
    )
    artifact_ports.validate_resource_file_item(port=port, item=item)
    return [item]


def _resolve_artifact_or_submission_file_identity_with_trace(
    *,
    run,
    step,
    step_config: dict,
    input_file_uris: dict[str, FileIdentity] | None,
    port,
    binding,
) -> tuple[FileIdentity, dict]:
    """Delegate source resolution and return an envelope-safe audit snapshot."""

    del step_config  # File identity comes from bindings and attempt materialization.
    try:
        resolved = resolve_file_inputs(
            run=run,
            step=step,
            load_content=False,
            materialized_file_identities=input_file_uris,
            contract_keys={port.contract_key},
        )[port.contract_key]
    except (KeyError, ValueError) as exc:
        _record_and_raise_artifact_resolution_error(
            run=run,
            port=port,
            binding=binding,
            error_message=str(exc),
        )

    snapshot = {
        "source": resolved.source_scope,
        "port_key": resolved.contract_key,
        "role": resolved.role,
        **resolved.identity.envelope_fields(),
    }
    if resolved.artifact_id:
        snapshot.update(
            {
                "artifact_id": resolved.artifact_id,
                "producer_step_key": resolved.producer_step_key,
                "producer_output_key": resolved.producer_output_key,
            }
        )
    return resolved.identity, snapshot


def _resolve_input_file_artifact_port_item(
    *,
    run,
    step,
    step_config: dict,
    input_file_uris: dict[str, FileIdentity] | None,
    contract_key: str,
    item_builder,
) -> tuple[InputFileItem, str]:
    """Resolve one declared input-files artifact port into an envelope item."""

    from validibot.validations.constants import StepIODirection
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition

    port = (
        StepIODefinition.objects.filter(
            validator_id=step.validator_id,
            direction=StepIODirection.INPUT,
            io_medium=StepIOMedium.ARTIFACT,
            contract_key=contract_key,
        )
        .order_by("pk")
        .first()
    )
    if port is None:
        msg = (
            f"Validator {step.validator_id} has no declared artifact port "
            f"'{contract_key}'"
        )
        raise ValueError(msg)

    binding = (
        StepInputBinding.objects.filter(
            workflow_step=step,
            io_definition=port,
        )
        .select_related("io_definition")
        .first()
    )
    if binding is None:
        msg = (
            f"Required artifact port '{port.contract_key}' on step {step.id} "
            "has no StepInputBinding."
        )
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope="",
            source_data_path="",
            resolved=False,
            error_message=msg,
        )
        raise ValueError(msg)

    try:
        artifact_ports.validate_source_scope(port, binding.source_scope)
    except ValueError as exc:
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope=binding.source_scope,
            source_data_path=binding.source_data_path,
            resolved=False,
            error_message=str(exc),
        )
        raise

    identity, value_snapshot = _resolve_artifact_or_submission_file_identity_with_trace(
        run=run,
        step=step,
        step_config=step_config,
        input_file_uris=input_file_uris,
        port=port,
        binding=binding,
    )
    item = item_builder(port, identity)
    try:
        artifact_ports.validate_input_file_item(port=port, item=item)
    except ValueError as exc:
        _record_and_raise_artifact_resolution_error(
            run=run,
            port=port,
            binding=binding,
            error_message=str(exc),
        )

    _record_artifact_input_trace(
        run=run,
        port=port,
        source_scope=binding.source_scope,
        source_data_path=binding.source_data_path,
        resolved=True,
        value_snapshot=value_snapshot,
    )
    return item, binding.source_scope


def _resolve_resource_file_artifact_port_items(
    *,
    run,
    step,
    contract_key: str,
    resource_uri_overrides: dict[str, FileIdentity] | None,
) -> list[ResourceFileItem]:
    """Resolve one declared workflow-resource port with traceable provenance."""
    from validibot.validations.constants import StepIODirection
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition

    port = (
        StepIODefinition.objects.filter(
            validator_id=step.validator_id,
            direction=StepIODirection.INPUT,
            io_medium=StepIOMedium.ARTIFACT,
            contract_key=contract_key,
        )
        .order_by("pk")
        .first()
    )
    if port is None:
        msg = (
            f"Validator {step.validator_id} has no declared resource port "
            f"'{contract_key}'"
        )
        raise ValueError(msg)

    binding = (
        StepInputBinding.objects.filter(
            workflow_step=step,
            io_definition=port,
        )
        .select_related("io_definition")
        .first()
    )
    if binding is None:
        msg = (
            f"Artifact port '{port.contract_key}' on step {step.id} "
            "has no StepInputBinding."
        )
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope="",
            source_data_path="",
            resolved=False,
            error_message=msg,
        )
        raise ValueError(msg)

    try:
        artifact_ports.validate_source_scope(port, binding.source_scope)
    except ValueError as exc:
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope=binding.source_scope,
            source_data_path=binding.source_data_path,
            resolved=False,
            error_message=str(exc),
        )
        raise

    if binding.source_scope != BindingSourceScope.WORKFLOW_RESOURCE:
        msg = (
            f"Resource port '{port.contract_key}' cannot materialize source "
            f"scope '{binding.source_scope}'."
        )
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope=binding.source_scope,
            source_data_path=binding.source_data_path,
            resolved=False,
            error_message=msg,
        )
        raise ValueError(msg)

    try:
        items = _resolve_workflow_resource_port(
            run=run,
            step=step,
            port=port,
            binding=binding,
            resource_uri_overrides=resource_uri_overrides,
        )
    except ValueError as exc:
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope=binding.source_scope,
            source_data_path=binding.source_data_path,
            resolved=False,
            error_message=str(exc),
        )
        raise

    _record_artifact_input_trace(
        run=run,
        port=port,
        source_scope=binding.source_scope,
        source_data_path=binding.source_data_path,
        resolved=True,
        value_snapshot=[_resource_file_item_snapshot(item) for item in items],
    )
    return items


def _resource_file_item_snapshot(item: ResourceFileItem) -> dict:
    """Return JSON-safe audit metadata for a resolved resource file."""

    return {
        "source": BindingSourceScope.WORKFLOW_RESOURCE,
        "id": item.id,
        "name": item.name,
        "type": item.type,
        "port_key": item.port_key,
        "uri": item.uri,
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
        "storage_version": item.storage_version,
    }


def _record_artifact_input_trace(
    *,
    run,
    port,
    source_scope: str,
    source_data_path: str,
    resolved: bool,
    value_snapshot=None,
    error_message: str = "",
) -> None:
    """Persist a ``ResolvedInputTrace`` row for an artifact input port."""

    current_step_run = run.current_step_run
    if current_step_run is None:
        return

    from validibot.validations.models import ResolvedInputTrace

    upstream_step_key = ""
    if source_scope == BindingSourceScope.UPSTREAM_ARTIFACT and "." in source_data_path:
        upstream_step_key = source_data_path.split(".", 1)[0]

    ResolvedInputTrace.objects.create(
        step_run=current_step_run,
        io_definition=port,
        input_contract_key=port.contract_key,
        source_scope_used=source_scope,
        source_data_path_used=source_data_path or port.contract_key,
        upstream_step_key=upstream_step_key,
        resolved=resolved,
        used_default=False,
        value_snapshot=value_snapshot if resolved else None,
        error_message=error_message,
    )


def _record_and_raise_artifact_resolution_error(
    *,
    run,
    port,
    binding,
    error_message: str,
) -> None:
    """Persist a failed artifact trace, then raise the user-facing error."""

    _record_artifact_input_trace(
        run=run,
        port=port,
        source_scope=binding.source_scope,
        source_data_path=binding.source_data_path,
        resolved=False,
        error_message=error_message,
    )
    raise ValueError(error_message)


def _build_fmu_input_file_item(
    port_key: str,
    file: FileIdentity,
    *,
    role: str = "fmu",
) -> InputFileItem:
    """Build an FMU ``InputFileItem`` from a resolved immutable file."""

    return InputFileItem(
        name=_filename_from_uri(file.uri) or "model.fmu",
        mime_type=SupportedMimeType.FMU,
        role=role,
        port_key=port_key,
        **file.envelope_fields(),
    )


def _resolve_fmu_file_port_item(
    *,
    run,
    step,
    validator,
    input_file_uris: dict[str, FileIdentity] | None,
    resource_uri_overrides: dict[str, FileIdentity] | None,
) -> tuple[InputFileItem, dict]:
    """Resolve the declared FMU model artifact port into an input file item."""

    from validibot.validations.constants import StepIODirection
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition

    port = (
        StepIODefinition.objects.filter(
            validator_id=validator.id,
            direction=StepIODirection.INPUT,
            io_medium=StepIOMedium.ARTIFACT,
            contract_key="fmu_model",
        )
        .order_by("pk")
        .first()
    )
    if port is None:
        msg = f"FMU validator {validator.id} has no declared fmu_model port"
        raise ValueError(msg)

    binding = (
        StepInputBinding.objects.filter(
            workflow_step=step,
            io_definition=port,
        )
        .select_related("io_definition")
        .first()
    )
    if binding is None:
        msg = (
            f"Required artifact port '{port.contract_key}' on step {step.id} "
            "has no StepInputBinding."
        )
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope="",
            source_data_path="",
            resolved=False,
            error_message=msg,
        )
        raise ValueError(msg)

    try:
        artifact_ports.validate_source_scope(port, binding.source_scope)
    except ValueError as exc:
        _record_artifact_input_trace(
            run=run,
            port=port,
            source_scope=binding.source_scope,
            source_data_path=binding.source_data_path,
            resolved=False,
            error_message=str(exc),
        )
        raise

    if binding.source_scope not in {
        BindingSourceScope.WORKFLOW_RESOURCE,
        BindingSourceScope.SYSTEM,
    }:
        msg = (
            f"Artifact port '{port.contract_key}' source scope "
            f"'{binding.source_scope}' is not materializable for FMU yet."
        )
        _record_and_raise_artifact_resolution_error(
            run=run,
            port=port,
            binding=binding,
            error_message=msg,
        )

    system_file_identities = None
    fmu_model = getattr(validator, "fmu_model", None)
    if binding.source_scope == BindingSourceScope.SYSTEM:
        if not fmu_model:
            msg = f"Validator {validator.id} has no FMU model attached"
            _record_and_raise_artifact_resolution_error(
                run=run,
                port=port,
                binding=binding,
                error_message=msg,
            )
        system_file_identities = {
            port.contract_key: (input_file_uris or {}).get("fmu_model_uri")
            or _stored_fmu_model_identity(fmu_model),
        }

    try:
        resolved = resolve_file_inputs(
            run=run,
            step=step,
            load_content=False,
            materialized_file_identities=input_file_uris,
            resource_identity_overrides=resource_uri_overrides,
            system_file_identities=system_file_identities,
            contract_keys={port.contract_key},
        )[port.contract_key]
        item = _build_fmu_input_file_item(
            resolved.contract_key,
            resolved.identity,
            role=resolved.role or "fmu",
        )
        artifact_ports.validate_input_file_item(port=port, item=item)
    except ValueError as exc:
        _record_and_raise_artifact_resolution_error(
            run=run,
            port=port,
            binding=binding,
            error_message=str(exc),
        )

    value_snapshot = {
        "source": resolved.source_scope,
        "port_key": resolved.contract_key,
        "resource_id": resolved.resource_id,
        "type": resolved.resource_type,
        "fmu_model_id": str(fmu_model.id) if fmu_model is not None else "",
        **resolved.identity.envelope_fields(),
    }
    _record_artifact_input_trace(
        run=run,
        port=port,
        source_scope=binding.source_scope,
        source_data_path=binding.source_data_path,
        resolved=True,
        value_snapshot=value_snapshot,
    )
    return item, value_snapshot


def _stored_fmu_model_identity(fmu_model) -> FileIdentity:
    """Resolve the immutable identity of a library-owned FMU model."""
    expected_sha256 = str(fmu_model.checksum or "").removeprefix("sha256:")
    uri = str(fmu_model.gcs_uri or "")
    if uri.startswith("gs://"):
        from validibot.validations.services.cloud_run.gcs_client import (
            get_gcs_file_identity,
        )

        return get_gcs_file_identity(uri=uri, sha256=expected_sha256)

    try:
        path = Path(fmu_model.file.path)
    except (AttributeError, NotImplementedError) as exc:
        msg = f"FMU model {fmu_model.id} has no immutable storage identity"
        raise ValueError(msg) from exc
    local_uri = f"file://{path}"
    identity = local_file_identity(path=path, uri=local_uri)
    if expected_sha256 and identity.sha256 != expected_sha256:
        msg = f"FMU model {fmu_model.id} no longer matches its stored digest"
        raise ValueError(msg)
    return identity


def _build_shacl_input_file_item(
    port,
    file: FileIdentity,
    *,
    rdf_format: str,
) -> InputFileItem:
    """Build a SHACL ``InputFileItem`` from a resolved ``data_graph`` port."""

    return InputFileItem(
        name=_filename_from_uri(file.uri) or "submission.rdf",
        mime_type=mime_type_for_rdf_format(rdf_format),
        role=port.role or "data-graph",
        port_key=port.contract_key,
        **file.envelope_fields(),
    )


def _shacl_inputs_for_upstream_data_graph_uri(shacl_inputs, uri: str):
    """Adjust SHACL auto-detection when the data graph is an upstream artifact."""

    if shacl_inputs.submission_format != "auto":
        return shacl_inputs

    from validibot.validations.validators.shacl import engine

    rdf_format = engine.detect_serialization(
        file_name=_filename_from_uri(uri),
        file_type=None,
        explicit_format=None,
    )
    if rdf_format == shacl_inputs.rdf_format:
        return shacl_inputs
    return shacl_inputs.model_copy(update={"rdf_format": rdf_format})


def _build_schematron_input_file_item(
    port,
    file: FileIdentity,
) -> InputFileItem:
    """Build a Schematron ``InputFileItem`` from an ``xml_document`` port."""

    return InputFileItem(
        name=_filename_from_uri(file.uri) or "submission.xml",
        mime_type=SupportedMimeType.APPLICATION_XML,
        role=port.role or "xml-document",
        port_key=port.contract_key,
        **file.envelope_fields(),
    )


def _build_portfolio_manager_input_file_item(
    port,
    file: FileIdentity,
) -> InputFileItem:
    """Build a report item whose carrier is inferred from its immutable filename."""
    name = _filename_from_uri(file.uri) or "portfolio-manager-report"
    return InputFileItem(
        name=name,
        mime_type=mime_type_for_portfolio_manager_filename(name),
        role=port.role or "portfolio-manager-report",
        port_key=port.contract_key,
        uri=file.uri,
        size_bytes=file.size_bytes,
        sha256=file.sha256,
        storage_version=file.storage_version,
    )


def _build_pdf_input_file_item(port, file: FileIdentity) -> InputFileItem:
    """Build the immutable PDF document item from its declared input port."""
    return InputFileItem(
        name=_filename_from_uri(file.uri) or "document.pdf",
        mime_type=SupportedMimeType.APPLICATION_PDF,
        role=port.role or "pdf-document",
        port_key=port.contract_key,
        **file.envelope_fields(),
    )


def build_input_envelope(
    run,  # ValidationRun instance
    callback_url: str,
    callback_id: str | None,
    execution_bundle_uri: str,
    *,
    callback_nonce: str | None = None,
    callback_nonce_commitment: str | None = None,
    skip_callback: bool = False,
    input_file_uris: dict[str, FileIdentity] | None = None,
    resource_uri_overrides: dict[str, FileIdentity] | None = None,
) -> ValidationInputEnvelope:
    """
    Build the appropriate input envelope based on validator type.

    This is the main entry point for envelope creation. It dispatches to
    type-specific builders based on the current step's validator type.

    Args:
        run: ValidationRun Django model instance
        callback_url: Django callback endpoint URL
        callback_id: Unique identifier for idempotent callback processing
        callback_nonce: Per-attempt secret echoed only in the callback.
        callback_nonce_commitment: Public commitment included in canonical
            input-envelope hashing.
        execution_bundle_uri: Directory URI for this attempt's files. For
            local Docker this is the attempt-specific container output path;
            for Cloud Run it is the attempt-specific ``gs://`` prefix.
        skip_callback: If True, container won't POST callback after completion.
            Used for synchronous execution where results are read directly.
        input_file_uris: Optional dict of file role to complete immutable file
            identity. Recognised roles include ``primary_file_uri``
            (EnergyPlus model file), ``fmu_model_uri`` (FMU model file), and
            declared artifact-port keys. Local Docker identities point into
            the per-attempt mount; Cloud Run identities carry the uploaded GCS
            generation.
        resource_uri_overrides: Optional mapping of ``resource_id`` to
            a complete materialized file identity for resource files (weather
            data, FMU dependencies, etc.). Local Docker uses identities below
            the workspace's ``input/resources/`` mount instead of host
            ``MEDIA_ROOT`` paths. Cloud Run derives current object metadata
            and the durable stored SHA-256 when no override is supplied.

    Returns:
        Typed envelope (EnergyPlusInputEnvelope, FMUInputEnvelope, etc.)

    Raises:
        ValueError: If validator type is not supported or no active step run

    Example:
        >>> from validibot.validations.models import ValidationRun
        >>> run = ValidationRun.objects.get(id="abc-123")
        >>> envelope = build_input_envelope(
        ...     run=run,
        ...     callback_url="https://api.example.com/callbacks/",
        ...     callback_id="uuid-for-idempotency",
        ...     execution_bundle_uri="gs://bucket/runs/abc-123/",
        ... )
    """
    # Get the current step run to access validator and step info
    current_step_run = run.current_step_run
    if not current_step_run:
        msg = f"No active step run found for ValidationRun {run.id}"
        raise ValueError(msg)

    from validibot.validations.services.execution_attempts import (
        get_active_execution_attempt,
    )

    execution_attempt = get_active_execution_attempt(current_step_run)
    if execution_attempt is None:
        msg = f"Step run {current_step_run.pk} has no active execution attempt"
        raise ValueError(msg)
    execution_attempt_id = str(execution_attempt.pk)
    step_run_id = str(current_step_run.pk)
    expected_output_uri = f"{execution_bundle_uri.rstrip('/')}/output.json"

    step = current_step_run.workflow_step
    validator = step.validator
    if not validator:
        msg = f"WorkflowStep {step.id} has no validator configured"
        raise ValueError(msg)

    # Merge both step-config buckets with input_file_uris for runtime lookups.
    # ``config`` holds semantic keys (e.g. timestep_per_hour), ``display_settings``
    # holds cosmetic/runtime-injected keys (ADR-2026-06-18); input_file_uris takes
    # precedence last (it contains the dynamically uploaded primary_file_uri).
    runtime_file_uris = {
        key: identity.uri for key, identity in (input_file_uris or {}).items()
    }
    step_config = {
        **(step.config or {}),
        **(step.display_settings or {}),
        **runtime_file_uris,
    }

    if validator.validation_type == ValidationType.ENERGYPLUS:
        timestep_per_hour = step_config.get("timestep_per_hour", 4)
        idf_checks = list(step_config.get("idf_checks", []))
        # Full simulation is the contract default; preflight-only steps store
        # an explicit false value.
        run_simulation = bool(step_config.get("run_simulation", True))
        review_profile = step_config.get("review_profile", "standard")
        resolved_file_ports = _resolve_energyplus_file_port_items(
            run=run,
            step=step,
            step_config=step_config,
            input_file_uris=input_file_uris,
            resource_uri_overrides=resource_uri_overrides,
        )
        input_files, resource_files = resolved_file_ports
        if not any(item.port_key == "primary_model" for item in input_files):
            msg = f"Step {step.id} has no primary_model file port resolved"
            raise ValueError(msg)
        if run_simulation and not any(
            item.port_key == "weather_file" for item in [*input_files, *resource_files]
        ):
            msg = f"Step {step.id} has no weather_file port resolved"
            raise ValueError(msg)

        return build_energyplus_input_envelope(
            run_id=str(run.id),
            validator=validator,
            org_id=str(run.org.id),
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            model_file=None,
            input_files=input_files,
            resource_files=resource_files,
            callback_url=callback_url,
            callback_id=callback_id,
            callback_nonce=callback_nonce,
            callback_nonce_commitment=callback_nonce_commitment,
            execution_bundle_uri=execution_bundle_uri,
            execution_attempt_id=execution_attempt_id,
            step_run_id=step_run_id,
            expected_output_uri=expected_output_uri,
            timestep_per_hour=timestep_per_hour,
            idf_checks=idf_checks,
            run_simulation=run_simulation,
            review_profile=review_profile,
            skip_callback=skip_callback,
        )
    if validator.validation_type == ValidationType.FMU:
        resolved_fmu_port = _resolve_fmu_file_port_item(
            run=run,
            step=step,
            validator=validator,
            input_file_uris=input_file_uris,
            resource_uri_overrides=resource_uri_overrides,
        )
        fmu_file_item, fmu_value_snapshot = resolved_fmu_port
        sim_config = (
            (step.config or {}).get("fmu_simulation") or {}
            if fmu_value_snapshot.get("source") == BindingSourceScope.WORKFLOW_RESOURCE
            else {}
        )

        # Build simulation config, only overriding fields that have values.
        # The shared FMUSimulationConfig has non-optional defaults for
        # start_time, stop_time, step_size — only pass them if explicitly set.
        sim_kwargs = {}
        for key in ("start_time", "stop_time", "step_size", "tolerance"):
            val = sim_config.get(key)
            if val is not None:
                sim_kwargs[key] = val

        # Resolve FMU input values from explicit StepInputBinding rows only.
        # There is no raw-submission fallback: bindings are the contract that
        # makes input identity, defaults, traces, and cross-step references
        # auditable.
        input_values: dict = {}
        has_bindings = (
            step.input_bindings.filter(
                io_definition__direction="input",
            )
            .exclude(io_definition__io_medium=StepIOMedium.ARTIFACT)
            .exists()
        )

        if has_bindings and current_step_run:
            from validibot.validations.models import ResolvedInputTrace
            from validibot.validations.services.path_resolution import (
                StepInputResolutionError,
            )
            from validibot.validations.services.path_resolution import (
                resolve_step_input_values,
            )

            submission_data: dict = {}
            submission_metadata: dict = {}
            if run.submission:
                try:
                    content = run.submission.get_content()
                    if content:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            submission_data = parsed
                except (json.JSONDecodeError, Exception):
                    logger.warning(
                        "Could not parse submission content as JSON for run %s",
                        run.id,
                    )
                # Submission metadata is a JSONField (always a dict),
                # needed for inputs scoped to SUBMISSION_METADATA
                # (e.g., EnergyPlus expected_floor_area_m2).
                submission_metadata = run.submission.metadata or {}

            # Canonical upstream values for cross-step resolution. These come
            # from completed step-run records, never from presentation JSON.
            from validibot.validations.services.run_context import RunContextBuilder

            upstream = RunContextBuilder(run, step).build_upstream_steps()

            # Resolve workflow-level signals so SIGNAL-scoped bindings
            # can look up values from the workflow's signal namespace.
            # This intentionally propagates exceptions: if signal
            # resolution fails the step must not proceed with
            # potentially missing input values.
            workflow_signals_dict: dict = {}
            if step.workflow:
                from validibot.validations.services.signal_resolution import (
                    resolve_workflow_signals,
                )

                signal_result = resolve_workflow_signals(
                    step.workflow,
                    submission_data,
                )
                workflow_signals_dict = signal_result.signals

            try:
                input_values, traces = resolve_step_input_values(
                    step,
                    current_step_run,
                    submission_data=submission_data,
                    submission_metadata=submission_metadata,
                    upstream_steps=upstream,
                    workflow_signals=workflow_signals_dict,
                )
                if traces:
                    ResolvedInputTrace.objects.bulk_create(traces)
            except StepInputResolutionError as exc:
                # Persist ALL traces (successes + failures) for diagnostics
                # even when resolution fails. The exception carries the
                # complete trace list so operators can see exactly which
                # inputs resolved and which didn't.
                if exc.traces:
                    ResolvedInputTrace.objects.bulk_create(exc.traces)
                raise

            # Persist the fully resolved values once, under the canonical
            # Validibot contract keys. Native/provider names belong only in
            # the backend input envelope; assertions and downstream steps use
            # ``ValidationStepRun.input_values`` and StepIODefinition keys.
            if current_step_run:
                current_step_run.input_values = {
                    trace.input_contract_key: trace.value_snapshot
                    for trace in traces
                    if trace.resolved
                }
                current_step_run.save(update_fields=["input_values"])
        elif _fmu_step_declares_inputs(step):
            msg = (
                f"Step {step.id} declares FMU inputs but has no "
                "StepInputBinding rows. Configure input bindings before launch."
            )
            raise ValueError(msg)

        # Extract output variable names: prefer StepIODefinition rows,
        # fall back to step config JSON.
        from validibot.validations.constants import StepIODirection
        from validibot.validations.constants import StepIOOriginKind
        from validibot.validations.models import StepIODefinition

        output_definitions = StepIODefinition.objects.filter(
            workflow_step=step,
            direction=StepIODirection.OUTPUT,
            origin_kind=StepIOOriginKind.FMU,
        )
        output_variables = [
            io_definition.native_name for io_definition in output_definitions
        ]

        fmu_inputs = FMUInputs(
            input_values=input_values,
            simulation=FMUSimulationConfig(**sim_kwargs),
            output_variables=output_variables,
        )

        input_files = [fmu_file_item]
        context = ExecutionContext(
            callback_id=callback_id,
            callback_nonce=callback_nonce,
            callback_nonce_commitment=callback_nonce_commitment,
            callback_url=callback_url,
            execution_bundle_uri=execution_bundle_uri,
            execution_attempt_id=execution_attempt_id,
            step_run_id=step_run_id,
            attempt_contract_version=ATTEMPT_CONTRACT_VERSION,
            expected_output_uri=expected_output_uri,
            skip_callback=skip_callback,
        )
        return FMUInputEnvelope(
            run_id=str(run.id),
            validator=ValidatorInfo(
                id=str(validator.id),
                type=ValidatorType(validator.validation_type),
                version=str(validator.version),
            ),
            org=OrganizationInfo(id=str(run.org.id), name=run.org.name),
            workflow=WorkflowInfo(
                id=str(run.workflow.id),
                step_id=str(step.id),
                step_name=step.name,
            ),
            input_files=input_files,
            inputs=fmu_inputs,
            context=context,
        )

    if validator.validation_type == ValidationType.SHACL:
        # The RDF submission is the primary file. For sync Docker dispatch the
        # workspace materialiser sets ``primary_file_uri`` to the container path;
        # for async Cloud Run, ``launch_shacl_validation`` uploads the submission
        # to GCS and passes its gs:// URI here via ``input_file_uris``.
        # Resolve shapes/ontology/settings/SPARQL-ASK assertions from the DB
        # (the container has none) and ship them in the typed inputs.
        from validibot.validations.validators.shacl.launch import resolve_shacl_inputs

        shacl_inputs = resolve_shacl_inputs(
            validator=validator,
            ruleset=step.ruleset,
            submission=run.submission,
        )
        resolved_data_graph = _resolve_input_file_artifact_port_item(
            run=run,
            step=step,
            step_config=step_config,
            input_file_uris=input_file_uris,
            contract_key="data_graph",
            item_builder=lambda port, file: _build_shacl_input_file_item(
                port,
                file,
                rdf_format=shacl_inputs.rdf_format,
            ),
        )
        data_graph_item, source_scope = resolved_data_graph
        if source_scope == BindingSourceScope.UPSTREAM_ARTIFACT:
            shacl_inputs = _shacl_inputs_for_upstream_data_graph_uri(
                shacl_inputs,
                data_graph_item.uri,
            )
            data_graph_item.mime_type = mime_type_for_rdf_format(
                shacl_inputs.rdf_format,
            )
        submission_file = FileIdentity.from_envelope_item(data_graph_item)

        envelope = build_shacl_input_envelope(
            run_id=str(run.id),
            validator=validator,
            org_id=str(run.org.id),
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            submission_uri=submission_file.uri,
            submission_size_bytes=submission_file.size_bytes,
            submission_sha256=submission_file.sha256,
            submission_storage_version=submission_file.storage_version,
            inputs=shacl_inputs,
            callback_url=callback_url,
            callback_id=callback_id,
            callback_nonce=callback_nonce,
            callback_nonce_commitment=callback_nonce_commitment,
            execution_bundle_uri=execution_bundle_uri,
            execution_attempt_id=execution_attempt_id,
            step_run_id=step_run_id,
            expected_output_uri=expected_output_uri,
            skip_callback=skip_callback,
        )
        envelope.input_files = [data_graph_item]
        return envelope

    if validator.validation_type == ValidationType.SCHEMATRON:
        # The XML submission is the primary file; the author's Schematron
        # rules travel INLINE in the typed inputs (ADR-2026-07-01 D4b) —
        # the SHACL shapes_text pattern. ``resolve_schematron_inputs``
        # reads them from the step's ruleset (where the step-config upload
        # stored them); the container compiles and runs them in isolation.
        #
        # Imports are deliberately local: ``validibot_shared.schematron``
        # requires validibot-shared >= 0.12.0, and this branch is the only
        # part of the envelope builder that touches it.
        from validibot_shared.schematron.envelopes import (
            build_schematron_input_envelope,
        )

        from validibot.validations.validators.schematron.launch import (
            resolve_schematron_inputs,
        )

        schematron_inputs = resolve_schematron_inputs(
            validator=validator,
            ruleset=step.ruleset,
        )
        resolved_xml_document = _resolve_input_file_artifact_port_item(
            run=run,
            step=step,
            step_config=step_config,
            input_file_uris=input_file_uris,
            contract_key="xml_document",
            item_builder=_build_schematron_input_file_item,
        )
        xml_document_item, _source_scope = resolved_xml_document
        submission_file = FileIdentity.from_envelope_item(xml_document_item)

        schematron_envelope = build_schematron_input_envelope(
            run_id=str(run.id),
            validator=validator,
            org_id=str(run.org.id),
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            submission_uri=submission_file.uri,
            submission_size_bytes=submission_file.size_bytes,
            submission_sha256=submission_file.sha256,
            submission_storage_version=submission_file.storage_version,
            inputs=schematron_inputs,
            callback_url=callback_url,
            callback_id=callback_id,
            callback_nonce=callback_nonce,
            callback_nonce_commitment=callback_nonce_commitment,
            execution_bundle_uri=execution_bundle_uri,
            execution_attempt_id=execution_attempt_id,
            step_run_id=step_run_id,
            expected_output_uri=expected_output_uri,
            skip_callback=skip_callback,
        )
        schematron_envelope.input_files = [xml_document_item]
        return schematron_envelope

    if validator.validation_type == ValidationType.PDF:
        resolved_pdf = _resolve_input_file_artifact_port_item(
            run=run,
            step=step,
            step_config=step_config,
            input_file_uris=input_file_uris,
            contract_key="pdf_document",
            item_builder=_build_pdf_input_file_item,
        )
        pdf_item, _source_scope = resolved_pdf

        from validibot.validations.services.execution.deployments import (
            effective_execution_budget_seconds,
        )

        pdf_config = step.config or {}
        pdf_inputs = PdfInputs.model_validate(
            {
                "profile": PDF_STATIC_TEXT_PROFILE,
                "emit_extracted_files_bundle": bool(
                    pdf_config.get("emit_extracted_files_bundle", False)
                ),
                "selected_xml": pdf_config.get("selected_xml"),
                "selected_json": pdf_config.get("selected_json"),
                "selected_step_p21": pdf_config.get("selected_step_p21"),
                "limits": PdfProcessingLimits(
                    max_execution_seconds=min(
                        effective_execution_budget_seconds(step=step),
                        300,
                    )
                ),
            }
        )
        context = ExecutionContext.model_validate(
            {
                "callback_id": callback_id,
                "callback_nonce": callback_nonce,
                "callback_nonce_commitment": callback_nonce_commitment,
                "callback_url": callback_url,
                "execution_bundle_uri": execution_bundle_uri,
                "execution_attempt_id": execution_attempt_id,
                "step_run_id": step_run_id,
                "attempt_contract_version": ATTEMPT_CONTRACT_VERSION,
                "expected_output_uri": expected_output_uri,
                "skip_callback": skip_callback,
            }
        )
        return PdfInputEnvelope(
            run_id=str(run.id),
            validator=ValidatorInfo(
                id=str(validator.id),
                type=ValidatorType.PDF,
                version=str(validator.version),
            ),
            org=OrganizationInfo(id=str(run.org.id), name=run.org.name),
            workflow=WorkflowInfo(
                id=str(run.workflow.id),
                step_id=str(step.id),
                step_name=step.name,
            ),
            input_files=[pdf_item],
            inputs=pdf_inputs,
            context=context,
        )

    if validator.validation_type == ValidationType.PORTFOLIO_MANAGER:
        portfolio_submission_file: FileIdentity | None
        resolved_report = _resolve_input_file_artifact_port_item(
            run=run,
            step=step,
            step_config=step_config,
            input_file_uris=input_file_uris,
            contract_key="portfolio_manager_report",
            item_builder=_build_portfolio_manager_input_file_item,
        )
        report_item, _source_scope = resolved_report
        portfolio_submission_file = FileIdentity.from_envelope_item(report_item)

        ebl_resources = _resolve_resource_file_artifact_port_items(
            run=run,
            step=step,
            contract_key="expected_buildings_list",
            resource_uri_overrides=resource_uri_overrides,
        )
        if len(ebl_resources) > 1:
            msg = f"Step {step.id} has more than one Expected Buildings List"
            raise ValueError(msg)
        ebl_resource = ebl_resources[0] if ebl_resources else None

        config = step.config or {}
        resolved_inputs = current_step_run.input_values or {}
        default_euit = resolved_inputs.get("default_euit_kbtu_ft2_yr")
        if default_euit is None:
            default_euit = config.get("default_euit_kbtu_ft2_yr")
        reference_datetime = (
            run.started_at or current_step_run.started_at or run.created
        )
        portfolio_inputs = PortfolioManagerInputs(
            submission_structure=config.get(
                "submission_structure",
                "single_report",
            ),
            default_euit_kbtu_ft2_yr=default_euit,
            compare_to_euit=bool(config.get("compare_to_euit", False)),
            near_target_percent=config.get("near_target_percent", 10),
            require_complete_reporting_period=bool(
                config.get("require_complete_reporting_period", False)
            ),
            minimum_reporting_period_months=config.get(
                "minimum_reporting_period_months",
                12,
            ),
            maximum_reporting_period_age_months=config.get(
                "maximum_reporting_period_age_months"
            ),
            reporting_period_reference_date=reference_datetime.date(),
            require_benchmark_ready=bool(config.get("require_benchmark_ready", False)),
            require_form_c_ready=bool(config.get("require_form_c_ready", False)),
            require_weather_normalized_site_eui=bool(
                config.get("require_weather_normalized_site_eui", False)
            ),
            require_washington_standard_id=bool(
                config.get("require_washington_standard_id", False)
            ),
            require_energy_star_score=bool(
                config.get("require_energy_star_score", False)
            ),
            meter_less_than_12_months_policy=config.get(
                "meter_less_than_12_months_policy",
                "allow",
            ),
            meter_gap_policy=config.get("meter_gap_policy", "allow"),
            meter_overlap_policy=config.get("meter_overlap_policy", "allow"),
            no_meters_selected_policy=config.get(
                "no_meters_selected_policy",
                "allow",
            ),
            long_meter_entry_policy=config.get(
                "long_meter_entry_policy",
                "allow",
            ),
            estimated_energy_policy=config.get(
                "estimated_energy_policy",
                "allow",
            ),
            other_alert_policy=config.get("other_alert_policy", "allow"),
            max_input_bytes=PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES,
            max_archive_members=config.get("max_archive_members", 250),
            max_member_bytes=config.get("max_member_bytes", 20_000_000),
            max_uncompressed_bytes=config.get(
                "max_uncompressed_bytes",
                250_000_000,
            ),
        )
        context = ExecutionContext.model_validate(
            {
                "callback_id": callback_id,
                "callback_nonce": callback_nonce,
                "callback_nonce_commitment": callback_nonce_commitment,
                "callback_url": callback_url,
                "execution_bundle_uri": execution_bundle_uri,
                "execution_attempt_id": execution_attempt_id,
                "step_run_id": step_run_id,
                "attempt_contract_version": ATTEMPT_CONTRACT_VERSION,
                "expected_output_uri": expected_output_uri,
                "skip_callback": skip_callback,
            },
        )
        portfolio_envelope = build_portfolio_manager_input_envelope(
            run_id=str(run.id),
            validator=validator,
            org_id=str(run.org.id),
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            submission_name=_filename_from_uri(portfolio_submission_file.uri),
            submission_uri=portfolio_submission_file.uri,
            submission_size_bytes=portfolio_submission_file.size_bytes,
            submission_sha256=portfolio_submission_file.sha256,
            submission_storage_version=portfolio_submission_file.storage_version,
            inputs=portfolio_inputs,
            context=context,
            expected_buildings_list=ebl_resource,
        )
        portfolio_envelope.input_files = [report_item]
        return portfolio_envelope

    msg = f"Unsupported validator type: {validator.validation_type}"
    raise ValueError(msg)


def _fmu_step_declares_inputs(step) -> bool:
    """Return whether this FMU step has declared input definitions.

    Step-owned FMU uploads attach I/O definitions to ``workflow_step``. Library FMU
    validators may attach them to the reusable validator. Either form means
    launch requires explicit ``StepInputBinding`` rows.
    """
    from validibot.validations.constants import StepIODirection
    from validibot.validations.constants import StepIOOriginKind
    from validibot.validations.models import StepIODefinition

    step_owned_inputs = StepIODefinition.objects.filter(
        workflow_step=step,
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.FMU,
    ).exists()
    if step_owned_inputs:
        return True

    validator_id = getattr(step, "validator_id", None)
    if validator_id is None:
        return False

    return StepIODefinition.objects.filter(
        validator_id=validator_id,
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.FMU,
    ).exists()
