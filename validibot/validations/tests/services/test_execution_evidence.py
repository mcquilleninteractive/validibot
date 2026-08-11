"""Tests for launch-time execution evidence snapshots.

The snapshot is the durable bridge between a strict input envelope and the
later portable evidence manifest. These tests ensure it keeps exact file
identity and preprocessing provenance while excluding private storage URIs and
callback credentials from the database record.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import SupportedMimeType

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ENERGYPLUS_MODEL_TEMPLATE
from validibot.validations.services.execution_evidence import (
    build_input_evidence_snapshot,
)
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory

pytestmark = pytest.mark.django_db


def _envelope(
    *,
    sha256: str,
    uri: str = "file:///private/model.idf",
    port_key: str = "primary_model",
    role: str = "primary-model",
):
    """Build the minimal envelope-shaped object required by the projector."""
    return SimpleNamespace(
        context=SimpleNamespace(
            attempt_contract_version="validibot.attempt.v2",
            callback_nonce="raw-secret-that-must-not-persist",
        ),
        input_files=[
            InputFileItem(
                name="model.idf",
                mime_type=SupportedMimeType.ENERGYPLUS_IDF,
                role=role,
                port_key=port_key,
                uri=uri,
                size_bytes=123,
                sha256=sha256,
                storage_version=f"sha256:{sha256}",
            ),
        ],
        resource_files=[],
    )


def _set_original_submission_identity(submission, *, sha256: str) -> None:
    """Persist original metadata that preprocessing must not overwrite."""
    submission.checksum_sha256 = sha256
    submission.original_filename = "original.idf"
    submission.size_bytes = 123
    submission.save(
        update_fields=[
            "checksum_sha256",
            "original_filename",
            "size_bytes",
            "modified",
        ],
    )


class TestBuildInputEvidenceSnapshot:
    """Project strict envelope identity without persisting live capabilities."""

    def test_snapshot_keeps_identity_but_excludes_uri_and_callback_nonce(self):
        """A direct submission is linked to identical execution bytes safely."""
        digest = "a" * 64
        submission = SubmissionFactory()
        _set_original_submission_identity(submission, sha256=digest)
        step = WorkflowStepFactory(workflow=submission.workflow)
        private_uri = "file:///private/attempt/input/model.idf"

        snapshot = build_input_evidence_snapshot(
            _envelope(sha256=digest, uri=private_uri),
            submission=submission,
            step=step,
        )

        serialized = json.dumps(snapshot, sort_keys=True)
        assert snapshot["attempt_contract_version"] == "validibot.attempt.v2"
        assert snapshot["input_files"][0]["sha256"] == digest
        assert snapshot["input_relationships"][0]["relationship"] == "identical"
        assert private_uri not in serialized
        assert "raw-secret-that-must-not-persist" not in serialized
        assert "uri" not in snapshot["input_files"][0]

    def test_primary_relationship_uses_the_declared_port_not_the_role(self):
        """Descriptive labels cannot reclassify evidence-bound input bytes."""
        digest = "a" * 64
        submission = SubmissionFactory()
        _set_original_submission_identity(submission, sha256=digest)
        step = WorkflowStepFactory(workflow=submission.workflow)

        exact = build_input_evidence_snapshot(
            _envelope(sha256=digest, role="unrelated-description"),
            submission=submission,
            step=step,
        )
        unrelated = build_input_evidence_snapshot(
            _envelope(
                sha256=digest,
                port_key="weather_file",
                role="primary-model",
            ),
            submission=submission,
            step=step,
        )

        assert exact["input_relationships"][0]["target_port_key"] == "primary_model"
        assert unrelated["input_relationships"] == []

    def test_template_snapshot_records_both_original_sources_and_executed_bytes(self):
        """Template parameters and template file both point to the generated IDF."""
        submission_digest = "a" * 64
        executed_digest = "b" * 64
        submission = SubmissionFactory()
        _set_original_submission_identity(submission, sha256=submission_digest)
        step = WorkflowStepFactory(workflow=submission.workflow)
        template = WorkflowStepResourceFactory(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            validator_resource_file=None,
            step_resource_file=SimpleUploadedFile(
                "template.idf",
                b"Version,24.2;\nZone,$ZONE_NAME;\n",
            ),
            filename="template.idf",
            resource_type=ENERGYPLUS_MODEL_TEMPLATE,
        )
        template_file = template.step_resource_file
        try:
            # Mirror preprocessing: mutate the in-memory object only. The helper
            # must still read the persisted original filename and digest.
            submission.content = "Version,24.2;\nZone,Office;\n"
            submission.original_filename = "resolved_model.idf"

            snapshot = build_input_evidence_snapshot(
                _envelope(sha256=executed_digest),
                submission=submission,
                step=step,
            )
        finally:
            template_file.delete(save=False)

        relationships = snapshot["input_relationships"]
        assert len(relationships) == 2  # noqa: PLR2004
        submission_relationship = next(
            item for item in relationships if item["source_kind"] == "submission"
        )
        template_relationship = next(
            item for item in relationships if item["source_kind"] == "workflow_resource"
        )
        assert submission_relationship["source_name"] == "original.idf"
        assert submission_relationship["source_sha256"] == submission_digest
        assert submission_relationship["target_sha256"] == executed_digest
        assert submission_relationship["relationship"] == "transformed"
        assert (
            submission_relationship["transformation"]
            == "energyplus-template-substitution.v1"
        )
        assert template_relationship["source_sha256"] == template.content_hash
        assert template_relationship["target_sha256"] == executed_digest
