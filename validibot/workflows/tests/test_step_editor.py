"""Tests for workflow step authoring, including the add-step wizard.

These tests cover the selector-to-editor handoff because the wizard and
create view enforce related but separate visibility rules.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from http import HTTPStatus

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import CredentialActionType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.actions.models import SlackMessageAction
from validibot.actions.registry import get_action_form
from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import AssertionType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import ValidationType
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import StepInputBinding
from validibot.validations.models import Validator
from validibot.validations.tests.factories import CustomValidatorFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.tabular.metadata import TABULAR_DATASET_INPUTS
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db

SHACL_STEP_SETTINGS_SECTION_COUNT = 4
RESIZABLE_PANEL_COUNT = 2
TABULAR_STAGE_CONNECTOR_COUNT = 3


@pytest.fixture(autouse=True)
def seed_roles(db):
    ensure_all_roles_exist()


def ensure_validator(validation_type: str, slug: str, name: str) -> Validator:
    return Validator.objects.get_or_create(
        validation_type=validation_type,
        slug=slug,
        defaults={"name": name, "description": name},
    )[0]


def create_energyplus_file_ports(validator: Validator) -> None:
    """Declare the EnergyPlus file ports that drive source-picker rendering."""
    StepIODefinitionFactory(
        validator=validator,
        contract_key="primary_model",
        native_name="primary_model",
        label="Model file",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="primary-model",
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        min_items=1,
        max_items=1,
    )
    StepIODefinitionFactory(
        validator=validator,
        contract_key="weather_file",
        native_name="weather_file",
        label="Weather file",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        envelope_channel=EnvelopeChannel.RESOURCE_FILES,
        role="weather",
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        allowed_source_scopes=[
            BindingSourceScope.WORKFLOW_RESOURCE,
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        min_items=1,
        max_items=1,
    )


def make_action_definition(
    *,
    category: str = ActionCategoryType.INTEGRATION,
    name: str | None = None,
    type_value: str | None = None,
) -> ActionDefinition:
    if type_value is None:
        type_value = (
            IntegrationActionType.SLACK_MESSAGE
            if category == ActionCategoryType.INTEGRATION
            else CredentialActionType.SIGNED_CREDENTIAL
        )
    slug = f"{category.lower()}-{type_value.lower()}"
    definition, _ = ActionDefinition.objects.get_or_create(
        action_category=category,
        type=type_value,
        defaults={
            "slug": slug,
            "name": name or f"{category.title()} action",
            "description": "Automated test action",
            "icon": "bi-gear",
        },
    )
    if name:
        definition.name = name
        definition.save(update_fields=["name"])
    return definition


def _login_for_workflow(client, workflow):
    user = workflow.user
    membership = user.memberships.get(org=workflow.org)
    membership.set_roles({RoleCode.AUTHOR})
    user.set_current_org(workflow.org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()


def _login_user_for_org(client, user, org):
    user.set_current_org(org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()


def _select_step_option(client, workflow, value: str) -> str:
    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.post(
        url,
        data={"stage": "select", "choice": value},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    redirect_url = response.headers.get("HX-Redirect")
    assert redirect_url, "wizard should instruct the client to navigate to the editor"
    return redirect_url


def _select_validator(client, workflow, validator) -> str:
    return _select_step_option(client, workflow, f"validator:{validator.pk}")


def _select_action(client, workflow, definition: ActionDefinition) -> str:
    return _select_step_option(client, workflow, f"action:{definition.pk}")


def test_wizard_redirects_to_create_view(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    redirect_url = _select_validator(client, workflow, validator)

    expected = reverse(
        "workflows:workflow_step_create",
        args=[workflow.pk, validator.pk],
    )
    assert expected in redirect_url


def test_wizard_redirects_to_action_create_view(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Slack message")

    redirect_url = _select_action(client, workflow, definition)

    expected = reverse(
        "workflows:workflow_step_action_create",
        args=[workflow.pk, definition.pk],
    )
    assert expected in redirect_url


def test_wizard_lists_action_tabs(client):
    """The wizard should only show action tabs backed by registered plugins."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    integration_def = make_action_definition(
        category=ActionCategoryType.INTEGRATION,
        name="Send Slack message",
    )
    certification_def = make_action_definition(
        category=ActionCategoryType.CREDENTIAL,
        name="Issue signed credential",
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert integration_def.name in html
    assert "Integrations" in html

    if get_action_form(CredentialActionType.SIGNED_CREDENTIAL) is None:
        assert certification_def.name not in html
    else:
        assert certification_def.name in html
        assert "Credentials" in html


def test_wizard_shows_library_custom_validators(client):
    """Library custom validators must remain selectable from the add-step modal."""

    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    custom_validator = CustomValidatorFactory(org=workflow.org)

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Custom Validators" in html
    assert custom_validator.validator.name in html
    assert f'value="validator:{custom_validator.validator.pk}"' in html


def test_custom_validator_selection_handles_stale_validator_org(client):
    """Older library rows may rely on CustomValidator.org for ownership."""

    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    custom_validator = CustomValidatorFactory(org=workflow.org)
    Validator.objects.filter(pk=custom_validator.validator.pk).update(
        org=None,
        is_system=False,
    )
    custom_validator.validator.refresh_from_db()

    create_url = _select_validator(client, workflow, custom_validator.validator)
    response = client.get(create_url)

    assert response.status_code == HTTPStatus.OK
    assert "Add workflow step" in response.content.decode()


def test_wizard_groups_shacl_with_basic_validators(client):
    """SHACL should sit with local schema-style validators, not backend runners."""

    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    shacl_validator = ensure_validator(
        ValidationType.SHACL,
        "shacl-validator",
        "SHACL Validator",
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    panes = _tab_panes_by_id(response.content.decode())
    assert "Validate RDF graphs against SHACL shapes" in panes["workflow-tab-basic"]
    assert shacl_validator.name in panes["workflow-tab-basic"]
    assert shacl_validator.name not in panes["workflow-tab-advanced"]


def test_wizard_groups_tabular_with_basic_validators(client):
    """Tabular runs in-process (no container backend), so it belongs with the
    built-in validators, not under "Advanced Validators".

    Tabular was landing in the Advanced tab only because its ValidationType was
    absent from every tab's type set and fell through to the remaining-validators
    catch-all. Listing it in the "Validators" set fixes both the placement and
    removes it from that fallback.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    tabular_validator = ensure_validator(
        ValidationType.TABULAR,
        "tabular-validator",
        "Tabular Validator",
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    panes = _tab_panes_by_id(response.content.decode())
    assert tabular_validator.name in panes["workflow-tab-basic"]
    assert tabular_validator.name not in panes["workflow-tab-advanced"]


def test_wizard_shows_only_latest_version_per_validator_slug(client):
    """The picker shows one card per validator family (the latest version),
    not one card per published version.

    Validators are integer-versioned per slug (``uq_validator_slug_version``).
    Before the fix the picker listed every published row, so a validator with
    two published versions rendered as two identical-looking cards. Here two
    versions of the same slug must collapse to the newer one — the older
    version's distinct name must not appear at all.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    Validator.objects.create(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="dup-json",
        name="Dup JSON v1",
        version=1,
    )
    Validator.objects.create(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="dup-json",
        name="Dup JSON v2",
        version=2,
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    # Only the latest version is offered; the older one is collapsed away.
    assert "Dup JSON v2" in html
    assert "Dup JSON v1" not in html


def test_wizard_enables_xml_validator_for_json_workflow(client):
    """Typed upstream artifacts make the original submission type irrelevant.

    A JSON workflow may produce XML in an earlier step and bind that file to an
    XML validator. The picker must therefore let the author select the XML
    validator instead of disabling it based on the launch submission alone.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    xml_validator = ensure_validator(
        ValidationType.XML_SCHEMA,
        "xml-validator",
        "XML Schema",
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert xml_validator.name in html
    assert f'value="validator:{xml_validator.pk}"' in html
    assert not re.search(
        rf'value="validator:{xml_validator.pk}"\s+disabled',
        html,
    )


def test_wizard_allows_selecting_validator_for_a_different_submission_type(client):
    """The POST handoff must honor the same file-port composition rule as GET."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    xml_validator = ensure_validator(
        ValidationType.XML_SCHEMA,
        "selectable-xml-validator",
        "Selectable XML Schema",
    )

    redirect_url = _select_validator(client, workflow, xml_validator)

    expected = reverse(
        "workflows:workflow_step_create",
        args=[workflow.pk, xml_validator.pk],
    )
    assert expected in redirect_url


def test_create_xml_step_in_pdf_workflow_with_upstream_file_binding(client):
    """The step save must accept XML sourced from an earlier PDF-step output."""
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.PDF])
    _login_for_workflow(client, workflow)
    pdf_validator = ensure_validator(
        ValidationType.PDF,
        "pdf-producer",
        "PDF Package Validator",
    )
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=pdf_validator,
        order=10,
        name="Inspect PDF package",
    )
    output_port = StepIODefinitionFactory(
        validator=pdf_validator,
        workflow_step=None,
        contract_key="selected_xml",
        native_name="selected_xml",
        label="Selected XML",
        direction=StepIODirection.OUTPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        data_format=SubmissionDataFormat.XML,
        media_type="application/xml",
        min_items=0,
        max_items=1,
    )
    xml_validator = ensure_validator(
        ValidationType.XML_SCHEMA,
        "xml-consumer",
        "XML Schema Validator",
    )
    input_port = StepIODefinitionFactory(
        validator=xml_validator,
        workflow_step=None,
        contract_key="xml_document",
        native_name="xml_document",
        label="XML document",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        data_format=SubmissionDataFormat.XML,
        accepted_data_formats=[SubmissionDataFormat.XML],
        media_type="application/xml",
        accepted_media_types=["application/xml"],
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        min_items=1,
        max_items=1,
    )

    response = client.post(
        _select_validator(client, workflow, xml_validator),
        data={
            "name": "Validate extracted XML",
            "schema_type": "XSD",
            "schema_text": (
                '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                '<xs:element name="root" type="xs:string"/>'
                "</xs:schema>"
            ),
            "xml_document_source": BindingSourceScope.UPSTREAM_ARTIFACT,
            "xml_document_upstream_artifact": (
                f"{producer.step_key}.{output_port.contract_key}"
            ),
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    consumer = workflow.steps.get(validator=xml_validator)
    binding = StepInputBinding.objects.get(
        workflow_step=consumer,
        io_definition=input_port,
    )
    assert binding.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT
    assert binding.source_step == producer
    assert binding.source_output_io_definition == output_port


def test_wizard_enables_pdf_validator_for_pdf_workflow(client):
    """The dedicated PDF type must expose the PDF Package Validator card."""
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.PDF])
    _login_for_workflow(client, workflow)
    pdf_validator = ensure_validator(
        ValidationType.PDF,
        "pdf-validator",
        "PDF Package Validator",
    )
    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert pdf_validator.name in html
    assert f'value="validator:{pdf_validator.pk}"' in html
    assert not re.search(
        rf'value="validator:{pdf_validator.pk}"\s+disabled',
        html,
    )
    assert "Not allowed for this workflow&#x27;s submission types" not in html


