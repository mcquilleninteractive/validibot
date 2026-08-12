"""Typed file-input fixtures for validator unit and integration tests.

Validators consume bytes only through their declared artifact input ports.  These
helpers let focused engine tests exercise that same boundary without building a
complete workflow run and storage graph for every assertion-level scenario.
"""

from __future__ import annotations

from typing import Any

from validibot.actions.protocols import RunContext
from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.services.file_identity import local_bytes_identity
from validibot.validations.services.resolved_files import ResolvedFileInput

_DATA_FORMAT_BY_FILE_TYPE = {
    SubmissionFileType.JSON: SubmissionDataFormat.JSON,
    SubmissionFileType.XML: SubmissionDataFormat.XML,
    SubmissionFileType.TEXT: SubmissionDataFormat.TEXT,
    SubmissionFileType.YAML: SubmissionDataFormat.YAML,
    SubmissionFileType.PDF: SubmissionDataFormat.PDF,
    SubmissionFileType.BINARY: "",
}

_MEDIA_TYPE_BY_FILE_TYPE = {
    SubmissionFileType.JSON: "application/json",
    SubmissionFileType.XML: "application/xml",
    SubmissionFileType.TEXT: "text/plain",
    SubmissionFileType.YAML: "application/yaml",
    SubmissionFileType.PDF: "application/pdf",
    SubmissionFileType.BINARY: "application/octet-stream",
}

_EXTENSION_BY_FILE_TYPE = {
    SubmissionFileType.JSON: ".json",
    SubmissionFileType.XML: ".xml",
    SubmissionFileType.TEXT: ".txt",
    SubmissionFileType.YAML: ".yaml",
    SubmissionFileType.PDF: ".pdf",
    SubmissionFileType.BINARY: ".bin",
}


def resolved_file_input(
    *,
    contract_key: str,
    content: str | bytes,
    file_type: str,
    data_format: str | None = None,
    name: str | None = None,
) -> ResolvedFileInput:
    """Build one immutable, content-addressed artifact-port value."""
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    resolved_name = name or f"submission{_EXTENSION_BY_FILE_TYPE.get(file_type, '')}"
    uri = f"memory:{contract_key}/{resolved_name}"
    return ResolvedFileInput(
        contract_key=contract_key,
        name=resolved_name,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary",
        data_format=data_format or _DATA_FORMAT_BY_FILE_TYPE.get(file_type, ""),
        media_type=_MEDIA_TYPE_BY_FILE_TYPE.get(file_type, ""),
        file_type=file_type,
        identity=local_bytes_identity(content=content_bytes, uri=uri),
        content=content_bytes,
    )


def run_context_with_file(
    *,
    contract_key: str,
    content: str | bytes,
    file_type: str,
    data_format: str | None = None,
    name: str | None = None,
    **context_values: Any,
) -> RunContext:
    """Build a run context whose selected input port contains exact bytes."""
    return RunContext(
        resolved_file_inputs={
            contract_key: resolved_file_input(
                contract_key=contract_key,
                content=content,
                file_type=file_type,
                data_format=data_format,
                name=name,
            )
        },
        **context_values,
    )
