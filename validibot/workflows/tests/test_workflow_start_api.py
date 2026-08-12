"""
Tests for the workflow start REST API endpoint.

Covers the ``POST /api/v1/workflows/<uuid>/start/`` endpoint, which accepts
raw file bodies or multipart uploads and creates validation runs.  Most tests
stub ``ValidationRunService`` so we verify request parsing, authentication,
error codes, and response shaping without running actual validators.

Also covers run-source attribution: each launch route derives
``ValidationRun.source`` from its own trusted service boundary. The standard
REST API route (``/api/v1/workflows/<uuid>/start/``) records ``API``. The
embedded MCP application service and Cloud x402 service have their own tests
for ``MCP`` and ``X402_AGENT`` respectively. The previous
``X-Validibot-Source`` header was caller-controlled and was removed because
source attribution must never come from an attacker-controllable header.
"""

import contextlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

import validibot.workflows.views.launch as views_mod
import validibot.workflows.views_launch_helpers as launch_helpers_mod
from validibot.core.models import SiteSettings
from validibot.events.constants import AppEventType
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.tracking.models import TrackingEvent
from validibot.tracking.services import TrackingEventService
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunLaunchResults
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import WorkflowStartErrorCode
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

try:
    from validibot.workflows.tests.factories import WorkflowFactory
    from validibot.workflows.tests.factories import WorkflowStepFactory
except Exception:
    WorkflowFactory = None
    WorkflowStepFactory = None


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def org(db):
    return OrganizationFactory()


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def workflow(db, org, user):
    """
    Create a test workflow with JSON, XML, and TEXT file types.

    Step-level input compatibility is intentionally not part of admission;
    these tests exercise only the workflow's allowed primary file types.
    """
    allowed_types = [
        SubmissionFileType.JSON,
        SubmissionFileType.XML,
        SubmissionFileType.TEXT,
    ]
    if WorkflowFactory:
        wf = WorkflowFactory(
            org=org,
            user=user,
            allowed_file_types=allowed_types,
        )
    else:
        wf = Workflow.objects.create(
            org=org,
            user=user,
            name=f"WF {uuid4().hex}",
            allowed_file_types=allowed_types,
        )
    validator = ValidatorFactory(validation_type=ValidationType.BASIC)
    if WorkflowStepFactory:
        WorkflowStepFactory(workflow=wf, validator=validator)
    else:
        WorkflowStep.objects.create(workflow=wf, order=10, validator=validator)
    with contextlib.suppress(ValueError):
        user.set_current_org(org)
    return wf


@pytest.fixture
def workflow_without_steps(db, org, user):
    if WorkflowFactory:
        return WorkflowFactory(
            org=org,
            user=user,
            allowed_file_types=[SubmissionFileType.JSON],
        )
    wf = Workflow.objects.create(
        org=org,
        user=user,
        name=f"WF-no-steps-{uuid4().hex}",
        allowed_file_types=[SubmissionFileType.JSON],
    )
    with contextlib.suppress(ValueError):
        user.set_current_org(org)
    return wf


def start_url(workflow) -> str:
    """Return the org-scoped API URL for starting a workflow run."""
    return f"/api/v1/orgs/{workflow.org.slug}/workflows/{workflow.pk}/runs/"


@pytest.fixture(autouse=True)
def reset_site_settings(db):
    SiteSettings.objects.update_or_create(
        slug=SiteSettings.DEFAULT_SLUG,
        defaults={
            "metadata_key_value_only": False,
            "metadata_max_bytes": 4096,
            "metadata_max_depth": 8,
            "data": {},
        },
    )
    yield
    SiteSettings.objects.update_or_create(
        slug=SiteSettings.DEFAULT_SLUG,
        defaults={
            "metadata_key_value_only": False,
            "metadata_max_bytes": 4096,
            "metadata_max_depth": 8,
            "data": {},
        },
    )


