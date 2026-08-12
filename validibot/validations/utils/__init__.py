import logging

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from validibot.validations.constants import CustomValidatorType
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorReleaseState

logger = logging.getLogger(__name__)


def create_default_validators():
    """
    Create Validator model instances for every type of validator
    we need to have by default.
    """
    from validibot.validations.models import Validator
    from validibot.validations.validators.base.config import get_all_configs

    default_validators = [
        {
            "name": _("Basic Validator"),
            "slug": "basic-validator",
            "short_description": _(
                "The simplest validator. "
                "Allows workflow authors to define assertions directly "
                "without a predefined step I/O catalog.",
            ),
            "description": _(
                """
                <p>Workflow authors can use the 'Basic Validator' as a starting point
                for creating assertions directly. There are no predefined
                step inputs, step outputs, or assertions.
                Perfect for lightweight checks or ad-hoc rules expressed in CEL.</p>
                """
            ),
            "validation_type": ValidationType.BASIC,
            "version": 1,
            "order": 0,
            "allow_custom_assertion_targets": True,
            "supports_assertions": True,
        },
        {
            "name": _("JSON Schema Validator"),
            "slug": "json-schema-validator",
            "short_description": _(
                "Validate JSON payloads against a JSON schema provided "
                "by the workflow author.",
            ),
            "description": _(
                """
                <p>
                This validator validates JSON payloads against a predefined JSON schema.
                When a workflow author selects this validator, they must attach
                a valid JSON schema.
                </p>
                """
            ),
            "validation_type": ValidationType.JSON_SCHEMA,
            "version": 1,
            "order": 1,
            "supports_assertions": True,
        },
        {
            "name": _("XML Validator"),
            "slug": "xml-validator",
            "short_description": _(
                "Validate XML submissions against a XSD, DTD, or RelaxNG "
                "schema provided by the workflow author.",
            ),
            "description": _(
                """
                <p>
                This validator validates XML submissions against an XSD, DTD
                or RelaxNG schema.
                When a workflow author selects this validator, they must attach
                a valid schema.
                </p>
                """
            ),
            "validation_type": ValidationType.XML_SCHEMA,
            "version": 1,
            "order": 2,
            "supports_assertions": True,
        },
        {
            "name": _("EnergyPlus™ Validator"),
            "slug": "energyplus-idf-validator",
            "short_description": _(
                "Validate EnergyPlus IDF files and outputs.",
            ),
            "description": _(
                """
                <p>Validate EnergyPlus IDF files for correctness and expected outputs.
                Run simulations, surface findings, and keep building models
                reliable.</p>
                """
            ),
            "validation_type": ValidationType.ENERGYPLUS,
            "version": 1,
            "order": 3,
            "has_processor": True,
            "supports_assertions": True,
        },
        {
            "name": _("FMU Validator"),
            "slug": "fmu-validator",
            "short_description": _(
                "Run FMUs and assert against inputs and outputs.",
            ),
            "description": _(
                """
                <p>
                This validator allows a workflow author to write assertions
                against incoming data.
                It allows a workflow author to validating incoming data as
                well as simulation outputs.
                </p>
                <p>
                The validator sends inputs using the Functional Mock-up Interface (FMI)
                standard to an
                FMU-based simulation running in an isolated runtime. If the
                simulation succeeds it
                gathers outputs and returns them as output values for further
                validation, if defined.
                </p>
                <p>
                The workflow author to write assertions against simulation
                output values.
                </p>
                """
            ),
            "validation_type": ValidationType.FMU,
            "version": 2,
            "order": 4,
            "has_processor": True,
            "supports_assertions": True,
            "allow_custom_assertion_targets": True,
        },
        {
            "name": _("AI Assisted Validator"),
            "slug": "ai-assisted-validator",
            "short_description": _(
                "Use AI to validate submission content against your criteria.",
            ),
            "description": _(
                """
                <p>Use AI to validate submission content against your criteria. Blend
                traditional assertions with AI scoring to review nuanced data quickly.
                </p>
                """
            ),
            "validation_type": ValidationType.AI_ASSIST,
            "version": 1,
            "order": 5,
            "release_state": ValidatorReleaseState.COMING_SOON,
            "supports_assertions": True,
        },
        {
            "name": _("THERM Validator"),
            "slug": "therm-validator",
            "short_description": _(
                "Validate THERM thermal analysis files (THMX/THMZ) "
                "for geometry, materials, and boundary conditions.",
            ),
            "description": _(
                """
                <p>Validate LBNL THERM files before submission to NFRC or
                other certification bodies. Checks geometry closure, material
                property ranges, boundary condition completeness, and
                reference integrity. Extracts output values for downstream
                compliance assertions (e.g. NFRC 100 winter conditions).</p>
                """
            ),
            "validation_type": ValidationType.THERM,
            "version": 1,
            "order": 6,
            "supports_assertions": True,
        },
    ]

    config_by_slug = {cfg.slug: cfg for cfg in get_all_configs()}
    created = 0
    updated = 0
    for validator_data in default_validators:
        config = config_by_slug.get(validator_data["slug"])
        resolved_data = validator_data
        if config:
            resolved_data = {
                **validator_data,
                "name": config.name,
                "short_description": config.short_description,
                "description": config.description,
                "version": config.version,
                "order": config.order,
                "has_processor": config.has_processor,
                "supports_assertions": config.supports_assertions,
            }
        defaults = resolved_data
        validator, was_created = Validator.objects.get_or_create(
            slug=resolved_data["slug"],
            version=resolved_data["version"],
            defaults=defaults,
        )
        if was_created:
            created += 1
            logger.info(f"  - created default validator: {validator.slug}")
        else:
            updated += 1

        # Update order in case it has changed
        validator.name = resolved_data["name"]
        validator.order = resolved_data["order"]
        validator.is_system = True
        validator.org = None
        validator.short_description = resolved_data.get("short_description") or ""
        validator.description = resolved_data.get("description") or ""
        validator.has_processor = validator_data.get(
            "has_processor",
            validator.has_processor,
        )
        validator.supports_assertions = validator_data.get(
            "supports_assertions",
            validator.supports_assertions,
        )
        validator.allow_custom_assertion_targets = validator_data.get(
            "allow_custom_assertion_targets",
            validator.allow_custom_assertion_targets,
        )
        # Set release_state from config, defaulting to PUBLISHED for system validators
        validator.release_state = validator_data.get(
            "release_state",
            ValidatorReleaseState.PUBLISHED,
        )
        validator.save()

        # Ensure every system validator has a default_ruleset for holding
        # validator-level assertions. The save() method calls
        # ensure_default_ruleset(), but for validators created before this
        # feature existed we also call it explicitly here.
        validator.ensure_default_ruleset()

    # Note: Catalog entries for system validators are synced separately via:
    #   python manage.py sync_validators
    # This function only creates the validator instances.

    return created, updated


