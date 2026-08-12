"""Tests for stable, encrypted MCP workflow and run references.

References are the join keys used across separate model tool calls. These tests
protect stability while ensuring the wire values do not disclose internal
routing fields or accept tampering and cross-kind substitution.
"""

import pytest

from validibot.mcp_server.references import build_run_reference
from validibot.mcp_server.references import build_workflow_reference
from validibot.mcp_server.references import parse_run_reference
from validibot.mcp_server.references import parse_workflow_reference
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def test_workflow_reference_is_stable_and_hides_routing_fields() -> None:
    """Agents need a repeatable handle without receiving tenant identifiers."""

    workflow = WorkflowFactory()

    first = build_workflow_reference(workflow)
    second = build_workflow_reference(workflow)

    assert first == second
    assert workflow.org.slug not in first
    assert workflow.slug not in first
    assert parse_workflow_reference(first) == (workflow.org.slug, workflow.slug)


def test_run_reference_round_trips_without_disclosing_primary_key() -> None:
    """Run polling must not expose a raw database UUID in the model context."""

    validation_run = ValidationRunFactory()

    reference = build_run_reference(validation_run)

    assert str(validation_run.pk) not in reference
    assert parse_run_reference(reference) == str(validation_run.pk)


def test_reference_tampering_and_cross_kind_use_are_rejected() -> None:
    """Modified or context-confused references must fail authentication."""

    validation_run = ValidationRunFactory()
    reference = build_run_reference(validation_run)
    replacement = "A" if reference[-1] != "A" else "B"

    with pytest.raises(ValueError, match="Reference is invalid"):
        parse_run_reference(f"{reference[:-1]}{replacement}")
    with pytest.raises(ValueError, match="Reference is invalid"):
        parse_workflow_reference(reference.replace("run_", "wf_", 1))
