"""Tests for artifact detail and download views.

Step artifacts are durable run outputs, so the UI contract is security-sensitive:
users should only see artifacts through the parent run's access boundary, raw
storage URIs must not leak into HTML, and downloads should only stream byte
sources the web process can serve safely. These tests cover the supported local
and Django-storage paths plus the current remote-URI fallback.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html as lxml_html

from validibot.submissions.constants import OutputRetention
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.tests.factories import ArtifactFactory
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory


def _setup_run_with_owner_access():
    """Build an owner and a succeeded run in the owner's organization."""

    org = OrganizationFactory()
    owner = UserFactory(orgs=[org])
    grant_role(owner, org, RoleCode.OWNER)
    owner.memberships.get(org=org).set_roles({RoleCode.OWNER})
    owner.set_current_org(org)

    submission = SubmissionFactory(org=org, user=owner, project__org=org)
    run = ValidationRunFactory(
        org=org,
        user=owner,
        submission=submission,
        workflow=submission.workflow,
        project=submission.project,
        status=ValidationRunStatus.SUCCEEDED,
    )
    return owner, run


def _create_artifact(run, **overrides):
    """Create a representative step artifact for ``run``."""

    container_output_path = overrides.pop("container_output_path", "")
    payload_hash = hashlib.sha256(b"artifact-payload").hexdigest()
    step_run = ValidationStepRunFactory(
        validation_run=run,
        status=StepStatus.PASSED,
    )
    defaults = {
        "validation_run": run,
        "org": run.org,
        "step_run": step_run,
        "workflow_step": step_run.workflow_step,
        "label": "energy-report.html",
        "content_type": "text/html",
        "contract_key": "annual_report",
        "role": "annual_report",
        "kind": ArtifactKind.REPORT,
        "data_format": "html",
        "size_bytes": 16,
        "sha256": payload_hash,
        "retention_class": "store_30_days",
        "storage_uri": "gs://private-validibot-artifacts/runs/output.html",
    }
    if container_output_path:
        attempt = ExecutionAttemptFactory(step_run=step_run)
        defaults["storage_uri"] = (
            f"file:///validibot/attempts/{attempt.pk}/output/"
            f"{container_output_path.lstrip('/')}"
        )
    defaults.update(overrides)
    return ArtifactFactory(**defaults)


def _streaming_body(response) -> bytes:
    """Read a streaming ``FileResponse`` body for assertions."""

    return b"".join(response.streaming_content)


class ArtifactReportPanelTests(TestCase):
    """Verify run report surfaces show artifacts without leaking storage URIs."""

    def test_validation_detail_lists_artifacts_without_raw_storage_uri(self):
        """Standalone detail nests the safe artifact UI under run outputs."""

        user, run = _setup_run_with_owner_access()
        artifact = _create_artifact(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", kwargs={"pk": run.pk}),
        )

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Generated Files")
        self.assertContains(response, "energy-report.html")
        self.assertContains(response, "annual_report")
        self.assertContains(response, "Role")
        self.assertContains(response, "Kind")
        assert b"Role / Type" not in response.content
        document = lxml_html.fromstring(response.content)
        outputs_body = document.get_element_by_id("reportOutputsBody")
        outputs_section = document.get_element_by_id("reportOutputs")
        assert outputs_section.get("data-validation-run-section") == "outputs"
        assert "show" not in outputs_body.get("class", "").split()
        generated_buttons = outputs_body.xpath(
            ".//button[@data-bs-target='#reportGeneratedFilesBody']",
        )
        assert len(generated_buttons) == 1
        generated_button = generated_buttons[0]
        generated_button_classes = set(generated_button.get("class", "").split())
        assert {"bg-white", "text-body", "fw-semibold"}.issubset(
            generated_button_classes,
        )
        assert (
            generated_button.xpath("normalize-space(.//span/span[1])")
            == "Generated Files"
        )
        assert generated_button.xpath("normalize-space(.//span/span[2])") == "1"
        generated_bodies = outputs_body.xpath(
            ".//*[@id='reportGeneratedFilesBody']",
        )
        assert len(generated_bodies) == 1
        assert generated_bodies[0].xpath(
            ".//table[contains(normalize-space(.), 'energy-report.html')]",
        )
        assert (
            reverse(
                "validations:artifact_detail",
                kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
            ).encode()
            in response.content
        )
        assert b"gs://private-validibot-artifacts" not in response.content

    def test_classic_validation_detail_nests_artifacts_under_run_outputs(self):
        """The two-column report must keep Generated Files inside Outputs."""

        user, run = _setup_run_with_owner_access()
        _create_artifact(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", kwargs={"pk": run.pk}),
            {"layout": "classic"},
        )

        assert response.status_code == HTTPStatus.OK
        document = lxml_html.fromstring(response.content)
        outputs_body = document.get_element_by_id("reportOutputsBody")
        outputs_section = document.get_element_by_id("reportOutputs")
        assert outputs_section.get("data-validation-run-section") == "outputs"
        assert "show" not in outputs_body.get("class", "").split()
        generated_buttons = outputs_body.xpath(
            ".//button[@data-bs-target='#reportGeneratedFilesBody']",
        )
        assert len(generated_buttons) == 1
        assert (
            generated_buttons[0].xpath("normalize-space(.//span/span[1])")
            == "Generated Files"
        )

    def test_launch_run_detail_uses_same_artifact_panel(self):
        """Workflow launch reports render artifacts through the shared layout."""

        user, run = _setup_run_with_owner_access()
        _create_artifact(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse(
                "workflows:workflow_run_detail",
                kwargs={
                    "pk": run.workflow.pk,
                    "run_id": run.pk,
                },
            ),
        )

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Generated Files")
        self.assertContains(response, "energy-report.html")
        assert b"gs://private-validibot-artifacts" not in response.content


