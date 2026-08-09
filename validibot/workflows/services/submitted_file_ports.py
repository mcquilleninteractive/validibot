"""Submitted-file artifact port discovery for workflow launches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium


@dataclass(frozen=True)
class SubmittedFilePortRequirement:
    """One launch-time file upload required by a submitted-file artifact port."""

    field_name: str
    workflow_step_id: str
    workflow_step_name: str
    port_key: str
    label: str
    required: bool
    accepted_extensions: tuple[str, ...]

    @property
    def accepted_extensions_display(self) -> str:
        """Human-readable extension list for form help text."""

        return ", ".join(f".{ext}" for ext in self.accepted_extensions)


def submitted_file_port_field_name(*, workflow_step_id, port_key: str) -> str:
    """Return the launch-form field name for a submitted artifact-port file."""

    return f"submitted_file_port__{workflow_step_id}__{port_key}"


def submitted_file_source_path(io_definition) -> str:
    """Return the primary or auxiliary launch-file identity for one input port."""

    if io_definition.envelope_channel == EnvelopeChannel.INPUT_FILES:
        return "primary"
    return io_definition.contract_key


def submitted_file_port_requirements(
    workflow,
    *,
    include_primary: bool = False,
) -> list[SubmittedFilePortRequirement]:
    """Return submitted-file artifact ports that need launch-page file fields.

    The historical primary submission field continues to satisfy primary model
    ports. This helper returns only extra submitted-file ports by default, such
    as EnergyPlus ``weather_file`` when the author selected "Submitted file".
    """

    from validibot.validations.models import StepInputBinding

    queryset = (
        StepInputBinding.objects.filter(
            workflow_step__workflow=workflow,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            io_definition__direction=StepIODirection.INPUT,
            io_definition__io_medium=StepIOMedium.ARTIFACT,
        )
        .select_related("workflow_step", "io_definition")
        .order_by("workflow_step__order", "io_definition__order", "pk")
    )

    requirements: list[SubmittedFilePortRequirement] = []
    for binding in queryset:
        io_definition = binding.io_definition
        if (
            not include_primary
            and submitted_file_source_path(io_definition) == "primary"
        ):
            continue

        step = binding.workflow_step
        label = io_definition.label or _humanize_port_key(io_definition.contract_key)
        field_name = submitted_file_port_field_name(
            workflow_step_id=step.pk,
            port_key=io_definition.contract_key,
        )
        requirements.append(
            SubmittedFilePortRequirement(
                field_name=field_name,
                workflow_step_id=str(step.pk),
                workflow_step_name=step.name or step.step_number_display,
                port_key=io_definition.contract_key,
                label=str(label),
                required=bool(binding.is_required or io_definition.min_items > 0),
                accepted_extensions=_accepted_extensions(io_definition),
            )
        )
    return requirements


def _accepted_extensions(io_definition) -> tuple[str, ...]:
    """Return lowercase extensions declared on the artifact port metadata."""

    metadata = io_definition.metadata or {}
    extensions = metadata.get("accepted_extensions") or []
    normalized = []
    for ext in extensions:
        value = str(ext or "").strip().lower().lstrip(".")
        if value:
            normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def _humanize_port_key(port_key: str):
    """Convert a stable port key into simple user-facing copy."""

    return _(slugify(port_key).replace("-", " ").title() or "Submitted file")


def uploaded_file_extension(uploaded_file) -> str:
    """Return a submitted upload's lowercase extension without the dot."""

    filename = getattr(uploaded_file, "name", "") or ""
    suffix = Path(filename).suffix.lower()
    return suffix[1:] if suffix.startswith(".") else suffix
