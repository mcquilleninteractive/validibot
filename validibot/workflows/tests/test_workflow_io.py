"""Workflow import/export round-trip and import-service behaviour.

These prove the serialize/deserialize halves agree and that the import rules from
the design hold: a fresh workflow rebound to the importing org, validators
resolved (not recreated) with version-mismatch warnings and a hard error when
unresolvable, and the Tabular Validator's staged column guards re-applied on import.
The committed Darwin Core fixtures (``tests/workflows/darwin_core.{json,vaf}``)
are imported here too, so they can't silently drift from what the importer
expects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.conf import settings
from django.core.files.base import ContentFile

from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base.step_serializer import WorkflowImportError
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.services.io.exporter import export_definition
from validibot.workflows.services.io.importer import import_definition
from validibot.workflows.services.io.importer import import_from_upload
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db

# The four Darwin Core row rules, mirroring the committed example.
_ROW_RULES = [
    ("row.minimumDepthInMeters <= row.maximumDepthInMeters", "Depth order"),
    ("!(row.decimalLatitude == 0.0 && row.decimalLongitude == 0.0)", "Null Island"),
    ('row.occurrenceStatus != "present" || row.individualCount >= 1', "Present count"),
    ("row.coordinateUncertaintyInMeters > 0.0", "Positive uncertainty"),
]


def _table_schema() -> str:
    """Load the Darwin Core Table Schema asset as text (the ruleset rules)."""
    path = (
        Path(settings.BASE_DIR)
        / "tests"
        / "assets"
        / "csv"
        / "darwin_core"
        / "occurrence_schema.json"
    )
    return path.read_text(encoding="utf-8")


def _org_and_user():
    """Create an org with an active member user, set as current org."""
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)
    return org, user


def _tabular_validator():
    """A shared (system) Tabular Validator resolvable by validation_type."""
    return ValidatorFactory(
        validation_type=ValidationType.TABULAR,
        slug="tabular-validator",
        version=1,
        is_system=True,
        supports_assertions=True,
    )


def _darwin_core_workflow(org, user, validator):
    """Build a live Darwin Core tabular workflow with the four row assertions."""
    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.TABULAR,
        rules_text=_table_schema(),
    )
    for index, (expression, message) in enumerate(_ROW_RULES):
        RulesetAssertionFactory(
            ruleset=ruleset,
            order=index + 1,
            assertion_type=AssertionType.CEL_EXPRESSION,
            target_data_path="",
            rhs={"expr": expression},
            options={"tabular_stage": "row"},
            severity=Severity.ERROR,
            message_template=message,
        )
    workflow = WorkflowFactory(org=org, user=user)
    WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=10,
        name="Check incoming CSV",
    )
    return workflow


# ── Round-trip: export a live workflow, re-import into a different org ───────
def test_export_then_import_rebuilds_the_workflow_in_a_new_org():
    """A workflow survives a full export -> import into a fresh org unchanged.

    This is the headline guarantee: the serialized form carries enough to rebuild
    the workflow's shape (step, Table Schema ruleset, four row assertions) while
    the importer rebinds ownership and mints a new identity — never mutating or
    reusing the source rows.
    """
    src_org, src_user = _org_and_user()
    validator = _tabular_validator()
    workflow = _darwin_core_workflow(src_org, src_user, validator)

    definition, files = export_definition(workflow)
    assert files == {}  # file-free workflow -> importable as bare JSON too

    dst_org, dst_user = _org_and_user()
    result = import_definition(definition, files=files, org=dst_org, user=dst_user)

    new = result.workflow
    assert new.pk != workflow.pk
    assert new.org_id == dst_org.pk
    assert new.user_id == dst_user.pk
    assert new.version == 1
    assert new.uuid != workflow.uuid
    # Imported workflows are active and launchable immediately (not archived).
    assert new.is_active is True
    assert new.is_archived is False
    assert result.warnings == []

    steps = list(new.steps.all())
    assert len(steps) == 1
    step = steps[0]
    # Built-in validators are shared, so the SAME system row is reused.
    assert step.validator_id == validator.pk
    # The ruleset is a fresh copy owned by the importing org.
    assert step.ruleset_id != workflow.steps.first().ruleset_id
    assert step.ruleset.org_id == dst_org.pk
    assert step.ruleset.ruleset_type == RulesetType.TABULAR

    assertions = list(step.ruleset.assertions.all().order_by("order"))
    assert len(assertions) == 4  # noqa: PLR2004
    assert [a.rhs["expr"] for a in assertions] == [rule[0] for rule in _ROW_RULES]
    assert all(a.options.get("tabular_stage") == "row" for a in assertions)


def test_mutable_editing_policy_survives_export_import():
    """Portable workflows must preserve an author's explicit Mutable choice."""
    src_org, src_user = _org_and_user()
    workflow = WorkflowFactory(
        org=src_org,
        user=src_user,
        history_policy=WorkflowHistoryPolicy.MUTABLE,
    )

    definition, files = export_definition(workflow)
    dst_org, dst_user = _org_and_user()
    result = import_definition(definition, files=files, org=dst_org, user=dst_user)

    assert definition["workflow"]["history_policy"] == WorkflowHistoryPolicy.MUTABLE
    assert result.workflow.history_policy == WorkflowHistoryPolicy.MUTABLE


