"""
Tests for the workflow launch UI views.

Covers the end-to-end launch flow: rendering the launch form, submitting
files for validation, polling run status, and cancelling in-progress runs.
The ``ValidationRunService.launch`` method is monkeypatched in most tests
so we can verify the view layer in isolation from the orchestration engine.
"""

from __future__ import annotations

import html
import json
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from lxml import html as lxml_html

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import CredentialActionType
from validibot.actions.models import ActionDefinition
from validibot.actions.registry import get_action_model
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Ruleset
from validibot.validations.models import ValidationRun
from validibot.validations.services.custom_validator_contracts import (
    sync_configured_io_contract,
)
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.services.validation_run import ValidationRunLaunchResults
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db

if TYPE_CHECKING:
    from validibot.submissions.models import Submission


def _force_login_for_workflow(client, workflow, *, user=None):
    user = user or workflow.user
    has_membership = user.memberships.filter(
        org=workflow.org,
        is_active=True,
    ).exists()
    if not has_membership:
        grant_role(user, workflow.org, RoleCode.WORKFLOW_VIEWER)
    user.set_current_org(workflow.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return user


def _fake_pro_modules(credential):
    """Return a minimal validibot_pro module tree for credential UI tests."""

    pro_module = ModuleType("validibot_pro")
    credentials_module = ModuleType("validibot_pro.credentials")
    models_module = ModuleType("validibot_pro.credentials.models")
    models_module.IssuedCredential = SimpleNamespace(
        objects=SimpleNamespace(
            filter=lambda **_kwargs: SimpleNamespace(first=lambda: credential),
        ),
    )
    return {
        "validibot_pro": pro_module,
        "validibot_pro.credentials": credentials_module,
        "validibot_pro.credentials.models": models_module,
    }


def test_launch_page_requires_authentication(client):
    workflow = WorkflowFactory()
    url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})

    response = client.get(url)

    assert response.status_code == HTTPStatus.FOUND
    assert "login" in response.url


