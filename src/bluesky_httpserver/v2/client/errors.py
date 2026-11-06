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
        super().__init__(f"{code}: {message}")


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
