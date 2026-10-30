"""ASGI middleware of the v2 HTTP API."""

from __future__ import annotations

import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestIdMiddleware:
    """Give every HTTP request a server-generated id, as ``request.state.request_id`` and ``X-Request-ID``.

    A client-sent ``X-Request-ID`` is ignored, so ids in logs and error bodies are always the server's.
    Pure ASGI rather than ``BaseHTTPMiddleware``, which interferes with streaming responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        await self.app(scope, receive, send_with_request_id)
