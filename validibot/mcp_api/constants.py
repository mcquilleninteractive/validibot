"""Error-code constants for the legacy Community agent HTTP adapter.

These are the machine-readable ``code`` values in retained DRF error payloads.
Cloud's agent endpoints use a
parallel ``AgentRunErrorCode`` enum in ``validibot_cloud.agents.constants``
for x402-specific failures.
"""

from __future__ import annotations

from enum import StrEnum


class MCPHelperErrorCode(StrEnum):
    """Machine-readable error codes for the MCP helper API."""

    INVALID_PARAMS = "INVALID_PARAMS"
    NOT_FOUND = "NOT_FOUND"