def create_custom_validator(
    *,
    org,
    user,
    name: str,
    short_description: str = "",
    description: str,
    custom_type: str,
    notes: str = "",
    allow_custom_assertion_targets: bool = False,
    input_data_format: str,
):
    """Create a custom validator and matching CustomValidator wrapper."""
    from validibot.validations.models import CustomValidator
    from validibot.validations.models import Validator
    from validibot.validations.services.custom_validator_contracts import (
        sync_custom_validator_input_port,
    )

    base_validation_type = _custom_type_to_validation_type(custom_type)
    slug = _unique_validator_slug(org, name)
    validator = Validator.objects.create(
        name=name,
        short_description=short_description,
        description=description,
        validation_type=base_validation_type,
        org=org,
        is_system=False,
        slug=slug,
        allow_custom_assertion_targets=allow_custom_assertion_targets,
        supports_assertions=True,
    )
    custom_validator = CustomValidator.objects.create(
        validator=validator,
        org=org,
        created_by=user,
        custom_type=custom_type,
        base_validation_type=base_validation_type,
        notes=notes,
    )
    sync_custom_validator_input_port(
        validator=validator,
        data_format=input_data_format,
    )
    return custom_validator


def update_custom_validator(
    custom_validator,
    *,
    name: str,
    short_description: str,
    description: str,
    notes: str,
    allow_custom_assertion_targets: bool | None = None,
    input_data_format: str,
):
    """Update validator + custom metadata."""
    from validibot.validations.services.custom_validator_contracts import (
        sync_custom_validator_input_port,
    )

    validator = custom_validator.validator
    validator.name = name
    validator.short_description = short_description
    validator.description = description
    if allow_custom_assertion_targets is not None:
        validator.allow_custom_assertion_targets = allow_custom_assertion_targets
    validator.save(
        update_fields=[
            "name",
            "short_description",
            "description",
            "allow_custom_assertion_targets",
            "modified",
        ],
    )
    custom_validator.notes = notes
    custom_validator.save(update_fields=["notes", "modified"])
    sync_custom_validator_input_port(
        validator=validator,
        data_format=input_data_format,
    )
    return custom_validator


def _custom_type_to_validation_type(custom_type: str) -> ValidationType:
    """Map CustomValidatorType to the corresponding ValidationType."""
    mapping = {
        CustomValidatorType.MODELICA: ValidationType.CUSTOM_VALIDATOR,
        CustomValidatorType.KERML: ValidationType.CUSTOM_VALIDATOR,
    }
    return mapping.get(custom_type, ValidationType.CUSTOM_VALIDATOR)


