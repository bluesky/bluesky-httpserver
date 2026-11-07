"""Asynchronous client of the v2 controller's HTTP API."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import email.utils
import inspect
import logging
import random
import re
import ssl
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self, TypeVar
from urllib.parse import quote

import httpx

from .errors import ApiError, OutcomeUnknown, StaleRevision, UnknownOperation, error_from_response
from .models import (
    ActiveExecution,
    Attempt,
    Event,
    Lease,
    Operation,
    OperationCatalog,
    Queue,
    QueueExecution,
    Readiness,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

Token = str | Callable[[], str | Awaitable[str]]
"""A bearer token, or a callable that returns one, possibly awaitable."""

_API = "/api/v2"
_ETAG = re.compile(r'"qrev-(0|[1-9][0-9]*)"')

# Statuses after which a mutation is resent with the same key: a proxy or the server could not handle it now.
_RETRIED_MUTATION_STATUSES = frozenset({502, 503, 504})
# Statuses after which the event stream reconnects; any other error status ends it.
_RETRIED_STREAM_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _revision_of(response: httpx.Response) -> int | None:
    """The queue revision a response's ``ETag`` names, if any."""
    match = _ETAG.fullmatch(response.headers.get("ETag", ""))
    return None if match is None else int(match.group(1))


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds a response's ``Retry-After`` asks to wait, if it has a valid one."""
    value = response.headers.get("Retry-After", "").strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _backoff(failures: int, base_seconds: float, cap_seconds: float, retry_after: float | None) -> float:
    """Seconds to wait before the next try: ``base_seconds`` doubled per failure after the first, capped at
    ``cap_seconds``, with jitter; at least ``retry_after``, if the server sent one."""
    delay = min(cap_seconds, base_seconds * 2 ** max(0, failures - 1))
    delay = random.uniform(delay / 2, delay)
    return delay if retry_after is None else max(delay, retry_after)


@dataclass
class LeaseHolder:
    """The control lease held by `AsyncQueueServerClient.hold_lease`.

    Attributes
    ----------
    lease : Lease
        The latest grant, updated by each renewal.
    lost : ApiError or None
        Set if the lease was lost (expired, overridden or released elsewhere), after which renewal stops.
    """

    lease: Lease
    lost: ApiError | None = None


