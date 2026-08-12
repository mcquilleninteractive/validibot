"""Integration coverage for workflow authoring and launch surfaces.

These tests exercise the HTTP and API paths that stitch workflows,
validators, assertions, submissions, and versioning together. They protect
the product-level behavior authors rely on rather than only model/service
units.
"""

import re
from http import HTTPStatus

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import DataRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationRun
from validibot.validations.models import Validator
from validibot.validations.services.custom_validator_contracts import (
    sync_configured_io_contract,
)
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db

RESIZABLE_PANEL_COUNT = 2


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def run_validation_tasks_inline(monkeypatch):
    """
    Validation runs now execute inline; fixture retained for compatibility.
    """


def _ensure_custom_validator():
    """Get or create a CUSTOM_VALIDATOR for assertion testing.

    Uses CUSTOM_VALIDATOR because it's in ADVANCED_VALIDATION_TYPES and
    supports workflow step assertions with custom targets.
    """
    validator = Validator.objects.filter(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
    ).first()
    if validator:
        if not validator.allow_custom_assertion_targets:
            validator.allow_custom_assertion_targets = True
            validator.save(update_fields=["allow_custom_assertion_targets"])
        return validator
    return ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        name="Custom Assertions",
        allow_custom_assertion_targets=True,
    )