@pytest.fixture(autouse=True)
def mock_validation_service_success(monkeypatch):
    """
    Default: stub the ValidationRunService used by the view so tests focus on
    parsing/routing. Creates a real ValidationRun via factory (preferred) or ORM.
    Returns 201 with minimal payload that tests assert on.
    """

    def make_run(*, org, workflow, submission, status, extra=None):
        return ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=getattr(submission, "project", None),
            status=status,
            # Mirror the real ValidationRunService.launch, which applies
            # ``**(extra or {})`` to the create — so run-level fields the
            # launch path forwards (e.g. short_description) are reflected here
            # and tests that assert on them stay faithful to production.
            **(extra or {}),
        )

    def launch_side_effect(*_, **kwargs):
        request = kwargs.get("request")
        actor = getattr(request, "user", None)
        if not kwargs["workflow"].can_execute(user=actor):
            raise PermissionError("User lacks executor role")
        run = make_run(
            org=kwargs["org"],
            workflow=kwargs["workflow"],
            submission=kwargs["submission"],
            status=ValidationRunStatus.SUCCEEDED,
            extra=kwargs.get("extra"),
        )
        tracking_service = TrackingEventService()
        tracking_service.log_validation_run_created(
            run=run,
            user=actor,
            submission_id=run.submission_id,
        )
        tracking_service.log_validation_run_started(
            run=run,
            user=actor,
            extra_data={"status": ValidationRunStatus.RUNNING},
        )
        data = {
            "id": run.id,
            "workflow": run.workflow_id,
            "submission": run.submission_id,
            "status": run.status,
        }
        return ValidationRunLaunchResults(
            validation_run=run,
            data=data,
            status=status.HTTP_201_CREATED,
        )

    fake_service = SimpleNamespace(launch=launch_side_effect)
    # Patch where the view LOOKS UP the class
    monkeypatch.setattr(
        views_mod,
        "ValidationRunService",
        lambda: fake_service,
        raising=True,
    )
    monkeypatch.setattr(
        launch_helpers_mod,
        "ValidationRunService",
        lambda: fake_service,
        raising=True,
    )
    return fake_service


