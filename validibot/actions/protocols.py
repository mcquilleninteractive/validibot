from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

if TYPE_CHECKING:
    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidatorExecutionDeployment
    from validibot.workflows.models import WorkflowStep


@dataclass
class RunContext:
    """
    Context passed to step handlers and validators during execution.

    This dataclass provides execution context for both workflow step handlers
    (via StepHandler.execute()) and validators (via BaseValidator.validate()).

    For step handlers, all fields are required. For validators, the context
    is optional - simple validators (XML, JSON, Basic, AI) typically don't
    need it, while advanced validators (EnergyPlus, FMU) require it for
    job tracking.

    Attributes:
        validation_run: The ValidationRun model instance being executed.
        step: The WorkflowStep model instance being processed.
        upstream_steps: Canonical input and output values from completed
            workflow steps, keyed by stable step key. Each entry contains
            ``{"input": {...}, "output": {...}, "artifact": {...}}`` and is
            used for CEL cross-step assertions such as
            ``steps.<step_key>.output.<name>`` and data-plane references such
            as ``steps.<step_key>.artifact.<contract_key>``.
        workflow_signals: Author-defined signals resolved from the
            workflow-level signal mapping configuration. Populated once
            at run start from ``WorkflowSignalMapping`` rows resolved
            against the submission data. Available in CEL expressions
            via ``s.<name>`` (or ``signal.<name>``).
        workflow_constants: Author-defined Constants (the ``c.*`` /
            ``const.*`` namespace) — a literal ``{name: value}`` map built
            once from the workflow's ``WorkflowConstant`` rows (ADR-2026-06-18).
            Unlike signals these need no submission data and never "resolve":
            a constant is workflow-definition-derived. Available in CEL as
            ``c.<name>`` and, in Basic assertions, as a nested ``c``/``const``
            sub-dict of the enriched payload.
        step_input_contract_values: The merged contract-keyed step input
            dict for the current step, populated at the start of the
            input stage from (a) parser-extracted facts via
            ``extract_input_values()`` and (b) resolved
            ``StepInputBinding`` rows. Consumed by ``_build_cel_context``
            to populate the ``i.*`` namespace. Per ADR-2026-05-22, both
            sources feed the same namespace; bindings take precedence
            because they represent explicit author intent.
        execution_attempt: Durable identity allocated before provider contact
            for an advanced validator step.
        execution_deployment: Exact managed-provider route pinned to the
            attempt. Empty for local and self-hosted execution.
        resolved_file_inputs: Exact bounded files supplied through declared
            artifact input ports, keyed by input contract key.
    """

    validation_run: ValidationRun | None = None
    step: WorkflowStep | None = None
    upstream_steps: dict[str, Any] = field(default_factory=dict)
    workflow_signals: dict[str, Any] = field(default_factory=dict)
    workflow_constants: dict[str, Any] = field(default_factory=dict)
    step_input_contract_values: dict[str, Any] = field(default_factory=dict)
    resolved_file_inputs: dict[str, Any] = field(default_factory=dict)
    execution_attempt: ExecutionAttempt | None = None
    execution_deployment: ValidatorExecutionDeployment | None = None


@dataclass
class StepResult:
    """Standardized result from any step execution (validator or action)."""

    passed: bool | None  # None = Async Pending
    issues: list[Any] = field(default_factory=list)  # ValidationIssue list
    stats: dict[str, Any] = field(default_factory=dict)
    output_values: dict[str, Any] = field(default_factory=dict)


class StepHandler(Protocol):
    """Protocol for any class that handles the execution of a workflow step."""

    def execute(
        self,
        run_context: RunContext,
    ) -> StepResult:
        """
        Execute the business logic for this step.

        Args:
            run_context: The run context containing the step, run, and signals.

        Returns:
            StepResult indicating success/failure and any outputs.
        """
        ...