def test_missing_editing_policy_imports_as_versioned():
    """Older archives cannot silently downgrade a workflow to Mutable editing."""
    src_org, src_user = _org_and_user()
    definition, files = export_definition(
        WorkflowFactory(org=src_org, user=src_user),
    )
    definition["workflow"].pop("history_policy")

    dst_org, dst_user = _org_and_user()
    result = import_definition(definition, files=files, org=dst_org, user=dst_user)

    assert result.workflow.history_policy == WorkflowHistoryPolicy.VERSIONED


def test_invalid_editing_policy_is_rejected_as_an_import_error():
    """A crafted archive must fail cleanly instead of weakening edit guarantees."""
    src_org, src_user = _org_and_user()
    definition, files = export_definition(
        WorkflowFactory(org=src_org, user=src_user),
    )
    definition["workflow"]["history_policy"] = "anything-goes"

    dst_org, dst_user = _org_and_user()
    with pytest.raises(WorkflowImportError) as ctx:
        import_definition(definition, files=files, org=dst_org, user=dst_user)

    assert ctx.value.code == "vaf.invalid_history_policy"


def test_both_step_config_buckets_survive_export_import():
    """The semantic ``config`` AND cosmetic ``display_settings`` round-trip (VAF).

    ADR-2026-06-18 split step config into two fields; the exporter/importer
    serialize both independently. A portable workflow must keep its dialect
    (``config``) *and* its submitter-facing summary labels/counts
    (``display_settings``) — dropping the latter would silently lose display data
    a shared workflow carries. Fields are set with ``.update()`` to write them
    directly without re-running step validation.
    """
    from validibot.workflows.models import WorkflowStep

    src_org, src_user = _org_and_user()
    validator = _tabular_validator()
    workflow = _darwin_core_workflow(src_org, src_user, validator)

    source_step = workflow.steps.first()
    WorkflowStep.objects.filter(pk=source_step.pk).update(
        config={"delimiter": ",", "encoding": "utf-8", "has_header": True},
        display_settings={"delimiter_label": "Comma", "column_count": 2},
    )

    definition, files = export_definition(workflow)
    dst_org, dst_user = _org_and_user()
    result = import_definition(definition, files=files, org=dst_org, user=dst_user)

    imported = result.workflow.steps.first()
    assert imported.config == {
        "delimiter": ",",
        "encoding": "utf-8",
        "has_header": True,
    }
    assert imported.display_settings == {"delimiter_label": "Comma", "column_count": 2}