def test_launch_page_renders_for_org_member(client):
    """Executors should get a viewport-bound launch editor with pinned actions."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.get(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
    )

    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "Launch Validation" in body
    assert "workflow-launch-status-area" not in body
    assert "app-viewport-locked" in body
    assert 'class="container-fluid editor-shell" id="workflow-launch"' in body
    assert 'class="card app-card editor-card submit-content-card"' in body
    assert 'class="card-body editor-card__scroll"' in body


def test_launch_page_groups_optional_fields_under_submission_details(client):
    """The launch UI should use a clear umbrella label for optional details."""
    workflow = WorkflowFactory(allow_submission_meta_data=True)
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.get(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
    )

    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "Submission details" in body
    assert "Submission metadata (JSON)" in body
    assert ">Extra data<" not in body


def test_launch_page_disables_form_without_steps(client):
    """A non-editor launch gate should retain natural document scrolling."""
    workflow = WorkflowFactory()
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.get(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
    )

    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "This workflow has no steps yet." in body
    assert "Start Validation" not in body
    assert "app-viewport-locked" not in body


def test_launch_post_creates_run_and_redirects(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return ValidationRunLaunchResults(
            validation_run=run,
            data={"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=HTTPStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        "validibot.workflows.views.launch.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    body = response.content.decode()
    assert "workflow-run-detail-panel" in body
    assert ValidationRun.objects.filter(workflow=workflow).count() == 1
    session = client.session
    assert session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] == "paste"


def test_launch_start_records_upload_preference(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return ValidationRunLaunchResults(
            validation_run=run,
            data={"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=HTTPStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        "validibot.workflows.views.launch.ValidationRunService.launch",
        fake_launch,
    )

    upload = SimpleUploadedFile("test.json", b"{}", content_type="application/json")

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "attachment": upload,
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    session = client.session
    assert session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] == "upload"


def test_launch_upload_flow_accepts_file_and_creates_submission(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    asset_path = Path("tests/assets/json/example_product.json")
    payload_bytes = asset_path.read_bytes()
    uploaded = SimpleUploadedFile(
        asset_path.name,
        payload_bytes,
        content_type="application/json",
    )
    captured = {}

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
        captured["submission"] = submission
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return ValidationRunLaunchResults(
            validation_run=run,
            data={"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=HTTPStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        "validibot.workflows.views.launch.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "attachment": uploaded,
            "filename": asset_path.name,
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    assert "submission" in captured
    submission: Submission = captured["submission"]
    submission.refresh_from_db()
    assert submission.original_filename == asset_path.name
    assert submission.file_type == SubmissionFileType.JSON
    assert submission.input_file
    assert submission.input_file.name
    assert '"name"' in submission.get_content()
    assert ValidationRun.objects.filter(submission=submission).count() == 1
    assert client.session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] == "upload"


def test_launch_upload_detects_turtle_file_despite_default_json_choice(
    client,
    monkeypatch,
):
    """A .ttl upload should stay TEXT/Turtle even when JSON is the first choice.

    SHACL workflows often allow JSON-LD and Turtle. If the launch form defaults
    to JSON but the uploaded file is Turtle, filename detection must win so the
    SHACL engine does not try to parse Turtle as JSON-LD.
    """
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
    )
    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        supports_assertions=True,
    )
    WorkflowStepFactory(workflow=workflow, validator=validator)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    asset_path = Path("tests/assets/shacl/223p_example_building.ttl")
    uploaded = SimpleUploadedFile(
        asset_path.name,
        asset_path.read_bytes(),
        content_type="text/turtle",
    )
    captured = {}

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
        captured["submission"] = submission
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return ValidationRunLaunchResults(
            validation_run=run,
            data={"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=HTTPStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        "validibot.workflows.views.launch.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "attachment": uploaded,
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    submission: Submission = captured["submission"]
    submission.refresh_from_db()
    assert submission.file_type == SubmissionFileType.TEXT
    assert submission.original_filename == asset_path.name
    assert submission.input_file.name.endswith(".ttl")


def test_launch_upload_rejects_turtle_when_workflow_allows_only_json(
    client,
    monkeypatch,
):
    """A JSON-only workflow must not accept Turtle just because SHACL can parse it.

    Filename detection happens before contract enforcement. This test pins the
    second half: after `.ttl` is detected as TEXT, the launch form must reject
    it when the workflow contract only allows JSON.
    """
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.JSON])
    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        supports_assertions=True,
    )
    WorkflowStepFactory(workflow=workflow, validator=validator)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    asset_path = Path("tests/assets/shacl/223p_example_building.ttl")
    uploaded = SimpleUploadedFile(
        asset_path.name,
        asset_path.read_bytes(),
        content_type="text/turtle",
    )

    def fail_if_launched(*_args, **_kwargs):
        raise AssertionError("JSON-only workflow should reject Turtle before launch")

    monkeypatch.setattr(
        "validibot.workflows.views.launch.ValidationRunService.launch",
        fail_if_launched,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "attachment": uploaded,
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "File extension &#x27;.ttl&#x27; is not allowed." in body
    assert ValidationRun.objects.filter(workflow=workflow).count() == 0


def test_launch_inline_flow_accepts_json_payload(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    payload = Path("tests/assets/json/example_product.json").read_text()
    captured = {}

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
        captured["submission"] = submission
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return ValidationRunLaunchResults(
            validation_run=run,
            data={"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=HTTPStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        "validibot.workflows.views.launch.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": payload,
            "filename": "inline.json",
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    assert "submission" in captured
    submission: Submission = captured["submission"]
    submission.refresh_from_db()
    assert not submission.input_file.name
    assert submission.file_type == SubmissionFileType.JSON
    assert '"name"' in submission.get_content()
    assert ValidationRun.objects.filter(submission=submission).count() == 1
    assert client.session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] == "paste"


def test_launch_post_invalid_form_rerenders_page(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={"file_type": SubmissionFileType.JSON},
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Launch Validation" in body


def test_launch_start_requires_executor_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    viewer = UserFactory()
    _force_login_for_workflow(client, workflow, user=viewer)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert (
        "You do not have permission to run this workflow." in response.content.decode()
    )


def test_launch_org_policy_denial_surfaces_real_reason(client):
    """A metering/org-policy denial must show the policy's own reason.

    This is the regression test for the misleading-message bug: when a cloud
    org policy blocks a launch (billing not set up, out of credits, quota,
    rate limit), the user IS permitted to run the workflow, so the generic
    "You do not have permission" string was actively wrong and hid the fix
    ("finish onboarding", "add credits"). The launch service now raises
    OrgPolicyDeniedError carrying the policy reason, and the view must render that
    reason verbatim with a 403 — not the permission boilerplate.

    We register a denying policy through the community policy registry (the
    same hook cloud's metering uses) so the test needs no cloud install.
    """
    from validibot.core.policies import register_org_policy
    from validibot.core.policies import reset_org_policies

    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    # Avoid HTML-special characters (apostrophes, angle brackets) in the
    # reason so the assertion compares against the literal string; Django
    # auto-escapes those in the rendered template, which would otherwise mask
    # a correct fix behind an escaping mismatch.
    reason = "Finish billing onboarding before running validations"

    def deny_for_billing(org, action, **context):
        return (False, reason)

    register_org_policy(deny_for_billing)
    try:
        response = client.post(
            reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
            },
        )
    finally:
        # Policies are process-global; never leak into other tests.
        reset_org_policies()

    assert response.status_code == HTTPStatus.FORBIDDEN
    body = response.content.decode()
    # The real, actionable reason is shown ...
    assert reason in body
    # ... and the misleading generic permission message is NOT.
    assert "You do not have permission to run this workflow." not in body


def test_launch_toggle_sections_follow_session_preference(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})

    response = client.get(url)
    document = lxml_html.fromstring(response.content.decode())
    upload_section = document.xpath("//*[@data-upload-section]")[0]
    paste_section = document.xpath("//*[@data-paste-section]")[0]
    upload_button = document.xpath('//*[@data-content-mode="upload"]')[0]
    paste_button = document.xpath('//*[@data-content-mode="paste"]')[0]

    assert "d-none" not in (upload_section.classes or set())
    assert "d-none" in (paste_section.classes or set())
    assert "active" in (upload_button.classes or set())
    assert "active" not in (paste_button.classes or set())

    session = client.session
    session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] = "paste"
    session.save()

    response = client.get(url)
    document = lxml_html.fromstring(response.content.decode())
    upload_section = document.xpath("//*[@data-upload-section]")[0]
    paste_section = document.xpath("//*[@data-paste-section]")[0]
    upload_button = document.xpath('//*[@data-content-mode="upload"]')[0]
    paste_button = document.xpath('//*[@data-content-mode="paste"]')[0]

    assert "d-none" in (upload_section.classes or set())
    assert "d-none" not in (paste_section.classes or set())
    assert "active" not in (upload_button.classes or set())
    assert "active" in (paste_button.classes or set())


def test_browse_files_button_targets_attachment_input(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})

    response = client.get(url)
    document = lxml_html.fromstring(response.content.decode())
    browse_label = document.xpath("//*[@data-dropzone-browse]")[0]
    attachment_input = document.xpath('//input[@name="attachment"]')[0]

    assert browse_label.tag == "label"
    assert browse_label.get("for") == attachment_input.get("id")
    assert "btn" in (browse_label.classes or set())


def test_cancel_run_updates_status(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=workflow.user,
        status=ValidationRunStatus.RUNNING,
    )

    response = client.post(
        reverse(
            "workflows:workflow_launch_cancel",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
        HTTP_HX_REQUEST="true",
    )

    run.refresh_from_db()
    assert response.status_code == HTTPStatus.OK
    assert run.status == ValidationRunStatus.CANCELED
    hx_trigger = response.headers.get("HX-Trigger")
    assert hx_trigger
    assert "Workflow validation canceled" in hx_trigger


def test_cancel_run_reports_completed_before_cancel(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )

    response = client.post(
        reverse(
            "workflows:workflow_launch_cancel",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    hx_trigger = response.headers.get("HX-Trigger")
    assert hx_trigger
    assert "Process completed before it could be cancelled" in hx_trigger


def test_cancel_run_requires_executor_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    viewer = UserFactory()
    _force_login_for_workflow(client, workflow, user=viewer)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=workflow.user,
        status=ValidationRunStatus.RUNNING,
    )

    response = client.post(
        reverse(
            "workflows:workflow_launch_cancel",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_executor_cannot_view_another_users_launch_run(client):
    """Launch run details should hide another user's submission and findings."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    other_user = UserFactory()
    grant_role(other_user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=other_user,
        status=ValidationRunStatus.SUCCEEDED,
    )

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_executor_cannot_cancel_another_users_launch_run(client):
    """Workflow launch permission alone must not cancel someone else's run."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    other_user = UserFactory()
    grant_role(other_user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=other_user,
        status=ValidationRunStatus.RUNNING,
    )

    response = client.post(
        reverse(
            "workflows:workflow_launch_cancel",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
        HTTP_HX_REQUEST="true",
    )

    run.refresh_from_db()
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert run.status == ValidationRunStatus.RUNNING


def test_admin_can_cancel_another_users_launch_run(client):
    """Org admins need break-glass cancellation for stuck customer runs."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    admin = _force_login_for_workflow(client, workflow)
    grant_role(admin, workflow.org, RoleCode.ADMIN)
    other_user = UserFactory()
    grant_role(other_user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=other_user,
        status=ValidationRunStatus.RUNNING,
    )

    response = client.post(
        reverse(
            "workflows:workflow_launch_cancel",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
        HTTP_HX_REQUEST="true",
    )

    run.refresh_from_db()
    assert response.status_code == HTTPStatus.OK
    assert run.status == ValidationRunStatus.CANCELED


def test_run_detail_page_shows_status_area(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.RUNNING,
    )

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "workflow-run-detail-panel" in body
    assert "Cancel workflow" in body


def test_run_detail_page_shows_cancelled_actions(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.CANCELED,
    )

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Back to launch" in body
    assert "View previous runs" in body


def test_run_detail_layout_toggle_swaps_the_panel_in_place(client):
    """The report layout toggle must swap the HTMx panel, not navigate.

    Regression test: the toggle used to render as plain ``?layout=`` links,
    which navigated back to the launch page and lost the report the user
    was looking at. Inside the run status card the buttons must hx-get the
    panel-refresh URL (the layout choice rides in ``hx-vals``) and swap
    ``#workflow-run-detail-panel`` in place — the plain href remains only
    as a no-JS fallback.
    """
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )

    detail_url = reverse(
        "workflows:workflow_run_detail",
        kwargs={"pk": workflow.pk, "run_id": run.pk},
    )
    response = client.get(detail_url)

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    # Both layout buttons hx-get the panel-refresh URL with their layout.
    assert f'hx-get="{detail_url}' in body
    assert 'hx-vals=\'{"layout": "stacked"}\'' in body
    assert 'hx-vals=\'{"layout": "classic"}\'' in body
    assert 'hx-target="#workflow-run-detail-panel"' in body


def test_run_detail_page_shows_completion_actions(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Launch again" in body
    assert "View full run" in body


def test_run_detail_page_shows_retained_submission_file_view(client):
    """Launch run results should offer the data modal when retention allows it."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    upload = SimpleUploadedFile(
        "launch-visible.json",
        b'{"launch_visible": true}',
        content_type="application/json",
    )
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        submission__user=user,
        submission__retention_policy=SubmissionRetention.STORE_30_DAYS,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )
    run.submission.set_content(
        uploaded_file=upload,
        filename="launch-visible.json",
        file_type=SubmissionFileType.JSON,
    )
    run.submission.save()

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Data file" in body
    assert "launch-visible.json" in body
    assert 'data-bs-target="#submissionContentModal"' in body
    assert 'id="submissionContentModal"' in body
    assert response.context["submission_content"] == '{"launch_visible": true}'
    assert response.context["submission_content_can_be_viewed"] is True


def test_run_detail_page_hides_do_not_store_submission_content(client):
    """Launch results must hide no-store bytes and payload-derived filenames."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    upload = SimpleUploadedFile(
        "launch-private.json",
        b'{"launch_private": true}',
        content_type="application/json",
    )
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        submission__user=user,
        submission__retention_policy=SubmissionRetention.DO_NOT_STORE,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )
    run.submission.set_content(
        uploaded_file=upload,
        filename="launch-private.json",
        file_type=SubmissionFileType.JSON,
    )
    run.submission.save()

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Data file" not in body
    assert "launch-private.json" not in body
    assert (
        "Submission content has been purged per retention policy and cannot be viewed."
        in body
    )
    assert 'data-bs-target="#submissionContentModal"' not in body
    assert 'id="submissionContentModal"' not in body
    assert response.context["submission_content"] == ""
    assert response.context["submission_content_can_be_viewed"] is False