def _unique_validator_slug(org, name: str) -> str:
    """Generate a slug unique across validators."""
    from validibot.validations.models import Validator

    base = slugify(f"{org.pk}-{name}")[:50] or f"validator-{org.pk}"
    slug = base
    counter = 2
    while Validator.objects.filter(slug=slug).exists():
        slug_candidate = f"{base}-{counter}"
        slug = slug_candidate[:50]
        counter += 1
    return slug


# ════════════════════════════════════════════════════════════════════════════
# SHACL library validator services
# ════════════════════════════════════════════════════════════════════════════


def _unique_shacl_default_ruleset_name(org, slug: str, version: str) -> str:
    """Generate a unique Ruleset name for a SHACL library validator's default.

    Inlined here (rather than importing
    ``validibot.workflows.views_helpers.unique_ruleset_name``) to keep
    the validations app independent of the workflows app. Same uniqueness
    contract: ``(org, ruleset_type, name, version)`` must be unique.
    """
    from validibot.validations.constants import RulesetType
    from validibot.validations.models import Ruleset

    base = f"{slug}-default"
    name = base
    counter = 2
    while Ruleset.objects.filter(
        org=org,
        ruleset_type=RulesetType.SHACL,
        name=name,
        version=version,
    ).exists():
        name = f"{base}-{counter}"
        counter += 1
    return name


def _shacl_form_data_to_ruleset_state(form):
    """Convert SHACL library form cleaned data into Ruleset content.

    Shared by both :func:`create_shacl_library_validator` and
    :func:`update_shacl_library_validator` so the persistence shape
    (concatenated rules_text + metadata) stays consistent across the
    create / update paths. Returns
    ``(rules_text, metadata, has_shapes_content, has_ontology_content)``.
    The update path uses the two booleans to support ontology-only edits
    without forcing the author to re-upload the existing shapes.
    """
    from validibot.validations.validators.shacl.persistence import (
        concatenate_uploaded_files,
    )

    cleaned = form.cleaned_data
    shape_files = cleaned.get("shapes_files") or []
    shape_text = (cleaned.get("shapes_text") or "").strip()
    ontology_files = cleaned.get("ontology_files") or []
    ontology_text = (cleaned.get("ontology_text") or "").strip()
    inference_mode = cleaned.get("inference_mode") or "rdfs"
    advanced_shacl = bool(cleaned.get("advanced_shacl"))
    submission_format = cleaned.get("submission_format") or "auto"

    bundled_standards: list[str] = []
    if cleaned.get("bundle_brick"):
        bundled_standards.append("brick-1.4")
    if cleaned.get("bundle_qudt"):
        bundled_standards.append("qudt-2.1")

    shapes_concat, shape_files_meta = concatenate_uploaded_files(
        shape_files,
        shape_text,
    )
    ontology_concat, ontology_files_meta = concatenate_uploaded_files(
        ontology_files,
        ontology_text,
    )

    metadata = {
        "shape_files": shape_files_meta,
        "has_inline_shapes": bool(shape_text),
        "ontology_text": ontology_concat,
        "ontology_files": ontology_files_meta,
        "has_inline_ontology": bool(ontology_text),
        "bundled_standards": bundled_standards,
        "inference_mode": inference_mode,
        "advanced_shacl": advanced_shacl,
        "submission_format": submission_format,
    }
    return (
        shapes_concat,
        metadata,
        bool(shape_files or shape_text),
        bool(ontology_files or ontology_text),
    )


