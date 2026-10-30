"""Assembly of the v2 FastAPI application."""

from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from ..._version import __version__
from ..gateway import ManagerGateway
from ..identity import Authenticator
from ..store import Store
from . import events, leases, queue
from .dependencies import ReadPrincipal, Services
from .errors import install_error_handlers
from .middleware import RequestIdMiddleware
from .models import HealthView

# The OpenAPI document's description: how a client, human or agent, is meant to use the API.
_GUIDE = """\
Queue and run operations on a Bluesky instrument.

## Authentication

Every route except `GET /health` needs `Authorization: Bearer <token>`, an access token from the facility
identity provider. Its roles bound what the caller may do; each role includes those above it.

| Role | Claim value | Allows |
|---|---|---|
| Read | `QueueServer.Read` | every `GET` |
| Edit | `QueueServer.Edit` | taking the control lease and editing the queue |
| Run | `QueueServer.Run` | starting and stopping queue executions |
| Admin | `QueueServer.Admin` | taking the control lease from its holder |

## Queueing work

1. `GET /api/v2/catalog` lists the operations that may be submitted, with the JSON Schema of each one's
   `parameters`.
2. `POST /api/v2/control-lease` takes control of the instrument: only the holder may edit the queue or start
   and stop queue executions. Renew before `expires_at`; release when done.
3. `GET /api/v2/queue` returns the queue, and its revision as the `ETag` header, e.g. `"qrev-7"`.
4. `POST /api/v2/operations` with `If-Match: "qrev-7"` and a new `Idempotency-Key`. The response's `ETag` is
   the new revision, for the next edit.
5. `POST /api/v2/queue-executions` (Run role) starts running the queue.
6. `GET /api/v2/events`, or the server-sent event stream `GET /api/v2/events/stream`, reports progress.

## Retries and conflicts

- **Idempotency-Key.** Every mutation needs one, new for each intended change. After a timeout, a network
  error or a 5xx response, resend the same request with the same key: if it was performed, the original
  response is returned and nothing is repeated. Reusing a key on the same route for a different request is
  refused with 409 `idempotency_conflict`.
- **If-Match.** Every queue edit names the revision it is based on. If the queue has changed since, because
  another client edited it or an operation was taken to run, the edit is refused with 412 `stale_revision`
  and nothing is written; the response's `ETag` is the current revision. Re-read the queue, decide again, and
  send the new edit with a new key.

## Responses

- Every response carries an `X-Request-ID` header.
- Errors have the body `{"error": {"code", "message", "details", "request_id"}}`. Branch on `code`, which is
  stable; `message` is for people. Each route lists the codes it may return.
- Timestamps are RFC 3339 UTC strings, except inside event `payload`s, where they are integer microseconds
  since the Unix epoch.
"""

_TAGS = [
    {"name": "catalog", "description": "Operations that may be submitted."},
    {"name": "queue", "description": "The queue and its operations; edits need the control lease and `If-Match`."},
    {"name": "executions", "description": "Runs of the queue, and the attempts that execute its operations."},
    {"name": "lease", "description": "Control of the instrument; one holder at a time."},
    {"name": "events", "description": "Everything that happens to the queue, paged or streamed."},
    {"name": "health", "description": "Liveness; needs no token."},
]


def _operation_id(route: APIRoute) -> str:
    # The handler name, e.g. ``submit_operation``: short and stable for generated clients and agent tools.
    return route.name


def create_app(
    *,
    store: Store,
    authenticator: Authenticator,
    manager: ManagerGateway,
    clock: Callable[[], float] = time.time,
    sse_heartbeat_seconds: float = 15.0,
) -> FastAPI:
    """Build the v2 HTTP API over an open store.

    The caller opens and closes ``store`` and owns the HTTP client of ``authenticator``; the app does not
    manage their lifecycle.

    Parameters
    ----------
    store : Store
        The controller queue; must stay open while the app serves requests.
    authenticator : Authenticator
        Verifies bearer tokens.
    manager : ManagerGateway
        The queueserver manager; supplies the operation catalog.
    clock : callable, optional
        Returns the current time in Unix seconds; event streams end when the caller's token expires.
    sse_heartbeat_seconds : float, optional
        Longest silence on an event stream before a heartbeat comment is sent.

    Returns
    -------
    FastAPI

    Raises
    ------
    ValueError
        ``sse_heartbeat_seconds`` is not positive.
    """
    if sse_heartbeat_seconds <= 0:
        raise ValueError("sse_heartbeat_seconds must be positive")
    # The default documentation routes would be unauthenticated; the schema is served below instead.
    app = FastAPI(
        title="bluesky-httpserver v2",
        version=__version__,
        description=_GUIDE,
        openapi_tags=_TAGS,
        generate_unique_id_function=_operation_id,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = Services(store, authenticator, manager, clock, sse_heartbeat_seconds)
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/health", tags=["health"], response_model=HealthView)
    async def health() -> HealthView:
        """Whether the server is up."""
        return HealthView(status="ok")

    @app.get("/api/v2/openapi.json", include_in_schema=False)
    async def openapi(request: Request, principal: ReadPrincipal) -> JSONResponse:
        return JSONResponse(request.app.openapi())

    app.include_router(queue.router, prefix="/api/v2")
    app.include_router(leases.router, prefix="/api/v2")
    app.include_router(events.router, prefix="/api/v2")
    return app
