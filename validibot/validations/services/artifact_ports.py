"""
Artifact-port contract validation.

Artifact ports are the stable file/data interfaces between workflow steps and
validator envelopes. This module keeps their contract rules independent from
any one runner or envelope builder so every artifact materialization path can
enforce the same semantics.
"""

from pathlib import Path
from typing import Protocol
from urllib.parse import unquote
from urllib.parse import urlparse

from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import SupportedMimeType

from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import PORTFOLIO_MANAGER_EBL_RESOURCE
from validibot.validations.constants import ResourceFileType


class ArtifactPortLike(Protocol):
    """Minimal StepIODefinition shape used by artifact-port validators."""

    @property
    def contract_key(self) -> str: ...

    @property
    def role(self) -> str | None: ...

    @property
    def resource_type(self) -> str | None: ...

    @property
    def data_format(self) -> str | None: ...

    @property
    def media_type(self) -> str | None: ...

    @property
    def accepted_data_formats(self) -> list[str] | None: ...

    @property
    def accepted_media_types(self) -> list[str] | None: ...

    @property
    def allowed_source_scopes(self) -> list[str] | None: ...

    @property
    def metadata(self) -> dict | None: ...

    @property
    def min_items(self) -> int | None: ...

    @property
    def max_items(self) -> int | None: ...


def validate_source_scope(port: ArtifactPortLike, source_scope: str) -> None:
    """Fail closed when a binding uses a source scope the port did not allow."""

    allowed = list(port.allowed_source_scopes or [])
    if allowed and source_scope not in allowed:
        msg = (
            f"Artifact port '{port.contract_key}' does not allow source scope "
            f"'{source_scope}'. Allowed scopes: {', '.join(allowed)}."
        )
        raise ValueError(msg)


def validate_cardinality(
    *,
    port: ArtifactPortLike,
    count: int,
    source_description: str,
) -> None:
    """Validate resolved artifact count against min/max port cardinality."""

    min_items = port.min_items or 0
    max_items = port.max_items or 0
    if min_items and count < min_items:
        msg = (
            f"Required artifact port '{port.contract_key}' expected at least "
            f"{min_items} item(s) from {source_description} but found {count}."
        )
        raise ValueError(msg)
    if max_items and count > max_items:
        msg = (
            f"Artifact port '{port.contract_key}' accepts at most "
            f"{max_items} item(s) from {source_description} but found {count}."
        )
        raise ValueError(msg)


def validate_input_file_item(
    *,
    port: ArtifactPortLike,
    item: InputFileItem,
) -> None:
    """Validate a backend input file item against its artifact-port contract."""

    validate_file_uri(port=port, uri=item.uri)
    _validate_role(
        port=port,
        observed_role=item.role,
        source_description=f"input file '{item.uri}'",
    )
    observed_data_format = _data_format_from_known_uri(item.uri or item.name)
    if (
        port.data_format == SubmissionDataFormat.PORTFOLIO_MANAGER_REPORT
        and _extension_from_uri(item.uri or item.name)
        in _accepted_extensions_for_port(port)
    ):
        # Portfolio Manager is one domain format with several carriers. XML
        # alone would otherwise be classified as generic XML, while XLS/XLSX/
        # ZIP have no generic Validibot data-format identity at all.
        observed_data_format = SubmissionDataFormat.PORTFOLIO_MANAGER_REPORT
    _validate_data_format(
        port=port,
        observed_data_format=observed_data_format,
        source_description=f"input file '{item.uri}'",
    )
    _validate_media_type(
        port=port,
        observed_media_type=_media_type_value(item.mime_type),
        source_description=f"input file '{item.uri}'",
    )


def validate_resource_file_item(
    *,
    port: ArtifactPortLike,
    item: ResourceFileItem,
) -> None:
    """Validate a backend resource file item against its artifact-port contract."""

    if port.resource_type and item.type != port.resource_type:
        msg = (
            f"Artifact port '{port.contract_key}' expected resource type "
            f"'{port.resource_type}' but got '{item.type}'."
        )
        raise ValueError(msg)

    validate_file_uri(port=port, uri=item.uri)
    _validate_data_format(
        port=port,
        observed_data_format=item.type,
        source_description=f"resource file '{item.uri}'",
    )
    _validate_media_type(
        port=port,
        observed_media_type=(
            _media_type_for_resource_type(item.type)
            or _media_type_from_known_uri(item.uri)
        ),
        source_description=f"resource file '{item.uri}'",
    )


def validate_artifact_ref(
    *,
    port: ArtifactPortLike,
    artifact_ref: dict,
) -> None:
    """Validate an upstream ArtifactRef against the consumer artifact port."""

    uri = str(artifact_ref.get("uri") or artifact_ref.get("filename") or "")
    data_format = str(artifact_ref.get("data_format") or "")
    media_type = str(artifact_ref.get("media_type") or "")
    inferred_media_type = _media_type_from_known_uri(uri)

    _validate_data_format(
        port=port,
        observed_data_format=data_format or _data_format_from_known_uri(uri),
        source_description=f"upstream artifact '{uri or '<unknown>'}'",
    )
    _validate_media_type(
        port=port,
        observed_media_type=_media_type_for_artifact_ref(
            media_type=media_type,
            inferred_media_type=inferred_media_type,
        ),
        source_description=f"upstream artifact '{uri or '<unknown>'}'",
    )