class ArtifactDetailViewTests(TestCase):
    """Verify artifact metadata display and run-scoped authorization."""

    def test_artifact_detail_shows_metadata_without_raw_storage_uri(self):
        """Detail view shows author-facing metadata, not private storage URIs."""

        user, run = _setup_run_with_owner_access()
        artifact = _create_artifact(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse(
                "validations:artifact_detail",
                kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "energy-report.html")
        self.assertContains(response, "Producer step")
        self.assertContains(response, "annual_report")
        self.assertContains(response, "text/html")
        self.assertContains(response, "html")
        self.assertContains(response, "store_30_days")
        self.assertContains(response, artifact.sha256)
        assert b"gs://private-validibot-artifacts" not in response.content

    def test_user_from_other_org_gets_404_for_detail_and_download(self):
        """Artifact existence should not leak across organization boundaries."""

        _, run = _setup_run_with_owner_access()
        artifact = _create_artifact(run)
        other_org = OrganizationFactory()
        other_user = UserFactory(orgs=[other_org])
        grant_role(other_user, other_org, RoleCode.OWNER)
        other_user.memberships.get(org=other_org).set_roles({RoleCode.OWNER})
        other_user.set_current_org(other_org)

        self.client.force_login(other_user)
        detail_response = self.client.get(
            reverse(
                "validations:artifact_detail",
                kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
            ),
        )
        download_response = self.client.get(
            reverse(
                "validations:artifact_download",
                kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
            ),
        )

        assert detail_response.status_code == HTTPStatus.NOT_FOUND
        assert download_response.status_code == HTTPStatus.NOT_FOUND

    def test_expired_output_returns_404_for_detail_and_download(self):
        """An overdue purge worker must not extend artifact access."""
        user, run = _setup_run_with_owner_access()
        artifact = _create_artifact(run)
        run.output_retention_policy = OutputRetention.STORE_1_DAY
        run.output_expires_at = timezone.now() - timedelta(minutes=1)
        run.save(
            update_fields=["output_retention_policy", "output_expires_at"],
        )

        self.client.force_login(user)
        detail_response = self.client.get(
            reverse(
                "validations:artifact_detail",
                kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
            ),
        )
        download_response = self.client.get(
            reverse(
                "validations:artifact_download",
                kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
            ),
        )

        assert detail_response.status_code == HTTPStatus.NOT_FOUND
        assert download_response.status_code == HTTPStatus.NOT_FOUND


class ArtifactDownloadViewTests(TestCase):
    """Verify the supported and unsupported artifact byte sources."""

    def test_storage_backed_artifact_file_downloads(self):
        """Artifacts with a Django ``FileField`` stream through FileResponse."""

        user, run = _setup_run_with_owner_access()
        payload = b"storage-backed artifact"
        digest = hashlib.sha256(payload).hexdigest()
        with (
            TemporaryDirectory() as media_root,
            self.settings(MEDIA_ROOT=media_root),
        ):
            artifact = _create_artifact(
                run,
                storage_uri="",
                sha256=digest,
                size_bytes=len(payload),
            )
            artifact.file.save(
                "storage-backed.txt",
                ContentFile(payload),
                save=True,
            )

            self.client.force_login(user)
            response = self.client.get(
                reverse(
                    "validations:artifact_download",
                    kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
                ),
            )

            assert response.status_code == HTTPStatus.OK
            assert _streaming_body(response) == payload
            assert response["X-Validibot-Artifact-Sha256"] == digest
            assert "storage-backed.txt" in response["Content-Disposition"]
            assert response["Content-Disposition"].startswith("attachment;")
            assert response["X-Content-Type-Options"] == "nosniff"
            assert response["Cross-Origin-Resource-Policy"] == "same-origin"

    def test_container_output_file_uri_maps_to_run_workspace(self):
        """Local Docker artifact URIs map back to the owning run workspace."""

        user, run = _setup_run_with_owner_access()
        payload = b"workspace artifact"
        digest = hashlib.sha256(payload).hexdigest()
        with TemporaryDirectory() as data_root:
            with self.settings(DATA_STORAGE_ROOT=data_root):
                artifact = _create_artifact(
                    run,
                    container_output_path="outputs/report.txt",
                    content_type="text/plain",
                    sha256=digest,
                    size_bytes=len(payload),
                )
            attempt = artifact.step_run.execution_attempts.get()
            workspace_file = (
                Path(data_root)
                / "runs"
                / str(run.org_id)
                / str(run.pk)
                / "attempts"
                / str(attempt.pk)
                / "output"
                / "outputs"
                / "report.txt"
            )
            workspace_file.parent.mkdir(parents=True)
            workspace_file.write_bytes(payload)
            with self.settings(DATA_STORAGE_ROOT=data_root):
                self.client.force_login(user)
                response = self.client.get(
                    reverse(
                        "validations:artifact_download",
                        kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
                    ),
                )

                assert response.status_code == HTTPStatus.OK
                assert _streaming_body(response) == payload
                assert response["X-Validibot-Artifact-Sha256"] == digest
                assert "report.txt" in response["Content-Disposition"]

    def test_container_output_uri_rejects_another_runs_attempt(self):
        """An artifact URI cannot use another run's attempt as a path oracle."""
        user, run = _setup_run_with_owner_access()
        foreign_attempt = ExecutionAttemptFactory()
        payload = b"must not be served"

        with TemporaryDirectory() as data_root:
            exposed_if_unchecked = (
                Path(data_root)
                / "runs"
                / str(run.org_id)
                / str(run.pk)
                / "attempts"
                / str(foreign_attempt.pk)
                / "output"
                / "outputs"
                / "secret.txt"
            )
            exposed_if_unchecked.parent.mkdir(parents=True)
            exposed_if_unchecked.write_bytes(payload)
            artifact = _create_artifact(
                run,
                storage_uri=(
                    f"file:///validibot/attempts/{foreign_attempt.pk}/"
                    "output/outputs/secret.txt"
                ),
            )

            with self.settings(DATA_STORAGE_ROOT=data_root):
                self.client.force_login(user)
                response = self.client.get(
                    reverse(
                        "validations:artifact_download",
                        kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
                    ),
                )

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_missing_storage_backed_file_returns_404(self):
        """A stale Django file-storage reference should fail closed with 404."""

        user, run = _setup_run_with_owner_access()
        with (
            TemporaryDirectory() as media_root,
            self.settings(MEDIA_ROOT=media_root),
        ):
            artifact = _create_artifact(
                run,
                storage_uri="",
            )
            artifact.file.name = "artifacts/missing-storage-file.txt"
            artifact.save(update_fields=["file"])

            self.client.force_login(user)
            response = self.client.get(
                reverse(
                    "validations:artifact_download",
                    kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
                ),
            )

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_remote_storage_uri_returns_404_until_signed_downloads_exist(self):
        """Remote object storage is not fetched or exposed by the web view."""

        user, run = _setup_run_with_owner_access()
        artifact = _create_artifact(
            run,
            storage_uri="gs://private-validibot-artifacts/report.html",
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse(
                "validations:artifact_download",
                kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
            ),
        )

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_missing_local_file_uri_returns_404(self):
        """A stale local artifact row should fail closed with 404."""

        user, run = _setup_run_with_owner_access()
        with TemporaryDirectory() as data_root:
            missing_file = Path(data_root) / "runs" / "missing.txt"
            with self.settings(DATA_STORAGE_ROOT=data_root):
                artifact = _create_artifact(
                    run,
                    storage_uri=f"file://{missing_file}",
                )

                self.client.force_login(user)
                response = self.client.get(
                    reverse(
                        "validations:artifact_download",
                        kwargs={"pk": run.pk, "artifact_pk": artifact.pk},
                    ),
                )

        assert response.status_code == HTTPStatus.NOT_FOUND