def test_run_detail_page_hides_expired_submission_content(client):
    """Elapsed input retention must hide bytes and filenames before deletion."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    upload = SimpleUploadedFile(
        "launch-expired.json",
        b'{"launch_expired": true}',
        content_type="application/json",
    )
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        submission__user=user,
        submission__retention_policy=SubmissionRetention.STORE_1_DAY,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )
    run.submission.set_content(
        uploaded_file=upload,
        filename="launch-expired.json",
        file_type=SubmissionFileType.JSON,
    )
    run.submission.save()
    run.submission.expires_at = timezone.now() - timedelta(minutes=1)
    run.submission.save(update_fields=["expires_at"])
    assert run.submission.input_file.storage.exists(run.submission.input_file.name)

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Data file" not in body
    assert "launch-expired.json" not in body
    assert (
        "Submission content has been purged per retention policy and cannot be viewed."
        in body
    )
    assert 'data-bs-target="#submissionContentModal"' not in body
    assert 'id="submissionContentModal"' not in body
    assert "launch_expired" not in body
    assert response.context["submission_content"] == ""
    assert response.context["submission_content_can_be_viewed"] is False


def test_run_detail_page_shows_signed_credential_card(client, pro_installed):
    """Completed workflow status pages should render an issued credential card."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )
    credential = SimpleNamespace(
        media_type="application/vc+jwt",
        created=run.created,
        kid="kid-123456",
        payload_json={
            "credentialSubject": {
                "resourceLabel": "Product 1",
            },
        },
    )

    # The ``pro_installed`` fixture (in conftest) flips
    # apps.is_installed("validibot_pro") to True so the production
    # gate falls through to the fake-module path under sys.modules.
    with patch.dict("sys.modules", _fake_pro_modules(credential)):
        response = client.get(
            reverse(
                "workflows:workflow_run_detail",
                kwargs={"pk": workflow.pk, "run_id": run.pk},
            ),
        )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Signed Credential" in body
    assert "Product 1" in body
    assert "Download Credential" in body


