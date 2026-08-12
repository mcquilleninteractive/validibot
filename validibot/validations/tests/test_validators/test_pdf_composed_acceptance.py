"""Prove the real PDF-to-selected-XML-to-XSD workflow composition path.

This acceptance test is deliberately broader than a mocked envelope-builder
test. It runs the release PDF container against a digest-pinned synthetic PDF,
parses the real output envelope, registers its integrity-bound artifacts, and
then executes the ordinary inline XML Schema processor through a relational
workflow binding. The assertions preserve exact bytes and producer identity
at every boundary so the immutable PDF submission can never be mistaken for
the selected downstream XML payload.
"""

from __future__ import annotations

import hashlib
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from validibot_shared.canonicalization import sha256_hex_for_model
from validibot_shared.pdf import PdfInputEnvelope
from validibot_shared.pdf import PdfInputs
from validibot_shared.pdf import PdfOutputEnvelope
from validibot_shared.pdf import PdfPayloadSelector
from validibot_shared.validations.envelopes import ATTEMPT_CONTRACT_VERSION
from validibot_shared.validations.envelopes import ExecutionContext
from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import SupportedMimeType
from validibot_shared.validations.envelopes import ValidationStatus
from validibot_shared.validations.envelopes import ValidatorType

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import RulesetType
from validibot.validations.constants import StepStatus
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Artifact
from validibot.validations.models import Validator
from validibot.validations.services.artifact_bindings import set_artifact_input_binding
from validibot.validations.services.artifacts import register_output_artifacts
from validibot.validations.services.step_processor.simple import (
    SimpleValidationProcessor,
)
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.validators.xml_schema.validator import XmlSchemaValidator
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

if TYPE_CHECKING:
    from validibot.submissions.models import Submission

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.integration]

PDF_IMAGE = "validibot-validator-backend-pdf:latest"
PDF_FIXTURE = (
    Path(__file__).parents[4] / "tests" / "assets" / "pdf" / "composed-package.pdf"
)
PDF_SHA256 = "7f75249cf0108b7238891d4c3ff4e95a8855eae6933710795a4ce9727f595638"
PDF_SIZE_BYTES = 3457
EXPECTED_XML = b'<handover xmlns="urn:validibot:fixture"><id>A-1</id></handover>'
EXPECTED_XML_SHA256 = "b56d9195ef81805e1c1fd9a4096b9da11effe44eee717c6ba9c2ae7790d7c948"


def _docker_client_with_pdf_image():
    """Return a live Docker client and the locally built PDF release image."""
    docker = pytest.importorskip(
        "docker",
        reason="The docker-runner extra is required for composed PDF acceptance.",
    )
    try:
        client = docker.from_env()
        client.ping()
        image = client.images.get(PDF_IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"Build {PDF_IMAGE} before running composed PDF acceptance.")
    except docker.errors.DockerException as exc:
        pytest.skip(f"A reachable local Docker engine is required: {exc}")
    return client, image


