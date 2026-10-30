"""Headers that guard mutations, and the at-most-once execution of each mutation.

Every mutation is checked in this order: authentication, role, body, ``Idempotency-Key``, ``If-Match`` (queue
mutations only); then, in one transaction, a stored response for the same request is replayed if there is
one, otherwise the store checks the lease, the revision and the mutation's own preconditions. A replay
therefore returns the original response even if the lease, revision or catalog has changed since.

The two headers are read from the request rather than declared as FastAPI parameters: a declared required
header would be refused with the generic ``invalid_request`` instead of its own code, and a declared optional
one would be documented as optional. `mutation_headers` documents them instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..identity import Principal
from ..store import IdempotencyScope, StoredResponse, Transaction
from .dependencies import Services
from .errors import ApiError

_IDEMPOTENCY_KEY_PATTERN = "[!-~]{1,255}"  # visible ASCII
# One strong entity tag of a queue revision: "*", weak tags and lists would bypass concurrency control.
_QUEUE_ETAG_PATTERN = '"qrev-(0|[1-9][0-9]*)"'
_IDEMPOTENCY_KEY = re.compile(_IDEMPOTENCY_KEY_PATTERN)
_QUEUE_ETAG = re.compile(_QUEUE_ETAG_PATTERN)

IDEMPOTENCY_ERRORS = ("idempotency_key_required", "invalid_idempotency_key", "idempotency_conflict")
"""Errors every mutation may return."""
QUEUE_EDIT_ERRORS = ("precondition_required", "invalid_if_match", "lease_required", "stale_revision")
"""Errors every queue mutation may return, besides `IDEMPOTENCY_ERRORS`."""


def mutation_headers(*, if_match: bool) -> dict[str, Any]:
    """``openapi_extra`` of a mutation route, documenting its required precondition headers."""
    parameters: list[dict[str, Any]] = [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "description": (
                "Client-chosen key, new for each intended change (a UUID works). After a timeout or error,"
                " resend the same request with the same key: if it was performed, the original response is"
                " returned and nothing is repeated."
            ),
            "schema": {"type": "string", "pattern": f"^{_IDEMPOTENCY_KEY_PATTERN}$"},
        }
    ]
    if if_match:
        parameters.append(
            {
                "name": "If-Match",
                "in": "header",
                "required": True,
                "description": (
                    'The `ETag` of the queue revision the edit is based on, e.g. `"qrev-7"`. If the queue has'
                    " changed since, nothing is written and the response is 412 `stale_revision`."
                ),
                "schema": {"type": "string", "pattern": f"^{_QUEUE_ETAG_PATTERN}$"},
            }
        )
    return {"parameters": parameters}


def require_idempotency_key(request: Request) -> str:
    """Return the request's ``Idempotency-Key``.

    Raises
    ------
    ApiError
        The header is absent, or not 1 to 255 visible ASCII characters.
    """
    value = request.headers.get("Idempotency-Key")
    if value is None:
        raise ApiError("idempotency_key_required", "Idempotency-Key is required")
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ApiError("invalid_idempotency_key", "Idempotency-Key must be 1 to 255 visible ASCII characters")
    return value


def expected_revision(request: Request) -> int:
    """Return the queue revision named by the request's ``If-Match``.

    Raises
    ------
    ApiError
        The header is absent, or not exactly one entity tag of the form ``"qrev-N"``.
    """
    if_match = request.headers.get("If-Match")
    if if_match is None:
        raise ApiError("precondition_required", "If-Match is required")
    match = _QUEUE_ETAG.fullmatch(if_match)
    if match is None:
        raise ApiError("invalid_if_match", 'If-Match must be one entity tag of the form "qrev-N"')
    return int(match.group(1))


async def run_idempotent(
    request: Request,
    services: Services,
    principal: Principal,
    *,
    key: str,
    body: BaseModel | None,
    revision: int | None,
    action: Callable[[Transaction], Awaitable[StoredResponse]],
) -> JSONResponse:
    """Run ``action`` at most once per principal, method, path and ``key``; replay its response on repeats.

    Parameters
    ----------
    request : Request
        The mutation request.
    services : Services
        Services of the app.
    principal : Principal
        The authenticated caller.
    key : str
        The request's ``Idempotency-Key``.
    body : BaseModel or None
        The validated request body, if the route takes one.
    revision : int or None
        The queue revision named by ``If-Match``, if the route takes it.
    action : callable
        Performs the mutation in the given transaction and returns the response to record.

    Returns
    -------
    JSONResponse
        The response of ``action``, or the recorded one if this request was already performed.

    Raises
    ------
    IdempotencyConflict
        ``key`` was already used with a different body or ``If-Match``.
    """
    # The default allow_nan=True lets a body with NaN parameters be fingerprinted; validation refuses it later.
    fingerprint = {"body": None if body is None else body.model_dump(), "revision": revision}
    request_hash = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scope = IdempotencyScope(principal.id, request.method, request.url.path, key)
    result = await services.store.write_idempotent(scope, request_hash, action)
    etag = result.response.etag
    return JSONResponse(
        result.response.body,
        status_code=result.response.status_code,
        headers={"ETag": etag} if etag else None,
    )
