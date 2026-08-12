"""
ASGI config for Validibot project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/dev/howto/deployment/asgi/

"""

import os
import sys
from pathlib import Path
from typing import cast

from asgiref.typing import ASGI3Application
from asgiref.typing import ASGIReceiveCallable
from asgiref.typing import ASGISendCallable
from asgiref.typing import Scope
from django.core.asgi import get_asgi_application

# This allows easy placement of apps within the interior
# validibot directory.
BASE_DIR = Path(__file__).resolve(strict=True).parent.parent
sys.path.append(str(BASE_DIR / "validibot"))

# If DJANGO_SETTINGS_MODULE is unset, default to the local settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

# This application object is used by any ASGI server configured to use this file.
django_application = cast("ASGI3Application", get_asgi_application())

# Import websocket application here, so apps from django_application are loaded first
from config.websocket import websocket_application  # noqa: E402
from validibot.core.features import CommercialFeature  # noqa: E402
from validibot.core.features import is_feature_enabled  # noqa: E402

mcp_application: ASGI3Application | None
if is_feature_enabled(CommercialFeature.MCP_SERVER):
    from validibot.mcp_server.server import build_mcp_asgi_application

    mcp_application = cast("ASGI3Application", build_mcp_asgi_application())
else:
    mcp_application = None


def _is_mcp_path(scope: Scope) -> bool:
    """Return whether an HTTP request belongs to the official MCP app."""

    path = str(scope.get("path", ""))
    return (
        path == "/mcp"
        or path.startswith("/mcp/")
        or path == ("/.well-known/oauth-protected-resource/mcp")
    )


async def application(
    scope: Scope,
    receive: ASGIReceiveCallable,
    send: ASGISendCallable,
) -> None:
    """Route licensed MCP traffic while leaving normal Django paths unchanged."""

    if scope["type"] == "lifespan":
        if mcp_application is not None:
            await mcp_application(scope, receive, send)
            return
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    elif scope["type"] == "http":
        if mcp_application is not None and _is_mcp_path(scope):
            await mcp_application(scope, receive, send)
            return
        await django_application(scope, receive, send)
    elif scope["type"] == "websocket":
        websocket_asgi = cast("ASGI3Application", websocket_application)
        await websocket_asgi(scope, receive, send)
    else:
        msg = f"Unknown scope type {scope['type']}"
        raise NotImplementedError(msg)
