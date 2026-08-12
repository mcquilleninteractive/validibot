"""Validator config for the Schematron validator.

This is the single source of truth for the system Schematron validator's
metadata: slug, name, description, supported file types, and the dotted path
to the validator class. The community ``sync_validators`` management command
and the runtime registry both consume this config. Creating this module is
what makes the validator discoverable — ``discover_configs()`` scans
validator sub-packages via ``pkgutil`` and imports each ``config.py`` (no
``validators/__init__.py`` edit required, per ADR-2026-07-01 D2).

Schematron is an **advanced/container-routed** validator (SHACL posture):
Saxon + rule-pack XSLT run only in the isolated
``validibot-validator-backend-schematron`` container, while the compute tier
stays LOW — isolation is a safety posture, not a price change (D4).

NOTE on translations: ``ValidatorConfig`` / ``CatalogEntrySpec`` are pydantic
models with strict ``str`` fields — ``gettext_lazy`` proxies crash app boot.
Plain strings are intentional, matching every other validator config.

NOTE on ``output_envelope_class``: ``register_validator_config()`` resolves
the dotted path eagerly at app boot, so this config requires
``validibot-shared`` >= 0.11.0 (which provides
``validibot_shared.schematron``) — an older shared release makes Django
startup fail loudly here rather than 400 later on the callback path.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
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
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="schematron-validator",
    name="Schematron Validator",
    short_description=(
        "Validate XML submissions against your uploaded Schematron rules "
        "(e.g. EN 16931, Peppol BIS Billing 3.0) and report failed rules "
        "by their native IDs."
    ),
    description=(
        "Run Schematron business rules against XML documents. Upload the "
        "rules in the step configuration — for example a published "
        "standard's official .sch file — and findings preserve the native "
        "rule identifiers like BR-CO-15 and PEPPOL-EN16931-R010. Pairs "
        "with an XML Schema step for a complete structural + business-rule "
        "pre-flight. This is a pre-flight developer aid, not a "
        "certification of compliance."
    ),
    validation_type=ValidationType.SCHEMATRON,
    execution_backend_slug="schematron",
    execution_runtime_contract="validibot-execution-v1",
    validator_class=(
        "validibot.validations.validators.schematron.validator.SchematronValidator"
    ),
    # Typed container output contract — Django deserializes output.json with
    # this. The callback path hard-fails without it (D4b).
    output_envelope_class=(
        "validibot_shared.schematron.envelopes.SchematronOutputEnvelope"
    ),
    # Cloud Run Job / Docker image. Set explicitly because the slug
    # ("schematron-validator") would otherwise produce the wrong convention
    # name ("validibot-validator-backend-schematron-validator").
    image_name="validibot-validator-backend-schematron",
    has_processor=True,
    processor_name="Schematron Validation",
    # v3: ADR-2026-07-06 declares the uploaded SVRL report as the
    # ``svrl_report`` output artifact port, giving the canonical report bytes
    # a stable artifact reference in addition to parsed step outputs.
    #
    # v2: ADR-2026-07-06 declares the XML document as the ``xml_document``
    # artifact input port, rather than an implicit ``primary_file_uri`` envelope
    # convention. Schematron rules remain inline in SchematronInputs for this
    # slice; only the submitted XML document becomes a file-port contract.
    version=4,
    order=3,
    supports_assertions=True,
    # Routed to a container for isolation (Saxon/XSLT over untrusted XML),
    # not for heavy compute — metered by launch count (ADR-2026-07-01 D4).
    compute_tier=ComputeTier.LOW,
    icon="bi-card-checklist",
    card_image="default_card_img_small.png",
    # All step outputs, populated from the container's SVRL summary
    # (INTERNAL source, non-editable path) — same shape as the SHACL config.
    catalog_entries=[
        CatalogEntrySpec(
            slug="xml_document",
            label="XML Document",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Resolved XML document passed to the Schematron backend as "
                "the primary input file."
            ),
            accepted_file_types=[SubmissionFileType.XML],
            accepted_extensions=["xml"],
            is_required=True,
            on_missing="error",
            order=1,
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.FILE,
            media_type="application/xml",
            data_format=SubmissionDataFormat.XML,
            accepted_data_formats=[SubmissionDataFormat.XML],
            accepted_media_types=["application/xml", "text/xml"],
            allowed_source_scopes=[
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
            default_source_strategy=DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
            envelope_channel=EnvelopeChannel.INPUT_FILES,
            role="xml-document",
            min_items=1,
            max_items=1,
        ),
        CatalogEntrySpec(
            slug="svrl_report",
            label="SVRL Report",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            description=(
                "Schematron Validation Report Language (SVRL) XML report "
                "uploaded by the backend for evidence and downstream artifact "
                "references."
            ),
            binding_config={"source": "output_artifact", "role": "svrl-report"},
            accepted_extensions=["svrl"],
            is_required=False,
            on_missing="null",
            order=5,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.REPORT,
            media_type="application/xml",
            data_format=SubmissionDataFormat.XML,
            accepted_data_formats=[SubmissionDataFormat.XML],
            accepted_media_types=["application/xml", "text/xml"],
            allowed_source_scopes=[],
            default_source_strategy=DefaultSourceStrategy.NONE,
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="svrl-report",
            min_items=0,
            max_items=1,
        ),
        CatalogEntrySpec(
            slug="passed",
            label="Passed",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description=(
                "Whether the run produced zero ERROR-level findings. Null "
                "(unknown) when the engine could not run the rules."
            ),
            order=10,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="error_count",
            label="Error Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of ERROR-level Schematron findings.",
            order=20,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="warning_count",
            label="Warning Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of WARNING-level Schematron findings.",
            order=30,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # `fired_rule_count`, NOT an "assertion count": svrl:fired-rule marks
        # a rule/context the engine evaluated, not an assertion that fired
        # (ADR-2026-07-01 D3 SVRL note).
        CatalogEntrySpec(
            slug="fired_rule_count",
            label="Fired Rule Count",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description=(
                "Number of Schematron rules/contexts the engine evaluated "
                "(svrl:fired-rule elements) — not a count of failed "
                "assertions."
            ),
            order=40,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # A MAP of {rule_id: severity}, e.g. {"BR-CO-15": "ERROR"} — pinned
        # so CEL `"BR-CO-15" in o.finding_rule_ids_by_severity` is key
        # membership and severity is queryable (ADR-2026-07-01 D2). Named
        # "finding_*" because svrl:successful-report entries are active
        # findings too, not just failed asserts.
        CatalogEntrySpec(
            slug="finding_rule_ids_by_severity",
            label="Finding Rule IDs by Severity",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.OBJECT,
            description=(
                "Map of native rule id to resolved severity for every "
                'active finding, e.g. {"BR-CO-15": "ERROR"}. Supports CEL '
                "membership tests and severity-aware gates."
            ),
            order=50,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ── Provenance of the executed rules (D5) ──
        CatalogEntrySpec(
            slug="query_binding",
            label="Query Binding",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            description=(
                "Query binding detected from the uploaded rules (xslt1/xslt2)."
            ),
            order=60,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="engine",
            label="Engine",
            entry_type=CatalogEntryType.IO_DEFINITION,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            description=(
                "XSLT engine (name + version) that executed the rules, "
                "e.g. 'SaxonC-HE 12.9'."
            ),
            order=70,
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        ),
    ],
)
