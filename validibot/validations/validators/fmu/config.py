"""
Configuration for the FMU system validator.

Per-variable step I/O definitions for the FMU's native input/output variables
are created dynamically from the attached FMU via introspection in
``services.fmu._persist_variables`` (library FMU validators) and
``services.fmu_step_io.sync_step_fmu_io_definitions`` (step-level uploads) —
this static config only defines parser-fact step inputs derived from
``modelDescription.xml``. Per ADR-2026-05-22b Phase 6, these facts
let workflow authors gate dispatch with input-stage assertions like
``i.fmi_version == "2.0"`` or ``i.input_variable_count > 0`` before
paying for simulation compute.

The catalog entries are **derived** from
``services.fmu.PARSER_FACT_SPECS`` rather than hand-written. That
single-source-of-truth pattern (May 2026 review P2 finding) prevents
drift between this catalog and the parser-fact ``StepIODefinition``
rows seeded on per-FMU validators / per-step uploads — adding a new
fact only requires extending ``PARSER_FACT_SPECS``.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import FMU_MODEL_RESOURCE
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ComputeTier
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.services.fmu import PARSER_FACT_SPECS
from validibot.validations.services.fmu import FMUParserFactSpec
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig


def _spec_to_catalog_entry(spec: FMUParserFactSpec) -> CatalogEntrySpec:
    """Derive a ``CatalogEntrySpec`` from a ``FMUParserFactSpec``.

    Keeps the catalog and the seeded ``StepIODefinition`` rows in
    lockstep — both paths build from the same spec, so a new field on
    ``FMUParserFactSpec`` propagates to both surfaces without parallel
    edits. The May 2026 review caught that hand-written catalog
    entries diverged from seeded rows (richer descriptions on one
    side, missing units on the other) — keeping a single derivation
    rules that class of bug out.
    """
    return CatalogEntrySpec(
        entry_type=CatalogEntryType.IO_DEFINITION,
        run_stage=CatalogRunStage.INPUT,
        slug=spec.contract_key,
        label=spec.label,
        data_type=spec.data_type,
        description=spec.description,
        binding_config={"source": "parser", "key": spec.contract_key},
        metadata={"units": spec.units} if spec.units else {},
        is_required=False,
        on_missing=spec.on_missing,
        order=spec.order,
        source_kind=StepIOSourceKind.INTERNAL,
        is_path_editable=False,
    )


config = ValidatorConfig(
    slug="fmu-validator",
    name="FMU Validation",
    short_description="Run FMUs and assert against inputs and outputs.",
    description="Validate and simulate Functional Mock-up Units (FMUs).",
    validation_type=ValidationType.FMU,
    execution_backend_slug="fmu",
    execution_runtime_contract="validibot-execution-v1",
    validator_class="validibot.validations.validators.fmu.validator.FMUValidator",
    output_envelope_class="validibot_shared.fmu.envelopes.FMUOutputEnvelope",
    image_name="validibot-validator-backend-fmu",
    # Version bump to revision 2 per ADR-2026-07-06: the FMU model itself is
    # now a declared artifact input port (``fmu_model``), rather than an
    # implicit envelope-builder convention. This is semantic validator-contract
    # drift, so it must create a new validator version instead of mutating v1.
    #
    # Earlier history: ADR-2026-05-22b Phase 6 added seven parser-fact
    # step inputs derived from modelDescription.xml at upload/probe
    # time (model_name, fmi_version, variable counts, has_simulation_defaults).
    version=3,
    order=20,
    has_processor=True,
    processor_name="FMU Simulation",
    is_system=True,
    supports_assertions=True,
    compute_tier=ComputeTier.HIGH,
    icon="bi-cpu",
    card_image="FMU_card_img_small.png",
    catalog_entries=[
        CatalogEntrySpec(
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            slug="fmu_model",
            label="FMU Model",
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Resolved Functional Mock-up Unit file passed to the backend "
                "as the FMU model input."
            ),
            binding_config={
                "envelope_channel": EnvelopeChannel.INPUT_FILES,
                "role": "fmu",
            },
            accepted_extensions=["fmu"],
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/vnd.fmi.fmu",
            data_format=SubmissionDataFormat.FMU,
            accepted_data_formats=[SubmissionDataFormat.FMU],
            accepted_media_types=["application/vnd.fmi.fmu"],
            allowed_source_scopes=[
                BindingSourceScope.WORKFLOW_RESOURCE,
                BindingSourceScope.SYSTEM,
            ],
            default_source_strategy=DefaultSourceStrategy.WORKFLOW_RESOURCE_DEFAULT,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            resource_type=FMU_MODEL_RESOURCE,
            role="fmu",
            min_items=1,
            max_items=1,
        ),
        *[_spec_to_catalog_entry(spec) for spec in PARSER_FACT_SPECS],
    ],
)