def test_launch_status_partial_shows_signed_credential_card(client, pro_installed):
    """The status fragment should include the credential card for completed runs."""
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        user=user,
        status=ValidationRunStatus.SUCCEEDED,
    )
    credential = SimpleNamespace(
        media_type="application/vc+jwt",
        created=run.created,
        kid="kid-123456",
        payload_json={
            "credentialSubject": {
                "resourceLabel": "Product 1",
            },
        },
    )

    with patch.dict("sys.modules", _fake_pro_modules(credential)):
        response = client.get(
            reverse(
                "workflows:workflow_launch_status",
                kwargs={"pk": workflow.pk, "run_id": run.pk},
            ),
        )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Signed Credential" in body
    assert "Product 1" in body


def test_latest_run_view_loads_most_recent_run(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    superuser = UserFactory(is_superuser=True, is_staff=True)
    grant_role(superuser, workflow.org, RoleCode.ADMIN)
    superuser.set_current_org(workflow.org)
    client.force_login(superuser)
    ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        status=ValidationRunStatus.SUCCEEDED,
    )

    response = client.get(
        reverse("workflows:workflow_last_run", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "workflow-run-detail-panel" in body
    assert "Launch again" in body


def test_latest_run_view_redirects_when_no_runs_exist(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    superuser = UserFactory(is_superuser=True, is_staff=True)
    grant_role(superuser, workflow.org, RoleCode.ADMIN)
    superuser.set_current_org(workflow.org)
    client.force_login(superuser)

    response = client.get(
        reverse("workflows:workflow_last_run", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.FOUND
    assert (
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}) in response.url
    )


def test_public_info_view_accessible_when_enabled(client):
    workflow = WorkflowFactory(make_info_page_public=True)
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="public-json",
    )
    schema_text = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"sku": {"type": "string"}},
        },
    )
    ruleset = Ruleset.objects.create(
        org=workflow.org,
        user=workflow.user,
        ruleset_type=RulesetType.JSON_SCHEMA,
        name="Public schema",
    )
    ruleset.metadata = {
        "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
    }
    ruleset.rules_text = schema_text
    ruleset.save(update_fields=["metadata", "rules_text"])
    WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        description="Validates base product payload.",
        display_schema=True,
        ruleset=ruleset,
        config={
            "schema_source": "text",
            "schema_text_preview": schema_text[:100],
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            "schema_type_label": str(JSONSchemaVersion.DRAFT_2020_12.label),
        },
    )

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert workflow.name in body
    assert "All Workflows" in body
    assert html.escape(f"Workflow '{workflow.name}'") in body

    # Validation we can find the id "workflow-public-view" of the div that holds info
    assert 'id="workflow-public-view"' in body


