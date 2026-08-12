"""Analyze and enforce typed validator input-port contracts.

Workflow admission describes what a submission may contain. A validator port
describes what one step can consume. This module keeps those two decisions
separate: authoring analysis is advisory, while execution checks the concrete
source selected for the concrete run and returns structured diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium

if TYPE_CHECKING:
    from validibot.validations.models import StepIODefinition


class InputDiagnosticCode(StrEnum):
    """Stable machine codes for input-contract failures."""

    REQUIRED_INPUT_MISSING = "required_input_missing"
    SOURCE_SCOPE_NOT_ALLOWED = "input_source_scope_not_allowed"
    PRIMARY_FILE_TYPE_INCOMPATIBLE = "input_file_type_incompatible"
    SUBMISSION_METADATA_NOT_JSON_OBJECT = "submission_metadata_not_json_object"
    INPUT_RESOLUTION_FAILED = "input_resolution_failed"


class InputCompatibilityLevel(StrEnum):
    """Static confidence that admitted primary types satisfy one input port."""

    COMPATIBLE = "compatible"
    POSSIBLY_COMPATIBLE = "possibly_compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class InputAdvisoryCode(StrEnum):
    """Stable authoring codes for primary-source compatibility guidance."""

    PRIMARY_SOURCE_COMPATIBLE = "primary_source_compatible"
    PRIMARY_SOURCE_PARTIAL_COVERAGE = "primary_source_partial_coverage"
    PRIMARY_SOURCE_INCOMPATIBLE = "primary_source_incompatible"
    PRIMARY_SOURCE_UNKNOWN = "primary_source_unknown"


@dataclass(frozen=True, slots=True)
class InputSourceDiagnostic:
    """Typed, presentation-neutral analysis of one primary-bound input port."""

    code: InputAdvisoryCode
    level: InputCompatibilityLevel
    contract_key: str
    source_scope: str
    expected_file_types: tuple[str, ...]
    admitted_file_types: tuple[str, ...]
    unsupported_file_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InputDiagnostic:
    """One actionable failure for one validator input port."""

    code: InputDiagnosticCode
    message: str
    contract_key: str
    source_scope: str = ""
    expected_file_types: tuple[str, ...] = ()
    actual_file_type: str = ""

    def as_meta(self) -> dict[str, object]:
        """Return stable finding metadata for APIs and run evidence."""

        return {
            "contract_key": self.contract_key,
            "source_scope": self.source_scope,
            "expected_file_types": list(self.expected_file_types),
            "actual_file_type": self.actual_file_type,
        }


class InputResolutionError(ValueError):
    """Aggregate one or more typed input-contract diagnostics."""

    def __init__(self, diagnostics: list[InputDiagnostic]):
        self.diagnostics = tuple(diagnostics)
        super().__init__("; ".join(item.message for item in diagnostics))


def primary_source_advisory(*, workflow, port: StepIODefinition) -> str:
    """Explain a statically incompatible primary-source selection.

    The result is intentionally advisory. A workflow may accept several file
    types and a run may legitimately fail when the selected concrete file does
    not satisfy a step. Authors retain that freedom.
    """

    diagnostic = analyze_primary_source(workflow=workflow, port=port)
    if diagnostic.level in {
        InputCompatibilityLevel.COMPATIBLE,
        InputCompatibilityLevel.UNKNOWN,
    }:
        return ""
    labels = _file_type_labels(diagnostic.expected_file_types)
    expected_display = ", ".join(labels)
    if diagnostic.level == InputCompatibilityLevel.POSSIBLY_COMPATIBLE:
        unsupported_display = ", ".join(
            _file_type_labels(diagnostic.unsupported_file_types)
        )
        return str(
            _(
                "This step expects %(types)s from Primary submission, but this "
                "workflow also accepts %(unsupported)s. %(unsupported)s "
                "submissions will fail at this step."
            )
            % {
                "types": expected_display,
                "unsupported": unsupported_display,
            }
        )
    if BindingSourceScope.UPSTREAM_ARTIFACT in (port.allowed_source_scopes or []):
        return str(
            _(
                "This validator requires %(types)s. Add %(types)s to the allowed "
                "file types or select an earlier step output that produces it."
            )
            % {"types": expected_display}
        )
    return str(
        _(
            "This validator requires %(types)s. Add %(types)s to the allowed "
            "file types or choose another input source."
        )
        % {"types": expected_display}
    )


def analyze_primary_source(
    *, workflow, port: StepIODefinition
) -> InputSourceDiagnostic:
    """Classify the workflow's admitted primary carriers for one input port."""

    expected = _normalized_file_types(port.accepted_file_types)
    admitted = _normalized_file_types(workflow.allowed_file_types or [])
    expected_set = set(expected)
    admitted_set = set(admitted)
    unsupported = tuple(value for value in admitted if value not in expected_set)
    if not expected_set or not admitted_set:
        level = InputCompatibilityLevel.UNKNOWN
        code = InputAdvisoryCode.PRIMARY_SOURCE_UNKNOWN
    elif admitted_set.issubset(expected_set):
        level = InputCompatibilityLevel.COMPATIBLE
        code = InputAdvisoryCode.PRIMARY_SOURCE_COMPATIBLE
    elif admitted_set.isdisjoint(expected_set):
        level = InputCompatibilityLevel.INCOMPATIBLE
        code = InputAdvisoryCode.PRIMARY_SOURCE_INCOMPATIBLE
    else:
        level = InputCompatibilityLevel.POSSIBLY_COMPATIBLE
        code = InputAdvisoryCode.PRIMARY_SOURCE_PARTIAL_COVERAGE
    return InputSourceDiagnostic(
        code=code,
        level=level,
        contract_key=port.contract_key,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        expected_file_types=expected,
        admitted_file_types=admitted,
        unsupported_file_types=unsupported,
    )