def test_step_owned_artifact_port_survives_export_import():
    """A portable workflow must retain step-owned file-port contracts.

    Validator-owned ports are resolved from the target validator catalog on
    import, but user/plugin-authored step-owned ports travel in the VAF itself.
    This guards the artifact-port fields and binding source scope together.
    """
    src_org, src_user = _org_and_user()
    validator = _tabular_validator()
    workflow = WorkflowFactory(org=src_org, user=src_user)
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        step_key="build_model",
        order=10,
    )
    producer_output = StepIODefinitionFactory(
        workflow_step=producer,
        validator=None,
        contract_key="generated_model",
        native_name="generated-model",
        direction=StepIODirection.OUTPUT,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.energyplus.epjson",
        data_format="energyplus_epjson",
        accepted_data_formats=["energyplus_epjson"],
        accepted_media_types=["application/vnd.energyplus.epjson"],
        allowed_source_scopes=[],
        default_source_strategy=DefaultSourceStrategy.NONE,
        envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
        role="generated-model",
        min_items=1,
        max_items=1,
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        step_key="validate_model",
        order=20,
    )
    io_definition = StepIODefinitionFactory(
        workflow_step=step,
        validator=None,
        contract_key="generated_model",
        native_name="generated-model",
        direction=StepIODirection.INPUT,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.energyplus.epjson",
        data_format="energyplus_epjson",
        accepted_data_formats=["energyplus_epjson"],
        accepted_media_types=["application/vnd.energyplus.epjson"],
        allowed_source_scopes=[BindingSourceScope.UPSTREAM_ARTIFACT],
        default_source_strategy=DefaultSourceStrategy.MANUAL,
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="primary-model",
        min_items=1,
        max_items=1,
    )
    StepInputBindingFactory(
        workflow_step=step,
        io_definition=io_definition,
        source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        source_data_path="build_model.generated_model",
        source_step=producer,
        source_output_io_definition=producer_output,
    )

    definition, files = export_definition(workflow)
    exported_consumer = next(
        item for item in definition["steps"] if item["step_key"] == "validate_model"
    )
    exported_io_definition = exported_consumer["step_io_definitions"][0]
    assert exported_io_definition["io_medium"] == StepIOMedium.ARTIFACT
    assert exported_io_definition["envelope_channel"] == EnvelopeChannel.INPUT_FILES
    assert exported_io_definition["allowed_source_scopes"] == [
        BindingSourceScope.UPSTREAM_ARTIFACT,
    ]

    dst_org, dst_user = _org_and_user()
    result = import_definition(definition, files=files, org=dst_org, user=dst_user)

    imported_step = result.workflow.steps.get(step_key="validate_model")
    imported_producer = result.workflow.steps.get(step_key="build_model")
    imported_io_definition = imported_step.step_io_definitions.get(
        contract_key="generated_model",
    )
    assert imported_io_definition.io_medium == StepIOMedium.ARTIFACT
    assert imported_io_definition.data_type == CatalogValueType.ARTIFACT_REF
    assert imported_io_definition.envelope_channel == EnvelopeChannel.INPUT_FILES
    assert imported_io_definition.accepted_data_formats == ["energyplus_epjson"]
    assert imported_io_definition.allowed_source_scopes == [
        BindingSourceScope.UPSTREAM_ARTIFACT,
    ]
    imported_binding = imported_step.input_bindings.get()
    assert imported_binding.io_definition_id == imported_io_definition.pk
    assert imported_binding.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT
    assert imported_binding.source_data_path == "build_model.generated_model"
    assert imported_binding.source_step_id == imported_producer.pk
    assert (
        imported_binding.source_output_io_definition.contract_key == "generated_model"
    )


def test_import_binds_workflow_to_importing_orgs_default_project():
    """Imported workflows must belong to the importing org's default project.

    Imports don't carry a project reference, but every workflow must have one
    (``Workflow.clean()`` enforces a non-null project). The importer therefore
    binds the new workflow to the destination org's default project rather than
    leaving it project-less, which would otherwise make the import fail at save.
    """
    from validibot.users.models import ensure_default_project

    src_org, src_user = _org_and_user()
    validator = _tabular_validator()
    workflow = _darwin_core_workflow(src_org, src_user, validator)

    definition, files = export_definition(workflow)

    dst_org, dst_user = _org_and_user()
    dst_default = ensure_default_project(dst_org)

    result = import_definition(definition, files=files, org=dst_org, user=dst_user)

    new = result.workflow
    assert new.project_id == dst_default.pk
    assert new.project.org_id == dst_org.pk


