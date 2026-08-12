"""Typed input-port contracts for organization-owned validators."""

from __future__ import annotations

from typing import TypedDict

from django.core.exceptions import ValidationError

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.services.catalog_entry_normalization import (
    build_provider_binding_from_mapping,
)


class _FormatContract(TypedDict):
    """Concrete artifact properties for one custom-validator format."""

    media_type: str
    file_types: list[str]
    extensions: list[str]


_FORMAT_CONTRACTS: dict[str, _FormatContract] = {
    SubmissionDataFormat.JSON: {
        "media_type": "application/json",
        "file_types": [SubmissionFileType.JSON],
        "extensions": ["json"],
    },
    SubmissionDataFormat.YAML: {
        "media_type": "application/yaml",
        "file_types": [SubmissionFileType.YAML],
        "extensions": ["yaml", "yml"],
    },
}


def sync_custom_validator_input_port(
    *, validator: Validator, data_format: str
) -> StepIODefinition:
    """Create or update the exact document contract for a custom validator."""

    contract = _FORMAT_CONTRACTS.get(data_format)
    if contract is None:
        raise ValidationError(
            {"input_data_format": "Custom validators support JSON or YAML."}
        )
    media_type = contract["media_type"]
    port, _created = StepIODefinition.objects.update_or_create(
        validator=validator,
        contract_key="document",
        direction=StepIODirection.INPUT,
        defaults={
            "native_name": "document",
            "label": "Document",
            "description": "Document parsed by this custom validator.",
            "data_type": "artifact_ref",
            "io_medium": StepIOMedium.ARTIFACT,
            "artifact_kind": ArtifactKind.FILE,
            "media_type": media_type,
            "data_format": data_format,
            "accepted_data_formats": [data_format],
            "accepted_media_types": [media_type],
            "accepted_file_types": contract["file_types"],
            "accepted_extensions": contract["extensions"],
            "allowed_source_scopes": [
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            "default_source_strategy": DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            "envelope_channel": EnvelopeChannel.INPUT_FILES,
            "role": "document",
            "is_collection": False,
            "min_items": 1,
            "max_items": 1,
            "order": 1,
            "origin_kind": StepIOOriginKind.CATALOG,
            "source_kind": StepIOSourceKind.PAYLOAD_PATH,
            "is_path_editable": False,
            "on_missing": "error",
        },
    )
    return port


def custom_validator_data_format(validator) -> str:
    """Read the custom validator's declared document format from its port."""

    port = (
        validator.step_io_definitions.filter(
            contract_key="document",
            direction=StepIODirection.INPUT,
            io_medium=StepIOMedium.ARTIFACT,
        )
        .only("data_format")
        .first()
    )
    return port.data_format if port is not None else ""


def sync_configured_io_contract(*, validator) -> None:
    """Copy the registered validation-type I/O contract to an org validator."""

    from validibot.validations.constants import CatalogEntryType
    from validibot.validations.validators.base.config import get_config

    config = get_config(validator.validation_type)
    if config is None:
        raise ValidationError("No registered input contract exists for this validator.")
    for entry in config.catalog_entries:
        if entry.entry_type != CatalogEntryType.IO_DEFINITION:
            continue
        StepIODefinition.objects.update_or_create(
            validator=validator,
            contract_key=entry.slug,
            direction=entry.run_stage,
            defaults={
                "native_name": entry.slug,
                "label": entry.label,
                "description": entry.description,
                "data_type": entry.data_type,
                "io_medium": entry.io_medium,
                "artifact_kind": entry.artifact_kind,
                "media_type": entry.media_type,
                "data_format": entry.data_format,
                "accepted_data_formats": entry.accepted_data_formats,
                "accepted_media_types": entry.accepted_media_types,
                "accepted_file_types": entry.accepted_file_types,
                "accepted_extensions": entry.accepted_extensions,
                "allowed_source_scopes": entry.allowed_source_scopes,
                "default_source_strategy": entry.default_source_strategy,
                "envelope_channel": entry.envelope_channel,
                "resource_type": entry.resource_type,
                "role": entry.role,
                "is_collection": entry.is_collection,
                "min_items": entry.min_items,
                "max_items": entry.max_items,
                "order": entry.order,
                "origin_kind": StepIOOriginKind.CATALOG,
                "source_kind": entry.source_kind,
                "is_path_editable": entry.is_path_editable,
                "provider_binding": build_provider_binding_from_mapping(
                    entry.binding_config
                ),
                "metadata": entry.metadata,
                "on_missing": entry.on_missing,
            },
        )


__all__ = [
    "custom_validator_data_format",
    "sync_configured_io_contract",
    "sync_custom_validator_input_port",
]