def validate_output_artifact(
    *,
    port: ArtifactPortLike,
    artifact,
) -> None:
    """Validate a trusted output-envelope artifact against an output port."""

    name = str(getattr(artifact, "name", "") or "")
    uri = str(getattr(artifact, "uri", "") or "")
    media_type = str(getattr(artifact, "mime_type", "") or "")
    role = str(getattr(artifact, "type", "") or "")
    source = f"output artifact '{uri or name or '<unknown>'}'"

    validate_file_uri(port=port, uri=uri or name)
    _validate_role(port=port, observed_role=role, source_description=source)
    # ValidationArtifact carries an exact role and MIME type, but no domain
    # data-format field. The caller has already matched that role to this
    # declared output port and records the port's data_format on the resulting
    # ArtifactRef. A generic carrier suffix such as .json cannot independently
    # prove a domain format and must not be guessed as EnergyPlus epJSON.
    _validate_media_type(
        port=port,
        observed_media_type=media_type or _media_type_from_known_uri(uri or name),
        source_description=source,
    )


def validate_file_uri(*, port: ArtifactPortLike, uri: str) -> None:
    """Validate a submitted/upstream file URI against declared port extensions."""

    accepted = _accepted_extensions_for_port(port)
    if not accepted:
        return
    extension = _extension_from_uri(uri)
    if extension in accepted:
        return
    msg = (
        f"Artifact port '{port.contract_key}' expected one of "
        f"{', '.join(f'.{ext}' for ext in accepted)} but got "
        f"'{uri or '<empty>'}'."
    )
    raise ValueError(msg)


def _validate_role(
    *,
    port: ArtifactPortLike,
    observed_role: str | None,
    source_description: str,
) -> None:
    """Validate an envelope item role when the port declares one."""

    expected = str(port.role or "")
    if not expected:
        return

    observed = str(observed_role or "")
    if observed == expected:
        return

    msg = (
        f"Artifact port '{port.contract_key}' expected role '{expected}' "
        f"but {source_description} has role '{observed or '<empty>'}'."
    )
    raise ValueError(msg)


def _validate_data_format(
    *,
    port: ArtifactPortLike,
    observed_data_format: str,
    source_description: str,
) -> None:
    """Validate a resolved artifact's domain data format."""

    accepted = _normalized_strings(port.accepted_data_formats)
    if not accepted:
        accepted = _normalized_strings([port.data_format])
    if not accepted:
        return

    observed = _normalized_string(observed_data_format)
    if observed and observed in accepted:
        return

    observed_label = observed or "<empty>"
    msg = (
        f"Artifact port '{port.contract_key}' does not accept data format "
        f"'{observed_label}' from {source_description}. Accepted formats: "
        f"{', '.join(accepted)}."
    )
    raise ValueError(msg)


def _validate_media_type(
    *,
    port: ArtifactPortLike,
    observed_media_type: str,
    source_description: str,
) -> None:
    """Validate a resolved artifact's MIME/media type."""

    accepted = _normalized_strings(port.accepted_media_types)
    if not accepted:
        accepted = _normalized_strings([port.media_type])
    if not accepted:
        return

    observed = _normalized_string(observed_media_type)
    if observed and observed in accepted:
        return

    observed_label = observed or "<empty>"
    msg = (
        f"Artifact port '{port.contract_key}' does not accept media type "
        f"'{observed_label}' from {source_description}. Accepted media types: "
        f"{', '.join(accepted)}."
    )
    raise ValueError(msg)


def _normalized_strings(values) -> list[str]:
    """Return stable lowercase string values from a contract list."""

    normalized: list[str] = []
    for value in values or []:
        item = _normalized_string(value)
        if item:
            normalized.append(item)
    return list(dict.fromkeys(normalized))


def _normalized_string(value) -> str:
    """Return a lowercase string for enum, TextChoices, and plain values."""

    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _media_type_value(value) -> str:
    """Return a MIME string from a shared enum or string value."""

    return str(getattr(value, "value", value) or "")


def _media_type_for_resource_type(resource_type: str) -> str:
    """Return the MIME type implied by a known workflow resource type."""

    if resource_type == ResourceFileType.ENERGYPLUS_WEATHER:
        return SupportedMimeType.ENERGYPLUS_EPW.value
    if resource_type == PORTFOLIO_MANAGER_EBL_RESOURCE:
        return SupportedMimeType.APPLICATION_JSON.value
    return ""


