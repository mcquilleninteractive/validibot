"""Safe application errors exposed through the MCP adapter."""

from validibot.mcp_server.constants import MCPErrorCode


class MCPApplicationError(Exception):
    """Carry a stable code and intentionally curated user-facing detail."""

    def __init__(self, code: MCPErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")