# ── Author notes survive the export -> import round-trip ────────────────────
def test_assertion_notes_survive_export_import():
    """An assertion's author ``notes`` field round-trips through .vaf import.

    ``notes`` is non-semantic documentation (the rationale behind the rule),
    but it is part of the assertion's serialized contract — listed in
    ``_ASSERTION_SCALAR_FIELDS`` so export and import can't disagree on whether
    it travels. This test asserts both halves: the note appears in the exported
    definition AND is rebuilt verbatim on import into a fresh org. It guards
    against a future change dropping ``notes`` from one side of the round-trip
    while leaving the other, which would silently lose the reasoning a workflow
    author recorded.
    """
    src_org, src_user = _org_and_user()
    validator = _tabular_validator()
    ruleset = RulesetFactory(
        org=src_org,
        user=src_user,
        ruleset_type=RulesetType.TABULAR,
        rules_text=_table_schema(),
    )
    note = (
        "Zero uncertainty means the coordinate precision is unknown; Darwin "
        "Core treats that as a data-quality failure."
    )
    RulesetAssertionFactory(
        ruleset=ruleset,
        order=1,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_data_path="",
        rhs={"expr": "row.coordinateUncertaintyInMeters > 0.0"},
        options={"tabular_stage": "row"},
        severity=Severity.ERROR,
        message_template="Positive uncertainty",
        notes=note,
    )
    workflow = WorkflowFactory(org=src_org, user=src_user)
    WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=10,
        name="Check incoming CSV",
    )

    definition, files = export_definition(workflow)

    # Export half: the note is written into the serialized assertion.
    exported_assertion = definition["steps"][0]["ruleset"]["assertions"][0]
    assert exported_assertion["notes"] == note

    # Import half: the note is rebuilt verbatim on the new org's assertion row.
    dst_org, dst_user = _org_and_user()
    result = import_definition(definition, files=files, org=dst_org, user=dst_user)
    imported = result.workflow.steps.first().ruleset.assertions.first()
    assert imported.notes == note


def test_submission_assertion_targets_survive_export_import():
    """``submission.*`` assertion targets and expressions round-trip unchanged.

    ADR-2026-06-03b surface H records that assertion targets and CEL
    expressions travel as opaque strings, so the ``submission`` namespace needs
    no special import/export handling — but the ADR mandates a test proving it.
    We pack a BASIC ``submission.metadata.*`` target and a CEL expression using
    a string-keyed bracket (``submission.metadata["deliverable-type"]``, a
    non-identifier key) through a full export -> import into a fresh org and
    assert both survive verbatim on each half. This guards against a future
    serializer change silently mangling the new namespace or its bracket syntax.
    """
    src_org, src_user = _org_and_user()
    validator = ValidatorFactory(
        validation_type=ValidationType.BASIC,
        slug="basic-validator",
        version=1,
        is_system=True,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        org=src_org,
        user=src_user,
        ruleset_type=RulesetType.BASIC,
    )
    RulesetAssertionFactory(
        ruleset=ruleset,
        order=1,
        assertion_type=AssertionType.BASIC,
        operator=AssertionOperator.EQ,
        target_data_path="submission.metadata.deliverable",
        rhs={"value": "handover"},
        severity=Severity.ERROR,
    )
    cel_expr = 'submission.metadata["deliverable-type"] == "final"'
    RulesetAssertionFactory(
        ruleset=ruleset,
        order=2,
        assertion_type=AssertionType.CEL_EXPRESSION,
        operator=AssertionOperator.CEL_EXPR,
        target_data_path="",
        rhs={"expr": cel_expr},
        severity=Severity.ERROR,
    )
    workflow = WorkflowFactory(org=src_org, user=src_user)
    WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=10,
        name="Deliverable gate",
    )

    definition, files = export_definition(workflow)

    # Export half: both submission references are written verbatim.
    exported = definition["steps"][0]["ruleset"]["assertions"]
    assert exported[0]["target_data_path"] == "submission.metadata.deliverable"
    assert exported[1]["rhs"]["expr"] == cel_expr

    # Import half: both are rebuilt verbatim on the new org's assertion rows.
    dst_org, dst_user = _org_and_user()
    result = import_definition(definition, files=files, org=dst_org, user=dst_user)
    imported = list(
        result.workflow.steps.first().ruleset.assertions.all().order_by("order"),
    )
    assert imported[0].target_data_path == "submission.metadata.deliverable"
    assert imported[1].rhs["expr"] == cel_expr