def test_public_info_form_updates_visibility(client):
    workflow = WorkflowFactory(make_info_page_public=False)
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.AUTHOR)

    response = client.post(
        reverse("workflows:workflow_public_info_edit", kwargs={"pk": workflow.pk}),
        data={
            "title": "Public doc",
            "content_md": "## Overview\nDetails here.",
            "make_info_page_public": "on",
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    workflow.refresh_from_db()
    assert workflow.make_info_page_public is True


def test_public_visibility_toggle_updates_card(client):
    workflow = WorkflowFactory(make_info_page_public=False)
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.AUTHOR)

    response = client.post(
        reverse("workflows:workflow_public_visibility", kwargs={"pk": workflow.pk}),
        data={"make_info_page_public": "true"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    workflow.refresh_from_db()
    assert workflow.make_info_page_public is True
    assert "Visible" in response.content.decode()


def test_launch_start_allows_admitted_file_type_even_when_step_will_reject_it(client):
    """Workflow admission stays permissive and runtime reports the mismatch."""
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
    )
    validator = ValidatorFactory(validation_type=ValidationType.JSON_SCHEMA)
    sync_configured_io_contract(validator=validator)
    step = WorkflowStepFactory(workflow=workflow, validator=validator)
    ensure_step_input_bindings(step)
    user = workflow.user
    user.set_current_org(workflow.org)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    client.force_login(user)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.XML,
            "payload": "<data/>",
        },
    )

    assert response.status_code == HTTPStatus.CREATED
    run = ValidationRun.objects.get(workflow=workflow)
    assert run.status == ValidationRunStatus.FAILED
    finding = run.findings.get(code="input_file_type_incompatible")
    assert finding.message


