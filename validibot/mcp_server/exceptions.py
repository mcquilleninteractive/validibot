"""Safe application errors exposed through the MCP adapter."""

from mcp.server.mcpserver.exceptions import ToolError

from validibot.mcp_server.constants import MCPErrorCode


class MCPApplicationError(ToolError):
    """Carry a stable code and intentionally curated user-facing detail.

    This subclasses the official SDK's ``ToolError`` because that is how the
    SDK tells an *anticipated* failure from a crash. A ``ToolError`` returns
    ``is_error=True`` with our message in the result content, which is the
    whole point of this class: the detail is already sanitised and stable.
    Any other exception type is treated as a crash, so the SDK withholds its
    text and the client sees only ``Error executing tool <name>``. Before mcp
    2.2.0 the distinction did not exist and a plain ``Exception`` still
    carried its message through.
    """

    def __init__(self, code: MCPErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")
