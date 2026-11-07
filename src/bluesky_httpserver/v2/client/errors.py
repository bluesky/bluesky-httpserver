"""Errors the v2 API returns, as the client raises them."""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """The server refused the request, or the client refused it before sending.

    Branch on ``code``, the stable error code; ``ERRORS`` in ``bluesky_httpserver.v2.api.errors`` lists them all.

    Parameters
    ----------
    status_code : int
        HTTP status of the response.
    code : str
        Stable, machine-readable reason.
    message : str
        Human-readable explanation.
    details : dict, optional
        Code-specific data, if any.
    request_id : str, optional
        Identifies the request in the server's logs; None for an error the client found before sending.

    Attributes
    ----------
    idempotency_key : str or None
        The ``Idempotency-Key`` a refused mutation was sent with; None for a read, or if nothing was sent.
    revision : int or None
        The queue revision a refused mutation sent as ``If-Match``; None if it sent none.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id
        self.idempotency_key: str | None = None
        self.revision: int | None = None
        super().__init__(f"{code}: {message}")


class OutcomeUnknown(httpx.TransportError):
    """A mutation failed in the network on every try, so whether the server acted is unknown.

    Call the same method again with ``idempotency_key=`` and ``revision=`` set from this error: if the server
    acted, it replays its original response instead of acting twice, even if the queue changed meanwhile.
    The network error of the last try is the ``__cause__``.

    Parameters
    ----------
    message : str
        What failed.
    request : httpx.Request
        The last request sent.
    idempotency_key : str
        The ``Idempotency-Key`` every try was sent with.
    revision : int or None
        The queue revision every try sent as ``If-Match``; None if it sent none.
    """

    def __init__(
        self, message: str, *, request: httpx.Request, idempotency_key: str, revision: int | None
    ) -> None:
        super().__init__(message, request=request)
        self.idempotency_key = idempotency_key
        self.revision = revision


class StaleRevision(ApiError):
    """The queue changed after the revision an edit was based on; nothing was written."""

    @property
    def current_revision(self) -> int:
        """The queue's revision when the edit was refused."""
        assert self.details is not None
        return self.details["current_revision"]


class InvalidParameters(ApiError):
    """Parameters do not match the operation's schema or devices."""

    @property
    def errors(self) -> list[dict[str, str]]:
        """One ``{"path", "message"}`` per problem; ``path`` is a JSON path into the parameters."""
        assert self.details is not None
        return self.details["errors"]


class UnknownOperation(ApiError):
    """The catalog has no operation with this identifier and version."""


_CLASSES: dict[str, type[ApiError]] = {
    "stale_revision": StaleRevision,
    "invalid_parameters": InvalidParameters,
    "unknown_operation": UnknownOperation,
}


def error_from_response(response: httpx.Response) -> ApiError:
    """Return the error an error response carries.

    A body that is not the API's error envelope, such as a proxy's HTML page, gives code ``http_error``.
    """
    try:
        error = response.json()["error"]
        code, message, details, request_id = error["code"], error["message"], error["details"], error["request_id"]
    except (ValueError, KeyError, TypeError):
        return ApiError(
            response.status_code,
            "http_error",
            f"HTTP {response.status_code} {response.reason_phrase}",
            None,
            response.headers.get("X-Request-ID"),
        )
    return _CLASSES.get(code, ApiError)(response.status_code, code, message, details, request_id)