def test_public_info_view_hides_schema_when_not_shared(client):
    workflow = WorkflowFactory(make_info_page_public=True)
    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
        slug="public-xml",
    )
    xml_schema = """<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>\n
    <xs:element name='item' type='xs:string'/>\n</xs:schema>"""
    ruleset = Ruleset.objects.create(
        org=workflow.org,
        user=workflow.user,
        ruleset_type=RulesetType.XML_SCHEMA,
        name="Private schema",
    )
    ruleset.metadata = {
        "schema_type": XMLSchemaType.XSD.value,
    }
    ruleset.rules_text = xml_schema
    ruleset.save(update_fields=["metadata", "rules_text"])
    WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        description="Validates XML payload.",
        display_schema=False,
        ruleset=ruleset,
        config={
            "schema_source": "text",
            "schema_text_preview": xml_schema[:100],
            "schema_type": XMLSchemaType.XSD.value,
            "schema_type_label": str(XMLSchemaType.XSD.label),
        },
    )

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Show schema" not in body
    assert "Schema shared" not in body


def test_public_info_view_returns_404_when_disabled(client):
    """Private workflows do not expose their public info page."""
    workflow = WorkflowFactory(make_info_page_public=False)

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_public_info_view_renders_signed_credential_summary(client):
    """Public workflow pages only show credential summaries for loaded plugins."""
    workflow = WorkflowFactory(make_info_page_public=True)
    definition = ActionDefinition.objects.create(
        slug="signed-credential",
        name="Signed credential",
        description="Issue a signed credential for successful validations.",
        icon="bi-award",
        action_category=ActionCategoryType.CREDENTIAL,
        type=CredentialActionType.SIGNED_CREDENTIAL,
    )
    action_model = get_action_model(CredentialActionType.SIGNED_CREDENTIAL)
    action = action_model.objects.create(
        definition=definition,
        name="Issue credential",
        description="Issue a signed credential.",
    )
    WorkflowStepFactory(
        workflow=workflow,
        validator=None,
        action=action,
        name="Issue credential",
        description="Issue a signed credential.",
        config={},
    )

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Credential template" not in body