def test_fmu_validator_enabled_for_json_workflow(client):
    """A published and available FMU validator remains selectable."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    fmu_validator = ensure_validator(
        ValidationType.FMU,
        "fmu-validator",
        "FMU Validator",
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    value = f'value="validator:{fmu_validator.pk}"'
    assert value in html
    # FMU supports JSON, so it should NOT be disabled for a JSON workflow.
    assert not re.search(
        rf'value="validator:{fmu_validator.pk}"\s+disabled',
        html,
    )


def test_toggle_step_output_display_round_trips_step_owned_outputs(client):
    """The inline step-output toggle should update only the current step.

    Step-owned outputs have ``validator=None``, so the view must not
    accidentally include output values from other steps when it expands
    the step's explicit ``display_step_outputs`` allowlist.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    step = WorkflowStepFactory(
        workflow=workflow,
        config={},
    )
    StepIODefinitionFactory(
        workflow_step=step,
        validator=None,
        contract_key="t_room",
        native_name="T_room",
        direction="output",
        origin_kind="fmu",
    )
    StepIODefinitionFactory(
        workflow_step=step,
        validator=None,
        contract_key="q_cool",
        native_name="Q_cool",
        direction="output",
        origin_kind="fmu",
    )
    other_step = WorkflowStepFactory(
        workflow=workflow,
    )
    StepIODefinitionFactory(
        workflow_step=other_step,
        validator=None,
        contract_key="foreign_output",
        native_name="ForeignOutput",
        direction="output",
        origin_kind="fmu",
    )
    url = reverse(
        "workflows:workflow_step_toggle_step_output_display",
        args=[workflow.pk, step.pk, "t_room"],
    )

    show_response = client.post(url, HTTP_HX_REQUEST="true")

    assert show_response.status_code == HTTPStatus.OK
    step.refresh_from_db()
    assert set(step.display_settings["display_step_outputs"]) == {"t_room"}
    assert "foreign_output" not in step.display_settings["display_step_outputs"]
    assert "Shown in results" in show_response.content.decode()

    hide_response = client.post(url, HTTP_HX_REQUEST="true")

    assert hide_response.status_code == HTTPStatus.OK
    step.refresh_from_db()
    assert step.display_settings["display_step_outputs"] == []
    assert "Hidden from results" in hide_response.content.decode()


def test_create_view_creates_json_schema_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="json-validator",
    )

    create_url = _select_validator(client, workflow, validator)

    # GET renders the full-page editor
    response = client.get(create_url)
    assert response.status_code == HTTPStatus.OK
    assert "Add workflow step" in response.content.decode()

    schema_text = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"sku": {"type": "string"}},
        },
    )
    response = client.post(
        create_url,
        data={
            "name": "JSON Schema",
            "description": "Ensures posted documents follow the schema.",
            "notes": "Remember to update schema when payload changes.",
            "display_schema": "on",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            "schema_text": schema_text,
        },
    )
    assert response.status_code == HTTPStatus.FOUND

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator == validator
    assert step.ruleset is not None
    stored_schema = step.ruleset.rules
    assert "$schema" in stored_schema
    assert "type" in stored_schema
    assert step.display_settings["schema_source"] == "text"
    assert step.config["schema_type"] == JSONSchemaVersion.DRAFT_2020_12.value
    assert step.description == "Ensures posted documents follow the schema."
    assert step.notes == "Remember to update schema when payload changes."
    assert step.display_schema is True
    assert (
        step.ruleset.metadata.get("schema_type")
        == JSONSchemaVersion.DRAFT_2020_12.value
    )


def test_create_view_breadcrumb_includes_workflow_without_header_subtitle(client):
    """Add-step pages should put workflow context in breadcrumbs, not the header."""

    workflow = WorkflowFactory(name="A very long workflow name for breadcrumb testing")
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="json-breadcrumb-validator",
    )

    create_url = _select_validator(client, workflow, validator)
    response = client.get(create_url)

    assert response.status_code == HTTPStatus.OK
    breadcrumbs = response.context["breadcrumbs"]
    assert [str(crumb["name"]) for crumb in breadcrumbs] == [
        "Workflows",
        workflow.name,
        "Add step",
    ]
    assert breadcrumbs[1]["url"]
    assert breadcrumbs[1]["version_badge"]["label"] == "v1"

    html = response.content.decode()
    assert "Add workflow step" in html
    assert "Workflow:" not in html


