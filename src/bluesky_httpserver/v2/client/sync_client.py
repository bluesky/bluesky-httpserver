"""Blocking client of the v2 controller's HTTP API, running the asynchronous one on a thread of its own."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import ssl
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine, Generator, Mapping, Sequence
from typing import Any, Literal, Self, TypeVar

from .async_client import AsyncQueueServerClient, LeaseHolder
from .errors import StaleRevision
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

T = TypeVar("T")


class _LoopThread:
    """An event loop running in a daemon thread of its own."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="bluesky-httpserver-client", daemon=True
        )
        self._thread.start()

    def run(self, coroutine: Coroutine[Any, Any, T], context: contextvars.Context | None = None) -> T:
        """Run ``coroutine`` on the loop, in ``context`` if given, and return its result; an interruption cancels
        it."""
        ended = threading.Event()
        tasks: list[asyncio.Task[T]] = []

        def start() -> None:
            task = self._loop.create_task(coroutine, context=context)
            task.add_done_callback(lambda _: ended.set())
            tasks.append(task)

        self._loop.call_soon_threadsafe(start)
        try:
            ended.wait()
        except BaseException:
            # Interrupted, e.g. by Ctrl-C: stop the coroutine and wait until it has, so what it used can be used
            # again. Callbacks run in order, so the task exists by the time it is cancelled.
            self._loop.call_soon_threadsafe(lambda: tasks[0].cancel())
            ended.wait()
            raise
        return tasks[0].result()

    def close(self) -> None:
        """Cancel what still runs on the loop, then stop the loop and its thread."""

        async def cancel_others() -> None:
            current = asyncio.current_task()
            others = [task for task in asyncio.all_tasks() if task is not current]
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)
            await self._loop.shutdown_asyncgens()

        self.run(cancel_others())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()


async def _next(events: AsyncGenerator[Event, None]) -> Event:
    return await anext(events)


