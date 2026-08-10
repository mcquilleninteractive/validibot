"""Tests for step artifact reference registration and resolution.

Step artifacts are the workflow engine's data-plane counterpart to small
``output_values``. These tests lock in the v1 behavior: trusted backend
artifact metadata is indexed on the run, exposed through the canonical
``steps.<step_key>.artifact`` namespace, and resolvable by explicit bindings
without mutating payload or ordinary output values.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from validibot_shared.validations.envelopes import (
    ValidationArtifact as ValidationArtifactEnvelope,
)

from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Artifact
from validibot.validations.models import WorkflowStepIOPromotion
from validibot.validations.services.artifacts import build_step_artifact_refs
from validibot.validations.services.artifacts import register_output_artifacts
from validibot.validations.services.file_identity import FileIdentity
from validibot.validations.services.path_resolution import resolve_step_input
from validibot.validations.services.run_context import RunContextBuilder
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.pdf.config import config as pdf_config
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db

UPDATED_REPORT_SIZE_BYTES = 200
TEST_ARTIFACT_SHA256 = "a" * 64
PDF_OUTPUT_ARTIFACT_COUNT = 6


def _validation_artifact(**kwargs) -> ValidationArtifactEnvelope:
    """Build strict artifact metadata while keeping each test focused on intent."""
    content = kwargs.pop("content", None)
    if content is not None:
        digest = hashlib.sha256(content).hexdigest()
        kwargs.setdefault("size_bytes", len(content))
        kwargs.setdefault("sha256", digest)
        kwargs.setdefault("storage_version", f"sha256:{digest}")
    else:
        kwargs.setdefault("sha256", TEST_ARTIFACT_SHA256)
        kwargs.setdefault("storage_version", "generation-1")
    return ValidationArtifactEnvelope(**kwargs)


def _declare_generated_model_output(step):
    """Declare the producer port required by the relational binding contract."""
    return StepIODefinitionFactory(
        validator=step.validator,
        workflow_step=None,
        direction=StepIODirection.OUTPUT,
        contract_key="generated_model",
        native_name="generated_model",
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/json",
        data_format=SubmissionDataFormat.ENERGYPLUS_EPJSON,
        accepted_data_formats=[SubmissionDataFormat.ENERGYPLUS_EPJSON],
        accepted_media_types=["application/json"],
        metadata={"accepted_extensions": ["epjson", "json"]},
        envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
        role="generated-model",
        min_items=0,
        max_items=1,
    )


class TestArtifactPromotionContract:
    """Promotion guards keep artifacts out of the workflow signal namespace."""

    def test_artifact_step_io_definition_rejects_promoted_signal_name(self):
        """An artifact port cannot masquerade as a small CEL/JSON signal value."""
        io_definition = StepIODefinitionFactory(
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
        )
        io_definition.promoted_signal_name = "report"

        with pytest.raises(
            ValidationError,
            match="Artifacts cannot be promoted to workflow signals",
        ):
            io_definition.full_clean()

    def test_artifact_step_io_definition_rejects_promotion_overlay(self):
        """Validator-owned artifact ports must also reject overlay promotion."""
        step = WorkflowStepFactory()
        io_definition = StepIODefinitionFactory(
            validator=step.validator,
            direction=StepIODirection.OUTPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
        )
        promotion = WorkflowStepIOPromotion(
            workflow_step=step,
            io_definition=io_definition,
            promoted_signal_name="report",
        )

        with pytest.raises(
            ValidationError,
            match="Only value ports can be promoted to workflow signals",
        ):
            promotion.full_clean()


class TestStepArtifactRegistration:
    """Artifact registration tests protect the run artifact index contract."""

    def test_registers_output_envelope_artifacts_as_artifact_refs(self):
        """A trusted backend artifact becomes one indexed ``ArtifactRef``."""

        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow, name="Simulate Model")
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PASSED,
            validator_backend_image_digest="repo/image@sha256:" + "a" * 64,
        )
        output_envelope = SimpleNamespace(
            artifacts=[
                _validation_artifact(
                    name="eplusout.sql",
                    type="simulation-db",
                    mime_type="application/vnd.sqlite3",
                    uri="gs://validibot/runs/run-1/outputs/eplusout.sql",
                    size_bytes=123,
                ),
            ],
            raw_outputs=SimpleNamespace(
                manifest_uri="gs://validibot/runs/run-1/outputs/manifest.json",
            ),
        )

        refs = register_output_artifacts(
            step_run=step_run,
            output_envelope=output_envelope,
        )

        artifact = Artifact.objects.get(step_run=step_run)
        assert artifact.contract_key == "simulation_db"
        assert artifact.storage_uri == "gs://validibot/runs/run-1/outputs/eplusout.sql"
        assert artifact.manifest_uri.endswith("manifest.json")
        assert artifact.producer_validator_type == step.validator.validation_type
        assert (
            artifact.producer_backend_image_digest
            == step_run.validator_backend_image_digest
        )
        assert refs == [build_step_artifact_refs(step_run)["simulation_db"]]
        assert refs[0]["schema_version"] == "validibot.artifact_ref.v1"
        assert refs[0]["producer_step_key"] == step.step_key
        assert refs[0]["uri"] == artifact.storage_uri

    @patch(
        "validibot.validations.services.artifacts.hash_gcs_file_generation",
    )
    def test_accepts_gcs_artifact_and_manifest_inside_verified_attempt(
        self,
        hash_generation,
        settings,
    ):
        """Trusted GCS references require matching provider bytes and metadata."""
        settings.GCS_VALIDATION_BUCKET = "validation"
        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        attempt = ExecutionAttemptFactory(step_run=step_run)
        prefix = (
            f"gs://validation/runs/{step_run.validation_run.org_id}/"
            f"{step_run.validation_run_id}/attempts/{attempt.pk}"
        )
        artifact_uri = f"{prefix}/output/report.html"
        hash_generation.return_value = FileIdentity(
            uri=artifact_uri,
            size_bytes=123,
            sha256=TEST_ARTIFACT_SHA256,
            storage_version="1700000000000005",
        )

        refs = register_output_artifacts(
            step_run=step_run,
            output_envelope=SimpleNamespace(
                execution_attempt_id=str(attempt.pk),
                artifacts=[
                    _validation_artifact(
                        name="report.html",
                        type="report",
                        mime_type="text/html",
                        uri=artifact_uri,
                        size_bytes=123,
                        storage_version="1700000000000005",
                    ),
                ],
                raw_outputs=SimpleNamespace(
                    manifest_uri=f"{prefix}/output/manifest.json",
                ),
            ),
        )

        assert refs[0]["uri"] == artifact_uri
        assert Artifact.objects.get(step_run=step_run).manifest_uri == (
            f"{prefix}/output/manifest.json"
        )
        hash_generation.assert_called_once_with(
            uri=artifact_uri,
            storage_version="1700000000000005",
        )

    @patch(
        "validibot.validations.services.artifacts.hash_gcs_file_generation",
    )
    def test_rejects_gcs_artifact_identity_that_does_not_match_bytes(
        self,
        hash_generation,
        settings,
    ):
        """A container-reported cloud digest cannot enter signed evidence unchecked."""
        settings.GCS_VALIDATION_BUCKET = "validation"
        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        attempt = ExecutionAttemptFactory(step_run=step_run)
        uri = (
            f"gs://validation/runs/{step_run.validation_run.org_id}/"
            f"{step_run.validation_run_id}/attempts/{attempt.pk}/output/report.html"
        )
        hash_generation.return_value = FileIdentity(
            uri=uri,
            size_bytes=123,
            sha256="b" * 64,
            storage_version="1700000000000005",
        )

        with pytest.raises(ValueError, match="does not match stored bytes"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    execution_attempt_id=str(attempt.pk),
                    artifacts=[
                        _validation_artifact(
                            name="report.html",
                            type="report",
                            mime_type="text/html",
                            uri=uri,
                            size_bytes=123,
                            sha256=TEST_ARTIFACT_SHA256,
                            storage_version="1700000000000005",
                        ),
                    ],
                    raw_outputs=None,
                ),
            )

        assert not Artifact.objects.filter(step_run=step_run).exists()

    @pytest.mark.parametrize(
        "hostile_uri",
        [
            "gs://other/runs/org/run/attempts/attempt/report.html",
            "gs://validation/runs/other-org/run/attempts/attempt/report.html",
        ],
    )
    def test_rejects_gcs_artifact_outside_verified_attempt(
        self,
        settings,
        hostile_uri,
    ):
        """A backend cannot index an artifact from another bucket or org."""
        settings.GCS_VALIDATION_BUCKET = "validation"
        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        attempt = ExecutionAttemptFactory(step_run=step_run)

        with pytest.raises(ValueError, match="artifact URI is outside"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    execution_attempt_id=str(attempt.pk),
                    artifacts=[
                        _validation_artifact(
                            name="report.html",
                            type="report",
                            mime_type="text/html",
                            uri=hostile_uri,
                            size_bytes=123,
                        ),
                    ],
                    raw_outputs=None,
                ),
            )

        assert not Artifact.objects.filter(step_run=step_run).exists()

    def test_rejects_gcs_manifest_outside_verified_attempt(self, settings):
        """A raw-output manifest cannot redirect readers outside its attempt."""
        settings.GCS_VALIDATION_BUCKET = "validation"
        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        attempt = ExecutionAttemptFactory(step_run=step_run)
        prefix = (
            f"gs://validation/runs/{step_run.validation_run.org_id}/"
            f"{step_run.validation_run_id}/attempts/{attempt.pk}"
        )

        with pytest.raises(ValueError, match="manifest URI is outside"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    execution_attempt_id=str(attempt.pk),
                    artifacts=[
                        _validation_artifact(
                            name="report.html",
                            type="report",
                            mime_type="text/html",
                            uri=f"{prefix}/output/report.html",
                            size_bytes=123,
                        ),
                    ],
                    raw_outputs=SimpleNamespace(
                        manifest_uri="gs://validation/runs/other/run/manifest.json",
                    ),
                ),
            )

        assert not Artifact.objects.filter(step_run=step_run).exists()

    def test_rejects_gcs_artifact_bound_to_another_steps_attempt(self, settings):
        """Envelope identity cannot borrow a valid attempt owned by another step."""
        settings.GCS_VALIDATION_BUCKET = "validation"
        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        other_attempt = ExecutionAttemptFactory()
        prefix = (
            f"gs://validation/runs/{step_run.validation_run.org_id}/"
            f"{step_run.validation_run_id}/attempts/{other_attempt.pk}"
        )

        with pytest.raises(ValueError, match="attempt for this step"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    execution_attempt_id=str(other_attempt.pk),
                    artifacts=[
                        _validation_artifact(
                            name="report.html",
                            type="report",
                            mime_type="text/html",
                            uri=f"{prefix}/output/report.html",
                            size_bytes=123,
                        ),
                    ],
                    raw_outputs=None,
                ),
            )

        assert not Artifact.objects.filter(step_run=step_run).exists()

    def test_hashes_container_output_uri_through_run_workspace(
        self,
        settings,
        tmp_path,
    ):
        """Local container URIs must produce durable evidence hashes.

        Validator containers refer to their attempt-bound output mount.
        Hashing matters because evidence manifests and download responses rely
        on the indexed digest, not merely file size.
        """

        settings.DATA_STORAGE_ROOT = str(tmp_path)
        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        attempt = ExecutionAttemptFactory(step_run=step_run)
        payload = b"validator report bytes"
        report_path = (
            tmp_path
            / "runs"
            / str(step_run.validation_run.org_id)
            / str(step_run.validation_run_id)
            / "attempts"
            / str(attempt.pk)
            / "output"
            / "outputs"
            / "report.txt"
        )
        report_path.parent.mkdir(parents=True)
        report_path.write_bytes(payload)

        refs = register_output_artifacts(
            step_run=step_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="report.txt",
                        type="report",
                        mime_type="text/plain",
                        uri=(
                            f"file:///validibot/attempts/{attempt.pk}/"
                            "output/outputs/report.txt"
                        ),
                        size_bytes=len(payload),
                        content=payload,
                    ),
                ],
                raw_outputs=None,
            ),
        )

        expected = hashlib.sha256(payload).hexdigest()
        artifact = Artifact.objects.get(step_run=step_run)
        assert artifact.sha256 == expected
        assert refs[0]["sha256"] == expected

    def test_rejects_local_artifact_identity_that_does_not_match_bytes(
        self,
        settings,
        tmp_path,
    ):
        """Untrusted local artifact claims must not enter the durable index."""
        settings.DATA_STORAGE_ROOT = str(tmp_path)
        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        attempt = ExecutionAttemptFactory(step_run=step_run)
        report_path = (
            tmp_path
            / "runs"
            / str(step_run.validation_run.org_id)
            / str(step_run.validation_run_id)
            / "attempts"
            / str(attempt.pk)
            / "output"
            / "outputs"
            / "report.txt"
        )
        report_path.parent.mkdir(parents=True)
        report_path.write_bytes(b"actual report")
        uri = f"file:///validibot/attempts/{attempt.pk}/output/outputs/report.txt"

        with pytest.raises(ValueError, match="does not match stored bytes"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    artifacts=[
                        _validation_artifact(
                            name="report.txt",
                            type="report",
                            mime_type="text/plain",
                            uri=uri,
                            size_bytes=len(b"actual report"),
                            sha256="b" * 64,
                            storage_version="sha256:" + "b" * 64,
                        ),
                    ],
                    raw_outputs=None,
                ),
            )

        assert Artifact.objects.filter(step_run=step_run).count() == 0

    def test_reregistering_same_artifact_updates_without_duplicates(self):
        """Callback retries must not duplicate the same step artifact output."""

        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        first = SimpleNamespace(
            artifacts=[
                _validation_artifact(
                    name="report.html",
                    type="report",
                    mime_type="text/html",
                    uri="gs://bucket/old/report.html",
                    size_bytes=100,
                ),
            ],
            raw_outputs=None,
        )
        second = SimpleNamespace(
            artifacts=[
                _validation_artifact(
                    name="report.html",
                    type="report",
                    mime_type="text/html",
                    uri="gs://bucket/new/report.html",
                    size_bytes=200,
                ),
            ],
            raw_outputs=None,
        )

        register_output_artifacts(step_run=step_run, output_envelope=first)
        register_output_artifacts(step_run=step_run, output_envelope=second)

        artifacts = Artifact.objects.filter(step_run=step_run)
        assert artifacts.count() == 1
        artifact = artifacts.get()
        assert artifact.contract_key == "report"
        assert artifact.storage_uri == "gs://bucket/new/report.html"
        assert artifact.size_bytes == UPDATED_REPORT_SIZE_BYTES

    def test_declared_output_port_controls_artifact_ref_contract_key(self):
        """Output artifact ports should own stable public artifact keys.

        Backend envelopes use runner-facing roles such as ``simulation-db``.
        The declared output port maps that role to the author-facing
        ``eplusout_sql`` key used by ``steps.<step>.artifact.eplusout_sql``.
        """

        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow, name="Simulate Model")
        StepIODefinitionFactory(
            validator=step.validator,
            direction=StepIODirection.OUTPUT,
            contract_key="eplusout_sql",
            native_name="eplusout_sql",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/x-sqlite3",
            data_format="sqlite",
            accepted_data_formats=["sqlite"],
            accepted_media_types=["application/x-sqlite3", "application/vnd.sqlite3"],
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="simulation-db",
            metadata={"accepted_extensions": ["sql"]},
            min_items=0,
            max_items=1,
        )
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PASSED,
        )

        refs = register_output_artifacts(
            step_run=step_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="eplusout.sql",
                        type="simulation-db",
                        mime_type="application/x-sqlite3",
                        uri="gs://validibot/runs/run-1/outputs/eplusout.sql",
                        size_bytes=123,
                    ),
                ],
                raw_outputs=None,
            ),
        )

        artifact = Artifact.objects.get(step_run=step_run)
        assert artifact.contract_key == "eplusout_sql"
        assert artifact.role == "simulation-db"
        assert artifact.kind == ArtifactKind.DATASET
        assert artifact.data_format == "sqlite"
        assert artifact.metadata["source"] == "declared_output_port"
        assert refs == [build_step_artifact_refs(step_run)["eplusout_sql"]]

    def test_pdf_attempt_maps_all_six_artifacts_to_distinct_declared_ports(self):
        """One PDF attempt must not conflate its six similarly named outputs."""
        run = ValidationRunFactory()
        validator = ValidatorFactory(validation_type=ValidationType.PDF)
        step = WorkflowStepFactory(
            workflow=run.workflow,
            validator=validator,
            name="Inspect PDF",
        )
        output_entries = [
            entry
            for entry in pdf_config.catalog_entries
            if entry.run_stage == "output" and entry.io_medium == StepIOMedium.ARTIFACT
        ]
        for entry in output_entries:
            StepIODefinitionFactory(
                validator=validator,
                direction=StepIODirection.OUTPUT,
                contract_key=entry.slug,
                native_name=entry.slug,
                data_type=entry.data_type,
                io_medium=entry.io_medium,
                artifact_kind=entry.artifact_kind,
                media_type=entry.media_type,
                data_format=entry.data_format,
                accepted_data_formats=entry.accepted_data_formats,
                accepted_media_types=entry.accepted_media_types,
                envelope_channel=entry.envelope_channel,
                role=entry.role,
                metadata=entry.metadata,
                min_items=entry.min_items,
                max_items=entry.max_items,
            )
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PASSED,
        )
        envelope_artifacts = [
            _validation_artifact(
                name=f"{entry.slug}.artifact",
                type=entry.role,
                mime_type=entry.media_type,
                uri=f"gs://validibot/runs/run-1/outputs/{entry.slug}.artifact",
                size_bytes=123,
            )
            for entry in output_entries
        ]

        refs = register_output_artifacts(
            step_run=step_run,
            output_envelope=SimpleNamespace(
                artifacts=envelope_artifacts,
                raw_outputs=None,
            ),
        )

        expected_keys = {entry.slug for entry in output_entries}
        assert len(expected_keys) == PDF_OUTPUT_ARTIFACT_COUNT
        assert {ref["contract_key"] for ref in refs} == expected_keys
        assert set(build_step_artifact_refs(step_run)) == expected_keys

    def test_declared_json_output_uses_port_domain_format(self):
        """Portfolio Manager JSON should retain its declared domain identity."""

        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow, name="Benchmark Property")
        StepIODefinitionFactory(
            validator=step.validator,
            direction=StepIODirection.OUTPUT,
            contract_key="property_results",
            native_name="property_results",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/json",
            data_format="portfolio_manager_property_results_v1",
            accepted_data_formats=["portfolio_manager_property_results_v1"],
            accepted_media_types=["application/json"],
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="portfolio-manager-property-results",
            metadata={"accepted_extensions": ["json"]},
            min_items=0,
            max_items=1,
        )
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PASSED,
        )

        refs = register_output_artifacts(
            step_run=step_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="portfolio-manager-property-results.json",
                        type="portfolio-manager-property-results",
                        mime_type="application/json",
                        uri=(
                            "gs://validibot/runs/run-1/outputs/"
                            "portfolio-manager-property-results.json"
                        ),
                        size_bytes=123,
                    ),
                ],
                raw_outputs=None,
            ),
        )

        artifact = Artifact.objects.get(step_run=step_run)
        assert artifact.contract_key == "property_results"
        assert artifact.data_format == "portfolio_manager_property_results_v1"
        assert refs[0]["data_format"] == "portfolio_manager_property_results_v1"

    def test_declared_output_port_rejects_wrong_artifact_extension(self):
        """Declared output ports should fail before indexing incompatible files."""

        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        StepIODefinitionFactory(
            validator=step_run.workflow_step.validator,
            direction=StepIODirection.OUTPUT,
            contract_key="eplusout_sql",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/x-sqlite3",
            data_format="sqlite",
            accepted_data_formats=["sqlite"],
            accepted_media_types=["application/x-sqlite3"],
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="simulation-db",
            metadata={"accepted_extensions": ["sql"]},
            min_items=0,
            max_items=1,
        )

        with pytest.raises(ValueError, match=r"expected one of \.sql"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    artifacts=[
                        _validation_artifact(
                            name="eplusout.csv",
                            type="simulation-db",
                            mime_type="text/csv",
                            uri="gs://validibot/runs/run-1/outputs/eplusout.csv",
                            size_bytes=123,
                        ),
                    ],
                    raw_outputs=None,
                ),
            )

        assert Artifact.objects.filter(step_run=step_run).count() == 0

    def test_declared_output_port_enforces_scalar_cardinality(self):
        """A scalar output port must not silently index duplicate artifacts."""

        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        StepIODefinitionFactory(
            validator=step_run.workflow_step.validator,
            direction=StepIODirection.OUTPUT,
            contract_key="eplusout_sql",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/x-sqlite3",
            data_format="sqlite",
            accepted_data_formats=["sqlite"],
            accepted_media_types=["application/x-sqlite3"],
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="simulation-db",
            metadata={"accepted_extensions": ["sql"]},
            min_items=0,
            max_items=1,
        )

        with pytest.raises(ValueError, match="accepts at most 1"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    artifacts=[
                        _validation_artifact(
                            name="eplusout.sql",
                            type="simulation-db",
                            mime_type="application/x-sqlite3",
                            uri="gs://bucket/outputs/eplusout.sql",
                            size_bytes=100,
                        ),
                        _validation_artifact(
                            name="copy.sql",
                            type="simulation-db",
                            mime_type="application/x-sqlite3",
                            uri="gs://bucket/outputs/copy.sql",
                            size_bytes=100,
                        ),
                    ],
                    raw_outputs=None,
                ),
            )

        assert Artifact.objects.filter(step_run=step_run).count() == 0


class TestStepArtifactRunContext:
    """Run context tests prove artifacts have a separate namespace."""

    def test_upstream_artifacts_are_exposed_separately_from_values(self):
        """Completed upstream step artifacts appear under ``artifact`` only."""

        run = ValidationRunFactory()
        upstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            name="Build Model",
            order=10,
        )
        _declare_generated_model_output(upstream_step)
        downstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            name="Run Simulation",
            order=20,
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=10,
            status=StepStatus.PASSED,
            output_values={"site_eui": 42},
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="model.epjson",
                        type="generated-model",
                        mime_type="application/json",
                        uri="gs://bucket/model.epjson",
                        size_bytes=456,
                    ),
                ],
                raw_outputs=None,
            ),
        )

        context = RunContextBuilder(run, downstream_step).build()

        upstream = context.upstream_steps[upstream_step.step_key]
        assert upstream["output"] == {"site_eui": 42}
        assert "generated_model" not in upstream["output"]
        assert upstream["artifact"]["generated_model"]["filename"] == "model.epjson"

    def test_upstream_artifact_binding_resolves_artifact_ref(self):
        """Bindings can target a prior step's artifact namespace explicitly."""

        run = ValidationRunFactory()
        upstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            name="Build Model",
            order=10,
        )
        _declare_generated_model_output(upstream_step)
        downstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            name="Run Simulation",
            order=20,
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=10,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    _validation_artifact(
                        name="model.epjson",
                        type="generated-model",
                        mime_type="application/json",
                        uri="gs://bucket/model.epjson",
                        size_bytes=456,
                    ),
                ],
                raw_outputs=None,
            ),
        )
        io_definition = StepIODefinitionFactory(
            validator=None,
            workflow_step=downstream_step,
            direction=StepIODirection.INPUT,
            contract_key="primary_model",
            native_name="primary-model",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
        )
        binding = StepInputBindingFactory(
            workflow_step=downstream_step,
            io_definition=io_definition,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        context = RunContextBuilder(run, downstream_step).build()

        resolved = resolve_step_input(
            binding,
            upstream_steps=context.upstream_steps,
        )

        assert resolved.resolved is True
        assert resolved.value["schema_version"] == "validibot.artifact_ref.v1"
        assert resolved.value["uri"] == "gs://bucket/model.epjson"
        assert resolved.upstream_step_key == upstream_step.step_key