def test_create_view_with_custom_validator(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    custom_validator = CustomValidatorFactory(org=workflow.org)

    create_url = _select_validator(client, workflow, custom_validator.validator)
    response = client.post(
        create_url,
        data={
            "name": "Custom check",
            "description": "Runs custom logic",
            "notes": "Initial custom validator step",
        },
    )
    assert response.status_code == HTTPStatus.FOUND

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator == custom_validator.validator
    assert step.ruleset is not None
    assert step.ruleset.ruleset_type == custom_validator.validator.validation_type


def test_custom_validator_multiple_steps_get_unique_rulesets(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    custom_validator = CustomValidatorFactory(org=workflow.org)

    first_url = _select_validator(client, workflow, custom_validator.validator)
    second_url = _select_validator(client, workflow, custom_validator.validator)

    response = client.post(first_url, data={"name": "Step A"})
    assert response.status_code == HTTPStatus.FOUND

    response = client.post(second_url, data={"name": "Step B"})
    assert response.status_code == HTTPStatus.FOUND

    steps = WorkflowStep.objects.filter(workflow=workflow).order_by("order")
    assert steps.count() == 2  # noqa: PLR2004
    assert steps[0].ruleset != steps[1].ruleset


def test_create_view_creates_action_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Notify team")

    create_url = _select_action(client, workflow, definition)

    response = client.get(create_url)
    assert response.status_code == HTTPStatus.OK
    assert "Add workflow step" in response.content.decode()

    response = client.post(
        create_url,
        data={
            "name": "Send Slack alert",
            "description": "Notify the #alerts channel after validation.",
            "notes": "Rotate webhook quarterly.",
            "message": "Validation finished successfully.",
        },
    )
    assert response.status_code == HTTPStatus.FOUND

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator is None
    assert step.action is not None
    assert step.action.definition == definition
    assert step.action.name == "Send Slack alert"
    variant = step.action.get_variant()
    assert isinstance(variant, SlackMessageAction)
    assert variant.message == "Validation finished successfully."
    assert step.description == "Notify the #alerts channel after validation."
    assert step.notes == "Rotate webhook quarterly."
    assert step.config.get("message") == "Validation finished successfully."


def test_create_signed_credential_action_without_extra_config(client):
    """Credential step creation depends on the Pro action plugin being loaded."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(
        category=ActionCategoryType.CREDENTIAL,
        name="Issue signed credential",
    )

    create_url = reverse(
        "workflows:workflow_step_action_create",
        args=[workflow.pk, definition.pk],
    )

    if get_action_form(CredentialActionType.SIGNED_CREDENTIAL) is None:
        response = client.get(create_url)
        assert response.status_code == HTTPStatus.NOT_FOUND
        return

    response = client.get(create_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Signed credential steps must come after all validation steps" in html
    assert "Advisory actions may appear after the signed credential step." in html
    assert "Show success messages for passed assertions" not in html
    assert html.index("Step name") < html.index(
        "Signed credential steps must come after all validation steps",
    )

    response = client.post(
        create_url,
        data={
            "name": "Issue signed credential",
            "description": "Issue a signed credential for passing runs.",
        },
    )
    assert response.status_code == HTTPStatus.FOUND

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.action is not None
    assert step.action.definition == definition
    variant = step.action.get_variant()
    assert variant is not None


def test_create_view_validates_missing_upload(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.JSON_SCHEMA,
        "json-validator",
        "JSON Validator",
    )

    create_url = _select_validator(client, workflow, validator)

    response = client.post(
        create_url,
        data={
            "name": "JSON Schema",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    html = response.content.decode()
    assert "Add content directly or upload a file." in html


def test_create_json_schema_rejects_text_and_file(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.JSON_SCHEMA,
        "json-validator",
        "JSON Validator",
    )

    create_url = _select_validator(client, workflow, validator)
    fake_file = SimpleUploadedFile(
        "schema.json",
        b"{}",
        content_type="application/json",
    )
    response = client.post(
        create_url,
        data={
            "name": "JSON Schema",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            "schema_text": '{"type": "object"}',
            "schema_file": fake_file,
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    body = response.content.decode()
    assert "Paste the schema or upload a file, not both." in body


def test_create_xml_step_requires_schema_text(client):
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.XML])
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.XML_SCHEMA,
        "xml-validator",
        "XML Validator",
    )

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "XML Schema",
            "schema_type": "XSD",
            "schema_text": "",
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    html = response.content.decode()
    assert "We found a few issues" in html
    assert "Add content directly or upload a file." in html
    assert 'name="schema_text"' in html
    assert "is-invalid" in html
    assert "Create step" in html


def test_create_ai_policy_requires_rules(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "Policy check",
            "template": "policy_check",
            "policy_rules": "",
            "selectors": "",
            "mode": "BLOCKING",
            "cost_cap_cents": 15,
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    html = response.content.decode()
    assert "We found a few issues" in html
    assert "Add at least one policy rule." in html
    assert "Create step" in html


def test_create_energyplus_step_with_optional_model_review_checks(client):
    """Authors should only be offered review checks beyond native IDF validation."""

    from validibot.validations.constants import ResourceFileType
    from validibot.validations.models import ValidatorResourceFile

    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.ENERGYPLUS, "energyplus", "EnergyPlus")

    # Create a weather file resource for the dropdown
    weather_resource = ValidatorResourceFile.objects.create(
        validator=validator,
        org=None,  # System-wide
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="San Francisco, CA (TMY3)",
        filename="USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        is_default=True,
    )

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "EnergyPlus QA",
            "validation_mode": "direct",
            "weather_file": str(weather_resource.id),
            "run_simulation": "on",
            "idf_checks": ["hvac-sizing", "schedule-coverage"],
        },
    )
    # 302 redirect to assertions page on success
    assert response.status_code == HTTPStatus.FOUND
    step = workflow.steps.first()
    assert step is not None
    assert step.config["idf_checks"] == ["hvac-sizing", "schedule-coverage"]
    assert step.config["run_simulation"] is True
    # Weather file is stored relationally via WorkflowStepResource, not in config
    from validibot.workflows.models import WorkflowStepResource

    weather_sr = step.step_resources.filter(
        role=WorkflowStepResource.WEATHER_FILE
    ).first()
    assert weather_sr is not None
    assert weather_sr.validator_resource_file_id == weather_resource.id


def test_energyplus_file_source_picker_renders_for_declared_ports(client):
    """EnergyPlus file ports should render as author-facing source choices."""
    from validibot.validations.models import ValidatorResourceFile

    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.ENERGYPLUS, "energyplus", "EnergyPlus")
    create_energyplus_file_ports(validator)
    ValidatorResourceFile.objects.create(
        validator=validator,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="San Francisco, CA (TMY3)",
        filename="USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        is_default=True,
    )

    response = client.get(_select_validator(client, workflow, validator))

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert 'name="primary_model_source"' in html
    assert 'name="weather_file_source"' in html
    assert "Model file" in html
    assert "Weather file" in html
    assert "Submitted file" in html
    assert "Workflow resource" in html
    assert "Earlier step output" in html
    assert "SUBMISSION_FILE" not in html
    assert "WORKFLOW_RESOURCE" not in html
    assert "UPSTREAM_ARTIFACT" not in html


def test_energyplus_file_source_picker_saves_default_bindings(client):
    """Source picker submissions should persist StepInputBinding rows."""
    from validibot.validations.models import ValidatorResourceFile

    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.ENERGYPLUS, "energyplus", "EnergyPlus")
    create_energyplus_file_ports(validator)
    weather_resource = ValidatorResourceFile.objects.create(
        validator=validator,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="San Francisco, CA (TMY3)",
        filename="USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        is_default=True,
    )

    response = client.post(
        _select_validator(client, workflow, validator),
        data={
            "name": "EnergyPlus QA",
            "validation_mode": "direct",
            "primary_model_source": BindingSourceScope.SUBMISSION_FILE,
            "weather_file_source": BindingSourceScope.WORKFLOW_RESOURCE,
            "weather_file": str(weather_resource.id),
            "run_simulation": "on",
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    step = workflow.steps.get()
    bindings = {
        binding.io_definition.contract_key: binding
        for binding in StepInputBinding.objects.filter(
            workflow_step=step
        ).select_related("io_definition")
    }
    assert bindings["primary_model"].source_scope == BindingSourceScope.SUBMISSION_FILE
    assert bindings["primary_model"].source_data_path == "primary"
    assert bindings["weather_file"].source_scope == BindingSourceScope.WORKFLOW_RESOURCE
    assert (
        bindings["weather_file"].source_data_path == ResourceFileType.ENERGYPLUS_WEATHER
    )


def test_energyplus_file_source_picker_saves_upstream_artifact_binding(client):
    """Earlier-step generated files should persist as upstream artifact paths."""
    from validibot.validations.models import ValidatorResourceFile

    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    upstream_step = WorkflowStepFactory(
        workflow=workflow,
        name="Build Model",
        order=10,
    )
    StepIODefinitionFactory(
        workflow_step=upstream_step,
        validator=None,
        contract_key="generated_model",
        native_name="generated_model",
        label="Generated model",
        direction=StepIODirection.OUTPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
    )
    validator = ensure_validator(ValidationType.ENERGYPLUS, "energyplus", "EnergyPlus")
    create_energyplus_file_ports(validator)
    weather_resource = ValidatorResourceFile.objects.create(
        validator=validator,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="San Francisco, CA (TMY3)",
        filename="USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        is_default=True,
    )

    response = client.post(
        _select_validator(client, workflow, validator),
        data={
            "name": "EnergyPlus QA",
            "validation_mode": "direct",
            "primary_model_source": BindingSourceScope.UPSTREAM_ARTIFACT,
            "primary_model_upstream_artifact": (
                f"{upstream_step.step_key}.generated_model"
            ),
            "weather_file_source": BindingSourceScope.WORKFLOW_RESOURCE,
            "weather_file": str(weather_resource.id),
            "run_simulation": "on",
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    step = workflow.steps.exclude(pk=upstream_step.pk).get()
    binding = StepInputBinding.objects.get(
        workflow_step=step,
        io_definition__contract_key="primary_model",
    )
    assert binding.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT
    assert binding.source_data_path == f"{upstream_step.step_key}.generated_model"


def test_file_source_picker_is_hidden_without_declared_file_ports(client):
    """Validators without artifact input ports should keep the existing form."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.BASIC, "basic", "Basic")

    response = client.get(_select_validator(client, workflow, validator))

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert 'name="primary_model_source"' not in html
    assert 'name="weather_file_source"' not in html


def test_step_detail_lists_generated_files_separately_from_outputs(client):
    """Generated file paths should appear in the file-specific data section."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    upstream_step = WorkflowStepFactory(
        workflow=workflow,
        name="Build Model",
        order=10,
    )
    StepIODefinitionFactory(
        workflow_step=upstream_step,
        validator=None,
        contract_key="generated_model",
        native_name="generated_model",
        label="Generated model",
        direction=StepIODirection.OUTPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
    )
    downstream_step = WorkflowStepFactory(
        workflow=workflow,
        name="Run Simulation",
        order=20,
    )

    response = client.get(
        reverse("workflows:workflow_step_edit", args=[workflow.pk, downstream_step.pk]),
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    artifact_path = f"steps.{upstream_step.step_key}.artifact.generated_model"
    assert "Generated Files From Earlier Steps" in html
    assert artifact_path in html
    assert "Upstream Step Outputs" not in html


def test_step_settings_does_not_expose_validator_selector(client):
    """The settings page stays focused on the already-selected validator."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.JSON_SCHEMA,
        "json-validator",
        "JSON Validator",
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator)

    edit_url = reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk])
    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert 'name="validator_choice"' not in body


