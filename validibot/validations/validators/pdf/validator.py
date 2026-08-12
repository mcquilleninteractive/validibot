"""Django-side orchestration for isolated PDF package inspection."""

from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError

from validibot.validations.validators.base.advanced import AdvancedValidator

PDF_MAX_SUBMISSION_BYTES = 250_000_000


class PdfValidator(AdvancedValidator):
    """Launch the PDF backend and expose its bounded summary values."""

    @property
    def validator_display_name(self) -> str:
        """Return the name used in shared execution errors."""
        return "PDF package"

    def preprocess_submission(self, *, step, submission) -> dict[str, object]:
        """Reject non-PDF carriers before spending isolated compute."""
        del step, submission
        resolved = self.resolve_file_input("pdf_document", load_content=False)
        if Path(resolved.name).suffix.casefold() != ".pdf":
            raise ValidationError("PDF Package Validator requires one .pdf file.")
        if resolved.identity.size_bytes > PDF_MAX_SUBMISSION_BYTES:
            raise ValidationError("PDF submissions must be 250 MB or smaller.")
        return {}

    def _resolve_input_stage_payload(self, submission) -> None:
        """Avoid replacement-text decoding of binary PDF bytes."""

    def extract_output_values(self, output_envelope: Any) -> dict[str, Any] | None:
        """Project the typed backend summary into the catalog-controlled surface."""
        outputs = getattr(output_envelope, "outputs", None)
        if outputs is None:
            return None
        return {
            "passed": outputs.passed,
            "member_count": outputs.member_count,
            "finding_summary": dict(outputs.finding_summary),
        }
