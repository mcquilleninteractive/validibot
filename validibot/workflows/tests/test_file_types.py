"""Verify workflow MIME mappings for explicit submission file types.

The workflow launch surfaces use these mappings to translate HTTP content
types into logical workflow contracts and to preserve safe filename suffixes.
"""

from validibot.submissions.constants import SubmissionFileType
from validibot.workflows.constants import SUPPORTED_CONTENT_TYPES
from validibot.workflows.constants import preferred_content_type_for_file


def test_application_pdf_maps_to_the_pdf_workflow_type() -> None:
    """API and web uploads must classify application/pdf narrowly as PDF."""
    assert SUPPORTED_CONTENT_TYPES["application/pdf"] == SubmissionFileType.PDF


def test_pdf_file_type_prefers_application_pdf() -> None:
    """PDF submissions must carry their precise MIME type into safe ingest."""
    assert (
        preferred_content_type_for_file(
            SubmissionFileType.PDF,
            filename="package.pdf",
        )
        == "application/pdf"
    )