# ── Schema-driven launch integration tests ───────────────────────────
#
# These tests exercise the end-to-end view paths introduced by ADR
# 2026-03-19: form-mode submissions, paste-mode schema enforcement,
# upload-mode schema enforcement, and the preflight validation endpoint.


SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer", "minimum": 1},
    },
    "required": ["name", "count"],
}


def _schema_workflow_with_step(client, *, schema=None):
    """Create a JSON-only workflow with an input_schema, a step, and an
    authenticated executor.  Returns (workflow, user).
    """
    wf = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.JSON],
        input_schema=schema or SIMPLE_SCHEMA,
    )
    WorkflowStepFactory(workflow=wf)
    user = _force_login_for_workflow(client, wf)
    grant_role(user, wf.org, RoleCode.EXECUTOR)
    return wf, user


def test_launch_page_renders_form_mode_for_schema_workflow(client):
    """When a workflow has an input_schema, the launch page should render
    the structured form mode with a 'Fill form' button and the dynamic
    input form fields.
    """
    wf, _user = _schema_workflow_with_step(client)

    response = client.get(
        reverse("workflows:workflow_launch", kwargs={"pk": wf.pk}),
    )

    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    import re

    assert 'data-content-mode="form"' in body
    # Template whitespace: data-default-mode may span multiple lines
    assert re.search(r'data-default-mode="\s*form\s*"', body)
    assert 'name="name"' in body  # dynamic form field
    assert 'name="count"' in body  # dynamic form field


