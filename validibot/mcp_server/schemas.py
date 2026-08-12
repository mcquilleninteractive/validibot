"""Typed input-independent output contracts for Validibot MCP tools."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class MCPModel(BaseModel):
    """Base model that rejects accidental contract expansion."""

    model_config = ConfigDict(extra="forbid")


class WorkflowSummary(MCPModel):
    """Small workflow record returned during discovery."""

    workflow_ref: str
    name: str
    description: str
    version: int
    allowed_file_types: list[str]


class WorkflowListResult(MCPModel):
    """Bounded workflow discovery page."""

    workflows: list[WorkflowSummary]
    next_cursor: str | None = None


class WorkflowStepSummary(MCPModel):
    """User-relevant description of one workflow step."""

    order: int
    name: str
    description: str
    operation: str


class WorkflowDetailResult(WorkflowSummary):
    """Launch guidance for one accessible workflow."""

    steps: list[WorkflowStepSummary]


class ValidationRunResult(MCPModel):
    """Privacy-minimizing status for one validation run."""

    run_ref: str
    workflow_ref: str
    status: str
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    total_findings: int = 0
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    findings_available: bool
    next_action: str


class StartValidationResult(ValidationRunResult):
    """Launch result that tells retrying clients whether work was replayed."""

    idempotency_replayed: bool


class ValidationFindingResult(MCPModel):
    """One bounded, model-safe validation finding."""

    severity: str
    code: str
    message: str
    path: str
    step_name: str


class ValidationFindingListResult(MCPModel):
    """Page of findings for an accessible validation run."""

    run_ref: str
    findings: list[ValidationFindingResult]
    next_cursor: str | None = None


class StartValidationInput(MCPModel):
    """Documented launch contract used for hashing and direct service calls."""

    workflow_ref: str = Field(min_length=4, max_length=1024)
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    file_content: bytes = Field(min_length=1, repr=False)
    idempotency_key: str = Field(min_length=1, max_length=255)


class OpenAIFileInput(MCPModel):
    """OpenAI's prescribed top-level file parameter object."""

    download_url: str = Field(min_length=1)
    file_id: str = Field(min_length=1, max_length=255)
    mime_type: str | None = Field(default=None, max_length=255)
    file_name: str | None = Field(default=None, max_length=255)