@pytest.mark.django_db
class TestWorkflowStartAPI:
    def test_start_org_policy_denial_returns_403_with_reason_and_code(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        monkeypatch,
    ):
        """An org-policy denial returns 403 with the policy's reason and code.

        Distinct from a true permission failure, which returns 404 to hide the
        workflow's existence. Here the caller IS permitted (so 404 would be
        wrong and confusing) but an org policy — billing not set up, out of
        credits, quota, rate limit — blocked the launch. The API must surface
        the policy's own actionable reason and the machine-readable
        ORG_POLICY_DENIED code so clients can react, not the generic permission
        message.

        We make the (already-stubbed) service raise OrgPolicyDeniedError to exercise
        the helper's translation layer, which is what this fix changed.
        """
        from validibot.validations.exceptions import OrgPolicyDeniedError

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        reason = "Finish billing onboarding before running validations"

        def deny_launch(*_, **__):
            raise OrgPolicyDeniedError(reason)

        # Override the autouse success stub for this test only.
        monkeypatch.setattr(
            launch_helpers_mod,
            "ValidationRunService",
            lambda: SimpleNamespace(launch=deny_launch),
            raising=True,
        )

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"hello": "world"}),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.data
        body = resp.json()
        assert body["detail"] == reason
        assert body["code"] == WorkflowStartErrorCode.ORG_POLICY_DENIED.value

    def test_start_with_raw_body_json_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        """
        Mode 1: raw-body JSON (no envelope). Content-Type drives interpretation.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        raw_doc = {"hello": "world"}
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(raw_doc),
            content_type="application/json",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        assert body["workflow"] == workflow.id
        assert body["status"] in [
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
        ]
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.workflow_id == workflow.id
        assert run.submission_id is not None

    def test_start_with_invalid_json_returns_error(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data="{invalid-json",
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert resp.data["errors"][0]["field"] == "content"
        assert "Invalid JSON payload" in resp.data["errors"][0]["message"]

    def test_start_with_unsupported_content_type_returns_error(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        """The start API must reject media types outside its explicit registry."""
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data=b"raw-binary",
            content_type="application/x-unsupported",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "Unsupported Content-Type" in resp.data["errors"][0]["message"]

    def test_start_rejects_disallowed_file_type_even_with_valid_content_type(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        workflow.allowed_file_types = [SubmissionFileType.JSON]
        workflow.save(update_fields=["allowed_file_types"])
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data="<root/>",
            content_type="application/xml",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED
        assert "accepts" in resp.data["detail"]

    def test_start_admits_allowed_type_without_proving_every_step(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        """A concrete step mismatch is a runtime finding, not admission failure."""
        workflow.allowed_file_types = [
            SubmissionFileType.JSON,
            SubmissionFileType.XML,
        ]
        workflow.save(update_fields=["allowed_file_types"])
        step = workflow.steps.first()
        step.validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
        )
        step.save(update_fields=["validator"])
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data="<root/>",
            content_type="application/xml",
        )

        assert resp.status_code == status.HTTP_201_CREATED

    def test_start_with_missing_content_type_returns_error(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.generic(
            "POST",
            start_url(workflow),
            b"no-header",
            content_type="",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "Missing Content-Type" in resp.data["errors"][0]["message"]

    def test_metadata_key_value_only_blocks_nested_values(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        SiteSettings.objects.update_or_create(
            slug=SiteSettings.DEFAULT_SLUG,
            defaults={"metadata_key_value_only": True},
        )
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "{}",
            "content_type": "application/json",
            "metadata": {"nested": {"oops": True}},
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "Metadata value for 'nested'" in resp.data["errors"][0]["message"]

    def test_metadata_size_limit_enforced(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        SiteSettings.objects.update_or_create(
            slug=SiteSettings.DEFAULT_SLUG,
            defaults={"metadata_max_bytes": 20},
        )
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "{}",
            "content_type": "application/json",
            "metadata": {"big": "x" * 100},
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "Metadata is too large" in resp.data["errors"][0]["message"]

    def test_metadata_depth_limit_enforced(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        """API admission must bound nested metadata before creating a run."""
        SiteSettings.objects.update_or_create(
            slug=SiteSettings.DEFAULT_SLUG,
            defaults={"metadata_max_depth": 2},
        )
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        envelope = {
            "content": "{}",
            "content_type": "application/json",
            "metadata": {"asset": {"properties": {"height": 3}}},
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "maximum depth of 2" in resp.data["errors"][0]["message"]

    def test_non_finite_metadata_number_is_rejected(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        """API metadata cannot introduce non-standard NaN JSON tokens."""
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        envelope = {
            "content": "{}",
            "content_type": "application/json",
            "metadata": {"measurement": float("nan")},
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert list(resp.data) == ["metadata"]
        assert str(resp.data["metadata"][0]) == "Value must be valid JSON."

    def test_start_logs_tracking_event_with_user(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ) -> None:
        project = ProjectFactory(org=org)
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "<root><v>1</v></root>",
            "content_type": "application/xml",
            "filename": "sample.xml",
            "metadata": {"source": "test-suite"},
        }

        resp = api_client.post(
            f"{start_url(workflow)}?project={project.pk}",
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        run_id = resp.data["id"]

        created_event = TrackingEvent.objects.get(
            app_event_type=AppEventType.VALIDATION_RUN_CREATED,
            extra_data__validation_run_id=run_id,
        )
        started_event = TrackingEvent.objects.get(
            app_event_type=AppEventType.VALIDATION_RUN_STARTED,
            extra_data__validation_run_id=run_id,
        )

        assert created_event.project_id == project.id
        assert created_event.org_id == org.id
        assert created_event.user_id == user.id
        assert created_event.extra_data.get("workflow_pk") == workflow.pk

        assert started_event.project_id == project.id
        assert started_event.extra_data.get("status") == ValidationRunStatus.RUNNING

    def test_start_defaults_to_workflow_project(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ) -> None:
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        raw_doc = {"hello": "world"}
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(raw_doc),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data

        body = resp.json()
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.project_id == workflow.project_id

        created_event = TrackingEvent.objects.get(
            app_event_type=AppEventType.VALIDATION_RUN_CREATED,
            extra_data__validation_run_id=body["id"],
        )
        assert created_event.project_id == workflow.project_id

    def test_start_with_raw_body_xml_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        """
        Mode 1: raw-body XML (no envelope). Content-Type drives interpretation.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        raw_doc = "<root><v>1</v></root>"
        resp = api_client.post(
            start_url(workflow),
            data=raw_doc,
            content_type="application/xml",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        assert body["workflow"] == workflow.id
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.submission is not None

    def test_start_with_envelope_xml_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """
        Mode 2: JSON envelope with XML content.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "<root><v>1</v></root>",
            "content_type": "application/xml",
            "filename": "sample.xml",
            "metadata": {"source": "test-suite"},
        }
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        assert body["workflow"] == workflow.id
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.submission is not None

    def test_json_envelope_accepts_object_content(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ) -> None:
        """Mode 2 should coerce dict/list content into stored text."""

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": {"example": True},
            "content_type": "application/json",
            "filename": "body.json",
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.submission is not None
        assert run.submission.content.strip() == json.dumps(envelope["content"])

    def test_json_envelope_infers_content_type_for_json_payload(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ) -> None:
        """If content_type is omitted, JSON payloads fall back to inference."""

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": {"hello": "world"},
            "filename": "data.json",
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        run = ValidationRun.objects.get(pk=resp.data["id"])
        assert run.submission is not None
        assert run.submission.file_type == SubmissionFileType.JSON

    def test_json_envelope_infers_content_type_for_xml_payload(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ) -> None:
        """Filename and content sniffing should infer XML when not provided."""

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "<root><value>1</value></root>",
            "filename": "sample.xml",
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        run = ValidationRun.objects.get(pk=resp.data["id"])
        assert run.submission is not None
        assert run.submission.file_type == SubmissionFileType.XML

    def test_start_with_file_upload_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """
        Mode 3: multipart file upload. We override content_type explicitly.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        file_bytes = b"Version, 9.6.0\nBuilding, Example;"
        # Provide an IDF-like mime via override (text/x-idf handled in server mapping)
        up = SimpleUploadedFile("building.idf", file_bytes, content_type="text/x-idf")

        resp = api_client.post(
            start_url(workflow),
            data={
                "file": up,
                "filename": "building.idf",
                "content_type": "text/x-idf",
                "metadata": json.dumps({"tag": "upload"}),
            },
            format="multipart",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.submission
        assert run.submission.input_file

    def test_start_with_pdf_upload_persists_the_explicit_pdf_type(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """A PDF-only workflow must persist PDF rather than generic binary."""
        workflow.allowed_file_types = [SubmissionFileType.PDF]
        workflow.save(update_fields=["allowed_file_types"])
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        uploaded = SimpleUploadedFile(
            "engineering-package.pdf",
            b"%PDF-2.0\n%%EOF",
            content_type="application/pdf",
        )
        response = api_client.post(
            start_url(workflow),
            data={
                "file": uploaded,
                "filename": "engineering-package.pdf",
                "content_type": "application/pdf",
            },
            format="multipart",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        run = ValidationRun.objects.get(pk=response.json()["id"])
        assert run.submission.file_type == SubmissionFileType.PDF
        assert run.submission.original_filename == "engineering-package.pdf"

    def test_start_with_raw_pdf_body_preserves_binary_bytes(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """Raw PDF requests must never pass through lossy text decoding."""
        workflow.allowed_file_types = [SubmissionFileType.PDF]
        workflow.save(update_fields=["allowed_file_types"])
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        pdf_bytes = b"%PDF-2.0\n%\xff\xfe binary marker\n%%EOF"

        response = api_client.post(
            start_url(workflow),
            data=pdf_bytes,
            content_type="application/pdf",
            HTTP_X_FILENAME="raw-package.pdf",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        run = ValidationRun.objects.get(pk=response.json()["id"])
        assert run.submission.file_type == SubmissionFileType.PDF
        assert run.submission.original_filename == "raw-package.pdf"
        assert run.submission.read_bytes() == pdf_bytes

    def test_multipart_file_upload_persists_metadata(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """A multipart file upload must persist its submitter metadata.

        Regression test. The multipart/file-upload branch previously
        hard-coded ``metadata={}`` when constructing the ``Submission``,
        silently dropping any metadata sent alongside the file — even though
        the serializer accepted it and the inline-content branch persisted it
        correctly. The drop broke ``SUBMISSION_METADATA`` → ``i.*`` input
        bindings (e.g. EnergyPlus's ``expected_floor_area_m2``) for every file
        upload, and the metadata namespace work depends on this field being
        honoured. We assert the value actually round-trips onto the model.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        file_bytes = b"Version, 9.6.0\nBuilding, Example;"
        up = SimpleUploadedFile("building.idf", file_bytes, content_type="text/x-idf")

        resp = api_client.post(
            start_url(workflow),
            data={
                "file": up,
                "filename": "building.idf",
                "content_type": "text/x-idf",
                "metadata": json.dumps({"deliverable": "handover"}),
            },
            format="multipart",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        run = ValidationRun.objects.get(pk=resp.json()["id"])
        # The submitter-supplied metadata must survive to the Submission row,
        # not be discarded as an empty dict.
        assert run.submission.metadata.get("deliverable") == "handover"

    def test_api_persists_short_description_ungated(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """The API persists short_description as a trusted-setter field.

        Surfaced to assertions as ``submission.short_description``
        (ADR-2026-06-03b). Unlike the web form (gated by
        ``allow_submission_short_description``), the API is a trusted-setter
        path: the caller holds a token scoped to a workflow launcher, so the
        value is persisted regardless of the flag. We assert it lands on the
        ValidationRun even though the default workflow does not enable the flag,
        proving the ungated behaviour — and guarding the previously-missing
        ``extra`` hand-off on the API launch path.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        file_bytes = b"Version, 9.6.0\nBuilding, Example;"
        up = SimpleUploadedFile("building.idf", file_bytes, content_type="text/x-idf")

        resp = api_client.post(
            start_url(workflow),
            data={
                "file": up,
                "filename": "building.idf",
                "content_type": "text/x-idf",
                "short_description": "Final handover package",
            },
            format="multipart",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        run = ValidationRun.objects.get(pk=resp.json()["id"])
        assert run.short_description == "Final handover package"

    def test_api_rejects_over_long_short_description_with_400(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """An over-long short_description returns a clean 400, not a DB 500.

        ``short_description`` maps to ``ValidationRun.short_description``
        (``varchar(255)``), and the launch path creates the run via
        ``objects.create(**extra)`` without ``full_clean()``. Without serializer
        enforcement, a value over 255 chars would reach the DB as an
        integrity/data error (HTTP 500). The serializer caps it at the model's
        max_length, so the caller gets actionable validation feedback instead.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        up = SimpleUploadedFile(
            "building.idf",
            b"Version, 9.6.0;",
            content_type="text/x-idf",
        )
        resp = api_client.post(
            start_url(workflow),
            data={
                "file": up,
                "filename": "building.idf",
                "content_type": "text/x-idf",
                "short_description": "x" * 300,  # well over the 255 cap
            },
            format="multipart",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.data
        # No run should have been created — validation fails before launch.
        assert not ValidationRun.objects.exists()

    def test_start_long_running_returns_202_and_polling_then_succeeds(
        self,
        settings,
        monkeypatch,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """
        Force task path to time out so the endpoint returns 202,
        then simulate completion and poll.
        """

        def make_run(*, org, workflow, submission, status):
            if ValidationRunFactory:
                return ValidationRunFactory(
                    org=org,
                    workflow=workflow,
                    submission=submission,
                    status=status,
                )
            return ValidationRun.objects.create(
                org=org,
                workflow=workflow,
                submission=submission,
                status=status,
            )

        # Override the autouse 201 stub with a 202 stub for THIS test only
        def pending_side_effect(*_, **kwargs):
            request = kwargs.get("request")
            actor = getattr(request, "user", None)
            if not kwargs["workflow"].can_execute(user=actor):
                raise PermissionError("User lacks executor role")
            run = make_run(
                org=kwargs["org"],
                workflow=kwargs["workflow"],
                submission=kwargs["submission"],
                status=ValidationRunStatus.PENDING,
            )
            return ValidationRunLaunchResults(
                validation_run=run,
                data={"id": run.id, "status": run.status},
                status=status.HTTP_202_ACCEPTED,
            )

        fake_service = SimpleNamespace(launch=pending_side_effect)
        monkeypatch.setattr(
            views_mod,
            "ValidationRunService",
            lambda: fake_service,
            raising=True,
        )
        monkeypatch.setattr(
            launch_helpers_mod,
            "ValidationRunService",
            lambda: fake_service,
            raising=True,
        )

        # auth + role
        api_client.force_authenticate(user=user)

        grant_role(user, org, RoleCode.EXECUTOR)

        # POST -> expect 202 + Location
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"long": "run"}),
            content_type="application/json",
        )
        assert resp.status_code == status.HTTP_202_ACCEPTED, resp.data
        loc = resp["Location"]
        pending_id = resp.json()["id"]

        # flip run to SUCCEEDED to simulate background completion
        run = ValidationRun.objects.get(pk=pending_id)
        run.status = ValidationRunStatus.SUCCEEDED

        run.completed = timezone.now()
        run.save()

        # poll
        poll = api_client.get(loc)
        assert poll.status_code == status.HTTP_200_OK
        assert poll.json()["status"] == ValidationRunStatus.SUCCEEDED

    def test_requires_executor_role(self, api_client: APIClient, org, workflow):
        """
        Without EXECUTOR role we respond with 404 to avoid leaking existence.
        """
        viewer = UserFactory()
        grant_role(viewer, org, RoleCode.WORKFLOW_VIEWER)
        viewer.set_current_org(org)
        api_client.force_authenticate(user=viewer)
        envelope = {
            "content": json.dumps({"x": 1}),
            "content_type": "application/json",
        }
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )
        # We return 404 to avoid leaking workflow existence
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_start_rejects_inactive_workflow(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        workflow.is_active = False
        workflow.save(update_fields=["is_active"])

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"content": {"example": True}}),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.data == {
            "detail": "",
            "code": WorkflowStartErrorCode.WORKFLOW_INACTIVE.value,
        }

    def test_start_rejects_workflow_without_steps(
        self,
        api_client: APIClient,
        org,
        user,
        workflow_without_steps,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow_without_steps),
            data=json.dumps({"content": {"example": True}}),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data == {
            "detail": "This workflow has no steps defined and cannot be executed.",
            "code": WorkflowStartErrorCode.NO_WORKFLOW_STEPS.value,
        }


# ── launch_api_validation_run: route-derived source ────────────────────
#
# ``ValidationRun.source`` is derived from the *authenticated route*,
# never from a client-supplied header. The previous
# ``X-Validibot-Source`` mechanism was caller-controlled (any client
# could claim ``MCP`` while invoking the plain API) and was removed.
# These tests pin the new contract:
#
#   • REST API route ``/api/v1/workflows/<uuid>/start/`` → ``API``
#   • embedded MCP application service                   → ``MCP``
#   • x402 anonymous service (cloud)                     → ``X402_AGENT``
#
# The latter two are covered by their service-level suites.
#
# The check is on the ``source`` argument the view passes into
# ``launch_api_validation_run`` — tampering with a request header must
# have NO effect on the recorded source.


class TestApiRouteSourceAttribution:
    """The standard REST API route always records ``ValidationRunSource.API``.

    The previous header-based mechanism let any API caller spoof MCP
    (or any other enum value).  Removing the header and binding source
    to the route closes that channel.  The defensive guard in
    ``launch_api_validation_run`` also rejects ``LAUNCH_PAGE`` outright;
    those checks live alongside the call site here.
    """

    @pytest.fixture
    def captured_source(self, monkeypatch):
        """Wrap ``launch_api_validation_run`` to record the ``source`` kwarg.

        We don't assert against ORM rows because the surrounding
        ``mock_validation_service_success`` fixture already stubs the
        run creation.  All we need is the routed value.
        """
        captured: dict[str, object] = {}
        from validibot.workflows import api_views as api_views_mod
        from validibot.workflows import views_launch_helpers

        real = views_launch_helpers.launch_api_validation_run

        def spy(*args, **kwargs):
            captured["source"] = kwargs.get("source")
            return real(*args, **kwargs)

        # Patch the symbol the view module imported at module load —
        # patching the source module wouldn't take effect because
        # ``api_views`` already bound the function name.
        monkeypatch.setattr(api_views_mod, "launch_api_validation_run", spy)
        return captured

    def test_api_route_records_api_source(
        self,
        api_client,
        user,
        org,
        workflow,
        captured_source,
    ):
        """Vanilla POST to the workflow start endpoint records ``API``."""
        grant_role(user, org, RoleCode.EXECUTOR)
        api_client.force_authenticate(user=user)

        api_client.post(
            start_url(workflow),
            data=json.dumps({"hello": "world"}),
            content_type="application/json",
        )

        assert captured_source.get("source") == ValidationRunSource.API

    def test_api_route_ignores_spoofed_header(
        self,
        api_client,
        user,
        org,
        workflow,
        captured_source,
    ):
        """Any caller-supplied X-Validibot-Source header is silently ignored.

        The header used to drive run.source.  After the P1 #4 fix it
        has zero effect — a malicious or naive client cannot claim to
        be MCP via the open API endpoint.
        """
        grant_role(user, org, RoleCode.EXECUTOR)
        api_client.force_authenticate(user=user)

        api_client.post(
            start_url(workflow),
            data=json.dumps({"hello": "world"}),
            content_type="application/json",
            HTTP_X_VALIDIBOT_SOURCE="MCP",  # tries to claim MCP
        )

        # Source must still be API — header has no effect.
        assert captured_source.get("source") == ValidationRunSource.API


class TestLaunchApiValidationRunRejectsLaunchPage:
    """Defensive guard: ``LAUNCH_PAGE`` is reserved for the web form.

    ``launch_api_validation_run`` raises if a programmer wires it up
    with ``source=LAUNCH_PAGE`` so that this analytics value can never
    leak onto the API path even via a coding mistake.  The web form's
    own helper takes the LAUNCH_PAGE path; the API helper rejects it.
    """

    def test_launch_page_source_rejected(self, db, workflow, user):
        """Calling launch_api_validation_run with LAUNCH_PAGE raises."""
        from rest_framework.test import APIRequestFactory

        from validibot.workflows.views_launch_helpers import launch_api_validation_run

        factory = APIRequestFactory()
        request = factory.post(start_url(workflow))
        request.user = user

        # The current guard raises ValueError; the test asserts the
        # type rather than ``Exception`` so a regression that swallows
        # the guard with a broader except would be visible.
        with pytest.raises(ValueError, match="LAUNCH_PAGE"):
            launch_api_validation_run(
                request=request,
                workflow=workflow,
                submission_build=lambda: None,
                source=ValidationRunSource.LAUNCH_PAGE,
            )
