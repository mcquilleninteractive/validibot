"""Tests for authenticated MCP principal throttling.

Tool calls are intentionally bounded separately for discovery/status reads and
validation creation. These tests protect the stable rate-limit error contract
without depending on a wall-clock boundary or a particular shared-cache vendor.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache

from validibot.mcp_server.constants import MCPErrorCode
from validibot.mcp_server.exceptions import MCPApplicationError
from validibot.mcp_server.rate_limits import enforce_principal_rate_limit


@pytest.fixture(autouse=True)
def _empty_rate_limit_cache():
    """Give each test an isolated fixed-window counter."""

    cache.clear()
    yield
    cache.clear()


def test_read_limit_returns_the_stable_retryable_error(settings) -> None:
    """Excess model polling must be identified without leaking auth details."""

    settings.MCP_READS_PER_MINUTE = 1
    enforce_principal_rate_limit(user_id=41, operation="get_validation_run")

    with pytest.raises(MCPApplicationError) as limited:
        enforce_principal_rate_limit(user_id=41, operation="get_validation_run")

    assert limited.value.code == MCPErrorCode.RATE_LIMITED
    assert "Retry" in limited.value.detail


def test_read_limit_is_shared_across_read_tools(settings) -> None:
    """Switching tools must not multiply a principal's overall read budget."""

    settings.MCP_READS_PER_MINUTE = 1
    enforce_principal_rate_limit(user_id=43, operation="list_workflows")

    with pytest.raises(MCPApplicationError) as limited:
        enforce_principal_rate_limit(user_id=43, operation="get_workflow")

    assert limited.value.code == MCPErrorCode.RATE_LIMITED


def test_launch_and_read_counters_are_independent(settings) -> None:
    """Status polling must not consume the stricter validation-start budget."""

    settings.MCP_READS_PER_MINUTE = 1
    settings.MCP_STARTS_PER_MINUTE = 1

    enforce_principal_rate_limit(user_id=42, operation="get_workflow")
    enforce_principal_rate_limit(user_id=42, operation="start_validation")

    with pytest.raises(MCPApplicationError) as limited:
        enforce_principal_rate_limit(user_id=42, operation="start_validation")

    assert limited.value.code == MCPErrorCode.RATE_LIMITED