def test_import_mints_a_unique_slug_on_collision():
    """Importing twice into the same org yields two workflows with unique slugs.

    "Always a new copy" means a name/slug collision is resolved by suffixing, not
    by erroring or overwriting the first import.
    """
    src_org, src_user = _org_and_user()
    validator = _tabular_validator()
    workflow = _darwin_core_workflow(src_org, src_user, validator)
    definition, files = export_definition(workflow)

    dst_org, dst_user = _org_and_user()
    first = import_definition(definition, files=files, org=dst_org, user=dst_user)
    second = import_definition(definition, files=files, org=dst_org, user=dst_user)

    assert first.workflow.slug != second.workflow.slug
    assert second.workflow.slug.startswith(first.workflow.slug)


# ── Importing the committed fixtures ────────────────────────────────────────
def test_imports_committed_darwin_core_json():
    """The committed darwin_core.json imports cleanly into a working workflow.

    Guards the fixture: if the importer's expectations or the example drift apart,
    this fails. Bare JSON is the file-free import path.
    """
    org, user = _org_and_user()
    _tabular_validator()
    data = (
        Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
    ).read_bytes()

    result = import_from_upload(data, filename="darwin_core.json", org=org, user=user)

    assert result.warnings == []
    workflow = result.workflow
    assert workflow.name == "Darwin Core Occurrence QA"
    assert workflow.steps.count() == 1
    assert workflow.steps.first().ruleset.assertions.count() == 4  # noqa: PLR2004


def test_imports_committed_darwin_core_vaf():
    """The committed darwin_core.vaf imports identically to the .json.

    Same definition, archive path — proves the .vaf packaging is wired into the
    import flow end to end.
    """
    org, user = _org_and_user()
    _tabular_validator()
    data = (
        Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.vaf"
    ).read_bytes()

    result = import_from_upload(data, filename="darwin_core.vaf", org=org, user=user)

    assert result.workflow.steps.first().ruleset.assertions.count() == 4  # noqa: PLR2004


# ── Validator resolution ────────────────────────────────────────────────────
def test_unresolvable_validator_is_a_hard_error():
    """A step whose validator isn't available fails the import outright.

    A workflow with an unbacked step couldn't run, so a partial import would be
    worse than a clear failure.
    """
    org, user = _org_and_user()  # note: no Tabular validator created
    data = (
        Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
    ).read_bytes()

    with pytest.raises(WorkflowImportError) as ctx:
        import_from_upload(data, filename="darwin_core.json", org=org, user=user)
    assert ctx.value.code == "vaf.validator_unresolved"


def test_version_mismatch_resolves_with_a_warning():
    """A built-in present at a different version resolves, with a warning.

    Portability over precision for built-ins: the definition asked for version 1,
    only version 2 exists, so we use it and tell the user.
    """
    org, user = _org_and_user()
    ValidatorFactory(
        validation_type=ValidationType.TABULAR,
        slug="tabular-validator",
        version=2,
        is_system=True,
        supports_assertions=True,
    )
    data = (
        Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
    ).read_bytes()

    result = import_from_upload(data, filename="darwin_core.json", org=org, user=user)

    assert any("version" in warning.lower() for warning in result.warnings)
    assert result.workflow.steps.first().validator.version == 2  # noqa: PLR2004


# ── Imports never inherit external exposure ─────────────────────────────────
def test_import_forces_publication_and_agent_flags_private():
    """An import never makes a workflow public/agent-exposed, even if asked to.

    Imports are active (runnable), so the exposure toggles must not travel: a
    crafted (or faithfully exported) definition that sets ``workflow_visibility``
    wider than PRIVATE, ``make_info_page_public``, ``mcp_enabled``, or
    ``x402_enabled`` must land fully private. After the 2026-06-27 refactor an
    imported workflow gets ``workflow_visibility=PRIVATE`` (creator + explicit
    grants only) and both agent channels off. Otherwise importing a public or
    agent-exposed workflow would auto-publish it in the target org.

    We still send the legacy ``is_public`` / ``agent_public_discovery`` /
    ``agent_access_enabled`` keys in the hostile payload to prove the importer
    ignores unknown/renamed external-exposure keys too — it must not honour
    them under any name.
    """
    from validibot.workflows.constants import WorkflowVisibility

    org, user = _org_and_user()
    _tabular_validator()
    definition = json.loads(
        (
            Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
        ).read_text(),
    )
    # A hostile / over-eager definition asking for full external exposure,
    # using both the current field names and the legacy ones.
    definition["workflow"].update(
        {
            "workflow_visibility": WorkflowVisibility.ALL_USERS,
            "make_info_page_public": True,
            "mcp_enabled": True,
            "x402_enabled": True,
            # Legacy keys the importer must also refuse to honour.
            "is_public": True,
            "agent_public_discovery": True,
            "agent_access_enabled": True,
        },
    )

    result = import_definition(definition, files={}, org=org, user=user)

    new = result.workflow
    assert new.workflow_visibility == WorkflowVisibility.PRIVATE
    assert new.make_info_page_public is False
    assert new.mcp_enabled is False
    assert new.x402_enabled is False
    # ...but still active and runnable by its creator / invited guests.
    assert new.is_active is True


