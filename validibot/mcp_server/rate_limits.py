"""Small fixed-window limits for authenticated MCP tool calls."""

from __future__ import annotations

import time

from django.conf import settings

from validibot.core.rate_limit_counters import increment_rate_limit_counter
from validibot.mcp_server.constants import MCPErrorCode
from validibot.mcp_server.exceptions import MCPApplicationError


def enforce_principal_rate_limit(*, user_id: int, operation: str) -> None:
    """Bound calls per principal without adding a new infrastructure service."""

    bucket = "start" if operation == "start_validation" else "read"
    default_limit = 20 if bucket == "start" else 120
    configured = int(
        getattr(
            settings,
            "MCP_STARTS_PER_MINUTE" if bucket == "start" else "MCP_READS_PER_MINUTE",
            default_limit,
        ),
    )
    if configured <= 0:
        return
    window = int(time.time() // 60)
    key = f"mcp-rate:{user_id}:{bucket}:{window}"
    count = increment_rate_limit_counter(key=key, timeout=70)
    if count > configured:
        raise MCPApplicationError(
            MCPErrorCode.RATE_LIMITED,
            "Too many MCP requests. Retry after the current minute.",
        )
