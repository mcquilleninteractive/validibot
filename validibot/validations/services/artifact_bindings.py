"""Authoring and integrity services for cross-step artifact bindings.

Artifact ports are typed file interfaces. A workflow binding is a protected
edge from one earlier output port to one later input port. This module owns the
portable reference syntax, compatibility checks, selectable authoring choices,
and whole-workflow validation so individual validators never implement private
versions of that graph logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.services.artifact_ports import validate_source_scope

if TYPE_CHECKING:
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition
    from validibot.workflows.models import Workflow
    from validibot.workflows.models import WorkflowStep


PORTABLE_ARTIFACT_REFERENCE_RE = re.compile(
    r"^(?P<step_key>[A-Za-z0-9_-]+)\.(?P<contract_key>[A-Za-z0-9_-]+)$"
)


@dataclass(frozen=True)
class ArtifactBindingChoice:
    """One compatible earlier output shown in a file-source selector."""

    reference: str
    step_id: int
    step_key: str
    step_label: str
    output_definition_id: int
    output_contract_key: str
    output_label: str
    data_format: str
    media_type: str
    optional: bool

    @property
    def label(self) -> str:
        """Return concise domain-language copy for a dropdown option."""
        return f"{self.step_label} · {self.output_label}"


def format_artifact_reference(*, step_key: str, contract_key: str) -> str:
    """Return the public ``step_key.contract_key`` artifact reference."""
    reference = f"{step_key}.{contract_key}"
    parse_artifact_reference(reference)
    return reference


def parse_artifact_reference(reference: str) -> tuple[str, str]:
    """Parse one portable artifact reference or raise a field-safe error."""
    normalized = str(reference or "").strip()
    match = PORTABLE_ARTIFACT_REFERENCE_RE.fullmatch(normalized)
    if match is None:
        raise ValidationError(
            "Use an earlier-step artifact reference in the form 'step_key.output_key'."
        )
    return match.group("step_key"), match.group("contract_key")


def effective_artifact_ports(
    step: WorkflowStep,
    *,
    direction: str,
) -> list[StepIODefinition]:
    """Return unambiguous step-owned and validator-owned artifact ports.

    A duplicate effective key would make portable references and backend roles
    ambiguous, so fail closed instead of silently preferring one owner.
    """
    from validibot.validations.models import StepIODefinition

    owner_filter = Q(workflow_step=step)
    if step.validator_id:
        owner_filter |= Q(validator_id=step.validator_id)
    ports = list(
        StepIODefinition.objects.filter(
            owner_filter,
            direction=direction,
            io_medium=StepIOMedium.ARTIFACT,
        ).order_by("order", "pk")
    )
    seen: dict[str, StepIODefinition] = {}
    for port in ports:
        previous = seen.get(port.contract_key)
        if previous is not None:
            raise ValidationError(
                "Step "
                f"'{step.name or step.step_key}' has more than one artifact "
                f"{direction} named '{port.contract_key}'."
            )
        seen[port.contract_key] = port
    return ports


def artifact_ports_compatible(
    *,
    producer: StepIODefinition,
    consumer: StepIODefinition,
) -> tuple[bool, str]:
    """Return whether one singleton output can satisfy one singleton input."""
    if producer.direction != StepIODirection.OUTPUT:
        return False, "The producer contract is not an output."
    if consumer.direction != StepIODirection.INPUT:
        return False, "The consumer contract is not an input."
    if (
        producer.io_medium != StepIOMedium.ARTIFACT
        or consumer.io_medium != StepIOMedium.ARTIFACT
    ):
        return False, "Both contracts must carry file artifacts."
    if BindingSourceScope.UPSTREAM_ARTIFACT not in (
        consumer.allowed_source_scopes or []
    ):
        return False, "The consumer does not allow earlier-step outputs."
    if producer.is_collection or consumer.is_collection:
        return False, "Collection-valued artifact bindings are not supported yet."
    if producer.max_items not in {None, 1} or consumer.max_items not in {None, 1}:
        return False, "Only singleton artifact ports can be connected."

    producer_kind = str(producer.artifact_kind or ArtifactKind.FILE)
    consumer_kind = str(consumer.artifact_kind or ArtifactKind.FILE)
    if producer_kind != consumer_kind and ArtifactKind.OTHER not in {
        producer_kind,
        consumer_kind,
    }:
        return False, "Artifact kinds are incompatible."

    producer_formats = _declared_output_values(
        primary=producer.data_format,
        accepted=producer.accepted_data_formats,
    )
    consumer_formats = _declared_input_values(
        primary=consumer.data_format,
        accepted=consumer.accepted_data_formats,
    )
    if consumer_formats and not producer_formats:
        return False, "The producer does not declare a data format."
    if (
        producer_formats
        and consumer_formats
        and not (producer_formats & consumer_formats)
    ):
        return False, "Data formats are incompatible."

    producer_media = _declared_output_values(
        primary=producer.media_type,
        accepted=producer.accepted_media_types,
    )
    consumer_media = _declared_input_values(
        primary=consumer.media_type,
        accepted=consumer.accepted_media_types,
    )
    if consumer_media and not producer_media:
        return False, "The producer does not declare a media type."
    if producer_media and consumer_media and not (producer_media & consumer_media):
        return False, "Media types are incompatible."
    return True, ""


def compatible_artifact_choices(
    *,
    consumer_step: WorkflowStep | None,
    consumer_port: StepIODefinition,
    workflow: Workflow,
    proposed_order: int | None = None,
) -> list[ArtifactBindingChoice]:
    """Return compatible artifact outputs from every earlier workflow step."""
    from validibot.workflows.models import WorkflowStep

    consumer_order = proposed_order
    if consumer_order is None and consumer_step is not None:
        consumer_order = consumer_step.order

    steps = WorkflowStep.objects.filter(workflow=workflow).select_related("validator")
    if consumer_order is not None:
        steps = steps.filter(order__lt=consumer_order)
    if consumer_step is not None and consumer_step.pk:
        steps = steps.exclude(pk=consumer_step.pk)

    choices: list[ArtifactBindingChoice] = []
    for producer_step in steps.order_by("order", "pk"):
        if not producer_step.step_key:
            continue
        for producer_port in effective_artifact_ports(
            producer_step,
            direction=StepIODirection.OUTPUT,
        ):
            compatible, _reason = artifact_ports_compatible(
                producer=producer_port,
                consumer=consumer_port,
            )
            if not compatible:
                continue
            choices.append(
                ArtifactBindingChoice(
                    reference=format_artifact_reference(
                        step_key=producer_step.step_key,
                        contract_key=producer_port.contract_key,
                    ),
                    step_id=producer_step.pk,
                    step_key=producer_step.step_key,
                    step_label=producer_step.name or producer_step.step_key,
                    output_definition_id=producer_port.pk,
                    output_contract_key=producer_port.contract_key,
                    output_label=producer_port.label or producer_port.contract_key,
                    data_format=str(producer_port.data_format or ""),
                    media_type=str(producer_port.media_type or ""),
                    optional=producer_port.min_items == 0,
                )
            )
    return choices


def resolve_artifact_reference(
    *,
    workflow: Workflow,
    reference: str,
) -> tuple[WorkflowStep, StepIODefinition]:
    """Resolve a portable reference to one effective output port."""
    from validibot.workflows.models import WorkflowStep

    step_key, contract_key = parse_artifact_reference(reference)
    try:
        source_step = WorkflowStep.objects.select_related("validator").get(
            workflow=workflow,
            step_key=step_key,
        )
    except WorkflowStep.DoesNotExist as exc:
        raise ValidationError(
            f"Earlier workflow step '{step_key}' was not found."
        ) from exc

    matches = [
        port
        for port in effective_artifact_ports(
            source_step,
            direction=StepIODirection.OUTPUT,
        )
        if port.contract_key == contract_key
    ]
    if len(matches) != 1:
        raise ValidationError(
            f"Artifact output '{reference}' does not identify one output contract."
        )
    return source_step, matches[0]


@transaction.atomic
def set_artifact_input_binding(
    *,
    consumer_step: WorkflowStep,
    consumer_port: StepIODefinition,
    source_scope: str,
    artifact_reference: str = "",
    source_data_path: str = "",
) -> StepInputBinding:
    """Create or update one file input binding through the shared contract."""
    from validibot.validations.models import StepInputBinding

    validate_source_scope(consumer_port, source_scope)
    defaults = {
        "source_scope": source_scope,
        "default_value": None,
        "is_required": consumer_port.min_items > 0,
        "source_step": None,
        "source_output_io_definition": None,
    }
    if source_scope == BindingSourceScope.UPSTREAM_ARTIFACT:
        source_step, source_output = resolve_artifact_reference(
            workflow=consumer_step.workflow,
            reference=artifact_reference,
        )
        compatible, reason = artifact_ports_compatible(
            producer=source_output,
            consumer=consumer_port,
        )
        if not compatible:
            raise ValidationError(reason)
        defaults.update(
            {
                "source_step": source_step,
                "source_output_io_definition": source_output,
                "source_data_path": format_artifact_reference(
                    step_key=source_step.step_key,
                    contract_key=source_output.contract_key,
                ),
            }
        )
    elif source_scope == BindingSourceScope.SUBMISSION_FILE:
        defaults["source_data_path"] = source_data_path or "primary"
    else:
        defaults["source_data_path"] = source_data_path

    binding, _created = StepInputBinding.objects.update_or_create(
        workflow_step=consumer_step,
        io_definition=consumer_port,
        defaults=defaults,
    )
    binding.full_clean()
    binding.save()
    return binding


def validate_workflow_dependencies(
    workflow: Workflow,
    *,
    proposed_order: dict[int, int] | None = None,
) -> None:
    """Validate every persisted artifact edge in one workflow definition."""
    from validibot.validations.models import StepInputBinding

    bindings = (
        StepInputBinding.objects.filter(
            workflow_step__workflow_id=workflow.pk,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
        )
        .select_related(
            "workflow_step",
            "io_definition",
            "source_step",
            "source_output_io_definition",
        )
        .order_by("workflow_step__order", "pk")
    )
    errors: list[str] = []
    order_overrides = proposed_order or {}
    for binding in bindings:
        try:
            binding.full_clean()
        except ValidationError as exc:
            messages = getattr(exc, "messages", [str(exc)])
            errors.extend(
                f"{binding.workflow_step.name or binding.workflow_step.step_key}: "
                f"{message}"
                for message in messages
            )
            continue

        producer = binding.source_step
        output = binding.source_output_io_definition
        prefix = binding.workflow_step.name or binding.workflow_step.step_key
        if producer is None or output is None:
            errors.append(f"{prefix}: The artifact producer is incomplete.")
            continue
        producer_order = order_overrides.get(producer.pk, producer.order)
        consumer_order = order_overrides.get(
            binding.workflow_step_id,
            binding.workflow_step.order,
        )
        if producer_order >= consumer_order:
            errors.append(
                f"{prefix}: '{producer.name}' must remain before "
                f"'{binding.workflow_step.name}'."
            )
            continue
        compatible, reason = artifact_ports_compatible(
            producer=output,
            consumer=binding.io_definition,
        )
        if not compatible:
            errors.append(f"{prefix}: {reason}")
    if errors:
        raise ValidationError(errors)


def _normalized_values(values) -> set[str]:
    """Normalize TextChoices, Enum, and string contract values."""
    return {
        str(getattr(value, "value", value) or "").strip().lower()
        for value in values or []
        if str(getattr(value, "value", value) or "").strip()
    }


def _declared_output_values(*, primary, accepted) -> set[str]:
    """Return the formats a producer promises it can emit."""
    values = _normalized_values([primary])
    return values or _normalized_values(accepted)


def _declared_input_values(*, primary, accepted) -> set[str]:
    """Return the formats a consumer accepts."""
    values = _normalized_values(accepted)
    return values or _normalized_values([primary])