def test_step_settings_uses_sticky_action_footer_editor(client):
    """Long validator forms must keep Cancel and Save visible in the viewport.

    XML, SHACL, Schematron, and the other non-Tabular validators all share this
    template. Pinning the reusable editor-shell classes here protects the whole
    family from regressing to document-level scrolling.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.XML_SCHEMA,
        "xml-sticky-footer",
        "XML Validator",
    )
    validator.description = "This explanatory subhead should not appear."
    validator.save(update_fields=["description"])
    step = WorkflowStepFactory(workflow=workflow, validator=validator)

    response = client.get(
        reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk]),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "app-viewport-locked" in body
    assert 'id="workflow-step-form" class="container-fluid editor-shell"' in body
    assert 'class="card app-card editor-card"' in body
    assert "card-title h5 mb-0" in body
    assert "Workflow Step Validator: XML Validator" in body
    assert "This explanatory subhead should not appear." not in body
    assert '<span class="badge text-bg-primary">XML Schema</span>' not in body
    assert 'class="card-body editor-card__scroll"' in body
    assert "card-footer" in body
    assert "Save changes" in body


def test_shacl_step_settings_shows_current_shapes_and_ontologies(client):
    """Editing a SHACL step should show saved files as current state.

    Browser file inputs always render as "No file chosen", so the form needs a
    read-only summary to make the keep-existing behavior obvious.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        org=workflow.org,
        ruleset_type=RulesetType.SHACL,
        rules_text="@prefix sh: <http://www.w3.org/ns/shacl#> .",
        metadata={
            "shape_files": [
                {
                    "name": "223p-shapes.ttl",
                    "size_bytes": 128,
                    "sha256": "a" * 64,
                },
            ],
            "has_inline_shapes": False,
            "ontology_text": "@prefix ex: <http://example.com/> .",
            "ontology_files": [
                {
                    "name": "g36-ontology.ttl",
                    "size_bytes": 64,
                    "sha256": "b" * 64,
                },
            ],
            "has_inline_ontology": False,
        },
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        config={
            "shape_files": ruleset.metadata["shape_files"],
            "ontology_files": ruleset.metadata["ontology_files"],
            "shapes_text_preview": ruleset.rules_text,
        },
    )

    response = client.get(
        reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk]),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Current SHACL shapes" in body
    assert "223p-shapes.ttl" in body
    assert "Current supplementary ontologies" in body
    assert "g36-ontology.ttl" in body
    assert "Leave the fields below blank to keep them." in body
    assert body.count("app-form-section") >= SHACL_STEP_SETTINGS_SECTION_COUNT
    assert "Basic settings" in body
    assert "SHACL shapes" in body
    assert "Supplementary ontologies" in body
    assert "Advanced options" in body
    assert "SHACL result handling" in body
    assert "data-help-drawer-trigger" in body
    assert "shacl-validator" in body
    assert "SHACL validator help" in body
    assert "Report only" in body
    assert '<div class="text-start">' in body


