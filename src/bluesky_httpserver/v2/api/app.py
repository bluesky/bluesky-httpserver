"""Assembly of the v2 FastAPI application."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from ..._version import __version__
from . import events, leases, queue
from .dependencies import ReadPrincipal, ServicesDep
from .errors import install_error_handlers
from .middleware import RequestIdMiddleware
from .models import HealthView, ReadinessView

# The OpenAPI document's description: how a client, human or agent, is meant to use the API.
_GUIDE = """\
Queue and run operations on a Bluesky instrument.

## Authentication

Every route except `GET /health` and `GET /ready` needs `Authorization: Bearer <token>`, an access token from
the facility identity provider. Its roles bound what the caller may do; each role includes those above it.

| Role | Claim value | Allows |
|---|---|---|
| Read | `QueueServer.Read` | every `GET` |
| Edit | `QueueServer.Edit` | taking the control lease and editing the queue |
| Run | `QueueServer.Run` | starting queue executions, stopping them and their operations, and recovery |
| Admin | `QueueServer.Admin` | taking the control lease from its holder |

The token must also name the client application it was issued to, such as an agent acting on a user's
behalf; a token that does not is refused. Every change is recorded as the principal's, together with that
client: `submitted_via` on operations, `started_via` on queue executions and `client_id` on events.

## Queueing work

1. `GET /api/v2/catalog` lists the operations that may be submitted, with the JSON Schema of each one's
   `parameters`.
2. `POST /api/v2/control-lease` takes control of the instrument: only the holder may edit the queue, start a
   queue execution or acknowledge recovery. Control belongs to the principal, not the client: every client
   acting as it, such as an agent acting for a user, shares one lease. On `lease_held`, renew instead; that
   fails with `lease_required` if someone else holds it. Renew before `expires_at`; a renewal never shortens
   the lease. Releasing ends control for all of the principal's clients, so a client that is done just stops
   renewing.
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

## Stopping and recovery

Stopping needs the Run role but not the control lease, so an operator can stop work that another principal
started. `GET /api/v2/active-execution` names the running queue execution and its live attempt.

- `POST /api/v2/attempts/{attempt_uid}/safe-stop` pauses the running operation at its next checkpoint and
  aborts it. A move or a wait in progress runs to its end first.
- `POST /api/v2/attempts/{attempt_uid}/stop-now` pauses the running operation at once, interrupting a move or
  a wait, and aborts it; it also escalates a safe stop that has not taken effect.
- After either, the queue execution stops once the operation ends, whatever its outcome. Neither can be
  undone: no route pauses an operation to resume it later.
- `POST /api/v2/queue-executions/{queue_execution_uid}/stop` lets the current operation finish and then stops
  the queue execution; it takes `If-Match`.
- If the controller cannot prove how an operation ended, for example because RE Manager stopped answering,
  the operation becomes `unknown` and nothing more runs: starting a queue execution is refused with 409
  `dispatch_blocked`. Once RE Manager reports nothing running, `POST /api/v2/recovery/acknowledge` (Run role)
  clears the block; queued operations stay queued.
- `GET /ready` says whether work can run now, and if not, why.

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
    {"name": "health", "description": "Liveness and readiness; need no token."},
]


def _operation_id(route: APIRoute) -> str:
    # The handler name, e.g. ``submit_operation``: short and stable for generated clients and agent tools.
    return route.name


def create_app(lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]]) -> FastAPI:
    """Build the v2 HTTP API, which serves only inside ``lifespan``.

    On entry, ``lifespan`` sets ``app.state.services`` to the `Services` the routes use, and keeps them usable
    until it exits: the store open, the authenticator's HTTP client open, and ``worker.run()`` running. The app
    manages none of them and only reads from the worker gateway.

    Parameters
    ----------
    lifespan : callable
        Receives the app and returns the context it serves inside.

    Returns
    -------
    FastAPI
    """
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
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/health", tags=["health"], response_model=HealthView)
    async def health() -> HealthView:
        """Whether the server is up."""
        return HealthView(status="ok")

    @app.get(
        "/ready",
        tags=["health"],
        response_model=ReadinessView,
        responses={503: {"model": ReadinessView, "description": "Not ready; the fields say why."}},
    )
    async def ready(services: ServicesDep) -> JSONResponse:
        """Whether the controller can run work now; needs no token."""
        view = ReadinessView.from_readiness(services.worker.readiness())
        return JSONResponse(view.model_dump(mode="json"), status_code=200 if view.ready else 503)

    @app.get("/api/v2/openapi.json", include_in_schema=False)
    async def openapi(request: Request, principal: ReadPrincipal) -> JSONResponse:
        return JSONResponse(request.app.openapi())

    app.include_router(queue.router, prefix="/api/v2")
    app.include_router(leases.router, prefix="/api/v2")
    app.include_router(events.router, prefix="/api/v2")
    return app