class QueueServerClient:
    """Blocking client of the v2 controller's HTTP API; see `AsyncQueueServerClient`.

    A client may be shared by threads. Each thread's queue edits are based on the queue that thread read, as each
    task's are with `AsyncQueueServerClient`. Use it as ``with``, or call `close` when done.

    Parameters
    ----------
    base_url : str
        Address of the controller, e.g. ``https://controller.example``.
    token : str or callable
        Bearer token, or a callable called before each request that returns one, so a long-lived client can
        refresh it. The callable is called on the client's own thread.
    timeout_seconds : float, optional
        Timeout of each network operation: connecting, sending, or waiting for a response.
    verify : ssl.SSLContext or bool, optional
        How to verify the server's TLS certificate; True uses the system's trusted certificates.
    retries : int, optional
        Most times a mutation is resent after a network error or a 502, 503 or 504.
    retry_seconds : float, optional
        Wait before the first resend; each further one waits about twice as long.
    """

    def __init__(
        self,
        base_url: str,
        token: str | Callable[[], str],
        *,
        timeout_seconds: float = 10.0,
        verify: ssl.SSLContext | bool = True,
        retries: int = 3,
        retry_seconds: float = 0.5,
    ) -> None:
        self._loop = _LoopThread()
        self._client = AsyncQueueServerClient(
            base_url,
            token,
            timeout_seconds=timeout_seconds,
            verify=verify,
            retries=retries,
            retry_seconds=retry_seconds,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run ``coroutine`` on the client's thread in a copy of the calling thread's context, then copy the
        queue revision it leaves back, so each thread has its own."""
        context = contextvars.copy_context()
        try:
            return self._loop.run(coroutine, context)
        finally:
            basis = self._client._basis
            basis.set(context.get(basis))

    def close(self) -> None:
        """Close the client's connections, then stop its thread."""
        self._loop.run(self._client.aclose())
        self._loop.close()

    @property
    def revision(self) -> int | None:
        """`AsyncQueueServerClient.revision`, of the calling thread."""
        return self._client.revision

    def readiness(self) -> Readiness:
        """Blocking `AsyncQueueServerClient.readiness`."""
        return self._run(self._client.readiness())

    def catalog(self) -> OperationCatalog:
        """Blocking `AsyncQueueServerClient.catalog`."""
        return self._run(self._client.catalog())

    def queue(self) -> Queue:
        """Blocking `AsyncQueueServerClient.queue`."""
        return self._run(self._client.queue())

    def operation(self, operation_uid: str) -> Operation:
        """Blocking `AsyncQueueServerClient.operation`."""
        return self._run(self._client.operation(operation_uid))

    def attempt(self, attempt_uid: str) -> Attempt:
        """Blocking `AsyncQueueServerClient.attempt`."""
        return self._run(self._client.attempt(attempt_uid))

    def queue_execution(self, queue_execution_uid: str) -> QueueExecution:
        """Blocking `AsyncQueueServerClient.queue_execution`."""
        return self._run(self._client.queue_execution(queue_execution_uid))

    def active_execution(self) -> ActiveExecution:
        """Blocking `AsyncQueueServerClient.active_execution`."""
        return self._run(self._client.active_execution())

    def lease(self) -> Lease | None:
        """Blocking `AsyncQueueServerClient.lease`."""
        return self._run(self._client.lease())

    def events(self, after: int = 0, limit: int = 100) -> list[Event]:
        """Blocking `AsyncQueueServerClient.events`."""
        return self._run(self._client.events(after, limit))

    def acquire_lease(self, ttl_seconds: int, *, idempotency_key: str | None = None) -> Lease:
        """Blocking `AsyncQueueServerClient.acquire_lease`."""
        return self._run(self._client.acquire_lease(ttl_seconds, idempotency_key=idempotency_key))

    def renew_lease(self, ttl_seconds: int, *, idempotency_key: str | None = None) -> Lease:
        """Blocking `AsyncQueueServerClient.renew_lease`."""
        return self._run(self._client.renew_lease(ttl_seconds, idempotency_key=idempotency_key))

    def release_lease(self, *, idempotency_key: str | None = None) -> None:
        """Blocking `AsyncQueueServerClient.release_lease`."""
        return self._run(self._client.release_lease(idempotency_key=idempotency_key))

    def override_lease(self, reason: str, ttl_seconds: int, *, idempotency_key: str | None = None) -> Lease:
        """Blocking `AsyncQueueServerClient.override_lease`."""
        return self._run(self._client.override_lease(reason, ttl_seconds, idempotency_key=idempotency_key))

    def submit(
        self,
        operation_id: str,
        operation_version: str,
        parameters: Mapping[str, Any],
        *,
        revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        """Blocking `AsyncQueueServerClient.submit`."""
        return self._run(
            self._client.submit(
                operation_id, operation_version, parameters, revision=revision, idempotency_key=idempotency_key
            )
        )

    def replace(
        self,
        operation_uid: str,
        operation_id: str,
        operation_version: str,
        parameters: Mapping[str, Any],
        *,
        revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        """Blocking `AsyncQueueServerClient.replace`."""
        return self._run(
            self._client.replace(
                operation_uid,
                operation_id,
                operation_version,
                parameters,
                revision=revision,
                idempotency_key=idempotency_key,
            )
        )

    def cancel(
        self, operation_uid: str, *, revision: int | None = None, idempotency_key: str | None = None
    ) -> Operation:
        """Blocking `AsyncQueueServerClient.cancel`."""
        return self._run(self._client.cancel(operation_uid, revision=revision, idempotency_key=idempotency_key))

    def reorder(
        self, operation_uids: Sequence[str], *, revision: int | None = None, idempotency_key: str | None = None
    ) -> Queue:
        """Blocking `AsyncQueueServerClient.reorder`."""
        return self._run(self._client.reorder(operation_uids, revision=revision, idempotency_key=idempotency_key))

    def start_execution(
        self,
        policy: Literal["stop_on_failure", "continue_on_failure"] = "stop_on_failure",
        *,
        revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> QueueExecution:
        """Blocking `AsyncQueueServerClient.start_execution`."""
        return self._run(self._client.start_execution(policy, revision=revision, idempotency_key=idempotency_key))

    def stop_after_current(
        self, queue_execution_uid: str, *, revision: int | None = None, idempotency_key: str | None = None
    ) -> QueueExecution:
        """Blocking `AsyncQueueServerClient.stop_after_current`."""
        return self._run(
            self._client.stop_after_current(
                queue_execution_uid, revision=revision, idempotency_key=idempotency_key
            )
        )

    def safe_stop(self, attempt_uid: str, *, idempotency_key: str | None = None) -> Attempt:
        """Blocking `AsyncQueueServerClient.safe_stop`."""
        return self._run(self._client.safe_stop(attempt_uid, idempotency_key=idempotency_key))

    def stop_now(self, attempt_uid: str, *, idempotency_key: str | None = None) -> Attempt:
        """Blocking `AsyncQueueServerClient.stop_now`."""
        return self._run(self._client.stop_now(attempt_uid, idempotency_key=idempotency_key))

    def acknowledge_recovery(
        self, note: str, *, revision: int | None = None, idempotency_key: str | None = None
    ) -> QueueExecution:
        """Blocking `AsyncQueueServerClient.acknowledge_recovery`."""
        return self._run(
            self._client.acknowledge_recovery(note, revision=revision, idempotency_key=idempotency_key)
        )

    def edit_queue(self, edit: Callable[[Queue], T], *, attempts: int = 3) -> T:
        """Blocking `AsyncQueueServerClient.edit_queue`; ``edit`` runs on the caller's thread."""
        for _ in range(attempts - 1):
            try:
                return edit(self.queue())
            except StaleRevision:
                pass
        return edit(self.queue())

    @contextlib.contextmanager
    def hold_lease(self, ttl_seconds: int = 60) -> Generator[LeaseHolder, None, None]:
        """Blocking `AsyncQueueServerClient.hold_lease`; renewal runs on the client's thread meanwhile."""
        manager = self._client.hold_lease(ttl_seconds)
        holder = self._run(manager.__aenter__())
        try:
            yield holder
        except BaseException as error:
            if not self._run(manager.__aexit__(type(error), error, error.__traceback__)):
                raise
        else:
            self._run(manager.__aexit__(None, None, None))

    def stream_events(
        self,
        after: int = 0,
        *,
        reconnect_seconds: float = 1.0,
        max_reconnect_seconds: float = 30.0,
        idle_timeout_seconds: float = 60.0,
    ) -> Generator[Event, None, None]:
        """Blocking `AsyncQueueServerClient.stream_events`; closing the generator closes the stream."""
        events = self._client.stream_events(
            after,
            reconnect_seconds=reconnect_seconds,
            max_reconnect_seconds=max_reconnect_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        try:
            while True:
                yield self._run(_next(events))
        finally:
            self._run(events.aclose())
