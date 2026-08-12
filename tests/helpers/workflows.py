"""Production-shaped workflow helpers for end-to-end tests.

Ordinary unit tests may construct deliberately partial model graphs. End-to-end
tests need a different guarantee: a workflow step must carry the same default
input bindings created by the application authoring service, otherwise a fresh
database can bypass the declared file-port contract or fail before dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.services.custom_validator_contracts import (
    sync_configured_io_contract,
)
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.workflows.tests.factories import WorkflowStepFactory

if TYPE_CHECKING:
    from validibot.workflows.models import WorkflowStep


def create_workflow_step_with_default_bindings(**step_fields: Any) -> WorkflowStep:
    """Create a test step and enforce the production input-binding invariant.

    ``WorkflowStepFactory`` intentionally remains a low-level model factory so
    isolated unit tests can control every related row. Full workflow tests use
    this helper to mirror ``save_workflow_step()`` after persistence: every
    config-managed input receives an explicit ``StepInputBinding`` before the
    workflow can launch.
    """
    validator = step_fields.get("validator")
    if validator is not None:
        sync_configured_io_contract(validator=validator)
    step = WorkflowStepFactory(**step_fields)
    ensure_step_input_bindings(step)
    return step