def validate_runtime_input_contracts(*, run, step) -> None:
    """Validate concrete bindings before validator or provider execution."""

    from validibot.validations.models import StepInputBinding
    from validibot.validations.services.artifact_bindings import (
        effective_artifact_ports,
    )

    bindings = {
        binding.io_definition_id: binding
        for binding in StepInputBinding.objects.filter(
            workflow_step=step
        ).select_related("io_definition")
    }
    diagnostics: list[InputDiagnostic] = []
    for port in effective_artifact_ports(step, direction=StepIODirection.INPUT):
        if port.io_medium != StepIOMedium.ARTIFACT:
            continue
        binding = bindings.get(port.pk)
        if binding is None:
            if port.min_items:
                diagnostics.append(
                    InputDiagnostic(
                        code=InputDiagnosticCode.REQUIRED_INPUT_MISSING,
                        message=str(
                            _("Required input '%(input)s' has no configured source.")
                            % {"input": port.label or port.contract_key}
                        ),
                        contract_key=port.contract_key,
                    )
                )
            continue
        diagnostics.extend(diagnostics_for_binding(run=run, port=port, binding=binding))
    if diagnostics:
        raise InputResolutionError(diagnostics)


def diagnostics_for_binding(*, run, port, binding) -> list[InputDiagnostic]:
    """Return runtime diagnostics for a single selected source."""

    source_scope = str(binding.source_scope or "")
    allowed_scopes = set(port.allowed_source_scopes or [])
    if source_scope not in allowed_scopes:
        return [
            InputDiagnostic(
                code=InputDiagnosticCode.SOURCE_SCOPE_NOT_ALLOWED,
                message=str(
                    _("Input '%(input)s' does not allow source '%(source)s'.")
                    % {
                        "input": port.label or port.contract_key,
                        "source": source_scope,
                    }
                ),
                contract_key=port.contract_key,
                source_scope=source_scope,
            )
        ]

    if (
        source_scope == BindingSourceScope.SUBMISSION_FILE
        and binding.source_data_path == "primary"
    ):
        expected = _normalized_file_types(port.accepted_file_types)
        actual = str(run.submission.file_type or "")
        if expected and actual not in expected:
            expected_display = ", ".join(_file_type_labels(expected))
            actual_display = (
                _file_type_labels((actual,))[0] if actual else str(_("unknown"))
            )
            return [
                InputDiagnostic(
                    code=InputDiagnosticCode.PRIMARY_FILE_TYPE_INCOMPATIBLE,
                    message=str(
                        _(
                            "Input '%(input)s' requires %(expected)s, but this "
                            "submission's primary file is %(actual)s."
                        )
                        % {
                            "input": port.label or port.contract_key,
                            "expected": expected_display,
                            "actual": actual_display,
                        }
                    ),
                    contract_key=port.contract_key,
                    source_scope=source_scope,
                    expected_file_types=expected,
                    actual_file_type=actual,
                )
            ]

    if source_scope == BindingSourceScope.SUBMISSION_METADATA and not isinstance(
        run.submission.metadata, dict
    ):
        return [
            InputDiagnostic(
                code=InputDiagnosticCode.SUBMISSION_METADATA_NOT_JSON_OBJECT,
                message=str(_("Submission metadata must be a JSON object.")),
                contract_key=port.contract_key,
                source_scope=source_scope,
                expected_file_types=(SubmissionFileType.JSON,),
            )
        ]
    return []


def generic_resolution_diagnostic(*, port, binding, message: str) -> InputDiagnostic:
    """Wrap a source-specific materialization failure in the shared shape."""

    return InputDiagnostic(
        code=InputDiagnosticCode.INPUT_RESOLUTION_FAILED,
        message=message,
        contract_key=port.contract_key,
        source_scope=str(binding.source_scope or ""),
    )


def _normalized_file_types(values) -> tuple[str, ...]:
    """Normalize a file-type sequence without losing declaration order."""

    return tuple(dict.fromkeys(str(value) for value in (values or []) if value))


def _file_type_labels(values: tuple[str, ...]) -> list[str]:
    """Return translated labels while tolerating plugin-defined values."""

    labels: list[str] = []
    for value in values:
        try:
            labels.append(str(SubmissionFileType(value).label))
        except ValueError:
            labels.append(str(value))
    return labels


__all__ = [
    "InputAdvisoryCode",
    "InputCompatibilityLevel",
    "InputDiagnostic",
    "InputDiagnosticCode",
    "InputResolutionError",
    "InputSourceDiagnostic",
    "analyze_primary_source",
    "diagnostics_for_binding",
    "generic_resolution_diagnostic",
    "primary_source_advisory",
    "validate_runtime_input_contracts",
]
