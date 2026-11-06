"""Asynchronous client of the v2 controller's HTTP API."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import ssl
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self, TypeVar
from urllib.parse import quote

import httpx

from .errors import ApiError, StaleRevision, UnknownOperation, error_from_response
from .models import Attempt, Event, Lease, Operation, OperationCatalog, Queue, QueueExecution, Readiness

logger = logging.getLogger(__name__)

T = TypeVar("T")

Token = str | Callable[[], str | Awaitable[str]]
"""A bearer token, or a callable that returns one, possibly awaitable."""

_API = "/api/v2"
_ETAG = re.compile(r'"qrev-(0|[1-9][0-9]*)"')


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

    Raises
    ------
    ApiError
        From every request the server refuses; `StaleRevision`, `InvalidParameters` and `UnknownOperation` for
        the codes callers most often handle.
    httpx.TransportError
        From every request that fails in the network.
    """

    def __init__(
        self,
        base_url: str,
        token: Token,
        *,
        timeout_seconds: float = 10.0,
        verify: ssl.SSLContext | bool = True,
    ) -> None:
        self._token = token
        self._timeout = timeout_seconds
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds, verify=verify)
        self._revision: int | None = None
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
        """Queue revision of the newest queue state this client has seen, from a response's ``ETag`` or an
        event's ``queue_revision``. Queue edits send it as ``If-Match`` unless given ``revision=``.

        A response's ``ETag`` sets it exactly, so after a store restore, reading the queue fixes it. Events only
        raise it, so replaying old events never lowers it.
        """
        return self._revision

    async def _auth(self) -> dict[str, str]:
        token = self._token() if callable(self._token) else self._token
        if inspect.isawaitable(token):
            token = await token
        return {"Authorization": f"Bearer {token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        response = await self._http.request(
            method, path, json=json, params=params, headers={**(headers or {}), **await self._auth()}
        )
        if response.is_error:
            raise error_from_response(response)
        match = _ETAG.fullmatch(response.headers.get("ETag", ""))
        if match is not None:
            self._revision = int(match.group(1))
        return response

    def _saw_events(self, events: Iterable[Event]) -> None:
        newest = max((event.queue_revision for event in events), default=None)
        if newest is not None and (self._revision is None or newest > self._revision):
            self._revision = newest

    async def _mutate(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        if_match: bool,
        revision: int | None,
        idempotency_key: str | None,
    ) -> httpx.Response:
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        if if_match:
            revision = revision if revision is not None else self._revision
            if revision is None:
                revision = (await self.queue()).revision
            headers["If-Match"] = f'"qrev-{revision}"'
        return await self._request(method, path, json=json, headers=headers)

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

        Returns
        -------
        Queue
        """
        response = await self._request("GET", f"{_API}/queue")
        return Queue.model_validate_json(response.content)

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
        events = [Event.model_validate(event) for event in response.json()["events"]]
        self._saw_events(events)
        return events

    async def acquire_lease(self, ttl_seconds: int, *, idempotency_key: str | None = None) -> Lease:
        """Take control of the instrument. Needs the Edit role.

        Only the holder may edit the queue or start and stop queue executions. See also `hold_lease`.

        Parameters
        ----------
        ttl_seconds : int
            Seconds from now until control lapses unless renewed.
        idempotency_key : str, optional
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
            if_match=False,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
            if_match=False,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

        Raises
        ------
        ApiError
            Code ``lease_required`` if the caller does not hold the lease.
        """
        await self._mutate(
            "POST",
            f"{_API}/control-lease/release",
            if_match=False,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

        Returns
        -------
        Lease
        """
        response = await self._mutate(
            "POST",
            f"{_API}/control-lease/override",
            json={"reason": reason, "ttl_seconds": ttl_seconds},
            if_match=False,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
        await self._checked(operation_id, operation_version, parameters)
        response = await self._mutate(
            "POST",
            f"{_API}/operations",
            json={
                "operation_id": operation_id,
                "operation_version": operation_version,
                "parameters": dict(parameters),
            },
            if_match=True,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
        await self._checked(operation_id, operation_version, parameters)
        response = await self._mutate(
            "PUT",
            f"{_API}/operations/{quote(operation_uid, safe='')}",
            json={
                "operation_id": operation_id,
                "operation_version": operation_version,
                "parameters": dict(parameters),
            },
            if_match=True,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
            if_match=True,
            revision=revision,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
            if_match=True,
            revision=revision,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
            if_match=True,
            revision=revision,
            idempotency_key=idempotency_key,
        )
        return QueueExecution.model_validate_json(response.content)

    async def stop_after_current(
        self, queue_execution_uid: str, *, revision: int | None = None, idempotency_key: str | None = None
    ) -> QueueExecution:
        """Stop a running queue execution once its current operation finishes. Needs the Run role and the lease.

        The execution becomes ``stopping``, or ``stopped`` at once if no operation is claimed or running.
        Operations not yet taken to run stay queued.

        Parameters
        ----------
        queue_execution_uid : str
            The running queue execution.
        revision : int, optional
            Queue revision the stop is based on; defaults to `revision`.
        idempotency_key : str, optional
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
            if_match=True,
            revision=revision,
            idempotency_key=idempotency_key,
        )
        return QueueExecution.model_validate_json(response.content)

    async def safe_stop(self, attempt_uid: str, *, idempotency_key: str | None = None) -> Attempt:
        """Stop a live attempt safely. Needs the Run role and the lease.

        The controller pauses the plan at its next checkpoint and aborts it; if the operation finishes first, its
        own outcome stands. An aborted attempt stops the queue execution. Sends no queue revision, so an
        emergency stop never fails because the queue changed.

        Parameters
        ----------
        attempt_uid : str
            The live attempt.
        idempotency_key : str, optional
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
            if_match=False,
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
            Pass your own key to retry safely after a network error; a new key is made otherwise.

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
            if_match=True,
            revision=revision,
            idempotency_key=idempotency_key,
        )
        return QueueExecution.model_validate_json(response.content)

    async def edit_queue(self, edit: Callable[[Queue], Awaitable[T]], *, attempts: int = 3) -> T:
        """Read the queue and call ``edit`` with it; edits made inside ``edit`` send that queue's revision.

        If one fails with `StaleRevision` because the queue changed meanwhile, the queue is read again and
        ``edit`` is called again, at most ``attempts`` times in all. Edits that succeeded before the stale one stay
        made, so ``edit`` must decide from the queue it is given.

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
        self, after: int = 0, *, reconnect_seconds: float = 1.0, idle_timeout_seconds: float = 60.0
    ) -> AsyncGenerator[Event, None]:
        """Yield every event after ``after``, then each new one; never ends on its own.

        Reconnects with ``Last-Event-ID``, so nothing is missed or repeated across server restarts and token
        expiry; supply a callable token to stream past a token's expiry. Close it with ``aclose()``, e.g. through
        `contextlib.aclosing`; leaving an ``async for`` closes it only once it is garbage collected.

        Parameters
        ----------
        after : int, optional
            Yield events whose ``event_id`` is greater than this.
        reconnect_seconds : float, optional
            Wait before reconnecting after the stream ends or the connection fails.
        idle_timeout_seconds : float, optional
            Longest silence before the connection is deemed lost. The server sends a heartbeat every 15 s by
            default, so this must exceed that.

        Yields
        ------
        Event

        Raises
        ------
        ApiError
            The server refused the stream, e.g. 401 for an expired static token; not retried.
        """
        cursor = after
        timeout = httpx.Timeout(self._timeout, read=idle_timeout_seconds)
        while True:
            try:
                async with self._http.stream(
                    "GET",
                    f"{_API}/events/stream",
                    headers={**await self._auth(), "Last-Event-ID": str(cursor)},
                    timeout=timeout,
                ) as response:
                    if response.is_error:
                        await response.aread()
                        raise error_from_response(response)
                    parser = _EventStreamParser()
                    async for line in response.aiter_lines():
                        event = parser.feed(line)
                        if event is not None:
                            cursor = event.event_id
                            self._saw_events([event])
                            yield event
            except httpx.TransportError as error:
                # Connection refused or reset, or the idle timeout passed.
                logger.info("event stream interrupted, reconnecting in %g s: %s", reconnect_seconds, error)
            # Also after a normal end, so a server that ends each stream at once is not reconnected to in a loop.
            await asyncio.sleep(reconnect_seconds)


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