def _write_attempt_contract(
    *,
    attempt_dir: Path,
    fixture_bytes: bytes,
    run,
    producer_run,
    pdf_validator,
) -> tuple[PdfInputEnvelope, Path, Path]:
    """Materialize one exact local attempt contract visible at the same URI."""
    input_dir = attempt_dir / "input"
    input_dir.mkdir(parents=True)
    # The release image runs as uid 1000, which is deliberately unrelated to
    # the host test user. This isolated fixture workspace therefore needs write
    # access for that container identity.
    attempt_dir.chmod(0o777)
    input_dir.chmod(0o777)
    pdf_path = input_dir / "composed-package.pdf"
    output_path = attempt_dir / "output.json"
    input_path = attempt_dir / "input.json"
    pdf_path.write_bytes(fixture_bytes)
    pdf_path.chmod(0o644)

    envelope = PdfInputEnvelope(
        run_id=str(run.pk),
        validator={
            "id": str(pdf_validator.pk),
            "type": ValidatorType.PDF,
            "version": str(pdf_validator.version),
        },
        org={"id": str(run.org_id), "name": run.org.name},
        workflow={
            "id": str(run.workflow_id),
            "step_id": str(producer_run.workflow_step_id),
            "step_name": producer_run.workflow_step.name,
        },
        input_files=[
            InputFileItem(
                name=pdf_path.name,
                mime_type=SupportedMimeType.APPLICATION_PDF,
                role="pdf-document",
                port_key="pdf_document",
                uri=pdf_path.as_uri(),
                size_bytes=len(fixture_bytes),
                sha256=PDF_SHA256,
                storage_version=f"sha256:{PDF_SHA256}",
            )
        ],
        inputs=PdfInputs(
            selected_xml=PdfPayloadSelector(
                required=True,
                original_filename="handover.xml",
                declared_media_type="application/xml",
                xml_root_qname="{urn:validibot:fixture}handover",
            )
        ),
        context=ExecutionContext(
            execution_attempt_id=str(uuid4()),
            step_run_id=str(producer_run.pk),
            attempt_contract_version=ATTEMPT_CONTRACT_VERSION,
            expected_output_uri=output_path.as_uri(),
            execution_bundle_uri=attempt_dir.as_uri(),
            skip_callback=True,
        ),
    )
    input_path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
    input_path.chmod(0o644)
    return envelope, input_path, output_path


def _run_pdf_container(*, client, workspace: Path, input_path: Path, output_path: Path):
    """Run the PDF image with a read-only root and no network or capabilities."""
    client.containers.run(
        PDF_IMAGE,
        environment={
            "VALIDIBOT_INPUT_URI": input_path.as_uri(),
            "VALIDIBOT_OUTPUT_URI": output_path.as_uri(),
        },
        volumes={str(workspace): {"bind": str(workspace), "mode": "rw"}},
        network_disabled=True,
        read_only=True,
        tmpfs={"/tmp": "rw,noexec,nosuid,nodev,size=64m"},  # noqa: S108
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        pids_limit=128,
        mem_limit="512m",
        nano_cpus=1_000_000_000,
        detach=False,
        remove=True,
    )


