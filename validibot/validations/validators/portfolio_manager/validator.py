"""Django-side orchestration for the isolated Portfolio Manager backend."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError

from validibot.validations.constants import PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES
from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.portfolio_manager.output_groups import (
    GROUPED_PROPERTY_OUTPUT_KEYS,
)
from validibot.validations.validators.portfolio_manager.output_groups import (
    SINGLE_PROPERTY_OUTPUT_KEYS,
)


class PortfolioManagerValidator(AdvancedValidator):
    """Launch the first-party Portfolio Manager backend and expose its facts."""

    @property
    def validator_display_name(self) -> str:
        """Return the author-facing backend name used in shared errors."""
        return "Portfolio Manager"

    def preprocess_submission(self, *, step, submission) -> dict[str, object]:
        """Reject a mode/extension mismatch before spending container compute."""
        del submission
        resolved = self.resolve_file_input("benchmark_report", load_content=False)
        structure = (step.config or {}).get("submission_structure", "single_report")
        suffix = Path(resolved.name).suffix.casefold()
        expected = (
            {".zip"} if structure == "zip_collection" else {".xls", ".xlsx", ".xml"}
        )
        if suffix not in expected:
            if structure == "zip_collection":
                message = "ZIP collection mode requires one .zip submission."
            else:
                message = (
                    "Single-report mode requires a .xls, .xlsx, or .xml submission."
                )
            raise ValidationError(message)
        if resolved.identity.size_bytes > PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES:
            raise ValidationError(
                "Portfolio Manager submissions must be 500 MB or smaller."
            )
        return {"submission_structure": structure}

    def _resolve_input_stage_payload(self, submission) -> None:
        """Avoid replacement-text decoding of binary spreadsheet/archive bytes."""

    def extract_output_values(self, output_envelope: Any) -> dict[str, Any] | None:
        """Project typed backend facts into the catalog-controlled ``o.*`` surface."""
        outputs = getattr(output_envelope, "outputs", None)
        if outputs is None:
            return None
        values = {
            key: _json_number(getattr(outputs, key, None))
            for key in GROUPED_PROPERTY_OUTPUT_KEYS
        }
        record = (
            outputs.property_results[0] if len(outputs.property_results) == 1 else None
        )
        for key in SINGLE_PROPERTY_OUTPUT_KEYS:
            values[key] = _json_number(getattr(record, key, None)) if record else None
        return values


def _json_number(value: Any) -> Any:
    """Convert Decimal/date values to CEL- and JSON-safe scalar representations."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value
