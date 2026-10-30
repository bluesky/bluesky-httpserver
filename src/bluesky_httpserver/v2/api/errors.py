"""Error responses of the v2 HTTP API: every failure is an `ErrorEnvelope` carrying the request id.

`ERRORS` is the single list of error codes: handlers take each code's status from it and routes document their
errors with `error_responses`, so the OpenAPI document and the responses cannot disagree.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ..catalog import InvalidParameters, UnknownOperation
from ..identity import AuthenticationFailed, IdentityProviderUnavailable, InsufficientRole
from ..store import (
    AttemptNotFound,
    ExecutionActive,
    ExecutionNotFound,
    ExecutionNotRunning,
    IdempotencyConflict,
    InvalidReorder,
    LeaseHeld,
    LeaseRequired,
    OperationNotFound,
    OperationNotPending,
    QueueEmpty,
    StaleRevision,
)
from .models import ErrorBody, ErrorEnvelope, utc


class ErrorCode(NamedTuple):
    """HTTP status of an error code, and what the code means to a client."""

    status_code: int
    description: str


ERRORS: dict[str, ErrorCode] = {
    "unauthenticated": ErrorCode(
        401, "The bearer token is missing, malformed, expired or otherwise invalid; `WWW-Authenticate` says which."
    ),
    "insufficient_role": ErrorCode(
        403, "The token does not grant the role this route requires; `details.required_role` names it."
    ),
    "identity_provider_unavailable": ErrorCode(
        503, "The token could not be checked because the identity provider's signing keys are unavailable; retry."
    ),
    "invalid_request": ErrorCode(
        422, "The body, a query or a path parameter is malformed; `details.errors` lists each problem."
    ),
    "idempotency_key_required": ErrorCode(400, "The `Idempotency-Key` header is missing."),
    "invalid_idempotency_key": ErrorCode(
        400, "The `Idempotency-Key` header is not 1 to 255 visible ASCII characters."
    ),
    "idempotency_conflict": ErrorCode(
        409,
        "The caller already used this `Idempotency-Key` on this route with a different body or `If-Match`;"
        " a new request needs a new key.",
    ),
    "precondition_required": ErrorCode(
        428, "The `If-Match` header is missing; send the `ETag` of the queue revision the edit is based on."
    ),
    "invalid_if_match": ErrorCode(400, 'The `If-Match` header is not exactly one entity tag `"qrev-N"`.'),
    "invalid_last_event_id": ErrorCode(400, "The `Last-Event-ID` header is not a non-negative integer."),
    "lease_required": ErrorCode(
        403, "The caller does not hold the control lease; take it with `POST /api/v2/control-lease`."
    ),
    "lease_held": ErrorCode(
        409, "Someone holds the control lease, the caller included; `details` gives `holder` and `expires_at`."
    ),
    "stale_revision": ErrorCode(
        412,
        "The queue changed after the `If-Match` revision and nothing was written; the `ETag` header and"
        " `details.current_revision` give the current revision. Re-read before deciding again.",
    ),
    "not_found": ErrorCode(404, "No such route, operation, attempt or queue execution."),
    "method_not_allowed": ErrorCode(405, "The route does not accept this method."),
    "operation_not_queued": ErrorCode(
        409, "The operation is no longer queued: it was cancelled or replaced, or has been taken to run."
    ),
    "invalid_reorder": ErrorCode(422, "`operation_uids` does not name every queued operation exactly once."),
    "unknown_operation": ErrorCode(
        422, "`operation_id` and `operation_version` do not name an operation in `GET /api/v2/catalog`."
    ),
    "invalid_parameters": ErrorCode(
        422,
        "`parameters` do not match the operation's `parameters_schema`; `details.errors` gives the JSON path"
        " and message of each problem.",
    ),
    "queue_empty": ErrorCode(409, "No operation is queued, so there is nothing to run."),
    "execution_active": ErrorCode(409, "A queue execution is already running or stopping."),
    "execution_not_running": ErrorCode(409, "The queue execution is not running."),
    "internal_error": ErrorCode(500, "Unexpected server failure; report the `request_id`."),
}

AUTHENTICATION_ERRORS = ("unauthenticated", "insufficient_role", "identity_provider_unavailable")
"""Errors every route but ``/health`` may return."""


class ApiError(Exception):
    """A request the HTTP layer refuses itself, such as one with a missing or malformed header."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def queue_etag(revision: int) -> str:
    """Return the entity tag of queue revision ``revision``."""
    return f'"qrev-{revision}"'