def test_real_pdf_output_is_the_exact_xml_validated_by_the_next_step(
    tmp_path: Path,
    settings,
    monkeypatch,
) -> None:
    """A genuine container artifact must traverse the binding and pass its XSD."""
    client, image = _docker_client_with_pdf_image()
    fixture_bytes = PDF_FIXTURE.read_bytes()
    assert len(fixture_bytes) == PDF_SIZE_BYTES
    assert hashlib.sha256(fixture_bytes).hexdigest() == PDF_SHA256
    assert hashlib.sha256(EXPECTED_XML).hexdigest() == EXPECTED_XML_SHA256

    settings.DATA_STORAGE_ROOT = tmp_path
    settings.MEDIA_ROOT = tmp_path / "media"
    call_command(
        "sync_validators",
        stdout=StringIO(),
        stderr=StringIO(),
    )
    pdf_validator = Validator.objects.get(slug="pdf-validator", version=1)
    xml_validator = Validator.objects.get(slug="xml-validator", version=2)
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.PDF])
    ruleset = RulesetFactory(
        org=workflow.org,
        ruleset_type=RulesetType.XML_SCHEMA,
        rules_text=(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:validibot:fixture" '
            'elementFormDefault="qualified">'
            '<xs:element name="handover"><xs:complexType><xs:sequence>'
            '<xs:element name="id" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element></xs:schema>"
        ),
        metadata={"schema_type": XMLSchemaType.XSD},
    )
    producer = WorkflowStepFactory(
        workflow=workflow,
        validator=pdf_validator,
        order=10,
        name="Inspect PDF package",
    )
    consumer = WorkflowStepFactory(
        workflow=workflow,
        validator=xml_validator,
        ruleset=ruleset,
        order=20,
        name="Validate selected XML",
    )
    output_port = pdf_validator.step_io_definitions.get(contract_key="selected_xml")
    input_port = xml_validator.step_io_definitions.get(contract_key="xml_document")
    set_artifact_input_binding(
        consumer_step=consumer,
        consumer_port=input_port,
        source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        artifact_reference=f"{producer.step_key}.{output_port.contract_key}",
    )

    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=workflow.project,
        user=workflow.user,
        file_type=SubmissionFileType.PDF,
    )
    submission.set_content(
        uploaded_file=SimpleUploadedFile(
            "composed-package.pdf",
            fixture_bytes,
            content_type="application/pdf",
        ),
        filename="composed-package.pdf",
        file_type=SubmissionFileType.PDF,
    )
    submission.save()
    run = ValidationRunFactory(submission=submission)
    producer_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=producer,
        step_order=producer.order,
        status=StepStatus.PASSED,
        validator_backend_image_digest=f"{PDF_IMAGE}@{image.id}",
    )
    consumer_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=consumer,
        step_order=consumer.order,
        status=StepStatus.PENDING,
    )

    tmp_path.chmod(0o777)
    attempt_dir = tmp_path / "runs" / str(run.pk) / "attempt"
    attempt_dir.mkdir(parents=True)
    envelope, input_path, output_path = _write_attempt_contract(
        attempt_dir=attempt_dir,
        fixture_bytes=fixture_bytes,
        run=run,
        producer_run=producer_run,
        pdf_validator=pdf_validator,
    )
    try:
        _run_pdf_container(
            client=client,
            workspace=tmp_path,
            input_path=input_path,
            output_path=output_path,
        )
    finally:
        client.close()

    output = PdfOutputEnvelope.model_validate_json(output_path.read_bytes())
    assert output.status == ValidationStatus.SUCCESS
    assert output.run_id == str(run.pk)
    assert output.step_run_id == str(producer_run.pk)
    assert output.validator.id == str(pdf_validator.pk)
    assert output.validator.version == str(pdf_validator.version)
    assert output.input_envelope_sha256 == sha256_hex_for_model(envelope)
    register_output_artifacts(step_run=producer_run, output_envelope=output)

    artifact = Artifact.objects.get(
        validation_run=run,
        workflow_step=producer,
        contract_key="selected_xml",
    )
    assert artifact.storage_uri.endswith("/outputs/selected.xml")
    assert artifact.size_bytes == len(EXPECTED_XML)
    assert artifact.sha256 == EXPECTED_XML_SHA256
    assert artifact.storage_version == f"sha256:{EXPECTED_XML_SHA256}"
    assert artifact.producer_validator_type == pdf_validator.validation_type
    assert artifact.producer_validator_version == str(pdf_validator.version)
    assert artifact.producer_backend_image_digest == f"{PDF_IMAGE}@{image.id}"

    observed: dict[str, bytes] = {}
    original_validate = XmlSchemaValidator.validate

    def capture_downstream_bytes(
        self,
        validator,
        submission: Submission,
        ruleset,
        run_context=None,
    ):
        """Record the resolver output before invoking the real XML validator."""
        observed["xml_document"] = run_context.resolved_file_inputs[
            "xml_document"
        ].content
        return original_validate(
            self,
            validator,
            submission,
            ruleset,
            run_context,
        )

    monkeypatch.setattr(XmlSchemaValidator, "validate", capture_downstream_bytes)
    result = SimpleValidationProcessor(run, consumer_run).execute()

    consumer_run.refresh_from_db()
    assert result.passed is True
    assert consumer_run.status == StepStatus.PASSED
    assert observed["xml_document"] == EXPECTED_XML
    assert submission.file_type == SubmissionFileType.PDF
    assert submission.read_bytes() == fixture_bytes
    assert submission.read_bytes() != observed["xml_document"]
    trace = consumer_run.input_traces.get(input_contract_key="xml_document")
    assert trace.resolved is True
    assert trace.value_snapshot == {
        "name": "selected.xml",
        "source": BindingSourceScope.UPSTREAM_ARTIFACT,
        "size_bytes": len(EXPECTED_XML),
        "sha256": EXPECTED_XML_SHA256,
        "storage_version": f"sha256:{EXPECTED_XML_SHA256}",
        "artifact_id": str(artifact.pk),
        "producer_step_key": producer.step_key,
        "producer_output_key": "selected_xml",
    }