def test_shacl_step_settings_locked_ruleset_renders_400_not_500(client):
    """Saving SHACL settings on a step whose workflow has runs is a 400, not a 500.

    A SHACL step's ``Ruleset`` becomes immutable once the workflow has any
    validation runs (or the workflow itself is locked). Previously, the
    ``Ruleset.full_clean()`` call inside ``build_shacl_config`` raised a
    ``ValidationError`` that bubbled all the way to Django's request
    handler, surfacing to the operator as an opaque 500 from the SHACL
    step settings page.

    The view now catches the ``ValidationError`` and re-renders the form
    with the model-layer messages attached, so the operator gets a clear
    explanation that they need to create a new workflow version. This
    test pins that behaviour and guards against regressions that would
    re-expose the original 500.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        org=workflow.org,
        ruleset_type=RulesetType.SHACL,
        rules_text="@prefix sh: <http://www.w3.org/ns/shacl#> .",
        metadata={
            "has_inline_shapes": True,
            "ontology_text": "",
            "has_inline_ontology": False,
            "shape_files": [],
            "ontology_files": [],
        },
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        config={"shapes_text_preview": ruleset.rules_text},
    )
    # The lock invariant fires once the workflow has at least one run.
    ValidationRunFactory(workflow=workflow)

    edit_url = reverse(
        "workflows:workflow_step_settings",
        args=[workflow.pk, step.pk],
    )
    response = client.post(
        edit_url,
        data={
            "name": step.name,
            "description": "",
            "notes": "",
            "shapes_text": "@prefix sh: <http://www.w3.org/ns/shacl#> .\n# changed",
            "ontology_text": "",
            "inference_mode": "rdfs",
            "submission_format": "auto",
            "shacl_result_handling": "fail_after_assertions",
        },
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    body = response.content.decode()
    assert "referenced by a workflow that has runs" in body


def test_shacl_help_drawer_content_renders(client):
    """The SHACL help drawer should load as the standard right-drawer fragment."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)

    response = client.get(
        reverse("core:help_drawer", kwargs={"slug": "shacl-validator"}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "What the SHACL validator does" in body
    assert "SHACL result handling" in body
    assert "SPARQL ASK target graphs" in body


def test_create_basic_step_uses_minimal_fields(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.BASIC,
        "basic-validator",
        "Manual Assertions",
    )

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "Manual assertions",
            "description": "Hand-written checks",
            "notes": "Remember to add derivations later",
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    step = WorkflowStep.objects.get(workflow=workflow, validator=validator)
    assert step.name == "Manual assertions"
    assert step.config == {}


def test_update_view_prefills_ai_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")
    step = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=10,
        name="AI step",
        description="Existing summary",
        notes="Existing author notes",
        config={
            "template": "policy_check",
            "mode": "BLOCKING",
            "cost_cap_cents": 20,
            "selectors": ["$.zones[*].cooling_setpoint"],
            "policy_rules": [
                {
                    "path": "$.zones[*].cooling_setpoint",
                    "operator": ">=",
                    "value": 18,
                    "value_b": None,
                    "message": "Cooling must be ≥18°C",
                },
            ],
        },
    )

    edit_url = reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk])
    response = client.get(edit_url)
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "policy_check" in body
    assert "Cooling must be" in body
    assert "Existing summary" in body
    assert "Existing author notes" in body

    response = client.post(
        edit_url,
        data={
            "name": "AI step updated",
            "description": "Tweaked summary",
            "notes": "Revised notes",
            "template": "ai_critic",
            "selectors": "",
            "policy_rules": "",
            "cost_cap_cents": 15,
            "mode": "ADVISORY",
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    step.refresh_from_db()
    assert step.name == "AI step updated"
    assert step.description == "Tweaked summary"
    assert step.notes == "Revised notes"
    assert step.config["template"] == "ai_critic"
    assert step.config["mode"] == "ADVISORY"


def test_update_view_prefills_action_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Slack alert")
    action = SlackMessageAction.objects.create(
        definition=definition,
        name="Alert ops",
        description="Existing description",
        message="Original message",
    )
    step = WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=10,
        name="Alert ops",
        description="Existing description",
        notes="Existing notes",
        config={"message": "Original message"},
    )

    edit_url = reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk])
    response = client.get(edit_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Original message" in html
    assert "Existing notes" in html
    assert "Alert ops" in html

    response = client.post(
        edit_url,
        data={
            "name": "Alert ops updated",
            "description": "Updated description",
            "notes": "Updated notes",
            "message": "Escalate to ops channel.",
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    step.refresh_from_db()
    action_variant = step.action.get_variant()
    assert step.action.name == "Alert ops updated"
    assert step.description == "Updated description"
    assert step.notes == "Updated notes"
    assert isinstance(action_variant, SlackMessageAction)
    assert action_variant.message == "Escalate to ops channel."


def test_action_step_settings_collapses_self_referential_breadcrumb(client):
    """Action settings pages should not render a breadcrumb self-link."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Slack alert")
    action = SlackMessageAction.objects.create(
        definition=definition,
        name="Alert ops",
        description="Existing description",
        message="Original message",
    )
    step = WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=10,
        name="Alert ops",
        description="Existing description",
        notes="Existing notes",
        config={"message": "Original message"},
    )

    edit_url = reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk])
    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    breadcrumbs = response.context["breadcrumbs"]
    assert [str(crumb["name"]) for crumb in breadcrumbs] == [
        "Workflows",
        workflow.name,
        f"{step.step_number_display}: Edit Step Detail",
    ]
    assert breadcrumbs[-1]["url"] == ""


def test_validator_step_settings_keeps_step_breadcrumb_link(client):
    """Validator settings pages should keep the step overview breadcrumb."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")
    step = WorkflowStepFactory(workflow=workflow, validator=validator)

    edit_url = reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk])
    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    breadcrumbs = response.context["breadcrumbs"]
    assert [str(crumb["name"]) for crumb in breadcrumbs] == [
        "Workflows",
        workflow.name,
        step.step_number_display,
        "Edit Step Detail",
    ]
    assert breadcrumbs[-2]["url"]
    assert breadcrumbs[-1]["url"] == ""


def test_step_form_navigation_links(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")
    first_step = WorkflowStepFactory(workflow=workflow, validator=validator, order=10)
    second_step = WorkflowStepFactory(workflow=workflow, validator=validator, order=20)

    edit_url = reverse(
        "workflows:workflow_step_settings",
        args=[workflow.pk, second_step.pk],
    )
    response = client.get(edit_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert (
        reverse("workflows:workflow_step_edit", args=[workflow.pk, first_step.pk])
        in html
    )
    assert "Previous step" in html
    assert "Next step" not in html


def test_step_editor_shows_default_assertions_card(client):
    """Verify default assertions card shows when validator has rules defined."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        slug="custom-validator-with-defaults",
    )
    default_ruleset = validator.ensure_default_ruleset()
    RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_data_path="payload.price > 0",
        rhs={"expr": "payload.price > 0"},
        severity=Severity.ERROR,
        order=0,
        message_template="Baseline price check",
        cel_cache="payload.price > 0",
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator, order=10)

    edit_url = reverse("workflows:workflow_step_edit", args=[workflow.pk, step.pk])
    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "default-assertions-card" in html
    assert (
        "Default assertions run by the validator selected for this step: 1 assertion."
        in html
    )
    assert "View default assertions" in html


def test_step_editor_hides_default_assertions_when_none(client):
    """Verify default assertions card is hidden when validator has no rules."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        slug="custom-validator-no-defaults",
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator, order=10)

    edit_url = reverse("workflows:workflow_step_edit", args=[workflow.pk, step.pk])
    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "default-assertions-card" not in html


def test_step_editor_renders_resizable_column_separator(client):
    """The step editor must preserve the DOM contract used by its resizer."""
    workflow, step = _make_processor_step(client)
    edit_url = reverse(
        "workflows:workflow_step_edit",
        args=[workflow.pk, step.pk],
    )

    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert 'data-resizable-key="step-detail-v2"' in html
    assert 'data-resizable-default="66.6667"' in html
    assert html.count('class="resizable-panel') == RESIZABLE_PANEL_COUNT
    assert 'class="resizable-handle"' in html
    assert 'aria-label="Resize columns"' in html


def test_move_and_delete_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    step_a = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=10,
        name="First",
        config={"template": "ai_critic", "mode": "ADVISORY", "cost_cap_cents": 10},
    )
    step_b = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=20,
        name="Second",
        config={"template": "ai_critic", "mode": "ADVISORY", "cost_cap_cents": 10},
    )

    move_url = reverse("workflows:workflow_step_move", args=[workflow.pk, step_b.pk])
    response = client.post(move_url, data={"direction": "up"}, HTTP_HX_REQUEST="true")
    assert response.status_code == HTTPStatus.NO_CONTENT
    step_a.refresh_from_db()
    step_b.refresh_from_db()
    assert step_b.order == 10  # noqa: PLR2004
    assert step_a.order == 20  # noqa: PLR2004

    delete_url = reverse(
        "workflows:workflow_step_delete",
        args=[workflow.pk, step_a.pk],
    )
    response = client.post(delete_url, HTTP_HX_REQUEST="true")
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert list(workflow.steps.all()) == [step_b]


