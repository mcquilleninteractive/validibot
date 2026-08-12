"""Legacy HTTP compatibility surface for authenticated agent operations.

Cloud x402 adapters still import this package's reference and authentication
primitives and exercise its `/api/v1/mcp/*` routes. The embedded official-SDK
MCP endpoint does not use this package; its tools call typed Django application
services directly.

Keep new MCP work in ``validibot.mcp_server`` or a channel-neutral application
service. Remove this adapter only in a coordinated Community/Cloud change after
the production endpoint has passed external acceptance.
"""