# ── Uploaded-schema (rules_file) rulesets round-trip ────────────────────────
def _json_schema_validator():
    """A shared JSON Schema validator, resolvable by validation_type on import."""
    return ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="json-2020-12",
        version=1,
        is_system=True,
        supports_assertions=True,
    )


def _file_backed_json_ruleset(org, user, schema_bytes: bytes):
    """A JSON Schema ruleset whose schema lives in an uploaded file, not text."""
    # Start from a valid factory ruleset (gives the required schema_type
    # metadata), then convert it to the uploaded-file form the JSON/XML editors
    # produce: schema in rules_file, rules_text cleared.
    ruleset = RulesetFactory(org=org, user=user, ruleset_type=RulesetType.JSON_SCHEMA)
    ruleset.rules_file = ContentFile(schema_bytes, name="schema.json")
    ruleset.rules_text = ""
    ruleset.save()
    return ruleset


def test_uploaded_schema_file_ruleset_round_trips():
    """A ruleset that stores its schema in an uploaded file survives export/import.

    Regression: the base serializer used to export only ``rules_text``, so a
    JSON/XML upload (which stores the schema in ``rules_file`` and clears
    ``rules_text``) came back with neither and failed model validation. Export now
    bundles the file bytes and import restores them.
    """
    src_org, src_user = _org_and_user()
    validator = _json_schema_validator()
    schema_bytes = b'{"type": "object", "properties": {"name": {"type": "string"}}}'
    ruleset = _file_backed_json_ruleset(src_org, src_user, schema_bytes)
    workflow = WorkflowFactory(org=src_org, user=src_user)
    WorkflowStepFactory(
        workflow=workflow, validator=validator, ruleset=ruleset, order=10
    )

    definition, files = export_definition(workflow)

    # The schema file is bundled, and the ruleset references it (no inline text).
    assert len(files) == 1
    body = definition["steps"][0]["ruleset"]
    assert body["rules_text"] == ""
    assert body["rules_file"]["content_ref"] in files

    dst_org, dst_user = _org_and_user()
    result = import_definition(definition, files=files, org=dst_org, user=dst_user)

    new_ruleset = result.workflow.steps.first().ruleset
    assert new_ruleset.rules_text == ""
    assert new_ruleset.rules_file
    with new_ruleset.rules_file.open("rb") as handle:
        assert handle.read() == schema_bytes


def test_importing_a_file_backed_ruleset_as_bare_json_fails_clearly():
    """A definition that needs a bundled schema file can't be imported as bare JSON.

    Without the file bytes the schema would be lost, so the import fails with a
    clear, actionable error instead of a downstream model-validation crash.
    """
    src_org, src_user = _org_and_user()
    validator = _json_schema_validator()
    ruleset = _file_backed_json_ruleset(src_org, src_user, b'{"type": "object"}')
    workflow = WorkflowFactory(org=src_org, user=src_user)
    WorkflowStepFactory(
        workflow=workflow, validator=validator, ruleset=ruleset, order=10
    )
    definition, _files = export_definition(workflow)

    dst_org, dst_user = _org_and_user()
    with pytest.raises(WorkflowImportError) as ctx:
        # files={} simulates a bare-JSON import (no bundled bytes).
        import_definition(definition, files={}, org=dst_org, user=dst_user)
    assert ctx.value.code == "vaf.missing_bundled_file"


