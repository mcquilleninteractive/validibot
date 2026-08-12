from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from defusedxml import ElementTree as ET  # noqa: N817
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from slugify import slugify
from validibot_shared.fmu import FMUProbeResult as FMUProbeResultSchema
from validibot_shared.fmu import FMUVariableMeta

from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import FMU_MODEL_RESOURCE
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import FMUProbeStatus
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.models import FMUModel
from validibot.validations.models import FMUProbeResult
from validibot.validations.models import FMUVariable
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.step_io_metadata.metadata import FMUProviderBinding
from validibot.validations.step_io_metadata.metadata import FMUStepIOMetadata

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable

    from django.core.files.uploadedfile import UploadedFile

    from validibot.users.models import Organization


MAX_FMU_SIZE_BYTES = 50 * 1024 * 1024
DISALLOWED_EXTENSIONS = {
    ".exe",
    ".bat",
    ".sh",
    ".cmd",
}
FMU_MODEL_PORT_KEY = "fmu_model"


# ---------------------------------------------------------------------------
# Introspection data types
# ---------------------------------------------------------------------------
# These are plain dataclasses (not Django models) so that introspection
# results can be consumed by both:
#   - the library-validator flow (converts to FMUVariable + StepIODefinition rows)
#   - the step-level flow (converts to step config JSON dicts)


@dataclass
class FMUVariableInfo:
    """Parsed metadata for a single FMU variable.

    A plain data object (not a Django model instance) so it can be
    used by both the library-validator and step-level flows without
    coupling to the database layer.
    """

    name: str
    causality: str
    variability: str = ""
    value_reference: int = 0
    value_type: str = "Real"
    unit: str = ""
    description: str = ""


@dataclass
class FMUSimulationDefaults:
    """Default simulation settings from the FMU's DefaultExperiment element.

    These are optional — not all FMUs include a DefaultExperiment.
    When present, they serve as pre-populated defaults in the step
    config form.  The workflow author can override any of them.
    """

    start_time: float | None = None
    stop_time: float | None = None
    step_size: float | None = None
    tolerance: float | None = None


@dataclass
class FMUIntrospectionResult:
    """Result of introspecting an FMU archive.

    Contains everything needed by both the library-validator flow
    (which creates FMUModel + FMUVariable rows) and the step-level
    flow (which stores variable metadata in step config).
    """

    model_name: str
    fmi_version: str
    variables: list[FMUVariableInfo] = field(default_factory=list)
    simulation_defaults: FMUSimulationDefaults = field(
        default_factory=FMUSimulationDefaults,
    )
    checksum: str = ""


class FMUIntrospectionError(ValueError):
    """Raised when an FMU cannot be parsed or introspected."""


class FMUStorageError(ValueError):
    """Raised when FMU files cannot be stored or accessed."""


# ---------------------------------------------------------------------------
# Shared introspection layer
# ---------------------------------------------------------------------------