class AsyncQueueServerClient:
    """Client of the v2 controller's HTTP API.

    A client may be used by many tasks at once. Use it as ``async with``, or call `aclose` when done.

    Queue edits send ``If-Match``: the revision given as ``revision=``, or else `revision`, the revision of the
    queue the calling task last read with `queue` (or was given by `edit_queue`), advanced by each of its own
    queue edits since. Each task has its own; events and other tasks never change it. A task that has neither
    gets `ApiError` ``precondition_required`` before anything is sent.

    Each mutation is sent with one ``Idempotency-Key``, made unless given ``idempotency_key=``. A network error,
    or a 502, 503 or 504, resends it with the same key and ``If-Match`` up to ``retries`` times, so the server
    acts at most once. If every try fails, the error raised carries both as ``idempotency_key`` and
    ``revision``; passing them to the same call again replays the server's original response, if it acted.

    Parameters
    ----------
    base_url : str
        Address of the controller, e.g. ``https://controller.example``.
    token : str or callable
        Bearer token, or a callable called before each request that returns one, possibly awaitable, so a
        long-lived client can refresh it.
    timeout_seconds : float, optional
        Timeout of each network operation: connecting, sending, or waiting for a response.
    verify : ssl.SSLContext or bool, optional
        How to verify the server's TLS certificate; True uses the system's trusted certificates.
    retries : int, optional
        Most times a mutation is resent after a network error or a 502, 503 or 504.
    retry_seconds : float, optional
        Wait before the first resend; each further one waits about twice as long, or as long as the server's
        ``Retry-After`` asks, if longer.

    Raises
    ------
    ApiError
        From every request the server refuses; `StaleRevision`, `InvalidParameters` and `UnknownOperation` for
        the codes callers most often handle.
    OutcomeUnknown
        From every mutation that failed in the network on every try.
    httpx.TransportError
        From every read that fails in the network.
    """

    def __init__(
        self,
        base_url: str,
        token: Token,
        *,
        timeout_seconds: float = 10.0,
        verify: ssl.SSLContext | bool = True,
        retries: int = 3,
        retry_seconds: float = 0.5,
    ) -> None:
        self._token = token
        self._timeout = timeout_seconds
        self._retries = retries
        self._retry_seconds = retry_seconds
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds, verify=verify)
        # A context variable, so each task (and each thread of a QueueServerClient) has its own.
        self._basis: contextvars.ContextVar[int | None] = contextvars.ContextVar(
            f"queue_revision_basis_{id(self):x}", default=None
        )
        self._catalog: OperationCatalog | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the client's connections."""
        await self._http.aclose()

    @property
    def revision(self) -> int | None:
        """Queue revision the calling task's queue edits send as ``If-Match`` unless given ``revision=``.

        Set by the task's `queue` reads and advanced by its own successful queue edits; None until it reads the
        queue. A new task starts with its creator's. Events and other tasks never change it.
        """
        return self._basis.get()

    async def _auth(self) -> dict[str, str]:
        token = self._token() if callable(self._token) else self._token
        if inspect.isawaitable(token):
            token = await token
        return {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, *, params: Mapping[str, int] | None = None) -> httpx.Response:
        response = await self._http.request(method, path, params=params, headers=await self._auth())
        if response.is_error:
            raise error_from_response(response)
        return response

    def _if_match(self, revision: int | None) -> int:
        """The revision a queue edit sends as ``If-Match``: ``revision``, or else the calling task's basis."""
        if revision is None:
            revision = self._basis.get()
        if revision is None:
            raise ApiError(
                428,
                "precondition_required",
                "no queue revision to send as If-Match: read the queue in this task first, or pass revision=",
            )
        return revision

    async def _mutate(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        revision: int | None,
        idempotency_key: str | None,
    ) -> httpx.Response:
        """Send a mutation, with ``If-Match`` if ``revision`` is given, and resend it as the class describes."""
        key = idempotency_key or str(uuid.uuid4())
        headers = {"Idempotency-Key": key}
        if revision is not None:
            headers["If-Match"] = f'"qrev-{revision}"'
        failures = 0
        while True:
            try:
                response = await self._http.request(
                    method, path, json=json, headers={**headers, **await self._auth()}
                )
            except httpx.TransportError as error:
                failures += 1
                if failures > self._retries:
                    raise OutcomeUnknown(
                        f"{method} {path} failed in the network on all {failures} tries: {error!r}",
                        request=error.request,
                        idempotency_key=key,
                        revision=revision,
                    ) from error
                reason, retry_after = repr(error), None
            else:
                if response.status_code not in _RETRIED_MUTATION_STATUSES or failures >= self._retries:
                    break
                failures += 1
                reason, retry_after = f"HTTP {response.status_code}", _retry_after(response)
            delay = _backoff(failures, self._retry_seconds, self._retry_seconds * 2**self._retries, retry_after)
            logger.info("%s %s failed (%s), resending in %.2g s with the same key", method, path, reason, delay)
            await asyncio.sleep(delay)
        if response.is_error:
            refused = error_from_response(response)
            refused.idempotency_key, refused.revision = key, revision
            raise refused
        made = _revision_of(response)
        if revision is not None and made is not None:
            # The revision this edit made on top of the one it was based on.
            self._basis.set(made)
        return response

    async def _checked(self, operation_id: str, operation_version: str, parameters: Mapping[str, Any]) -> None:
        cached = self._catalog
        catalog = cached or await self.catalog()
        try:
            catalog.check_parameters(operation_id, operation_version, parameters)
        except UnknownOperation:
            if cached is None:
                raise
            # The catalog may have grown since it was read.
            (await self.catalog()).check_parameters(operation_id, operation_version, parameters)

    async def readiness(self) -> Readiness:
        """Return whether the controller can run work now, and if not, why.

        Returns
        -------
        Readiness
            Also when the server answers 503, not ready.
        """
        response = await self._http.get("/ready", headers=await self._auth())
        if response.is_error and response.status_code != 503:
            raise error_from_response(response)
        return Readiness.model_validate_json(response.content)

    async def catalog(self) -> OperationCatalog:
        """Return the operations that may be submitted now.

        Submissions are checked against the catalog this last returned.

        Returns
        -------
        OperationCatalog
        """
        response = await self._request("GET", f"{_API}/catalog")
        self._catalog = OperationCatalog.model_validate_json(response.content)
        return self._catalog

    async def queue(self) -> Queue:
        """Return the queued operations in the order they will run, and the queue's revision.

        The calling task's later queue edits are based on this revision; see `revision`.

        Returns
        -------
        Queue
        """
        response = await self._request("GET", f"{_API}/queue")
        queue = Queue.model_validate_json(response.content)
        self._basis.set(queue.revision)
        return queue

    async def operation(self, operation_uid: str) -> Operation:
        """Return one operation in any state, including cancelled, replaced and finished ones.

        Parameters
        ----------
        operation_uid : str
            The operation.

        Returns
        -------
        Operation
        """
        response = await self._request("GET", f"{_API}/operations/{quote(operation_uid, safe='')}")
        return Operation.model_validate_json(response.content)

    async def attempt(self, attempt_uid: str) -> Attempt:
        """Return one attempt to execute an operation.

        Parameters
        ----------
        attempt_uid : str
            The attempt, as named by an event's ``attempt_uid``.

        Returns
        -------
        Attempt
        """
        response = await self._request("GET", f"{_API}/attempts/{quote(attempt_uid, safe='')}")
        return Attempt.model_validate_json(response.content)

    async def queue_execution(self, queue_execution_uid: str) -> QueueExecution:
        """Return one run of the queue.

        Parameters
        ----------
        queue_execution_uid : str
            The queue execution.

        Returns
        -------
        QueueExecution
        """
        response = await self._request("GET", f"{_API}/queue-executions/{quote(queue_execution_uid, safe='')}")
        return QueueExecution.model_validate_json(response.content)

    async def active_execution(self) -> ActiveExecution:
        """Return what runs now: the running or stopping queue execution, and its claimed or running attempt.

        Names what `stop_after_current`, `safe_stop` and `stop_now` take, without replaying events.

        Returns
        -------
        ActiveExecution
            Either field is None when there is none.
        """
        response = await self._request("GET", f"{_API}/active-execution")
        return ActiveExecution.model_validate_json(response.content)

    async def lease(self) -> Lease | None:
        """Return who controls the instrument, and until when.

        Returns
        -------
        Lease or None
            None when nobody holds control.
        """
        response = await self._request("GET", f"{_API}/control-lease")
        lease = response.json()["lease"]
        return None if lease is None else Lease.model_validate(lease)

    async def events(self, after: int = 0, limit: int = 100) -> list[Event]:
        """Return events after ``after``, oldest first.

        Parameters
        ----------
        after : int, optional
            Return events whose ``event_id`` is greater than this.
        limit : int, optional
            Most events to return, at most 1000.

        Returns
        -------
        list of Event
            Empty when there is nothing newer.
        """
        response = await self._request("GET", f"{_API}/events", params={"after": after, "limit": limit})
        return [Event.model_validate(event) for event in response.json()["events"]]

    async def acquire_lease(self, ttl_seconds: int, *, idempotency_key: str | None = None) -> Lease:
        """Take control of the instrument. Needs the Edit role.

        Only the holder may edit the queue or start queue executions; stopping needs only the Run role. See also
        `hold_lease`.

        Parameters
        ----------
        ttl_seconds : int
            Seconds from now until control lapses unless renewed.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Lease

        Raises
        ------
        ApiError
            Code ``lease_held`` if anyone holds control, the caller included; the holder renews instead.
        """
        response = await self._mutate(
            "POST",
            f"{_API}/control-lease",
            json={"ttl_seconds": ttl_seconds},
            revision=None,
            idempotency_key=idempotency_key,
        )
        return Lease.model_validate_json(response.content)

    async def renew_lease(self, ttl_seconds: int, *, idempotency_key: str | None = None) -> Lease:
        """Extend the caller's control to ``ttl_seconds`` from now. Needs the Edit role and the lease.

        Parameters
        ----------
        ttl_seconds : int
            Seconds from now until control lapses unless renewed again.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Lease

        Raises
        ------
        ApiError
            Code ``lease_required`` if the caller does not hold the lease.
        """
        response = await self._mutate(
            "POST",
            f"{_API}/control-lease/renew",
            json={"ttl_seconds": ttl_seconds},
            revision=None,
            idempotency_key=idempotency_key,
        )
        return Lease.model_validate_json(response.content)

    async def release_lease(self, *, idempotency_key: str | None = None) -> None:
        """Give up the caller's control. Needs the Edit role and the lease.

        A running queue execution keeps running.

        Parameters
        ----------
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Raises
        ------
        ApiError
            Code ``lease_required`` if the caller does not hold the lease.
        """
        await self._mutate(
            "POST",
            f"{_API}/control-lease/release",
            revision=None,
            idempotency_key=idempotency_key,
        )

    async def override_lease(self, reason: str, ttl_seconds: int, *, idempotency_key: str | None = None) -> Lease:
        """Take control from whoever holds it. Needs the Admin role.

        Parameters
        ----------
        reason : str
            Why; recorded in the ``lease.overridden`` event with the previous holder.
        ttl_seconds : int
            Seconds from now until control lapses unless renewed.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Lease
        """
        response = await self._mutate(
            "POST",
            f"{_API}/control-lease/override",
            json={"reason": reason, "ttl_seconds": ttl_seconds},
            revision=None,
            idempotency_key=idempotency_key,
        )
        return Lease.model_validate_json(response.content)

    async def submit(
        self,
        operation_id: str,
        operation_version: str,
        parameters: Mapping[str, Any],
        *,
        revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        """Append an operation to the end of the queue. Needs the Edit role and the lease.

        ``parameters`` are checked against the catalog before anything is sent. An operation submitted while a
        queue execution is running runs in it.

        Parameters
        ----------
        operation_id, operation_version : str
            The catalog operation to run.
        parameters : mapping
            Its arguments; must match the operation's ``parameters_schema`` and devices.
        revision : int, optional
            Queue revision the edit is based on; defaults to `revision`.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Operation
            The queued operation.

        Raises
        ------
        UnknownOperation
            The operation is not in the catalog.
        InvalidParameters
            ``parameters`` do not match the operation; nothing was sent.
        StaleRevision
            The queue changed after ``revision``; nothing was written.
        """
        revision = self._if_match(revision)
        await self._checked(operation_id, operation_version, parameters)
        response = await self._mutate(
            "POST",
            f"{_API}/operations",
            json={
                "operation_id": operation_id,
                "operation_version": operation_version,
                "parameters": dict(parameters),
            },
            revision=revision,
            idempotency_key=idempotency_key,
        )
        return Operation.model_validate_json(response.content)

    async def replace(
        self,
        operation_uid: str,
        operation_id: str,
        operation_version: str,
        parameters: Mapping[str, Any],
        *,
        revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        """Replace a queued operation by a new one at the same position. Needs the Edit role and the lease.

        The old operation becomes ``replaced``, and its ``replaced_by`` names the new one.

        Parameters
        ----------
        operation_uid : str
            The queued operation to replace.
        operation_id, operation_version : str
            The catalog operation the new one runs.
        parameters : mapping
            Its arguments; must match the operation's ``parameters_schema`` and devices.
        revision : int, optional
            Queue revision the edit is based on; defaults to `revision`.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Operation
            The new operation.

        Raises
        ------
        UnknownOperation
            The operation is not in the catalog.
        InvalidParameters
            ``parameters`` do not match the operation; nothing was sent.
        StaleRevision
            The queue changed after ``revision``; nothing was written.
        ApiError
            Code ``operation_not_queued`` if the operation is no longer queued.
        """
        revision = self._if_match(revision)
        await self._checked(operation_id, operation_version, parameters)
        response = await self._mutate(
            "PUT",
            f"{_API}/operations/{quote(operation_uid, safe='')}",
            json={
                "operation_id": operation_id,
                "operation_version": operation_version,
                "parameters": dict(parameters),
            },
            revision=revision,
            idempotency_key=idempotency_key,
        )
        return Operation.model_validate_json(response.content)

    async def cancel(
        self, operation_uid: str, *, revision: int | None = None, idempotency_key: str | None = None
    ) -> Operation:
        """Remove a queued operation from the queue; it becomes ``cancelled``. Needs the Edit role and the lease.

        Parameters
        ----------
        operation_uid : str
            The queued operation.
        revision : int, optional
            Queue revision the edit is based on; defaults to `revision`.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Operation
            The cancelled operation.

        Raises
        ------
        StaleRevision
            The queue changed after ``revision``; nothing was written.
        ApiError
            Code ``operation_not_queued`` if the operation is no longer queued.
        """
        response = await self._mutate(
            "DELETE",
            f"{_API}/operations/{quote(operation_uid, safe='')}",
            revision=self._if_match(revision),
            idempotency_key=idempotency_key,
        )
        return Operation.model_validate_json(response.content)

    async def reorder(
        self, operation_uids: Sequence[str], *, revision: int | None = None, idempotency_key: str | None = None
    ) -> Queue:
        """Put the queued operations in a new order. Needs the Edit role and the lease.

        Parameters
        ----------
        operation_uids : sequence of str
            Every queued operation exactly once, in the order they should run.
        revision : int, optional
            Queue revision the edit is based on; defaults to `revision`.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Queue
            The reordered queue.

        Raises
        ------
        StaleRevision
            The queue changed after ``revision``; nothing was written.
        ApiError
            Code ``invalid_reorder`` if ``operation_uids`` does not name every queued operation exactly once.
        """
        response = await self._mutate(
            "POST",
            f"{_API}/queue/reorder",
            json={"operation_uids": list(operation_uids)},
            revision=self._if_match(revision),
            idempotency_key=idempotency_key,
        )
        return Queue.model_validate_json(response.content)

    async def start_execution(
        self,
        policy: Literal["stop_on_failure", "continue_on_failure"] = "stop_on_failure",
        *,
        revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> QueueExecution:
        """Start running the queue, one operation at a time in queue order. Needs the Run role and the lease.

        The execution also runs operations submitted after it started, and does not need the lease once started.
        It ends when no queued operation is left, when it is stopped, or when an operation fails under
        ``stop_on_failure``.

        Parameters
        ----------
        policy : {"stop_on_failure", "continue_on_failure"}, optional
            Whether a failed operation stops the execution.
        revision : int, optional
            Queue revision the start is based on; defaults to `revision`.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        QueueExecution

        Raises
        ------
        StaleRevision
            The queue changed after ``revision``; nothing was started.
        ApiError
            Code ``execution_active``, ``queue_empty`` or ``dispatch_blocked`` if the queue cannot start now.
        """
        response = await self._mutate(
            "POST",
            f"{_API}/queue-executions",
            json={"policy": policy},
            revision=self._if_match(revision),
            idempotency_key=idempotency_key,
        )
        return QueueExecution.model_validate_json(response.content)

    async def stop_after_current(
        self, queue_execution_uid: str, *, revision: int | None = None, idempotency_key: str | None = None
    ) -> QueueExecution:
        """Stop a running queue execution once its current operation finishes. Needs the Run role; the lease is
        not needed.

        The execution becomes ``stopping``, or ``stopped`` at once if no operation is claimed or running.
        Operations not yet taken to run stay queued.

        Parameters
        ----------
        queue_execution_uid : str
            The running queue execution.
        revision : int, optional
            Queue revision the stop is based on; defaults to `revision`.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        QueueExecution

        Raises
        ------
        StaleRevision
            The queue changed after ``revision``; nothing was stopped.
        ApiError
            Code ``execution_not_running`` if the execution is not running.
        """
        response = await self._mutate(
            "POST",
            f"{_API}/queue-executions/{quote(queue_execution_uid, safe='')}/stop",
            revision=self._if_match(revision),
            idempotency_key=idempotency_key,
        )
        return QueueExecution.model_validate_json(response.content)

    async def safe_stop(self, attempt_uid: str, *, idempotency_key: str | None = None) -> Attempt:
        """Stop a live attempt safely. Needs the Run role; the lease is not needed.

        The controller pauses the plan at its next checkpoint and aborts it; a plan in the middle of a move or a
        wait continues until then, which `stop_now` does not wait for. If the operation finishes first, its own
        outcome stands. Either way, the queue execution stops once the attempt ends. Sends no queue revision, so
        a stop never fails because the queue changed.

        Parameters
        ----------
        attempt_uid : str
            The live attempt.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Attempt
            The attempt, with ``safe_stop_requested_at`` set.

        Raises
        ------
        ApiError
            Code ``attempt_not_live`` if the attempt has already ended.
        """
        response = await self._mutate(
            "POST",
            f"{_API}/attempts/{quote(attempt_uid, safe='')}/safe-stop",
            revision=None,
            idempotency_key=idempotency_key,
        )
        return Attempt.model_validate_json(response.content)

    async def stop_now(self, attempt_uid: str, *, idempotency_key: str | None = None) -> Attempt:
        """Stop a live attempt at once. Needs the Run role; the lease is not needed.

        The controller pauses the plan immediately, interrupting a move or a wait in progress, and aborts it; this
        also escalates a `safe_stop` that has not taken effect yet. If the operation finishes first, or fails
        while being stopped, its own outcome stands. Either way, the queue execution stops once the attempt ends.
        Sends no queue revision, so a stop never fails because the queue changed.

        Parameters
        ----------
        attempt_uid : str
            The live attempt.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        Attempt
            The attempt, with ``stop_now_requested_at`` set.

        Raises
        ------
        ApiError
            Code ``attempt_not_live`` if the attempt has already ended.
        """
        response = await self._mutate(
            "POST",
            f"{_API}/attempts/{quote(attempt_uid, safe='')}/stop-now",
            revision=None,
            idempotency_key=idempotency_key,
        )
        return Attempt.model_validate_json(response.content)

    async def acknowledge_recovery(
        self, note: str, *, revision: int | None = None, idempotency_key: str | None = None
    ) -> QueueExecution:
        """Clear the dispatch block left by an attempt whose outcome could not be proven. Needs the Run role and
        the lease.

        Succeeds only while RE Manager reports nothing running. The blocked execution becomes ``stopped`` and
        queued operations stay queued; start a new queue execution to continue.

        Parameters
        ----------
        note : str
            What the operator checked before clearing the block; recorded in the ``worker.fenced`` event.
        revision : int, optional
            Queue revision the acknowledgement is based on; defaults to `revision`.
        idempotency_key : str, optional
            Sent as ``Idempotency-Key``, so resending the call acts at most once; a new key is made otherwise.

        Returns
        -------
        QueueExecution
            The execution that was blocked.

        Raises
        ------
        StaleRevision
            The queue changed after ``revision``; nothing was cleared.
        ApiError
            Code ``not_blocked`` if nothing is blocked, or ``no_fence_evidence`` if the worker is not provably
            stopped.
        """
        response = await self._mutate(
            "POST",
            f"{_API}/recovery/acknowledge",
            json={"note": note},
            revision=self._if_match(revision),
            idempotency_key=idempotency_key,
        )
        return QueueExecution.model_validate_json(response.content)

    async def edit_queue(self, edit: Callable[[Queue], Awaitable[T]], *, attempts: int = 3) -> T:
        """Read the queue and call ``edit`` with it; edits made inside ``edit`` are based on that queue.

        The first edit ``edit`` makes sends the revision of the queue it was given, and each further one the
        revision its previous edit made, so any change by someone else in between fails the edit with
        `StaleRevision`. Then the queue is read again and ``edit`` is called again, at most ``attempts`` times in
        all. Edits that succeeded before the stale one stay made, so ``edit`` must decide from the queue it is
        given. ``edit`` runs in the calling task; edits it makes in tasks it starts are based on the queue too,
        but each other's edits do not advance their revisions.

        Parameters
        ----------
        edit : callable
            Receives the queue and makes its edits through this client.
        attempts : int, optional
            Most calls of ``edit``.

        Returns
        -------
        object
            What the last call of ``edit`` returned.

        Raises
        ------
        StaleRevision
            The queue changed during each of the ``attempts`` calls.
        """
        for _ in range(attempts - 1):
            try:
                return await edit(await self.queue())
            except StaleRevision:
                pass
        return await edit(await self.queue())

    @contextlib.asynccontextmanager
    async def hold_lease(self, ttl_seconds: int = 60) -> AsyncGenerator[LeaseHolder, None]:
        """Hold the control lease while in the context: acquire it, renew it in the background, release it on exit.

        A lease the caller already holds is adopted. Renewal happens every third of ``ttl_seconds``; if the lease
        is lost (expired, overridden or released elsewhere), renewal stops and `LeaseHolder.lost` says why.

        Parameters
        ----------
        ttl_seconds : int, optional
            Seconds each grant lasts unless renewed.

        Yields
        ------
        LeaseHolder

        Raises
        ------
        ApiError
            Code ``lease_held`` if someone else holds the lease; on exit, any error releasing it, unless the
            context's body raised, in which case that error is logged.
        """
        try:
            lease = await self.acquire_lease(ttl_seconds)
        except ApiError as error:
            if error.code != "lease_held":
                raise
            try:
                # Adopts a lease the caller already holds.
                lease = await self.renew_lease(ttl_seconds)
            except ApiError as renewal_error:
                if renewal_error.code == "lease_required":
                    raise error from None
                raise
        holder = LeaseHolder(lease)
        task = asyncio.create_task(self._keep_renewed(holder, ttl_seconds), name="lease-renewal")
        body_failed = True
        try:
            yield holder
            body_failed = False
        finally:
            task.cancel()
            await asyncio.wait([task])
            if holder.lost is None:
                try:
                    await self.release_lease()
                except (ApiError, httpx.TransportError) as error:
                    if isinstance(error, ApiError) and error.code == "lease_required":
                        pass  # it lapsed or was taken since the last renewal
                    elif not body_failed:
                        raise
                    else:
                        logger.warning("could not release the control lease: %s", error)

    async def _keep_renewed(self, holder: LeaseHolder, ttl_seconds: int) -> None:
        interval = ttl_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                holder.lease = await self.renew_lease(ttl_seconds)
            except (ApiError, httpx.TransportError) as error:
                if isinstance(error, ApiError) and error.code == "lease_required":
                    holder.lost = error
                    logger.warning("lost the control lease: %s", error.message)
                    return
                # The lease stays valid until it expires, so two more tries remain.
                logger.warning("could not renew the control lease, retrying in %g s: %s", interval, error)

    async def stream_events(
        self,
        after: int = 0,
        *,
        reconnect_seconds: float = 1.0,
        max_reconnect_seconds: float = 30.0,
        idle_timeout_seconds: float = 60.0,
    ) -> AsyncGenerator[Event, None]:
        """Yield every event after ``after``, then each new one; never ends on its own.

        Reconnects with ``Last-Event-ID``, so nothing is missed or repeated across server restarts and token
        expiry; supply a callable token to stream past a token's expiry. Close it with ``aclose()``, e.g. through
        `contextlib.aclosing`; leaving an ``async for`` closes it only once it is garbage collected.

        After a network error or a 408, 429, 500, 502, 503 or 504, it reconnects after ``reconnect_seconds``,
        doubled after each further failure in a row up to ``max_reconnect_seconds``, with jitter, or after the
        server's ``Retry-After``, if longer. Events never change `revision`.

        Parameters
        ----------
        after : int, optional
            Yield events whose ``event_id`` is greater than this.
        reconnect_seconds : float, optional
            Wait before reconnecting after the stream ends or first fails.
        max_reconnect_seconds : float, optional
            Longest wait before reconnecting, unless the server's ``Retry-After`` asks for longer.
        idle_timeout_seconds : float, optional
            Longest silence before the connection is deemed lost. The server sends a heartbeat every 15 s by
            default, so this must exceed that.

        Yields
        ------
        Event

        Raises
        ------
        ApiError
            The server refused the stream with any other error status, e.g. 401 for an expired static token;
            not retried.
        """
        cursor = after
        timeout = httpx.Timeout(self._timeout, read=idle_timeout_seconds)
        failures = 0
        while True:
            retry_after = None
            try:
                async with self._http.stream(
                    "GET",
                    f"{_API}/events/stream",
                    headers={**await self._auth(), "Last-Event-ID": str(cursor)},
                    timeout=timeout,
                ) as response:
                    if response.is_error:
                        await response.aread()
                        if response.status_code not in _RETRIED_STREAM_STATUSES:
                            raise error_from_response(response)
                        failures += 1
                        retry_after = _retry_after(response)
                        logger.info("event stream refused with HTTP %d, reconnecting", response.status_code)
                    else:
                        failures = 0
                        parser = _EventStreamParser()
                        async for line in response.aiter_lines():
                            event = parser.feed(line)
                            if event is not None:
                                cursor = event.event_id
                                yield event
            except httpx.TransportError as error:
                # Connection refused or reset, or the idle timeout passed.
                failures += 1
                logger.info("event stream interrupted, reconnecting: %s", error)
            # Also after a normal end, so a server that ends each stream at once is not reconnected to in a loop.
            await asyncio.sleep(_backoff(failures, reconnect_seconds, max_reconnect_seconds, retry_after))


class _EventStreamParser:
    """Parses the subset of the WHATWG server-sent events format the server sends."""

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: str) -> Event | None:
        """Take one line of the stream, without its line ending; return the event it completes, if any."""
        if not line:
            if not self._data:
                return None
            data = "\n".join(self._data)
            self._data.clear()
            return Event.model_validate_json(data)
        if line.startswith(":"):
            return None  # a comment, such as a heartbeat
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        # ``id`` and ``event`` are ignored: the data carries them as ``event_id`` and ``event_type``.
        if field == "data":
            self._data.append(value)
        return None