def error_responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` of an authenticated route that may also fail with ``codes``."""
    grouped: dict[int, list[str]] = {}
    for code in (*AUTHENTICATION_ERRORS, *codes):
        grouped.setdefault(ERRORS[code].status_code, []).append(code)
    return {
        status_code: {
            "model": ErrorEnvelope,
            "description": "\n\n".join(f"`{code}`: {ERRORS[code].description}" for code in group),
        }
        for status_code, group in sorted(grouped.items())
    }


def error_response(
    request: Request,
    code: str,
    message: str,
    *,
    status_code: int | None = None,
    details: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build an error response for ``request``; ``status_code`` defaults to the one `ERRORS` gives ``code``."""
    envelope = ErrorEnvelope(
        error=ErrorBody(code=code, message=message, details=details, request_id=request.state.request_id)
    )
    return JSONResponse(
        envelope.model_dump(mode="json"),
        status_code=ERRORS[code].status_code if status_code is None else status_code,
        headers=headers,
    )


# Errors whose response needs nothing beyond code and message; keyed by exact type.
_CODES: dict[type[Exception], str] = {
    IdentityProviderUnavailable: "identity_provider_unavailable",
    LeaseRequired: "lease_required",
    IdempotencyConflict: "idempotency_conflict",
    OperationNotFound: "not_found",
    ExecutionNotFound: "not_found",
    AttemptNotFound: "not_found",
    OperationNotPending: "operation_not_queued",
    InvalidReorder: "invalid_reorder",
    ExecutionActive: "execution_active",
    ExecutionNotRunning: "execution_not_running",
    QueueEmpty: "queue_empty",
    UnknownOperation: "unknown_operation",
}

_WWW_AUTHENTICATE = {
    "missing bearer token": "Bearer",
    "malformed authorization header": 'Bearer error="invalid_request"',
}

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


async def _plain(request: Request, exc: Exception) -> JSONResponse:
    return error_response(request, _CODES[type(exc)], str(exc))


async def _authentication_failed(request: Request, exc: AuthenticationFailed) -> JSONResponse:
    challenge = _WWW_AUTHENTICATE.get(exc.reason, 'Bearer error="invalid_token"')
    return error_response(request, "unauthenticated", exc.reason, headers={"WWW-Authenticate": challenge})


async def _insufficient_role(request: Request, exc: InsufficientRole) -> JSONResponse:
    return error_response(
        request, "insufficient_role", str(exc), details={"required_role": exc.required.claim_value}
    )


async def _lease_held(request: Request, exc: LeaseHeld) -> JSONResponse:
    details = {"holder": exc.holder, "expires_at": utc(exc.expires_at)}
    return error_response(request, "lease_held", str(exc), details=details)


async def _stale_revision(request: Request, exc: StaleRevision) -> JSONResponse:
    return error_response(
        request,
        "stale_revision",
        str(exc),
        details={"current_revision": exc.current},
        headers={"ETag": queue_etag(exc.current)},
    )


async def _invalid_parameters(request: Request, exc: InvalidParameters) -> JSONResponse:
    return error_response(request, "invalid_parameters", str(exc), details={"errors": exc.errors})


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(request, exc.code, exc.message)


async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    # ``input`` and ``ctx`` are left out: they may echo submitted values or hold objects that are not JSON.
    errors = [
        {"loc": list(error["loc"]), "message": error["msg"], "type": error["type"]} for error in exc.errors()
    ]
    return error_response(request, "invalid_request", "request validation failed", details={"errors": errors})


async def _http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    return error_response(request, code, str(exc.detail), status_code=exc.status_code, headers=exc.headers)


async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    # Sent by the outermost middleware, past RequestIdMiddleware, so the header is set here.
    request_id = request.state.request_id
    return error_response(request, "internal_error", "internal server error", headers={"X-Request-ID": request_id})


def install_error_handlers(app: FastAPI) -> None:
    """Map every error the API raises to its status, code and envelope; anything unmapped is a 500."""
    for exc_class in _CODES:
        app.exception_handler(exc_class)(_plain)
    app.exception_handler(AuthenticationFailed)(_authentication_failed)
    app.exception_handler(InsufficientRole)(_insufficient_role)
    app.exception_handler(LeaseHeld)(_lease_held)
    app.exception_handler(StaleRevision)(_stale_revision)
    app.exception_handler(InvalidParameters)(_invalid_parameters)
    app.exception_handler(ApiError)(_api_error)
    app.exception_handler(RequestValidationError)(_request_validation)
    app.exception_handler(HTTPException)(_http_exception)
    app.exception_handler(Exception)(_internal_error)