def _login_user_for_org(client, user, org):
    grant_role(user, org, RoleCode.OWNER)
    user.set_current_org(org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()


def _workflow_settings_payload(workflow, **overrides):
    """Build a complete settings POST while allowing one concern to vary."""

    payload = {
        "name": workflow.name,
        "description": workflow.description,
        "slug": workflow.slug,
        "project": str(workflow.project_id),
        "allowed_file_types": list(workflow.allowed_file_types),
        "input_schema_source_mode": workflow.input_schema_source_mode,
        "input_schema_source_text": workflow.input_schema_source_text,
        "input_retention": workflow.input_retention,
        "output_retention": workflow.output_retention,
        "success_message": workflow.success_message,
        "allow_submission_name": "on",
        "allow_submission_meta_data": "on",
        "allow_submission_short_description": "on",
        "version": workflow.version,
        "is_active": "on",
    }
    payload.update(overrides)
    return payload


def test_create_workflow_with_custom_step_and_assertion(client):
    """Verify end-to-end creation of workflow, step, and custom target assertion."""
    user = UserFactory()
    org = OrganizationFactory()
    project = ProjectFactory(org=org, is_default=True)
    _login_user_for_org(client, user, org)

    validator = _ensure_custom_validator()

    # Create workflow
    response = client.post(
        reverse("workflows:workflow_create"),
        data={
            "name": "Price check",
            "slug": "price-check",
            "version": "1",
            "is_active": "on",
            "project": str(project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "input_retention": DataRetention.DO_NOT_STORE,
            "output_retention": "STORE_30_DAYS",
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    workflow = Workflow.objects.get(name="Price check")
    assert workflow.project == project

    # Add CUSTOM_VALIDATOR step
    step_response = client.post(
        reverse(
            "workflows:workflow_step_create",
            kwargs={"pk": workflow.pk, "validator_id": validator.pk},
        ),
        data={
            "name": "Manual price gate",
            "description": "",
            "notes": "",
        },
    )
    assert step_response.status_code == HTTPStatus.FOUND
    step = workflow.steps.get(name="Manual price gate")

    # Add assertion ensuring price < 20
    assertion_response = client.post(
        reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        ),
        data={
            "assertion_type": "basic",
            "target_data_path": "price",
            "operator": AssertionOperator.LT,
            "comparison_value": "20",
            "severity": "ERROR",
            "message_template": "Price is too expensive! It should be less than $20.",
        },
        HTTP_HX_REQUEST="true",
    )
    assert assertion_response.status_code == HTTPStatus.NO_CONTENT
    step.refresh_from_db()
    assertion = RulesetAssertion.objects.get(ruleset=step.ruleset)
    assert assertion.target_data_path == "price"
    assert assertion.operator == AssertionOperator.LT
    assert assertion.rhs.get("value") == 20.0  # noqa: PLR2004
    assert (
        assertion.message_template
        == "Price is too expensive! It should be less than $20."
    )


def test_workflow_clone_view_creates_explicit_new_version(client):
    """Authors need a concrete path when versioned-history edits require clone.

    Beyond proving the clone happens, this also pins the post-clone redirect
    target to the new version's *edit* screen. The policy doc (workflow-
    versioning-policy.md §"User Experience Rules") explicitly requires putting
    the author on the edit screen so their next click is an edit — landing on
    the read-only detail page would defeat the whole reason they cloned.
    """
    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(org=org, user=user, version=1)

    response = client.post(
        reverse("workflows:workflow_clone", kwargs={"pk": workflow.pk}),
    )

    workflow.refresh_from_db()
    clone = Workflow.objects.get(slug=workflow.slug, version=2)
    assert response.status_code == HTTPStatus.FOUND
    expected_edit_url = reverse(
        "workflows:workflow_update",
        kwargs={"pk": clone.pk},
    )
    assert response.url == expected_edit_url, (
        f"Standalone clone should redirect to the new version's edit screen, "
        f"got {response.url!r}"
    )
    assert workflow.is_locked is True
    assert clone.is_locked is False
    assert clone.history_policy == workflow.history_policy


def test_workflow_update_header_uses_shared_version_badge(client):
    """The workflow settings editor should show version context in the header.

    Authors often land on the edit screen immediately after cloning or from an
    older detail page. The header badge keeps the edited workflow row explicit
    and should use the same component classes as the list and step editors.
    """
    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    slug = "header-version-context"
    older = WorkflowFactory(org=org, user=user, slug=slug, version="1")
    WorkflowFactory(org=org, user=user, slug=slug, version="2")

    response = client.get(reverse("workflows:workflow_update", kwargs={"pk": older.pk}))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Edit Workflow Settings" in body
    assert "v1" in body
    assert "workflow-version-badge--previous" in body
    assert "Previous version" in body
    assert "app-form-section" in body
    assert "Workflow basics" in body
    assert "Submission settings" in body
    assert "app-viewport-locked" in body
    assert 'id="workflow-edit" class="container-fluid editor-shell"' in body
    assert 'class="card app-card editor-card"' in body
    assert 'class="card-body editor-card__scroll"' in body
    assert "card-footer" in body


def test_workflow_settings_explains_editing_policy_accessibly(client):
    """The select, information icon, and detailed copy form one discoverable UI."""

    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(org=org, user=user)

    response = client.get(
        reverse("workflows:workflow_update", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Editing policy" in body
    assert 'aria-label="About editing policies"' in body
    assert 'id="id_history_policy-details"' in body
    assert "Versioned" in body
    assert "is recommended when historical confidence matters" in body
    assert "semantic saves are temporarily rejected" in body
    assert 'value="versioned" selected' in body


def test_used_workflow_renders_fixed_editing_policy_reason(client):
    """Authors should understand why the consistent select is currently disabled."""

    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(org=org, user=user)
    ValidationRunFactory(workflow=workflow)

    response = client.get(
        reverse("workflows:workflow_update", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert 'name="history_policy"' in body
    assert "disabled" in body
    assert "editing policy is fixed for this version" in body
    assert 'id="id_history_policy-fixed-reason"' in body


def test_mutable_semantic_settings_save_retries_after_definition_release(client):
    """A busy Mutable workflow rejects atomically, then accepts the same later save."""

    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        history_policy=WorkflowHistoryPolicy.MUTABLE,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    run = ValidationRunFactory(workflow=workflow, status=ValidationRunStatus.RUNNING)
    update_url = reverse("workflows:workflow_update", kwargs={"pk": workflow.pk})
    payload = _workflow_settings_payload(
        workflow,
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
    )

    blocked_response = client.post(update_url, data=payload)

    assert blocked_response.status_code == HTTPStatus.OK
    assert "currently being used by one validation run" in (
        blocked_response.content.decode()
    )
    workflow.refresh_from_db()
    assert workflow.allowed_file_types == [SubmissionFileType.JSON]

    run.definition_released_at = timezone.now()
    run.save(update_fields=["definition_released_at"])
    retry_response = client.post(update_url, data=payload)

    assert retry_response.status_code == HTTPStatus.FOUND
    workflow.refresh_from_db()
    assert workflow.allowed_file_types == [
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
    ]


def test_mutable_cosmetic_settings_save_succeeds_during_run(client):
    """The safety fence should not prevent copy corrections or deactivation."""

    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        history_policy=WorkflowHistoryPolicy.MUTABLE,
    )
    ValidationRunFactory(workflow=workflow, status=ValidationRunStatus.RUNNING)
    update_url = reverse("workflows:workflow_update", kwargs={"pk": workflow.pk})

    response = client.post(
        update_url,
        data=_workflow_settings_payload(
            workflow,
            name="Clearer workflow name",
            is_active="",
        ),
    )

    assert response.status_code == HTTPStatus.FOUND
    workflow.refresh_from_db()
    assert workflow.name == "Clearer workflow name"
    assert workflow.is_active is False


def test_superuser_semantic_repair_is_rejected_during_versioned_run(client):
    """Break-glass history repair cannot change semantics beneath a live run."""

    user = UserFactory(is_superuser=True, is_staff=True)
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        history_policy=WorkflowHistoryPolicy.VERSIONED,
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
    )
    ValidationRunFactory(workflow=workflow, status=ValidationRunStatus.RUNNING)

    response = client.post(
        reverse("workflows:workflow_update", kwargs={"pk": workflow.pk}),
        data=_workflow_settings_payload(
            workflow,
            allowed_file_types=[SubmissionFileType.JSON],
            history_policy=WorkflowHistoryPolicy.VERSIONED,
        ),
    )

    assert response.status_code == HTTPStatus.OK
    assert "currently being used by one validation run" in response.content.decode()
    workflow.refresh_from_db()
    assert workflow.allowed_file_types == [
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
    ]


def test_workflow_breadcrumb_places_version_badge_before_truncated_name(client):
    """The workflow breadcrumb should keep the version visible before the name.

    Breadcrumb items have a tight max width, so the workflow name is the part
    that should truncate. The version badge must render before the name so
    authors can still see which row they are on.
    """
    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        name="A very long workflow name that should be truncated in breadcrumbs",
        version="1",
    )

    response = client.get(
        reverse("workflows:workflow_detail", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    breadcrumb_html = body[
        body.index('<nav id="breadcrumbs"') : body.index("</nav>") + len("</nav>")
    ]
    badge_index = breadcrumb_html.index("workflow-version-badge--sm")
    name_index = breadcrumb_html.index('<span class="breadcrumb-workflow-name">')
    assert badge_index < name_index
    assert "breadcrumb-workflow-name" in breadcrumb_html


def test_workflow_detail_uses_history_card_for_version_navigation(client):
    """The detail page must expose version navigation only through history.

    Older versions should be discoverable without putting a global selector in
    the header. Keeping navigation in the right-column history card makes the
    page chrome calmer while still linking to exact workflow rows.
    """
    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    slug = "building-energy-check"
    v1 = WorkflowFactory(org=org, user=user, slug=slug, version="1")
    v2 = WorkflowFactory(org=org, user=user, slug=slug, version="2")

    response = client.get(reverse("workflows:workflow_detail", kwargs={"pk": v1.pk}))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    v2_url = reverse("workflows:workflow_detail", kwargs={"pk": v2.pk})
    assert 'id="workflow-version-switcher"' not in body
    assert "workflow-version-history-card" in body
    assert f'href="{v2_url}"' in body
    assert "v1" in body
    assert "v2" in body
    assert "latest" in body
    assert "workflow-version-badge--previous" in body
    assert "Previous version" in body


def test_workflow_detail_uses_shared_resizable_editor_layout(client):
    """Workflow and step editors must use the same two-column layout contract."""
    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(org=org, user=user)

    response = client.get(
        reverse("workflows:workflow_detail", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert 'data-resizable-key="workflow-detail-v2"' in body
    assert 'data-resizable-default="66.6667"' in body
    assert body.count('class="resizable-panel') == RESIZABLE_PANEL_COUNT
    assert 'class="resizable-handle"' in body


def test_workflow_detail_toolbar_orders_launch_actions_and_delete(client):
    """The detail toolbar should group actions without changing permissions.

    Launch is the primary action, workflow tools sit together after the first
    3rem gap, and delete/destructive lifecycle actions sit after the second
    3rem gap. The settings action must stay in the grey group immediately
    before the destructive group.
    """
    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(org=org, user=user, is_active=True)

    response = client.get(
        reverse("workflows:workflow_detail", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    launch_url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})
    json_url = reverse("workflows:workflow_json", kwargs={"pk": workflow.pk})
    signals_url = reverse(
        "workflows:workflow_signal_mapping",
        kwargs={"pk": workflow.pk},
    )
    constants_url = reverse("workflows:workflow_constants", kwargs={"pk": workflow.pk})
    settings_url = reverse("workflows:workflow_update", kwargs={"pk": workflow.pk})
    delete_url = reverse("workflows:workflow_delete", kwargs={"pk": workflow.pk})
    launch_index = body.find(f'href="{launch_url}"')
    json_index = body.find(f'href="{json_url}"')
    signals_index = body.find(f'href="{signals_url}"')
    constants_index = body.find(f'href="{constants_url}"')
    settings_index = body.find(f'href="{settings_url}"')
    delete_index = body.find(f'hx-delete="{delete_url}"')
    assert -1 not in {
        launch_index,
        json_index,
        signals_index,
        constants_index,
        settings_index,
        delete_index,
    }
    assert (
        launch_index
        < json_index
        < signals_index
        < constants_index
        < settings_index
        < delete_index
    )
    assert 'title="Configure workflow-level constants (c.name)"' in body
    # The grey-actions and destructive-actions divs both carry the
    # ``d-flex flex-wrap gap-2 ms-5`` class set when a launch action
    # is present (the ms-5 conditionally adds a leftward gap so the
    # destructive cluster doesn't touch the launch button).
    #
    # We normalize whitespace before counting because djlint may
    # reformat the class attribute across multiple lines for
    # readability — the rendered HTML then has newlines + indentation
    # between the class tokens, so a literal substring match misses
    # the contiguous class string the layout actually produces. The
    # normalization collapses runs of whitespace to single spaces,
    # which is what the browser would do at render time anyway.
    body_normalized = re.sub(r"\s+", " ", body)
    expected_toolbar_gaps = 2
    assert body_normalized.count("d-flex flex-wrap gap-2 ms-5") >= expected_toolbar_gaps
    assert 'title="Workflow settings"' in body
    settings_anchor_start = body.rfind(
        '<a class="btn btn-light text-dark"',
        0,
        settings_index,
    )
    assert settings_anchor_start != -1


def test_workflow_detail_toolbar_places_archive_before_settings(client):
    """Keep the lifecycle/settings swap stable when run history adds Archive.

    This branch needs its own ordering guard because unused workflows show
    Delete instead of Archive and intentionally retain the existing layout.
    """
    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    ValidationRunFactory(workflow=workflow)

    response = client.get(
        reverse("workflows:workflow_detail", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    constants_url = reverse("workflows:workflow_constants", kwargs={"pk": workflow.pk})
    archive_url = reverse("workflows:workflow_archive", kwargs={"pk": workflow.pk})
    settings_url = reverse("workflows:workflow_update", kwargs={"pk": workflow.pk})
    constants_index = body.find(f'href="{constants_url}"')
    archive_index = body.find(f'action="{archive_url}"')
    settings_index = body.find(f'href="{settings_url}"')

    assert -1 not in {constants_index, archive_index, settings_index}
    assert constants_index < archive_index < settings_index
    actions_between = re.sub(r"\s+", " ", body[archive_index:settings_index])
    assert "d-flex flex-wrap gap-2 ms-5" in actions_between


def test_workflow_detail_toolbar_hides_manage_actions_for_executor(client):
    """Executor-only users can launch but cannot see manage/delete actions."""
    org = OrganizationFactory()
    author = UserFactory()
    executor = UserFactory()
    grant_role(author, org, RoleCode.OWNER)
    grant_role(executor, org, RoleCode.EXECUTOR)
    executor.set_current_org(org)
    workflow = WorkflowFactory(org=org, user=author, is_active=True)
    client.force_login(executor)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.get(
        reverse("workflows:workflow_detail", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    launch_url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})
    settings_url = reverse("workflows:workflow_update", kwargs={"pk": workflow.pk})
    delete_url = reverse("workflows:workflow_delete", kwargs={"pk": workflow.pk})
    assert f'href="{launch_url}"' in body
    assert f'href="{settings_url}"' not in body
    assert f'hx-delete="{delete_url}"' not in body
    assert "Create new workflow version" not in body


def test_workflow_detail_shows_version_history_panel_with_run_count(client):
    """The version-history panel must surface per-version run counts.

    Run count is one of the columns the policy doc (workflow-versioning-
    policy.md §"Viewing Earlier Workflow Versions") explicitly requires —
    an author deciding which old version to inspect needs to know which ones
    actually carry validation history. A panel that hides run counts forces
    the author to click through each version to find that out.

    This test pins three things:
      - the panel renders when more than one version exists;
      - the current version shows a "current" badge;
      - run counts surface for the version that has runs.
    """
    from validibot.submissions.tests.factories import SubmissionFactory

    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    slug = "building-energy-check"
    v1 = WorkflowFactory(org=org, user=user, slug=slug, version="1")
    v2 = WorkflowFactory(org=org, user=user, slug=slug, version="2")
    # Attach two runs to v1 so the panel has a non-zero run count to display.
    for _ in range(2):
        ValidationRunFactory(workflow=v1, submission=SubmissionFactory(workflow=v1))

    response = client.get(reverse("workflows:workflow_detail", kwargs={"pk": v2.pk}))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "workflow-version-history-card" in body, (
        "Version history panel should render when a family has 2+ versions"
    )
    assert "Version history" in body
    # The viewed version (v2) is the current row — its row should carry the
    # "current" badge.
    assert ">current<" in body
    # v1 has two attached runs; the panel must display that count somewhere.
    # We assert on the table body to avoid matching incidental "2" strings
    # elsewhere on the page.
    assert "workflow-version-history-table" in body


def test_workflow_detail_hides_version_history_panel_for_single_version(client):
    """A single-version family should not render an empty history panel.

    The panel exists to compare/switch between versions. With only one row
    there is nothing to compare, and showing an empty card wastes vertical
    space and signals more state than there is.
    """
    user = UserFactory()
    org = OrganizationFactory()
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(org=org, user=user, slug="single-family", version="1")

    response = client.get(
        reverse("workflows:workflow_detail", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "workflow-version-history-card" not in body, (
        "Version history panel should be hidden for single-version families"
    )


def test_workflow_update_can_clone_and_apply_locked_contract_change(client):
    """A blocked semantic edit can be applied to a new version in one action.

    Versioned workflows with runs should not silently mutate their historical
    contract, but the author still needs an efficient way to keep iterating.
    This verifies the two-step UX: the first submit explains that a new version
    is required, and the explicit clone-and-apply submit preserves the old row
    while applying the change to the clone.
    """
    from validibot.submissions.tests.factories import SubmissionFactory

    user = UserFactory()
    org = OrganizationFactory()
    project = ProjectFactory(org=org, is_default=True)
    _login_user_for_org(client, user, org)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        project=project,
        slug="contracted-workflow",
        version="1",
        history_policy=WorkflowHistoryPolicy.VERSIONED,
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
    )
    submission = SubmissionFactory(workflow=workflow)
    ValidationRunFactory(workflow=workflow, submission=submission)
    update_url = reverse("workflows:workflow_update", kwargs={"pk": workflow.pk})
    payload = {
        "name": workflow.name,
        "description": workflow.description,
        "slug": workflow.slug,
        "project": str(project.pk),
        "allowed_file_types": [SubmissionFileType.JSON],
        "input_schema_source_mode": "json_schema",
        "input_schema_source_text": "",
        "input_retention": DataRetention.DO_NOT_STORE,
        "output_retention": "STORE_30_DAYS",
        "success_message": workflow.success_message,
        "allow_submission_name": "on",
        "allow_submission_meta_data": "on",
        "allow_submission_short_description": "on",
        "version": workflow.version,
        "history_policy": WorkflowHistoryPolicy.VERSIONED,
        "is_active": "on",
    }

    blocked_response = client.post(update_url, data=payload)

    assert blocked_response.status_code == HTTPStatus.OK
    blocked_body = blocked_response.content.decode()
    assert "This edit needs a new workflow version." in blocked_body
    workflow.refresh_from_db()
    assert workflow.allowed_file_types == [
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
    ]

    clone_response = client.post(
        update_url,
        data={**payload, "clone_and_apply": "1"},
    )

    workflow.refresh_from_db()
    clone = Workflow.objects.get(slug=workflow.slug, version=2)
    assert clone_response.status_code == HTTPStatus.FOUND
    assert str(clone.pk) in clone_response.url
    assert workflow.allowed_file_types == [
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
    ]
    assert workflow.is_locked is True
    assert clone.allowed_file_types == [SubmissionFileType.JSON]
    assert clone.history_policy == WorkflowHistoryPolicy.VERSIONED


def test_workflow_clone_view_requires_workflow_edit_permission(client):
    """View-only users may inspect workflow history but cannot fork versions."""
    org = OrganizationFactory()
    author = UserFactory()
    viewer = UserFactory()
    grant_role(author, org, RoleCode.OWNER)
    grant_role(viewer, org, RoleCode.WORKFLOW_VIEWER)
    viewer.set_current_org(org)
    workflow = WorkflowFactory(org=org, user=author)
    client.force_login(viewer)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.post(
        reverse("workflows:workflow_clone", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert Workflow.objects.filter(slug=workflow.slug).count() == 1


def test_basic_workflow_api_flow_returns_failure_when_price_high(
    api_client,
    run_validation_tasks_inline,
):
    """Test that validation run API correctly reports failures for invalid data.

    Since the workflow API is read-only, we create the workflow using
    factories and then test the validation flow via the start endpoint.
    """
    user = UserFactory()
    org = OrganizationFactory()
    grant_role(user, org, RoleCode.OWNER)
    grant_role(user, org, RoleCode.EXECUTOR)
    api_client.force_authenticate(user=user)

    # Create workflow using factory since API is read-only
    project = ProjectFactory(org=org)  # project is required on Workflow now
    workflow = Workflow.objects.create(
        org=org,
        user=user,
        project=project,
        name="Price check",
        slug="price-check",
        version=1,
        is_active=True,
        allowed_file_types=[SubmissionFileType.JSON],
    )

    validator = ValidatorFactory(
        validation_type=ValidationType.BASIC,
        allow_custom_assertion_targets=True,
        org=org,
        is_system=False,
    )
    ruleset = Ruleset.objects.create(
        org=org,
        user=user,
        name="price-check-rules",
        ruleset_type=RulesetType.BASIC,
        version="1.0.0",
    )
    sync_configured_io_contract(validator=validator)
    step = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=10,
        name="Manual price gate",
        description="",
        notes="",
        config={},
    )
    ensure_step_input_bindings(step)
    message = "The price {{price}} is too expensive! Limit {{ value }}"
    RulesetAssertion.objects.create(
        ruleset=ruleset,
        order=10,
        assertion_type=AssertionType.BASIC,
        operator=AssertionOperator.LT,
        target_data_path="price",
        severity=Severity.ERROR,
        rhs={"value": 20},
        options={},
        message_template=message,
    )

    # Use the org-scoped API route.
    start_url = reverse(
        "api:org-workflows-runs",
        kwargs={"org_slug": org.slug, "pk": workflow.pk},
    )
    run_resp = api_client.post(start_url, data={"price": 25}, format="json")
    # 201 Created when execution completes, 202 Accepted when still processing
    assert run_resp.status_code == status.HTTP_201_CREATED
    body = run_resp.json()
    assert body["status"] == ValidationRunStatus.FAILED
    assert body["workflow"] == workflow.id
    assert body["workflow_slug"] == workflow.slug
    assert body["error"]
    assert body["steps"], body
    price_step = body["steps"][0]
    assert price_step["status"] == "FAILED"
    issues = price_step["issues"]
    assert issues
    issue = issues[0]
    assert issue["path"] == "price"
    assert issue["message"] == "The price 25 is too expensive! Limit 20"
    assert issue["severity"] == Severity.ERROR

    run = ValidationRun.objects.get(pk=body["id"])
    assert run.status == ValidationRunStatus.FAILED