def test_step_create_rejects_validator_from_other_org(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    foreign_org = OrganizationFactory()
    bad_validator = ValidatorFactory(org=foreign_org, is_system=False)

    url = reverse(
        "workflows:workflow_step_create",
        args=[workflow.pk, bad_validator.pk],
    )
    response = client.get(url)
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_step_delete_requires_manager_role(client):
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow)
    user = UserFactory()
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    _login_user_for_org(client, user, workflow.org)

    url = reverse(
        "workflows:workflow_step_delete",
        args=[workflow.pk, step.pk],
    )
    response = client.post(url)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert WorkflowStep.objects.filter(pk=step.pk).exists()


def test_step_move_requires_manager_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow, order=10)
    target_step = WorkflowStepFactory(workflow=workflow, order=20)
    user = UserFactory()
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    _login_user_for_org(client, user, workflow.org)

    url = reverse(
        "workflows:workflow_step_move",
        args=[workflow.pk, target_step.pk],
    )
    response = client.post(url, data={"direction": "up"})
    assert response.status_code == HTTPStatus.FORBIDDEN
    orders = list(
        workflow.steps.order_by("order").values_list("order", flat=True),
    )
    assert orders == [10, 20]


def test_step_list_shows_author_notes_for_authors(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    WorkflowStepFactory(
        workflow=workflow,
        notes="Private deployment checklist",
        description="Performs strict validation",
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Author notes" in body
    assert "Private deployment checklist" in body


def test_step_list_renders_action_step(client):
    """The step list renders Slack action summaries from step config."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Slack integration")
    action = SlackMessageAction.objects.create(
        definition=definition,
        name="Notify Slack",
        description="Send a Slack notification",
        message="Ping #alerts when the workflow completes.",
    )
    WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=10,
        name="Notify Slack",
        description="Send a Slack notification",
        config={"message": "Ping #alerts when the workflow completes."},
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Notify Slack" in html
    assert definition.get_action_category_display() in html
    assert "#alerts" in html


def test_step_list_places_type_badge_before_step_name(client):
    """Top-level step cards should identify the operation before the author name.

    The step number is positional metadata, the validator/action badge says what
    kind of operation runs, and the author-provided name follows. Keeping that
    order makes validator and action cards scan consistently in the workflow
    builder.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.JSON_SCHEMA,
        "json-schema-card-order",
        "JSON Schema",
    )
    validator_step = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=10,
        name="Product Schema",
        config={},
    )
    definition = make_action_definition(name="Slack integration")
    action = SlackMessageAction.objects.create(
        definition=definition,
        name="Notify Slack",
        message="Ping #alerts.",
    )
    action_step = WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=20,
        name="Notify Slack",
        config={"message": "Ping #alerts."},
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    validator_match = re.search(
        rf'data-step-id="{validator_step.id}".*?</div>\s*</div>\s*'
        rf'<div class="workflow-step-connector"',
        html,
        re.S,
    )
    action_match = re.search(
        rf'data-step-id="{action_step.id}".*?</div>\s*</div>',
        html,
        re.S,
    )
    assert validator_match
    assert action_match
    validator_card = validator_match.group(0)
    action_card = action_match.group(0)
    assert validator_card.index("JSON Schema") < validator_card.index(
        "Product Schema",
    )
    assert action_card.index(
        definition.get_action_category_display(),
    ) < action_card.index("Notify Slack")


def test_step_list_keeps_energyplus_card_to_header_fields(client):
    """EnergyPlus cards should not repeat simulation configuration.

    The workflow builder already identifies an EnergyPlus step through its type
    badge. Keeping the card to its step number, validator type, and authored
    name avoids a redundant ``Simulation: Yes`` detail row.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.ENERGYPLUS,
        "energyplus-card-summary",
        "EnergyPlus",
    )
    WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=10,
        name="Test Energy plus validation",
        config={"run_simulation": True},
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    normalized_html = re.sub(r"\s+", " ", html)
    assert "Step 1" in normalized_html
    assert validator.get_validation_type_display() in normalized_html
    assert "Test Energy plus validation" in normalized_html
    assert "Simulation" not in html
    assert "Yes" not in html


def test_step_list_keeps_actions_in_right_column(client):
    """Long step summaries must not wrap the action buttons below the content.

    Workflow cards should follow the assertion-card layout: the content column
    may shrink while the action group remains fixed at the right edge.
    """
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    WorkflowStepFactory(
        workflow=workflow,
        description=(
            "A deliberately long description that should yield space to the "
            "workflow step action buttons."
        ),
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert 'class="d-flex justify-content-between align-items-start gap-3"' in html
    assert 'class="min-w-0 flex-grow-1"' in html
    assert 'class="btn-group btn-group-sm flex-shrink-0"' in html


def test_step_list_renders_signed_credential_summary(client):
    """Credential steps should render a minimal summary in the step list."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(
        category=ActionCategoryType.CREDENTIAL,
        name="Signed credential",
        type_value=CredentialActionType.SIGNED_CREDENTIAL,
    )
    action = Action.objects.create(
        definition=definition,
        name="Issue credential",
        description="Issue a signed credential.",
    )
    WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=10,
        name="Issue credential",
        description="Issue a signed credential.",
        config={},
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "bi-award" in html
    assert "Signed credential · Credential" not in html
    assert "No additional configuration" not in html


