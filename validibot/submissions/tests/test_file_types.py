"""Protect the canonical relationship between submission carriers and formats.

These tests keep author-facing file types, filename detection, and
domain-specific data formats aligned. PDF is intentionally its own carrier so
a workflow can accept PDF documents without also accepting arbitrary binaries.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import data_format_allowed_file_types
from validibot.submissions.models import detect_file_type


def test_pdf_filename_detects_the_explicit_pdf_file_type() -> None:
    """A PDF upload must not widen the workflow contract to generic binary."""
    assert detect_file_type(filename="engineering-package.PDF") == (
        SubmissionFileType.PDF
    )


def test_pdf_data_format_uses_only_the_pdf_file_type() -> None:
    """PDF validator compatibility must derive the narrow PDF carrier type."""
    assert data_format_allowed_file_types(SubmissionDataFormat.PDF) == [
        SubmissionFileType.PDF,
    ]
