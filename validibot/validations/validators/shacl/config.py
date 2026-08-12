"""Validator config for the SHACL validator.

This is the single source of truth for the system SHACL validator's
metadata: slug, name, description, supported file types, and the
dotted path to the validator class. The community ``sync_validators``
management command and the runtime registry both consume this config.

Library-level custom SHACL validators (org-owned ``Validator`` rows
with ``is_system=False`` and a populated ``default_ruleset``) reuse
the same engine class and the same ``validation_type`` but are created
through the validator-library UI rather than declared here.

NOTE on translations: ``ValidatorConfig`` and ``CatalogEntrySpec`` are
pydantic BaseModels with strict ``str`` fields. ``gettext_lazy`` proxies
are NOT valid strings to pydantic and raise ``ValidationError`` on
import — crashing app boot. Plain strings are intentional here and match
the convention used by every other validator config. If translation is
needed for these labels in the future, translate at the render site
(template ``{% trans %}`` or view-layer ``_()``), not at the config
definition.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="shacl-validator",
    name="SHACL Validator",
    short_description=(
        "Validate RDF graphs against SHACL shapes to check required "
        "classes, properties, and semantic constraints."
    ),
    description=(
        "Validate RDF graphs (Turtle, JSON-LD, RDF/XML) against SHACL "
        "shapes. Common configurations include ASHRAE 223P, Guideline "
        "36, Brick Schema, Project Haystack 4, and project-specific "
        "shapes."
    ),
    validation_type=ValidationType.SHACL,
    execution_backend_slug="shacl",
    execution_runtime_contract="validibot-execution-v1",
    validator_class=("validibot.validations.validators.shacl.validator.SHACLValidator"),
    # Typed container output contract — Django deserializes output.json with this.
    output_envelope_class="validibot_shared.shacl.envelopes.SHACLOutputEnvelope",
    # Cloud Run Job / Docker image. Set explicitly because the slug
    # ("shacl-validator") would otherwise produce the wrong convention name
    # ("validibot-validator-backend-shacl-validator").
    image_name="validibot-validator-backend-shacl",
    has_processor=True,
    processor_name="SHACL Validation",
    # v4: ADR-2026-07-06 declares the uploaded SHACL validation report as the
    # ``shacl_report`` output artifact port, giving report bytes a stable
    # workflow artifact reference in addition to the typed output field.
    #
    # v3: ADR-2026-07-06 declares the RDF submission as the ``data_graph``
    # artifact input port, rather than an implicit ``primary_file_uri`` envelope
    # convention. This is semantic validator-contract drift, so it creates a new
    # validator version instead of mutating v2 in place.
    #
    # v2: SHACL moved from in-process execution to the isolated container backend
    # (RDF parsing + author SPARQL now run in validibot-validator-backend-shacl,
    # never in the worker). The semantic digest changes with image_name /
    # output_envelope_class / has_processor, so the version MUST bump — see
    # services/validator_digest.py. compute_tier stays LOW: billing is unchanged.
    version=5,
    order=4,
    supports_assertions=True,
    catalog_entries=[
        CatalogEntrySpec(
            slug="data_graph",
            label="Data Graph",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Resolved RDF data graph passed to the SHACL backend as the "
                "primary submission file."
            ),
            accepted_file_types=[
                SubmissionFileType.TEXT,
                SubmissionFileType.JSON,
                SubmissionFileType.XML,
            ],
            accepted_extensions=["ttl", "rdf", "jsonld", "nt", "nq"],
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="text/turtle",
            data_format=SubmissionDataFormat.TEXT,
            accepted_data_formats=[
                SubmissionDataFormat.TEXT,
                SubmissionDataFormat.JSON,
                SubmissionDataFormat.XML,
            ],
            accepted_media_types=[
                "text/turtle",
                "application/rdf+xml",
                "application/ld+json",
                "application/n-triples",
                "application/n-quads",
            ],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="data-graph",
            min_items=1,
            max_items=1,
        ),
        CatalogEntrySpec(
            slug="shacl_report",
            label="SHACL Report",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Serialized SHACL validation report uploaded by the backend "
                "as Turtle for evidence and downstream artifact references."
            ),
            binding_config={"source": "output_artifact", "role": "shacl-report"},
            accepted_extensions=["ttl"],
            is_required=False,
            on_missing="null",
            order=5,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.REPORT,
            media_type="text/turtle",
            data_format=SubmissionDataFormat.TEXT,
            accepted_data_formats=[SubmissionDataFormat.TEXT],
            accepted_media_types=["text/turtle"],
            allowed_source_scopes=[],
            default_source_strategy=DefaultSourceStrategy.NONE,
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="shacl-report",
            min_items=0,
            max_items=1,
        ),
        CatalogEntrySpec(
            slug="parse_ok",
            label="Parse OK",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether the submitted RDF parsed successfully.",
            order=10,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="parse_serialization",
            label="Parse Serialization",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            description="RDF serialization used by the SHACL parser.",
            order=20,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="triple_count",
            label="Triple Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of triples in the submitted RDF graph.",
            order=30,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="namespaces_present",
            label="Namespaces Present",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.OBJECT,
            description="Namespace URI list seen in the submitted RDF graph.",
            order=40,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="has_s223_namespace",
            label="Has ASHRAE 223P Namespace",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether the graph uses the ASHRAE 223P namespace.",
            order=50,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="has_g36_namespace",
            label="Has Guideline 36 Namespace",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether the graph uses the Guideline 36 namespace.",
            order=60,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="has_brick_namespace",
            label="Has Brick Namespace",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether the graph uses the Brick namespace.",
            order=70,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="shacl_violation_count",
            label="SHACL Violation Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of SHACL violation results.",
            order=80,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="shacl_warning_count",
            label="SHACL Warning Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of SHACL warning results.",
            order=90,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="shacl_info_count",
            label="SHACL Info Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of SHACL info results.",
            order=100,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="shacl_total_count",
            label="SHACL Total Result Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Total number of SHACL results at all severities.",
            order=110,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
    ],
    icon="bi-diagram-3",
    card_image="default_card_img_small.png",
)