# ── Tabular staged-column guards re-applied on import ───────────────────────
def test_tabular_row_assertion_referencing_unknown_column_is_rejected():
    """An imported tabular row assertion can't reference an undeclared column.

    The step editor blocks this at authoring time; import bypasses the form, so
    the Tabular serializer re-checks it. Without the guard the archive would
    create a ruleset that fails every row at runtime.
    """
    org, user = _org_and_user()
    _tabular_validator()
    definition = json.loads(
        (
            Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
        ).read_text(),
    )
    # Point one row assertion at a column the Table Schema doesn't declare.
    definition["steps"][0]["ruleset"]["assertions"][0]["rhs"] = {
        "expr": "row.notAColumn > 0",
    }

    with pytest.raises(WorkflowImportError) as ctx:
        import_definition(definition, files={}, org=org, user=user)
    assert ctx.value.code == "vaf.tabular_unknown_column"


def test_tabular_row_assertion_with_column_name_in_a_string_literal_is_allowed():
    """A column-shaped token inside a CEL string literal must not be rejected.

    Import and authoring share one column scan now, so a valid expression like
    ``row.scientificName != "row.notAColumn"`` references only ``scientificName``
    — the quoted ``row.notAColumn`` is a literal, not a reference. Before the fix
    the importer flagged it as an undeclared column while the editor accepted it.
    """
    org, user = _org_and_user()
    _tabular_validator()
    definition = json.loads(
        (
            Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
        ).read_text(),
    )
    definition["steps"][0]["ruleset"]["assertions"][0]["rhs"] = {
        "expr": 'row.scientificName != "row.notAColumn"',
    }

    # Must NOT raise — the literal isn't a real column reference.
    result = import_definition(definition, files={}, org=org, user=user)
    assert result.workflow.steps.first().ruleset.assertions.count() == 4  # noqa: PLR2004


def test_tabular_column_assertion_with_unknown_aggregate_is_rejected():
    """Imported V2 assertions cannot bypass aggregate-name validation."""
    org, user = _org_and_user()
    _tabular_validator()
    definition = json.loads(
        (
            Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
        ).read_text(),
    )
    assertion = definition["steps"][0]["ruleset"]["assertions"][0]
    assertion["options"] = {"tabular_stage": "column"}
    assertion["rhs"] = {"expr": "col.decimalLatitude.mean > 0"}

    with pytest.raises(WorkflowImportError) as ctx:
        import_definition(definition, files={}, org=org, user=user)
    assert ctx.value.code == "vaf.tabular_unknown_aggregate"


def test_tabular_assertion_with_unknown_stage_is_rejected():
    """An imported assertion with a misspelled stage is rejected, not dropped.

    A stage outside {row, column, dataset} matches neither the stage checks in
    the serializer nor the runtime collectors (which compare against "row" /
    "column" exactly), so it would import cleanly and then silently never run.
    The serializer must refuse it.
    """
    org, user = _org_and_user()
    _tabular_validator()
    definition = json.loads(
        (
            Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
        ).read_text(),
    )
    assertion = definition["steps"][0]["ruleset"]["assertions"][0]
    assertion["options"] = {"tabular_stage": "rwo"}  # typo of "row"

    with pytest.raises(WorkflowImportError) as ctx:
        import_definition(definition, files={}, org=org, user=user)
    assert ctx.value.code == "vaf.tabular_invalid_assertion_stage"


def test_tabular_column_assertion_cannot_mix_row_namespace():
    """An archive cannot tag a row expression as a column-stage assertion."""
    org, user = _org_and_user()
    _tabular_validator()
    definition = json.loads(
        (
            Path(settings.BASE_DIR) / "tests" / "workflows" / "darwin_core.json"
        ).read_text(),
    )
    assertion = definition["steps"][0]["ruleset"]["assertions"][0]
    assertion["options"] = {"tabular_stage": "column"}
    assertion["rhs"] = {"expr": "row.decimalLatitude > 0"}

    with pytest.raises(WorkflowImportError) as ctx:
        import_definition(definition, files={}, org=org, user=user)
    assert ctx.value.code == "vaf.tabular_invalid_assertion_stage"