def test_step_list_disables_invalid_signed_credential_move(client):
    """The workflow step list disables moves that would violate credential order."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")
    WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=10,
        name="Validate first",
        config={"template": "ai_critic", "mode": "ADVISORY", "cost_cap_cents": 10},
    )
    definition = make_action_definition(
        category=ActionCategoryType.CREDENTIAL,
        name="Signed credential",
        type_value=CredentialActionType.SIGNED_CREDENTIAL,
    )
    action = Action.objects.create(
        definition=definition,
        name="Issue credential",
        description="Issue a signed credential.",
    )
    step = WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=20,
        name="Issue credential",
        description="Issue a signed credential.",
        config={},
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Move buttons that would break this rule are disabled." in html
    assert (
        "This step must remain after all validation steps and blocking actions." in html
    )
    assert re.search(
        rf'data-step-id="{step.id}".*?data-move-direction="up".*?disabled',
        html,
        re.S,
    )


def test_step_list_hides_author_notes_for_non_authors(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(
        workflow=workflow,
        notes="Only authors should see this",
        description="General description",
    )

    other_user = UserFactory()
    grant_role(other_user, workflow.org, RoleCode.EXECUTOR)
    other_user.set_current_org(workflow.org)
    client.force_login(other_user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Author notes" not in body
    assert "Only authors should see this" not in body


def test_wizard_select_highlights_selected_card(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    ensure_validator(ValidationType.JSON_SCHEMA, "json-validator", "JSON Validator")
    validator_b = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(
        url,
        {"selected": f"validator:{validator_b.pk}"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    parser = _CardParser()
    parser.feed(html)
    assert parser.selected_cards == 1
    assert parser.checked_values == [f"validator:{validator_b.pk}"]


class _CardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.selected_cards = 0
        self.checked_values: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "label":
            classes = attrs_dict.get("class", "")
            if "validator-card" in classes and "is-selected" in classes:
                self.selected_cards += 1
        if (
            tag == "input"
            and attrs_dict.get("type") == "radio"
            and attrs_dict.get("name") == "choice"
        ):
            if any(name == "checked" for name, _ in attrs):
                self.checked_values.append(str(attrs_dict.get("value")))


def _tab_panes_by_id(html: str) -> dict[str, str]:
    parser = _TabPaneParser()
    parser.feed(html)
    return parser.panes


class _TabPaneParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.panes: dict[str, str] = {}
        self._current_id: str | None = None
        self._depth = 0
        self._text_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "")
        element_id = attrs_dict.get("id")
        if (
            tag == "div"
            and element_id
            and element_id.startswith("workflow-tab-")
            and "tab-pane" in classes
        ):
            self._current_id = element_id
            self._depth = 1
            self._text_parts = []
            return
        if self._current_id:
            self._depth += 1

    def handle_endtag(self, tag):
        if not self._current_id:
            return
        self._depth -= 1
        if self._depth == 0:
            self.panes[self._current_id] = " ".join(self._text_parts)
            self._current_id = None

    def handle_data(self, data):
        if self._current_id:
            self._text_parts.append(data)


# ── Step detail: three-section layout for processor validators ────────
# Validators with has_processor=True always show the input assertions /
# process divider / output assertions layout, even when no step I/O
# definitions exist yet.  The Inputs and Outputs card in the right
# column also always shows both tabs.


def _make_processor_step(client):
    """Create a workflow step with a processor validator and log in."""
    validator = Validator.objects.create(
        validation_type=ValidationType.FMU,
        slug="fmu-test-processor",
        name="FMU Test",
        description="FMU validator for testing",
        has_processor=True,
        supports_assertions=True,
    )
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    _login_for_workflow(client, workflow)
    return workflow, step


def test_processor_step_shows_io_stages_layout(client):
    """A step with a processor validator must render the three-section
    assertions layout (input assertions / process divider / output
    assertions) even when no step I/O definitions exist.

    This ensures the user always sees the structural slots for input
    and output assertions on processor-based validators like FMU and
    EnergyPlus.
    """
    workflow, step = _make_processor_step(client)
    url = reverse(
        "workflows:workflow_step_edit",
        args=[workflow.pk, step.pk],
    )

    response = client.get(url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "has-validator-flow" in html
    assert "validator-process-chip" in html
    assert "Input assertions" in html or "input" in html.lower()
    assert "Output assertions" in html or "output" in html.lower()


def test_step_editor_header_marks_previous_workflow_version(client):
    """The step editor should show the same compact version pill as detail.

    Authors can open a step from an older workflow version through the
    Version history card. The editor header needs to keep that version context
    visible so editing/navigating does not make v1 look like the latest row.
    """
    workflow, step = _make_processor_step(client)
    WorkflowFactory(
        org=workflow.org,
        user=workflow.user,
        slug=workflow.slug,
        version="2",
    )
    url = reverse(
        "workflows:workflow_step_edit",
        args=[workflow.pk, step.pk],
    )

    response = client.get(url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "v1" in html
    assert "workflow-version-badge--previous" in html
    assert "Previous version" in html


def test_step_settings_header_marks_previous_workflow_version(client):
    """The step settings form should use the shared version badge component.

    Step settings is a separate edit route from the assertion editor. Both
    routes need the same visible version context so an author knows when they
    are configuring a historical workflow version.
    """
    workflow, step = _make_processor_step(client)
    WorkflowFactory(
        org=workflow.org,
        user=workflow.user,
        slug=workflow.slug,
        version="2",
    )
    url = reverse(
        "workflows:workflow_step_settings",
        args=[workflow.pk, step.pk],
    )

    response = client.get(url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert workflow.name in html
    assert "v1" in html
    assert "workflow-version-badge--previous" in html
    assert "Previous version" in html


def test_processor_step_shows_both_io_tabs(client):
    """A step with a processor validator must render both Step Inputs and
    Step Outputs tabs in the Inputs and Outputs card, even when one or
    both sides have zero step-input/output definitions.

    Without this, users of FMU/EnergyPlus validators see a confusing
    flat card instead of the expected tabbed layout.

    Per ADR-2026-05-22b, the tab labels are "Step Inputs" and
    "Step Outputs" (previously "Validator Inputs"/"Validator Outputs"
    under the legacy terminology).
    """
    workflow, step = _make_processor_step(client)
    url = reverse(
        "workflows:workflow_step_edit",
        args=[workflow.pk, step.pk],
    )

    response = client.get(url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "step-io-input-tab" in html
    assert "step-io-output-tab" in html
    assert "Step Inputs" in html
    assert "Step Outputs" in html
    assert "Inputs and Outputs" in html


def test_non_processor_step_hides_empty_io_card(client):
    """A step without a processor (e.g. Basic validator) should not
    render the Inputs and Outputs card when there are no I/O definitions.

    This avoids showing an empty, confusing card for simple validators
    that don't have a processor stage.
    """
    validator = Validator.objects.create(
        validation_type=ValidationType.BASIC,
        slug="basic-test-layout",
        name="Basic Test",
        description="Basic validator for testing",
        has_processor=False,
        supports_assertions=True,
    )
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    _login_for_workflow(client, workflow)

    url = reverse(
        "workflows:workflow_step_edit",
        args=[workflow.pk, step.pk],
    )

    response = client.get(url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    # No process chip for basic validators
    assert "validator-process-chip" not in html
    # No I/O tabs when there are no definitions and no processor
    assert "step-io-input-tab" not in html


# ── Step detail: visible operation item for inline validators ─────────
# Schema/RDF validators run a core validation before optional assertions, so
# the editor needs a display-only card for that operation even when no
# assertions exist yet.


@pytest.mark.parametrize(
    ("validation_type", "expected_label", "expected_detail"),
    [
        (
            ValidationType.JSON_SCHEMA,
            "JSON Schema Validation",
            "Validates the submitted JSON document",
        ),
        (
            ValidationType.XML_SCHEMA,
            "XML Validation",
            "Validates the submitted XML document",
        ),
    ],
)
def test_schema_steps_show_operation_card_and_first_assertion_connector(
    client,
    validation_type,
    expected_label,
    expected_detail,
):
    """JSON/XML schema steps should show validation plus an assertion add lane.

    Schema validation is the built-in operation. Authors can then add
    independent step assertions underneath it, so a no-assertions state needs
    the same dotted connector and terminal plus button used by SHACL.
    """
    validator = ValidatorFactory(
        validation_type=validation_type,
        supports_assertions=True,
    )
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    _login_for_workflow(client, workflow)

    response = client.get(
        reverse(
            "workflows:workflow_step_edit",
            args=[workflow.pk, step.pk],
        ),
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "validator-operation-card" in html
    assert expected_label in html
    assert expected_detail in html
    assert "This validator does not support assertions" not in html
    assert "assertion-add-connector--terminal" in html
    assert html.count("assertion-add-button") == 1
    # Every validation operation card now carries a right-edge edit icon
    # linking to the step settings page.  This affordance used to be
    # Tabular-only, so JSON and XML schema steps must show it too.  Scope
    # the check to the card so it can't be satisfied by the page header's
    # own edit pencil, which sits above the assertion lane.
    card_start = html.index("validator-operation-card-wrapper")
    card_end = html.index("assertion-add-connector", card_start)
    operation_html = html[card_start:card_end]
    settings_url = reverse(
        "workflows:workflow_step_settings",
        args=[workflow.pk, step.pk],
    )
    assert settings_url in operation_html
    assert "bi-pencil-square" in operation_html


def test_assertions_partial_keeps_schema_operation_card_after_refresh(client):
    """The HTMx refresh endpoint must preserve the inline validation card.

    Assertion changes refresh only the editor body, so the partial view needs
    the same operation-card context as the full step detail page.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        supports_assertions=True,
    )
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    _login_for_workflow(client, workflow)

    response = client.get(
        reverse(
            "workflows:workflow_step_assertions_partial",
            args=[workflow.pk, step.pk],
        ),
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "validator-operation-card" in html
    assert "JSON Schema Validation" in html
    assert "This validator does not support assertions" not in html
    assert "assertion-add-connector--terminal" in html
    # The edit icon must survive the HTMx refresh, since the card is
    # re-rendered by this partial after every assertion change.  The
    # partial response carries no page header, so any edit pencil here
    # belongs to the operation card itself.
    settings_url = reverse(
        "workflows:workflow_step_settings",
        args=[workflow.pk, step.pk],
    )
    assert settings_url in html
    assert "bi-pencil-square" in html


def test_shacl_step_shows_validation_operation_before_assertions(client):
    """SHACL steps should show SHACL Validation before assertion cards.

    SHACL assertions are optional checks that run after the SHACL validation
    itself, so the editor needs to make the base validation visible at the top
    of the assertion lane.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        supports_assertions=True,
    )
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    _login_for_workflow(client, workflow)

    response = client.get(
        reverse(
            "workflows:workflow_step_edit",
            args=[workflow.pk, step.pk],
        ),
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    operation_index = html.index("SHACL Validation")
    connector_index = html.index("assertion-add-connector")
    assert "validator-operation-card" in html
    assert "Validates the submitted RDF graph" in html
    assert operation_index < connector_index
    assert "assertion-add-connector--terminal" in html
    assert html.count("assertion-add-button") == 1
    assert "insert_at_start=1" not in html
    assert "No assertions have been added yet." not in html
    # SHACL's card carries the same right-edge edit icon as the other
    # schema validators — the affordance is no longer Tabular-only.  Scope
    # the check to the card (up to the first assertion connector) so it
    # can't be satisfied by the page header's edit pencil.
    card_start = html.index("validator-operation-card-wrapper")
    operation_html = html[card_start:connector_index]
    settings_url = reverse(
        "workflows:workflow_step_settings",
        args=[workflow.pk, step.pk],
    )
    assert settings_url in operation_html
    assert "bi-pencil-square" in operation_html


def test_tabular_step_shows_validation_operation_before_assertions(client):
    """Tabular validation owns its settings action without a redundant pill.

    The Tabular Validator runs its column-schema + row-rule validation before
    any step-level assertions, so the operation card must remain first and is
    the clearest place to edit that operation's settings. The side summary is
    read-only, and the generic "Validation" pill adds no information beside the
    explicit "Tabular Validation" title.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.TABULAR,
        supports_assertions=True,
    )
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    _login_for_workflow(client, workflow)

    response = client.get(
        reverse(
            "workflows:workflow_step_edit",
            args=[workflow.pk, step.pk],
        ),
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    # The operation card appears before the staged assertion authoring surface.
    operation_card_index = html.index("validator-operation-card-wrapper")
    operation_index = html.index("Tabular Validation")
    edit_settings_index = html.index("Edit settings")
    dataset_index = html.index("Dataset assertions")
    assert "validator-operation-card" in html
    assert "Validates the submitted CSV" in html
    assert operation_index < edit_settings_index < dataset_index
    settings_url = reverse(
        "workflows:workflow_step_settings",
        args=[workflow.pk, step.pk],
    )
    operation_html = html[operation_card_index:dataset_index]
    assert settings_url in operation_html
    # The card exposes exactly one edit affordance: the pencil icon that
    # links to the step settings page.  Assert on the link target, not the
    # "Edit settings" label — that text now lives in both the title and
    # aria-label attributes of the icon-only button, so a string count of
    # the label would be a brittle "2" rather than the one button we mean.
    assert operation_html.count(settings_url) == 1
    assert "bi-pencil-square" in operation_html
    assert "text-bg-light text-uppercase" not in operation_html
    assert "data-tabular-operation-summary" in operation_html
    assert "Reader" in operation_html
    assert "Delimiter" in operation_html
    assert "Header row" in operation_html
    assert "Columns" in operation_html
    assert "Required columns" in operation_html
    assert "Tabular configuration" not in html
    assert "step-io-input-tab" in html
    assert "step-io-output-tab" in html
    assert "Inputs and Outputs" in html
    assert "Available Data" in html
    assert "Edit Signals" in html
    input_panel_start = html.index('id="step-io-input-panel"')
    input_panel_end = html.index('id="step-io-output-panel"')
    input_panel_html = html[input_panel_start:input_panel_end]
    for contract_key, label in TABULAR_DATASET_INPUTS:
        assert f"i.{contract_key}" in input_panel_html
        assert str(label) in input_panel_html
    assert "No step inputs." not in input_panel_html
    assert input_panel_html.count("Provided by validator") == len(
        TABULAR_DATASET_INPUTS,
    )
    assert html.count("assertion-add-connector--stage") == TABULAR_STAGE_CONNECTOR_COUNT
    assert (
        html.count("assertion-add-connector--line-only")
        == TABULAR_STAGE_CONNECTOR_COUNT
    )
    assert "assertion-add-button" not in html
    assert "Add dataset assertion" in html
    assert "Add row assertion" in html
    assert "Add column assertion" in html
    assert html.count('data-bs-toggle="tooltip"') >= TABULAR_STAGE_CONNECTOR_COUNT
    # The bare placeholder must be gone now that the operation card stands in
    # as the panel's first item.
    assert "No assertions have been added yet." not in html


def test_tabular_assertions_partial_keeps_operation_summary(client):
    """HTMx assertion refreshes must retain the Tabular settings summary.

    The operation card lives inside the refreshed editor content, so its compact
    reader/schema facts must be rebuilt by the partial endpoint rather than
    disappearing after an assertion is added, edited, or reordered.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.TABULAR,
        supports_assertions=True,
    )
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config={
            "delimiter_label": "Comma",
            "has_header": True,
            "column_count": 3,
            "required_column_count": 2,
        },
    )
    _login_for_workflow(client, workflow)

    response = client.get(
        reverse(
            "workflows:workflow_step_assertions_partial",
            args=[workflow.pk, step.pk],
        ),
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "data-tabular-operation-summary" in html
    assert "Comma" in html
    assert "Header row" in html
    assert re.search(r"Columns:</span>\s*3", html)
    assert re.search(r"Required columns:</span>\s*2", html)


def test_step_delete_with_runs_returns_warning(client):
    """Deleting a step that has existing validation runs should return a
    user-friendly warning instead of a 500 error.

    The ValidationStepRun model uses on_delete=PROTECT, so the delete
    would fail with a ProtectedError. The view must catch this and
    return a toast warning message.
    """
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun

    workflow = WorkflowFactory()
    validator = Validator.objects.create(
        validation_type=ValidationType.BASIC,
        slug="basic-delete-test",
        name="Basic Delete Test",
        description="For testing delete protection",
        supports_assertions=True,
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    _login_for_workflow(client, workflow)

    run = ValidationRun.objects.create(
        workflow=workflow,
        org=workflow.org,
    )
    ValidationStepRun.objects.create(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
    )

    url = reverse(
        "workflows:workflow_step_delete",
        args=[workflow.pk, step.pk],
    )
    response = client.post(url, HTTP_HX_REQUEST="true")

    # The step must still exist (delete was blocked by PROTECT).
    assert WorkflowStep.objects.filter(pk=step.pk).exists()
    # Response triggers a page refresh so the warning toast renders.
    assert response.status_code == HTTPStatus.OK
    assert response["HX-Refresh"] == "true"