def test_launch_form_mode_creates_run(client, monkeypatch):
    """Submitting via input_mode=form should serialize the form data to
    JSON, validate it against the schema, and create a submission + run.
    """
    wf, _user = _schema_workflow_with_step(client)

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return ValidationRunLaunchResults(
            validation_run=run,
            data={"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=HTTPStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        "validibot.workflows.views.launch.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": wf.pk}),
        data={
            "input_mode": "form",
            "name": "Alice",
            "count": "5",
            "file_type": SubmissionFileType.JSON,
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    assert ValidationRun.objects.filter(workflow=wf).count() == 1
    submission: Submission = ValidationRun.objects.get(workflow=wf).submission
    content = json.loads(submission.get_content())
    assert content["name"] == "Alice"
    expected_count = 5
    assert content["count"] == expected_count


def test_launch_form_mode_invalid_input_rerenders(client):
    """Submitting form mode with missing required fields should re-render
    the launch page with validation errors — not 500 or redirect.
    """
    wf, _user = _schema_workflow_with_step(client)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": wf.pk}),
        data={
            "input_mode": "form",
            "file_type": SubmissionFileType.JSON,
            # name and count are missing
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "This field is required" in body


def test_launch_paste_mode_rejects_invalid_json_against_schema(client):
    """Pasting JSON that doesn't match the workflow's input_schema should
    show validation errors on the launch page — the schema contract must
    be enforced for paste mode, not just form mode.
    """
    wf, _user = _schema_workflow_with_step(client)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": wf.pk}),
        data={
            "input_mode": "paste",
            "file_type": SubmissionFileType.JSON,
            "payload": '{"name": "Alice"}',  # missing required 'count'
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "count" in body.lower()


def test_launch_paste_mode_rejects_malformed_json(client):
    """Pasting text that is not valid JSON should produce a clear
    validation error — not silently pass through to the launch pipeline.
    """
    wf, _user = _schema_workflow_with_step(client)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": wf.pk}),
        data={
            "input_mode": "paste",
            "file_type": SubmissionFileType.JSON,
            "payload": "not-json-at-all",
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Invalid JSON" in body


def test_launch_upload_mode_rejects_invalid_json_against_schema(client):
    """Uploading a JSON file that doesn't match the input_schema should
    be rejected — the schema contract must be enforced for uploads too.
    """
    wf, _user = _schema_workflow_with_step(client)
    upload = SimpleUploadedFile(
        "bad.json",
        b'{"wrong_field": 123}',
        content_type="application/json",
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": wf.pk}),
        data={
            "input_mode": "upload",
            "file_type": SubmissionFileType.JSON,
            "attachment": upload,
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    # Should mention the missing required fields
    assert "name" in body.lower()
    assert "count" in body.lower()


def test_validate_input_endpoint_returns_success(client):
    """The preflight validation endpoint should return a success status
    when the input conforms to the schema.
    """
    wf, _user = _schema_workflow_with_step(client)

    response = client.post(
        reverse(
            "workflows:workflow_launch_validate_input",
            kwargs={"pk": wf.pk},
        ),
        data={
            "input_mode": "form",
            "name": "Alice",
            "count": "5",
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "validation_success" not in body or "alert-danger" not in body


def test_validate_input_endpoint_returns_errors(client):
    """The preflight validation endpoint should return validation errors
    when required fields are missing.
    """
    wf, _user = _schema_workflow_with_step(client)

    response = client.post(
        reverse(
            "workflows:workflow_launch_validate_input",
            kwargs={"pk": wf.pk},
        ),
        data={
            "input_mode": "form",
            # name and count missing
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "required" in body.lower() or "This field is required" in body


def test_validate_input_endpoint_paste_mode_malformed_json(client):
    """The preflight endpoint should return a clear error when paste-mode
    input is not valid JSON.
    """
    wf, _user = _schema_workflow_with_step(client)

    response = client.post(
        reverse(
            "workflows:workflow_launch_validate_input",
            kwargs={"pk": wf.pk},
        ),
        data={
            "input_mode": "paste",
            "payload": "not json",
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Invalid JSON" in body


def test_validate_input_endpoint_paste_mode_non_object_json(client):
    """The preflight endpoint should reject JSON that is valid but not
    an object (e.g. an array or primitive).
    """
    wf, _user = _schema_workflow_with_step(client)

    response = client.post(
        reverse(
            "workflows:workflow_launch_validate_input",
            kwargs={"pk": wf.pk},
        ),
        data={
            "input_mode": "paste",
            "payload": "[1, 2, 3]",
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "object" in body.lower()


def test_validate_input_endpoint_404_without_schema(client):
    """The preflight endpoint should return 404 for workflows that do
    not have an input_schema — there is nothing to validate against.
    """
    wf = WorkflowFactory()
    WorkflowStepFactory(workflow=wf)
    user = _force_login_for_workflow(client, wf)
    grant_role(user, wf.org, RoleCode.EXECUTOR)

    response = client.post(
        reverse(
            "workflows:workflow_launch_validate_input",
            kwargs={"pk": wf.pk},
        ),
        data={"input_mode": "form"},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
