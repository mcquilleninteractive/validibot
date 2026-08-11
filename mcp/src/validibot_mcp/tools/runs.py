"""
Run status tools: get_run_status and wait_for_run.

Validation runs are asynchronous — EnergyPlus simulations can take minutes.
These tools let agents check status or block until completion.

The opaque ``run_ref`` decodes to the member-access helper path that launched
the run; polling always goes through the authenticated MCP helper API.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from validibot_mcp import auth, client
from validibot_mcp.errors import MCPToolError
from validibot_mcp.gating import check_global_enabled
from validibot_mcp.refs import RUN_REF_MEMBER_KIND, build_member_run_ref, parse_run_ref
from validibot_mcp.tools import format_error

if TYPE_CHECKING:
    from fastmcp import FastMCP

# States that indicate the run is finished (no more polling needed).
#
# Both backends emit the projected lifecycle state under ``state``:
# ``PENDING`` → ``RUNNING`` → ``COMPLETED``. The terminal outcome
# travels in the separate ``result`` field (PASS / FAIL / ERROR /
# CANCELED / TIMED_OUT / UNKNOWN). Follow-up still pending: move the
# enum constants to ``validibot-shared`` so MCP, CLI, and Django stop
# encoding the same vocabulary independently.
_TERMINAL_STATES = {"COMPLETED"}

_DEFAULT_TIMEOUT = 300  # 5 minutes
_POLL_INITIAL_INTERVAL = 2  # seconds — catches fast validators quickly
_POLL_MAX_INTERVAL = 30  # seconds — cap for slow validators (EnergyPlus, FMU)
_POLL_BACKOFF_FACTOR = 2  # double the interval each iteration

_READ_ONLY_TOOL_ANNOTATIONS = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}


async def _get_run_for_path(
    *,
    api_key: str | None,
    user_sub: str | None,
    run_ref: str | None,
) -> dict[str, Any]:
    """Dispatch run status lookup through the authenticated MCP helper endpoint."""

    if run_ref:
        try:
            resolved_run = parse_run_ref(run_ref)
        except ValueError:
            return {
                "error": {
                    "code": "INVALID_PARAMS",
                    "message": "run_ref is invalid.",
                },
            }

        if resolved_run.auth_kind == RUN_REF_MEMBER_KIND:
            if api_key is None:
                return {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "A bearer token is required for member-access run_ref values.",
                    },
                }
            run = await client.get_authenticated_run(
                run_ref,
                user_sub=user_sub,
                api_token=None if user_sub else api_key,
            )
            return _with_run_ref(run)

    return {
        "error": {
            "code": "INVALID_PARAMS",
            "message": ("run_ref is required."),
        },
    }


def _with_run_ref(run: dict[str, Any]) -> dict[str, Any]:
    """Attach the opaque ``run_ref`` used by the MCP contract."""

    enriched = {**run}
    run_id = str(enriched.get("run_id") or enriched.get("id") or "").strip()
    org_slug = str(enriched.get("org") or "").strip()

    if run_id and org_slug:
        enriched["run_ref"] = build_member_run_ref(
            org_slug=org_slug,
            run_id=run_id,
        )
        enriched.setdefault("run_id", run_id)
    return enriched


async def get_run_status(
    run_ref: str = "",
) -> dict[str, Any]:
    """Check the current status of a validation run.

    Pass the opaque ``run_ref`` returned by ``validate_file``.

    Returns:
        A dict with these stable fields:

        - ``state``: lifecycle, one of ``PENDING`` / ``RUNNING`` /
          ``COMPLETED``. Use this to decide whether to poll again.
        - ``result``: terminal outcome, one of ``PASS`` / ``FAIL`` /
          ``ERROR`` / ``CANCELED`` / ``TIMED_OUT`` / ``UNKNOWN``. Only
          informative once ``state`` is ``COMPLETED``.
        - ``run_ref``: the opaque ref you passed in.
        - ``findings``: validation findings (if the run is terminal).
    """
    try:
        check_global_enabled()
        api_key = auth.get_api_key_or_none()
        user_sub = auth.get_authenticated_user_sub_or_none()
        return await _get_run_for_path(
            api_key=api_key,
            user_sub=user_sub,
            run_ref=run_ref,
        )
    except MCPToolError as exc:
        return format_error(exc)


async def wait_for_run(
    run_ref: str = "",
    timeout_seconds: int = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Wait for a validation run to complete, polling periodically.

    Blocks until the run reaches a terminal state (``state == "COMPLETED"``)
    or ``timeout_seconds`` elapses. Useful when the agent wants to act on
    the results immediately.

    Args:
        run_ref: Opaque run handle from ``validate_file``.
        timeout_seconds: Maximum time to wait (default: 300 = 5 minutes).

    Returns:
        A dict with the same fields as ``get_run_status``:

        - ``state``: ``PENDING`` / ``RUNNING`` / ``COMPLETED``.
        - ``result``: ``PASS`` / ``FAIL`` / ``ERROR`` / ``CANCELED`` /
          ``TIMED_OUT`` / ``UNKNOWN``.
        - ``findings`` if the run is terminal.

        If the *client-side* ``timeout_seconds`` budget expires before the
        server reports the run done, the response carries the last known
        snapshot plus ``is_complete=False`` and ``result="TIMED_OUT"``.
        Note that this client-side timeout is distinct from a server-side
        ``TIMED_OUT`` outcome — the former means "we stopped waiting", the
        latter means "the validator ran past its time limit".
    """
    try:
        check_global_enabled()
        api_key = auth.get_api_key_or_none()
        user_sub = auth.get_authenticated_user_sub_or_none()
        start = time.monotonic()
        interval = _POLL_INITIAL_INTERVAL

        while True:
            run = await _get_run_for_path(
                api_key=api_key,
                user_sub=user_sub,
                run_ref=run_ref,
            )

            # If the helper returned an error, propagate it immediately.
            # Check for a truthy value — the API may include "error": null
            # on success responses.
            if run.get("error"):
                return run

            state = run.get("state", "")
            if state in _TERMINAL_STATES:
                return run

            elapsed = time.monotonic() - start
            if elapsed > timeout_seconds:
                return {
                    **run,
                    "result": "TIMED_OUT",
                    "is_complete": False,
                    "message": (
                        f"Run still in progress after {timeout_seconds}s. "
                        f"Use get_run_status to check again later."
                    ),
                }

            # Clamp sleep to the remaining time budget so we never
            # overshoot the caller's requested timeout.
            remaining = timeout_seconds - (time.monotonic() - start)
            await asyncio.sleep(min(interval, max(remaining, 0)))
            interval = min(
                interval * _POLL_BACKOFF_FACTOR,
                _POLL_MAX_INTERVAL,
            )
    except MCPToolError as exc:
        return format_error(exc)


def register_tools(server: FastMCP) -> None:
    """Register the run tools on a FastMCP server instance."""

    server.tool(get_run_status, annotations=_READ_ONLY_TOOL_ANNOTATIONS)
    server.tool(wait_for_run, annotations=_READ_ONLY_TOOL_ANNOTATIONS)