def _parse_model_description(
    xml_text: str,
) -> tuple[str, str, list[FMUVariableInfo], FMUSimulationDefaults]:
    """Parse modelDescription.xml into variable info and simulation defaults.

    Returns ``(model_name, fmi_version, variables, simulation_defaults)``.
    This is a pure parsing function with no side effects or database access.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FMUIntrospectionError("Unable to parse modelDescription.xml.") from exc

    fmi_version = root.attrib.get("fmiVersion", "2.0")
    model_name = root.attrib.get("modelName", "fmu")
    ns_prefix = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""

    # -- ScalarVariable extraction --
    tag_name = f"{{{ns_prefix}}}ScalarVariable" if ns_prefix else "ScalarVariable"
    variables: list[FMUVariableInfo] = []
    for node in root.iter(tag_name):
        attrs = node.attrib
        name = attrs.get("name") or ""
        if not name:
            continue
        value_type = next(
            (child.tag.split("}", 1)[-1] for child in node if child.tag),
            "Real",
        )
        variables.append(
            FMUVariableInfo(
                name=name,
                causality=attrs.get("causality", "unknown"),
                variability=attrs.get("variability", ""),
                value_reference=int(attrs.get("valueReference") or 0),
                value_type=value_type,
                unit=attrs.get("unit", ""),
                description=attrs.get("description", ""),
            ),
        )

    # -- DefaultExperiment extraction --
    de_tag = f"{{{ns_prefix}}}DefaultExperiment" if ns_prefix else "DefaultExperiment"
    sim_defaults = FMUSimulationDefaults()
    de_node = root.find(de_tag)
    if de_node is not None:
        de_attrs = de_node.attrib
        if "startTime" in de_attrs:
            sim_defaults.start_time = float(de_attrs["startTime"])
        if "stopTime" in de_attrs:
            sim_defaults.stop_time = float(de_attrs["stopTime"])
        if "stepSize" in de_attrs:
            sim_defaults.step_size = float(de_attrs["stepSize"])
        if "tolerance" in de_attrs:
            sim_defaults.tolerance = float(de_attrs["tolerance"])

    return model_name, fmi_version, variables, sim_defaults


def introspect_fmu(payload: bytes, filename: str) -> FMUIntrospectionResult:
    """Validate and introspect an FMU archive.

    Performs structural validation (ZIP, modelDescription.xml presence,
    no dangerous files), parses variable metadata from ScalarVariables,
    and extracts DefaultExperiment settings.

    Used by both:
    - ``create_fmu_validator()`` (library flow) — results feed into
      FMUModel + FMUVariable + StepIODefinition creation.
    - ``build_fmu_config()`` (step-level flow) — results feed into
      ``sync_step_fmu_io_definitions()`` and ``step.config["fmu_simulation"]``.

    Raises ``FMUIntrospectionError`` on validation failure.
    """
    display_name = filename or "fmu"
    if len(payload) > MAX_FMU_SIZE_BYTES:
        raise FMUIntrospectionError(
            _("FMU %(name)s exceeds the maximum size of %(limit)s bytes.")
            % {"name": display_name, "limit": MAX_FMU_SIZE_BYTES},
        )
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            names = archive.namelist()
            if "modelDescription.xml" not in names:
                raise FMUIntrospectionError(
                    _("FMU %(name)s is missing modelDescription.xml.")
                    % {"name": display_name},
                )
            for name in names:
                member = name.lower()
                suffix = Path(member).suffix
                if member.startswith(("../", "/")):
                    raise FMUIntrospectionError(
                        _("FMU %(name)s contains unsafe path entries.")
                        % {"name": display_name},
                    )
                if suffix in DISALLOWED_EXTENSIONS:
                    raise FMUIntrospectionError(
                        _("FMU %(name)s contains disallowed file %(file)s.")
                        % {"name": display_name, "file": name},
                    )
            with archive.open("modelDescription.xml") as handle:
                xml_text = handle.read().decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise FMUIntrospectionError(
            _("FMU %(name)s is not a valid zip archive.") % {"name": display_name}
        ) from exc
    except UnicodeDecodeError as exc:
        raise FMUIntrospectionError(
            _("modelDescription.xml in %(name)s is not UTF-8 text.")
            % {"name": display_name}
        ) from exc

    model_name, fmi_version, variables, sim_defaults = _parse_model_description(
        xml_text,
    )
    checksum = hashlib.sha256(payload).hexdigest()
    return FMUIntrospectionResult(
        model_name=model_name,
        fmi_version=fmi_version,
        variables=variables,
        simulation_defaults=sim_defaults,
        checksum=checksum,
    )


# ── FMU parser-fact specs (single source of truth) ───────────────────
#
# Phase 6 (ADR-2026-05-22b) parser facts. This list is the canonical
# definition consumed by THREE call sites:
#
#   1. ``build_introspection_metadata`` (this module) — extracts and
#      stamps these values on ``FMUModel.introspection_metadata`` at
#      upload/probe time, AND on ``WorkflowStep.config['fmu_introspection']``
#      for step-level uploads (via ``build_step_fmu_introspection``).
#   2. ``validators/fmu/config.py`` — derives the system FMU validator's
#      static ``CatalogEntrySpec`` list from this collection so a new
#      fact added here surfaces in the catalog without a parallel edit.
#   3. ``_seed_parser_fact_io_definitions`` (this module) and
#      ``services/fmu_step_io.seed_step_parser_fact_io_definitions`` — seed
#      identical INPUT-direction ``StepIODefinition`` rows on per-FMU
#      validators (library path) and per-step (step-level path), so
#      ``i.fmi_version`` etc. resolve regardless of which path was used.
#
# A single dataclass instead of two parallel lists (the original Phase 6
# sketch) means richness can't drift between catalog and seeded rows —
# the May 2026 review caught that the system catalog had descriptions
# and units while the seeded rows didn't, making the same ``i.*`` fact
# look different depending on the validator binding.
#
# ``extractor`` is a callable that pulls the value out of an
# ``FMUIntrospectionResult``. Keeping extraction co-located with the
# rest of the spec is the simplest way to keep the dict-builder honest:
# the keys in build_introspection_metadata's return value cannot drift
# from PARSER_FACT_SPECS because they ARE PARSER_FACT_SPECS.


@dataclass(frozen=True)
class FMUParserFactSpec:
    """Single-source-of-truth spec for one FMU parser fact.

    Holds everything the three call sites need: extraction logic
    (``extractor``), runtime contract (``contract_key``, ``data_type``),
    UI/catalog richness (``label``, ``description``, ``units``,
    ``order``), and authoring policy (``on_missing``).

    The CatalogEntrySpec equivalents are derived in
    ``validators/fmu/config.py``; the StepIODefinition equivalents are
    seeded in ``_seed_parser_fact_io_definitions`` and
    ``seed_step_parser_fact_io_definitions``. All three paths read from the
    SAME spec, so a change here propagates without parallel edits.
    """

    contract_key: str
    label: str
    data_type: str  # one of CatalogValueType.*
    description: str
    extractor: Callable[[FMUIntrospectionResult], Any]
    units: str = ""
    order: int = 0
    on_missing: str = "null"


def _count_causality(result: FMUIntrospectionResult, causality: str) -> int:
    """Count variables matching the given causality (case-insensitive)."""
    return sum(1 for v in result.variables if (v.causality or "").lower() == causality)


def _has_simulation_defaults(result: FMUIntrospectionResult) -> bool:
    """True when DefaultExperiment supplied at least one timing field.

    Per the May 2026 review (P3 finding): the original
    ``has_default_experiment`` name implied XML-element presence, but
    the underlying dataclass discards element presence — only the
    populated attribute values survive parsing. Renamed to
    ``has_simulation_defaults`` so the name matches what's actually
    detected (any of start_time / stop_time / step_size / tolerance
    being set). An empty ``<DefaultExperiment/>`` element therefore
    returns False, matching the renamed semantics.
    """
    sim = result.simulation_defaults
    return any(
        getattr(sim, field) is not None
        for field in ("start_time", "stop_time", "step_size", "tolerance")
    )


PARSER_FACT_SPECS: tuple[FMUParserFactSpec, ...] = (
    FMUParserFactSpec(
        contract_key="model_name",
        label="Model Name",
        data_type=CatalogValueType.STRING,
        description=(
            "Name declared in the FMU's modelDescription.xml "
            "``modelName`` attribute. Useful for sanity-checking that "
            "the expected FMU is bound, e.g. "
            "``i.model_name == 'BuildingControl'``."
        ),
        extractor=lambda result: result.model_name,
        order=10,
    ),
    FMUParserFactSpec(
        contract_key="fmi_version",
        label="FMI Version",
        data_type=CatalogValueType.STRING,
        description=(
            "FMI specification version declared in modelDescription.xml "
            "(e.g. ``2.0`` or ``3.0``). Gate dispatch on FMI "
            "compatibility with ``i.fmi_version == '2.0'``."
        ),
        extractor=lambda result: result.fmi_version,
        order=11,
    ),
    FMUParserFactSpec(
        contract_key="variable_count",
        label="Variable Count",
        data_type=CatalogValueType.NUMBER,
        description=(
            "Total count of ScalarVariable entries in modelDescription.xml "
            "(all causalities combined). Quick sanity check that the FMU "
            "is non-empty."
        ),
        extractor=lambda result: len(result.variables),
        units="count",
        order=12,
    ),
    FMUParserFactSpec(
        contract_key="input_variable_count",
        label="Input Variable Count",
        data_type=CatalogValueType.NUMBER,
        description=(
            "Count of ScalarVariable entries with ``causality='input'``. "
            "Use ``i.input_variable_count >= N`` to guard that the FMU "
            "exposes the inputs the workflow expects."
        ),
        extractor=lambda result: _count_causality(result, "input"),
        units="count",
        order=13,
    ),
    FMUParserFactSpec(
        contract_key="output_variable_count",
        label="Output Variable Count",
        data_type=CatalogValueType.NUMBER,
        description=(
            "Count of ScalarVariable entries with ``causality='output'``. "
            "An FMU with zero outputs can be simulated but yields no "
            "observables — ``i.output_variable_count >= 1`` catches it."
        ),
        extractor=lambda result: _count_causality(result, "output"),
        units="count",
        order=14,
    ),
    FMUParserFactSpec(
        contract_key="parameter_count",
        label="Parameter Count",
        data_type=CatalogValueType.NUMBER,
        description=(
            "Count of ScalarVariable entries with ``causality='parameter'``. "
            "Useful for assertions on FMUs that expect tunable parameters."
        ),
        extractor=lambda result: _count_causality(result, "parameter"),
        units="count",
        order=15,
    ),
    FMUParserFactSpec(
        contract_key="has_simulation_defaults",
        label="Has Simulation Defaults",
        data_type=CatalogValueType.BOOLEAN,
        description=(
            "True when modelDescription.xml's DefaultExperiment supplies "
            "at least one timing field (startTime, stopTime, stepSize, "
            "or tolerance). An empty ``<DefaultExperiment/>`` returns "
            "False because no usable defaults are available. Some "
            "workflow templates require an FMU to ship its own "
            "simulation defaults."
        ),
        extractor=_has_simulation_defaults,
        order=16,
    ),
)


# Set of contract keys for fast catalog-filtering in ``extract_input_values``.
PARSER_FACT_KEYS: frozenset[str] = frozenset(s.contract_key for s in PARSER_FACT_SPECS)


def build_introspection_metadata(
    result: FMUIntrospectionResult,
) -> dict[str, Any]:
    """Build the FMU parser-fact dict persisted on ``FMUModel.introspection_metadata``.

    The dict is keyed by ``contract_key`` for direct consumption by
    ``FMUValidator.extract_input_values``. Each value is produced by
    its spec's ``extractor`` callable, so adding a new fact to
    ``PARSER_FACT_SPECS`` automatically extends what this writes —
    no parallel edit here.

    Kept as a top-level helper so both the upload
    (``create_fmu_validator``) and probe (``run_fmu_probe``) paths
    stamp the same shape.
    """
    return {spec.contract_key: spec.extractor(result) for spec in PARSER_FACT_SPECS}


def _data_type_for_variable(value_type: str) -> str:
    vt = (value_type or "").lower()
    if vt in {"real", "integer", "enumeration"}:
        return CatalogValueType.NUMBER
    if vt == "boolean":
        return CatalogValueType.BOOLEAN
    if vt == "string":
        return CatalogValueType.STRING
    return CatalogValueType.OBJECT


def _parser_fact_step_io_defaults(spec: FMUParserFactSpec) -> dict[str, Any]:
    """Shared StepIODefinition ``defaults`` payload for one parser fact.

    Used by ``_seed_parser_fact_io_definitions`` (library FMU validators) and
    ``services.fmu_step_io.seed_step_parser_fact_io_definitions`` (step-level
    FMU uploads). Sharing the dict-builder means the two seeding paths
    can't drift in subtle ways (e.g., one ends up with a label and the
    other doesn't), which was the May 2026 review's P2 concern.

    ``on_missing`` is copied through so the seeded rows match the
    richness of the system catalog entries derived from the same spec
    in ``validators/fmu/config.py``. Today every spec defaults to
    ``"null"``; carrying it explicitly removes the latent drift risk
    the next reviewer flagged in the P3 follow-up.
    """
    return {
        "native_name": spec.contract_key,
        "label": spec.label,
        "description": spec.description,
        "origin_kind": StepIOOriginKind.FMU,
        "source_kind": StepIOSourceKind.INTERNAL,
        "is_path_editable": False,
        "data_type": spec.data_type,
        "on_missing": spec.on_missing,
        "provider_binding": {},
        "metadata": {"units": spec.units} if spec.units else {},
    }


def _fmu_model_port_step_io_defaults() -> dict[str, Any]:
    """Return StepIODefinition defaults for a validator-owned FMU file port."""

    return {
        "native_name": FMU_MODEL_PORT_KEY,
        "label": "FMU Model",
        "description": (
            "Resolved Functional Mock-up Unit file passed to the backend "
            "as the FMU model input."
        ),
        "origin_kind": StepIOOriginKind.CATALOG,
        "source_kind": StepIOSourceKind.PAYLOAD_PATH,
        "is_path_editable": False,
        "data_type": CatalogValueType.ARTIFACT_REF,
        "io_medium": StepIOMedium.ARTIFACT,
        "artifact_kind": ArtifactKind.FILE,
        "media_type": "application/vnd.fmi.fmu",
        "data_format": SubmissionDataFormat.FMU,
        "accepted_data_formats": [SubmissionDataFormat.FMU],
        "accepted_media_types": ["application/vnd.fmi.fmu"],
        "accepted_extensions": ["fmu"],
        "allowed_source_scopes": [
            BindingSourceScope.SYSTEM,
            BindingSourceScope.WORKFLOW_RESOURCE,
        ],
        "default_source_strategy": DefaultSourceStrategy.WORKFLOW_RESOURCE_DEFAULT,
        "envelope_channel": EnvelopeChannel.INPUT_FILES,
        "resource_type": FMU_MODEL_RESOURCE,
        "role": "fmu",
        "is_collection": False,
        "min_items": 1,
        "max_items": 1,
        "provider_binding": {
            "envelope_channel": EnvelopeChannel.INPUT_FILES,
            "role": "fmu",
        },
        "metadata": {},
        "on_missing": "error",
        "order": 1,
    }


def _seed_parser_fact_io_definitions(validator: Validator) -> None:
    """Seed parser-fact and file-port rows on a user-created FMU validator.

    These rows declare the ``fmu_model`` artifact input port and
    INPUT-direction parser facts (one per spec in ``PARSER_FACT_SPECS``) so
    the user's FMU validator advertises the same file/input contract as the
    system FMU validator does via its config.py catalog. Without this seeding,
    input-stage CEL assertions targeting ``i.fmi_version`` etc. would resolve
    cleanly on a workflow step bound to the system validator and silently
    resolve to null when re-bound to a user-created FMU validator that wraps
    the same FMU — a footgun for workflow authors who organise reusable
    assertion logic.

    Identity-stable: keyed by ``(validator, contract_key, direction)``
    via ``update_or_create``. Probe refreshes
    (``_refresh_variables_from_probe``) reuse the same rows rather
    than recreating them, preserving downstream FK relationships
    (StepInputBinding, WorkflowStepIOPromotion) that cascade rules
    would otherwise nuke on every re-probe.

    Collision tracking lives in the caller's ``survivors`` set, not
    here — this helper is concerned only with the parser-fact rows
    themselves, and the caller separately records the
    (contract_key, INPUT) tuples it claimed.
    """
    StepIODefinition.objects.update_or_create(
        validator=validator,
        contract_key=FMU_MODEL_PORT_KEY,
        direction=StepIODirection.INPUT,
        defaults=_fmu_model_port_step_io_defaults(),
    )

    for spec in PARSER_FACT_SPECS:
        StepIODefinition.objects.update_or_create(
            validator=validator,
            contract_key=spec.contract_key,
            direction=StepIODirection.INPUT,
            defaults=_parser_fact_step_io_defaults(spec),
        )


def _direction_for_causality(causality: str) -> str | None:
    """Map FMU causality to step I/O direction, or None for unsupported types."""
    lowered = (causality or "").lower()
    if lowered == "input":
        return StepIODirection.INPUT
    if lowered == "output":
        return StepIODirection.OUTPUT
    return None


def _should_use_cloud_storage() -> bool:
    """
    Determine whether FMUs should be uploaded to cloud storage or stored locally.

    Local/dev runs should keep files on the filesystem; production/staging
    enables cloud storage by configuring GCS_VALIDATION_BUCKET (GCP) or
    equivalent for other platforms.
    """

    return bool(settings.GCS_VALIDATION_BUCKET)


def _upload_fmu_to_cloud_storage(checksum: str, payload: bytes) -> str:
    """
    Upload a validated FMU payload to cloud storage and return its URI.

    Currently supports GCS (gs:// URIs). Uses checksum-based naming to
    deduplicate identical FMUs.
    """

    from google.cloud import storage

    bucket_name = settings.GCS_VALIDATION_BUCKET
    if not bucket_name:
        msg = "Cloud storage bucket is not configured (GCS_VALIDATION_BUCKET)."
        raise FMUStorageError(msg)

    object_path = f"fmus/{checksum}.fmu"
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    blob.upload_from_string(payload, content_type="application/octet-stream")
    return f"gs://{bucket_name}/{object_path}"


def _variable_info_to_model(info: FMUVariableInfo) -> FMUVariable:
    """Convert a plain FMUVariableInfo dataclass to an unsaved FMUVariable model."""
    return FMUVariable(
        name=info.name,
        causality=info.causality,
        variability=info.variability,
        value_reference=info.value_reference,
        value_type=info.value_type,
        unit=info.unit,
    )


def create_fmu_validator(
    *,
    org: Organization,
    project,
    name: str,
    upload: UploadedFile,
    short_description: str = "",
    description: str = "",
    approve_immediately: bool = True,
    storage_backend=None,
) -> Validator:
    """
    Create an FMU validator, parse the uploaded FMU, and seed catalog entries.

    Uses the shared ``introspect_fmu()`` layer for validation and metadata
    extraction, then converts the results into Django model instances
    (FMUModel, FMUVariable, StepIODefinition).

    When ``approve_immediately`` is False the FMU will remain unapproved until a
    probe run is completed. ``storage_backend`` can be supplied to stream the
    upload to S3 or other storage providers; by default Django's FileField
    storage is used.
    """

    with transaction.atomic():
        raw_bytes = upload.read()
        result = introspect_fmu(payload=raw_bytes, filename=upload.name)
        wrapped_upload = ContentFile(raw_bytes, name=upload.name)
        fmu = FMUModel.objects.create(
            org=org,
            project=project,
            name=name,
            description=description,
            size_bytes=len(raw_bytes),
            checksum=result.checksum,
            gcs_uri=_upload_fmu_to_cloud_storage(result.checksum, raw_bytes)
            if _should_use_cloud_storage()
            else "",
        )
        try:
            stored_file = (
                wrapped_upload
                if storage_backend is None
                else storage_backend(wrapped_upload)
            )
            # Save the FMU payload to the configured storage to ensure a local path
            # exists in dev/test and an object exists in cloud storage when enabled.
            fmu.file.save(upload.name, stored_file, save=False)
        except Exception as exc:  # pragma: no cover - storage failures are surfaced
            raise FMUStorageError(str(exc)) from exc
        fmu.fmu_version = result.fmi_version
        fmu.introspection_metadata = build_introspection_metadata(result)
        fmu.is_approved = approve_immediately
        fmu.save()

        validator = Validator.objects.create(
            org=org,
            name=name,
            slug=slugify(f"{org.id}-{name}"),
            short_description=short_description,
            description=description,
            validation_type=ValidationType.FMU,
            has_processor=True,
            fmu_model=fmu,
            supports_assertions=True,
        )

        model_variables = [_variable_info_to_model(info) for info in result.variables]
        _persist_variables(fmu, validator, model_variables)
        FMUProbeResult.objects.create(
            fmu_model=fmu,
            status=FMUProbeStatus.SUCCEEDED
            if approve_immediately
            else FMUProbeStatus.PENDING,
            last_error="" if approve_immediately else _("Awaiting probe run."),
            details={"variable_count": len(result.variables)},
        )
    return validator


def _persist_variables(
    fmu_model: FMUModel,
    validator: Validator,
    variables: Iterable[FMUVariable],
) -> None:
    """Persist FMUVariable rows and reconcile step I/O definitions.

    Identity-stable: ``StepIODefinition`` rows are reconciled by
    ``(validator, contract_key, direction)`` via ``update_or_create``.
    Existing rows for variables that survive re-upload keep their
    primary key, so downstream FKs — ``StepInputBinding``,
    ``WorkflowStepIOPromotion``, ``RulesetAssertion`` — keep
    pointing at the same row instead of getting nuked by a
    delete-then-recreate cycle.

    Orphans (rows whose contract_key didn't appear in this call) are
    deleted at the end — that's the only path that cascades.
    """
    prepared: list[FMUVariable] = []
    for var in variables:
        var.fmu_model = fmu_model
        prepared.append(var)
    FMUVariable.objects.bulk_create(prepared)

    # ``survivors`` tracks every (contract_key, direction) tuple we
    # touched in THIS call — both parser facts and FMU variables.
    # Two distinct uses:
    #   1. In-batch collision detection. When two variables slugify to
    #      the same key with the same direction (e.g., ``T_outdoor`` and
    #      ``t_outdoor`` both → ``t_outdoor`` INPUT), the second gets a
    #      ``-2`` suffix. Cross-direction (``T`` INPUT + ``T`` OUTPUT) is
    #      allowed by the model's (workflow_step|validator, contract_key,
    #      direction) uniqueness, so we don't suffix across directions.
    #   2. Orphan-detection at the end. Rows already in the DB whose
    #      tuple isn't in survivors correspond to variables that have
    #      disappeared and get deleted.
    #
    # CRITICAL: we do NOT check pre-existing DB keys for collisions
    # here. ``update_or_create`` reuses the existing row when
    # (validator, contract_key, direction) match — that's how we get
    # identity stability across re-probes. The May 2026 review's P1
    # finding caught that the prior check against DB keys defeated this:
    # a re-probe of T_outdoor would suffix to T_outdoor-2, leaving the
    # original as an orphan to be deleted, cascading any StepInputBinding,
    # WorkflowStepIOPromotion, or RulesetAssertion FKs on every probe.
    survivors: set[tuple[str, str]] = set()

    # Seed parser-fact StepIODefinition rows on this user-created FMU
    # validator so ``i.fmi_version`` / ``i.input_variable_count`` /
    # etc. resolve consistently whether the workflow author picked the
    # system FMU validator (whose catalog entries come from config.py
    # via sync_validators) or a user-created FMU validator (which is
    # seeded here, per FMU upload). Without this branch, the same
    # input-stage CEL assertion would pass on one validator and silently
    # resolve to null on another.
    _seed_parser_fact_io_definitions(validator)
    survivors.add((FMU_MODEL_PORT_KEY, StepIODirection.INPUT))
    survivors.update(
        (spec.contract_key, StepIODirection.INPUT) for spec in PARSER_FACT_SPECS
    )

    for var in prepared:
        direction = _direction_for_causality(var.causality)
        if not direction:
            continue
        base_key = slugify(var.name, separator="_") or "io_value"
        key = base_key
        counter = 2
        # Only suffix when THIS (key, direction) has already been
        # claimed in this batch — never against pre-existing DB rows.
        # Letting update_or_create reuse existing rows naturally is
        # what keeps StepIODefinition.pk stable across re-probes.
        #
        # Underscore separator matches ``services.fmu_step_io``'s
        # step-level path so CEL identifier-safe contract_keys stay
        # the convention across both seeding paths. Hyphenated keys
        # would force authors into bracket-access (``i["t_outdoor-2"]``)
        # instead of dot-access (``i.t_outdoor_2``).
        while (key, direction) in survivors:
            key = f"{base_key}_{counter}"
            counter += 1
        survivors.add((key, direction))
        StepIODefinition.objects.update_or_create(
            validator=validator,
            contract_key=key,
            direction=direction,
            defaults={
                "native_name": var.name,
                "origin_kind": StepIOOriginKind.FMU,
                "source_kind": (
                    StepIOSourceKind.PAYLOAD_PATH
                    if direction == StepIODirection.INPUT
                    else StepIOSourceKind.INTERNAL
                ),
                "is_path_editable": direction == StepIODirection.INPUT,
                "data_type": _data_type_for_variable(var.value_type),
                "provider_binding": FMUProviderBinding(
                    causality=var.causality,
                ).model_dump(),
                "metadata": FMUStepIOMetadata(
                    variability=var.variability,
                    value_reference=var.value_reference,
                    value_type=var.value_type,
                ).model_dump(),
            },
        )

    # ── Orphan cleanup ───────────────────────────────────────────
    # Delete StepIODefinition rows whose (contract_key, direction)
    # tuple didn't appear in this call — they correspond to variables
    # (or legacy parser facts) that no longer exist in this FMU.
    # Identity for surviving rows is preserved (they were updated in
    # place via ``update_or_create``), so downstream FKs to
    # StepInputBinding / WorkflowStepIOPromotion / RulesetAssertion
    # stay intact.
    #
    # Composite (contract_key, direction) membership matters: the same
    # contract_key can legitimately appear in both INPUT and OUTPUT
    # directions, so filtering by contract_key alone would either
    # over-delete (drop a valid surviving direction) or under-delete
    # (miss a row whose key matches but direction doesn't). The
    # explicit Python walk handles the tuple check; the queryset is
    # tiny (one validator) so the O(n) scan is fine.
    orphan_ids = [
        io_definition.pk
        for io_definition in validator.step_io_definitions.all()
        if (io_definition.contract_key, io_definition.direction) not in survivors
    ]
    if orphan_ids:
        validator.step_io_definitions.filter(pk__in=orphan_ids).delete()


def _read_fmu_bytes(fmu_model: FMUModel) -> bytes:
    """
    Read FMU file bytes from local storage or cloud storage.

    Raises FMUIntrospectionError if the file cannot be read.
    """
    # Try local file first
    if fmu_model.file:
        try:
            fmu_model.file.open("rb")
            payload = fmu_model.file.read()
            fmu_model.file.close()
        except Exception:  # noqa: S110 - intentionally silent, will try GCS next
            pass
        else:
            return payload

    # Try GCS if configured
    if fmu_model.gcs_uri:
        from google.cloud import storage

        # Parse gs://bucket/path format
        uri = fmu_model.gcs_uri
        if uri.startswith("gs://"):
            uri = uri[5:]
        parts = uri.split("/", 1)
        bucket_name, object_path = parts[0], parts[1] if len(parts) > 1 else ""
        if not object_path:
            msg = f"Invalid GCS URI: {fmu_model.gcs_uri}"
            raise FMUIntrospectionError(msg)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_path)
        return blob.download_as_bytes()

    msg = "FMU file not found in local storage or cloud storage."
    raise FMUIntrospectionError(msg)


def run_fmu_probe(
    fmu_model: FMUModel,
    *,
    _return_logs: bool = False,
) -> FMUProbeResultSchema:
    """
    Probe an FMU by parsing its modelDescription.xml and update metadata + catalog.

    This runs in-process (no container needed) since probing just unpacks the ZIP
    and parses XML to extract variable metadata. We use it to populate variables
    and mark the FMU as approved before allowing workflow authors to attach
    assertions.

    The return_logs parameter is kept for API compatibility but unused since
    in-process probing doesn't produce container logs.
    """
    import time

    probe_record, _ = FMUProbeResult.objects.get_or_create(
        fmu_model=fmu_model,
        defaults={"status": FMUProbeStatus.PENDING},
    )
    probe_record.status = FMUProbeStatus.RUNNING
    probe_record.last_error = ""
    probe_record.save(update_fields=["status", "last_error", "modified"])

    start_time = time.monotonic()

    try:
        payload = _read_fmu_bytes(fmu_model)
        result = introspect_fmu(
            payload=payload,
            filename=fmu_model.name or "model.fmu",
        )
    except FMUIntrospectionError as exc:
        probe_record.status = FMUProbeStatus.FAILED
        probe_record.last_error = str(exc)
        probe_record.save(update_fields=["status", "last_error", "modified"])
        fmu_model.is_approved = False
        fmu_model.save(update_fields=["is_approved", "modified"])
        return FMUProbeResultSchema.failure(errors=[str(exc)])
    except Exception as exc:
        probe_record.status = FMUProbeStatus.FAILED
        probe_record.last_error = str(exc)
        probe_record.save(update_fields=["status", "last_error", "modified"])
        fmu_model.is_approved = False
        fmu_model.save(update_fields=["is_approved", "modified"])
        return FMUProbeResultSchema.failure(errors=[str(exc)])

    elapsed = time.monotonic() - start_time

    # Convert FMUVariableInfo (dataclass) to FMUVariableMeta (Pydantic schema)
    variable_metas = [
        FMUVariableMeta(
            name=var.name,
            causality=var.causality,
            variability=var.variability or None,
            value_reference=var.value_reference,
            value_type=var.value_type,
            unit=var.unit or None,
        )
        for var in result.variables
    ]

    # Update FMU metadata. Keep both write sites (upload and probe)
    # using the same helper so the parser-fact contract surfaced via
    # ``FMUValidator.extract_input_values`` is identical regardless of
    # which path stamped the metadata.
    fmu_model.fmu_version = result.fmi_version
    fmu_model.introspection_metadata = build_introspection_metadata(result)

    # Refresh variables and catalog entries from probe results
    _refresh_variables_from_probe(fmu_model, variable_metas)

    # Mark as approved
    probe_record.status = FMUProbeStatus.SUCCEEDED
    probe_record.last_error = ""
    probe_record.details = {"variable_count": len(result.variables)}
    fmu_model.is_approved = True

    probe_record.save(update_fields=["status", "last_error", "details", "modified"])
    fmu_model.save(
        update_fields=[
            "is_approved",
            "fmu_version",
            "introspection_metadata",
            "modified",
        ]
    )

    return FMUProbeResultSchema.success(
        variables=variable_metas,
        execution_seconds=elapsed,
        messages=[
            f"Parsed {len(result.variables)} variables from modelDescription.xml",
        ],
    )


def _refresh_variables_from_probe(
    fmu_model: FMUModel,
    variables: list,
) -> None:
    """Reconcile FMU variables and step I/O definitions from probe output.

    Drops the legacy delete-then-recreate cycle (May 2026 review's
    P1/P2 finding). Instead:

    1. ``FMUVariable`` rows are deleted (they're identity-less —
       authors don't FK into them) and rebuilt — cheap and harmless.
    2. ``StepIODefinition`` rows are reconciled in-place by
       ``_persist_variables``' identity-stable upsert. Surviving
       (validator, contract_key, direction) tuples keep their PK so
       StepInputBinding, WorkflowStepIOPromotion, and
       RulesetAssertion FKs aren't cascaded away on every re-probe.

    The May 2026 review caught that ``StepIODefinition.objects.filter
    (validator=validator).delete()`` cascaded all of the author's
    assertion wiring on every probe refresh. This is now an
    orphan-only delete inside ``_persist_variables``.
    """
    validator = fmu_model.validators.first()
    if validator is None:
        return
    # FMUVariable rows have no FK from author-facing models, so the
    # delete-and-rebuild is safe here (unlike StepIODefinition).
    FMUVariable.objects.filter(fmu_model=fmu_model).delete()
    shaped_vars = [
        FMUVariable(
            fmu_model=fmu_model,
            name=var.name,
            causality=var.causality,
            variability=var.variability or "",
            value_reference=getattr(var, "value_reference", 0) or 0,
            value_type=var.value_type,
            unit=var.unit or "",
        )
        for var in variables
    ]
    _persist_variables(fmu_model, validator, shaped_vars)
