"""
THERM validator.

Parses THMX/THMZ files, runs domain checks, and extracts output values
for downstream assertion evaluation. Does NOT run THERM simulations.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.simple import SimpleValidator
from validibot.validations.validators.therm.boundaries import run_boundary_checks
from validibot.validations.validators.therm.geometry import run_geometry_checks
from validibot.validations.validators.therm.materials import run_material_checks
from validibot.validations.validators.therm.output_values import extract_output_values
from validibot.validations.validators.therm.parser import parse_therm_file

if TYPE_CHECKING:
    from validibot.submissions.models import Submission
    from validibot.validations.validators.therm.models import ThermModel

logger = logging.getLogger(__name__)


class ThermValidator(SimpleValidator):
    """
    THERM thermal analysis file validator.

    Validates THMX and THMZ files by parsing their XML structure,
    running domain checks, and extracting structured output values for
    use in downstream assertion evaluation.

    This is a parser and checker only — it does not run THERM
    simulations or compute U-factors.

    **No ``extract_input_values`` override yet (per ADR-2026-05-22b
    Phase 6).** Because THERM is a SimpleValidator that finishes
    inline, dispatch-gating from ``i.*`` saves no compute, so the
    pattern's primary motivation (avoid paying for simulation when
    we already know a precondition fails) doesn't apply. Once
    ``output_values.extract_output_values`` is implemented (currently a stub),
    file-metadata facts could be split into ``i.*`` (e.g.,
    ``therm_version``, ``has_glazing_system``) and parsed values
    into ``o.*``. Deferred until ``extract_output_values`` ships its
    initial pass.
    """

    def validate_file_type(
        self,
        submission: Submission,
    ) -> ValidationIssue | None:
        """Accept XML (THMX) and BINARY (THMZ) submissions."""
        if self._resolved_therm_file() is None:
            return ValidationIssue(
                path="therm_model",
                message="The THERM model input was not resolved.",
                severity=Severity.ERROR,
                code="required_input_missing",
            )
        return None

    def parse_content(self, submission: Submission) -> ThermModel:
        """Parse the resolved THMX/THMZ bytes into a ``ThermModel``."""
        resolved_file = self._resolved_therm_file()
        if resolved_file is None or not resolved_file.content:
            msg = "Resolved THERM model is empty."
            raise ValueError(msg)
        content = resolved_file.content
        filename = resolved_file.name

        return parse_therm_file(content, filename=filename)

    def run_domain_checks(
        self,
        parsed: ThermModel,
    ) -> list[ValidationIssue]:
        """Run all THERM domain checks."""
        issues: list[ValidationIssue] = []
        issues.extend(run_geometry_checks(parsed.polygons))
        issues.extend(run_material_checks(parsed.materials))
        issues.extend(
            run_boundary_checks(
                parsed.polygons,
                parsed.materials,
                parsed.boundary_conditions,
            ),
        )
        return issues

    def extract_output_values(self, parsed: ThermModel) -> dict[str, Any]:
        """Extract output values from the parsed ThermModel."""
        return extract_output_values(parsed)

    def _resolved_therm_file(self):
        """Return the runtime-resolved model, independent of its original source."""
        run_context = getattr(self, "run_context", None)
        if run_context is None:
            return None
        return (run_context.resolved_file_inputs or {}).get("therm_model")