def create_shacl_library_validator(
    *,
    org,
    user,
    form,
    notes: str = "",
):
    """Create an org-owned SHACL validator with a populated default_ruleset.

    Used by ``ShaclLibraryValidatorCreateView``. Mirrors the existing
    :func:`create_custom_validator` and :func:`create_fmu_validator`
    patterns: build the ``Validator`` row, build the supporting
    ``Ruleset`` row with the validator's bundled shapes + metadata,
    attach it as ``Validator.default_ruleset``.

    Workflow steps that later reference this validator inherit its
    shapes via the engine's library + step ruleset merge (see
    :meth:`validibot.validations.validators.shacl.validator.SHACLValidator._resolve_settings`).

    Args:
        org: The organisation that owns the new validator.
        user: The user creating it (recorded for audit only — there is
            no ``created_by`` field on Validator today; the audit log
            captures provenance).
        form: A bound + validated ``ShaclLibraryValidatorCreateForm``.
        notes: Optional notes shown to other authors in the same org.

    Returns:
        The created ``Validator`` instance.
    """
    from validibot.validations.constants import RulesetType
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator

    name = form.cleaned_data["name"]
    short_description = form.cleaned_data.get("short_description") or ""
    description = form.cleaned_data.get("description") or ""
    rules_text, metadata, _has_shapes_content, _has_ontology_content = (
        _shacl_form_data_to_ruleset_state(form)
    )

    slug = _unique_validator_slug(org, name)

    # Build the default ruleset first so we can attach it on Validator creation.
    ruleset_name = _unique_shacl_default_ruleset_name(org, slug, "1")
    ruleset = Ruleset.objects.create(
        org=org,
        user=user,
        name=ruleset_name,
        ruleset_type=RulesetType.SHACL,
        version="1",
        rules_text=rules_text,
        metadata=metadata,
    )

    validator = Validator.objects.create(
        name=name,
        short_description=short_description,
        description=description,
        validation_type=ValidationType.SHACL,
        org=org,
        is_system=False,
        slug=slug,
        supports_assertions=True,
        default_ruleset=ruleset,
    )
    from validibot.validations.services.custom_validator_contracts import (
        sync_configured_io_contract,
    )

    sync_configured_io_contract(validator=validator)
    # SHACL library validators do not currently have a separate
    # CustomValidator-style wrapper for notes. Store them in the ruleset
    # metadata so the create/edit surfaces round-trip the field.
    metadata = dict(ruleset.metadata or {})
    metadata["library_validator_notes"] = notes or ""
    ruleset.metadata = metadata
    ruleset.save(update_fields=["metadata"])
    return validator


def update_shacl_library_validator(
    validator,
    *,
    form,
    notes: str = "",
):
    """Update an org-owned SHACL library validator from a bound update form.

    Mirrors :func:`update_custom_validator`. Validator metadata (name and
    descriptions) refreshes always. SHACL content (shapes,
    ontologies, bundled standards, engine knobs) only re-saves when the
    author supplied new uploads or text — leaving everything blank is
    treated as keep-existing, same semantics as the workflow step
    config form's edit mode.
    """
    from validibot.validations.constants import RulesetType
    from validibot.validations.models import Ruleset

    validator.name = form.cleaned_data["name"]
    validator.short_description = form.cleaned_data.get("short_description") or ""
    validator.description = form.cleaned_data.get("description") or ""
    validator.save(
        update_fields=[
            "name",
            "short_description",
            "description",
            "modified",
        ],
    )

    ruleset = validator.default_ruleset
    if ruleset is None:
        # The library validator was created without a default_ruleset
        # somehow (legacy data, or admin meddling). Create one now.
        ruleset = Ruleset.objects.create(
            org=validator.org,
            name=f"{validator.slug}-default",
            ruleset_type=RulesetType.SHACL,
            # ``Validator.version`` is a PositiveIntegerField with default=1
            # and a MinValueValidator(1), so it cannot be falsy. The cast to
            # str is still needed because ``Ruleset.version`` is a CharField.
            version=str(validator.version),
        )
        validator.default_ruleset = ruleset
        validator.save(update_fields=["default_ruleset", "modified"])

    rules_text, metadata, has_shapes_content, has_ontology_content = (
        _shacl_form_data_to_ruleset_state(form)
    )

    if has_shapes_content:
        # Author supplied fresh shapes. Replace the whole SHACL content bundle;
        # an omitted ontology means "clear ontology" in this branch.
        ruleset.rules_text = rules_text
        ruleset.metadata = metadata
    elif has_ontology_content:
        # Ontology-only edit: preserve shapes and replace the inference context.
        existing_meta = dict(ruleset.metadata or {})
        existing_meta.pop("sparql_assertions", None)
        existing_meta["ontology_text"] = metadata["ontology_text"]
        existing_meta["ontology_files"] = metadata["ontology_files"]
        existing_meta["has_inline_ontology"] = metadata["has_inline_ontology"]
        existing_meta["inference_mode"] = metadata["inference_mode"]
        existing_meta["advanced_shacl"] = metadata["advanced_shacl"]
        existing_meta["submission_format"] = metadata["submission_format"]
        existing_meta["bundled_standards"] = metadata["bundled_standards"]
        ruleset.metadata = existing_meta
    else:
        # Keep-existing: refresh only the engine-knob + bundled-standards
        # subset of metadata.
        existing_meta = dict(ruleset.metadata or {})
        existing_meta.pop("sparql_assertions", None)
        existing_meta["inference_mode"] = metadata["inference_mode"]
        existing_meta["advanced_shacl"] = metadata["advanced_shacl"]
        existing_meta["submission_format"] = metadata["submission_format"]
        existing_meta["bundled_standards"] = metadata["bundled_standards"]
        ruleset.metadata = existing_meta

    meta = dict(ruleset.metadata or {})
    meta["library_validator_notes"] = notes or ""
    ruleset.metadata = meta

    ruleset.save()
    return validator