def _media_type_for_artifact_ref(
    *,
    media_type: str,
    inferred_media_type: str,
) -> str:
    """Return the media type to validate for an upstream artifact ref.

    Older artifacts may carry generic MIME types such as ``application/json``
    even when their filename tells us they are EnergyPlus epJSON. Accept those
    known generic aliases, while preserving clearly wrong explicit media types
    so contract validation can reject them.
    """

    if not media_type:
        return inferred_media_type
    if _artifact_ref_media_type_matches_inference(
        media_type=media_type,
        inferred_media_type=inferred_media_type,
    ):
        return inferred_media_type
    return media_type


def _artifact_ref_media_type_matches_inference(
    *,
    media_type: str,
    inferred_media_type: str,
) -> bool:
    """Return whether a generic artifact MIME is compatible with the URI."""

    normalized_media_type = _normalized_string(media_type)
    normalized_inferred = _normalized_string(inferred_media_type)
    if not normalized_inferred:
        return False
    if normalized_media_type == normalized_inferred:
        return True

    aliases = {
        SupportedMimeType.ENERGYPLUS_EPJSON.value: {"application/json"},
        SupportedMimeType.ENERGYPLUS_IDF.value: {"text/plain"},
        SupportedMimeType.ENERGYPLUS_EPW.value: {"text/plain"},
        SupportedMimeType.FMU.value: {"application/octet-stream"},
        SupportedMimeType.RDF_TURTLE.value: {
            "application/x-turtle",
            "text/plain",
        },
        SupportedMimeType.RDF_XML.value: {"application/xml", "text/xml"},
        SupportedMimeType.RDF_JSON_LD.value: {"application/json"},
        SupportedMimeType.RDF_N_TRIPLES.value: {"text/plain"},
        SupportedMimeType.RDF_N_QUADS.value: {"text/plain"},
        "application/x-sqlite3": {"application/vnd.sqlite3"},
    }
    return normalized_media_type in aliases.get(normalized_inferred, set())


def _media_type_from_known_uri(uri: str) -> str:
    """Infer a known artifact MIME type from a filename or storage URI."""

    extension = _extension_from_uri(uri)
    if extension == "idf":
        return SupportedMimeType.ENERGYPLUS_IDF.value
    if extension in {"epjson", "json"}:
        return SupportedMimeType.ENERGYPLUS_EPJSON.value
    if extension == "epw":
        return SupportedMimeType.ENERGYPLUS_EPW.value
    if extension == "sql":
        return "application/x-sqlite3"
    if extension == "csv":
        return "text/csv"
    if extension in {"err", "eso", "log", "txt"}:
        return "text/plain"
    if extension == "fmu":
        return SupportedMimeType.FMU.value
    if extension == "xml":
        return SupportedMimeType.APPLICATION_XML.value
    if extension == "svrl":
        return SupportedMimeType.APPLICATION_XML.value
    if extension == "ttl":
        return SupportedMimeType.RDF_TURTLE.value
    if extension == "rdf":
        return SupportedMimeType.RDF_XML.value
    if extension == "jsonld":
        return SupportedMimeType.RDF_JSON_LD.value
    if extension == "nt":
        return SupportedMimeType.RDF_N_TRIPLES.value
    if extension == "nq":
        return SupportedMimeType.RDF_N_QUADS.value
    return ""


def _data_format_from_known_uri(uri: str) -> str:
    """Infer the artifact data format from a known filename or URI."""

    extension = _extension_from_uri(uri)
    if extension == "idf":
        return SubmissionDataFormat.ENERGYPLUS_IDF
    if extension in {"epjson", "json"}:
        return SubmissionDataFormat.ENERGYPLUS_EPJSON
    if extension == "epw":
        return ResourceFileType.ENERGYPLUS_WEATHER
    if extension == "sql":
        return "sqlite"
    if extension == "csv":
        return "csv"
    if extension in {"err", "log", "txt"}:
        return "text"
    if extension == "eso":
        return "energyplus_eso"
    if extension == "fmu":
        return SubmissionDataFormat.FMU
    if extension == "pdf":
        return SubmissionDataFormat.PDF
    if extension == "xml":
        return SubmissionDataFormat.XML
    if extension == "svrl":
        return SubmissionDataFormat.XML
    if extension in {"ttl", "nt", "nq"}:
        return SubmissionDataFormat.TEXT
    if extension == "rdf":
        return SubmissionDataFormat.XML
    if extension == "jsonld":
        return SubmissionDataFormat.JSON
    return ""


def _accepted_extensions_for_port(port: ArtifactPortLike) -> tuple[str, ...]:
    """Return accepted file extensions declared on the artifact port."""

    metadata = port.metadata or {}
    raw_extensions = metadata.get("accepted_extensions") or []
    normalized: list[str] = []
    for ext in raw_extensions:
        value = str(ext or "").strip().lower().lstrip(".")
        if value:
            normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def _extension_from_uri(uri: str) -> str:
    """Return a URI's lowercase file extension without the dot."""

    filename = _filename_from_uri(uri)
    suffix = Path(filename).suffix.lower()
    return suffix[1:] if suffix.startswith(".") else suffix


def _filename_from_uri(uri: str) -> str:
    """Return the final path component from a storage URI."""

    parsed = urlparse(uri)
    path = parsed.path or uri
    return Path(unquote(path)).name
