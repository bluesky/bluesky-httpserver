from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import signal
import socket
import threading
import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from bluesky_httpserver.v2.api import Services, create_app
from bluesky_httpserver.v2.catalog import Catalog
from bluesky_httpserver.v2.client import (
    ApiError,
    AsyncQueueServerClient,
    Event,
    InvalidParameters,
    Operation,
    OutcomeUnknown,
    Queue,
    QueueServerClient,
    StaleRevision,
    UnknownOperation,
)
from bluesky_httpserver.v2.gateway import WorkerGateway
from bluesky_httpserver.v2.relay import ManagerStatus
from bluesky_httpserver.v2.server import SHUTDOWN_GRACE_SECONDS, uvicorn_config
from bluesky_httpserver.v2.settings import ServerSettings
from bluesky_httpserver.v2.store import Store

from ..fake_relay import DEMO_COUNT, FakeRelay
from .conftest import FakeAuthenticator


class ScriptedRelay(FakeRelay):
    """Ends each item it runs as scripted: `outcomes` holds ``"completed"``, ``"failed"`` or
    ``"environment_lost"``, consumed one per item."""

    def __init__(self, catalog: Catalog) -> None:
        super().__init__(catalog)
        self.outcomes: list[str] = []

    async def status(self) -> ManagerStatus:
        if self.state.running_item_uid is not None and self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome == "environment_lost":
                self.state = dataclasses.replace(
                    self.state, manager_state="idle", running_item_uid=None, worker_environment_exists=False
                )
            elif outcome == "failed":
                self.finish_running("failed", "boom")
            else:
                self.finish_running(outcome)
        return await super().status()


class LiveController:
    """The v2 API served over HTTP by uvicorn in a thread of its own, with the fake relay and authenticator."""

    relay: ScriptedRelay

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.authenticator = FakeAuthenticator()
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        self.port = sock.getsockname()[1]
        self._server = uvicorn.Server(uvicorn_config(create_app(self._lifespan), ServerSettings()))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while True:
            with contextlib.suppress(httpx.TransportError):
                if httpx.get(f"{self.url}/ready").status_code == 200:
                    return
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the controller did not become ready")
            time.sleep(0.05)

    @contextlib.asynccontextmanager
    async def _lifespan(self, app: FastAPI) -> AsyncGenerator[None, None]:
        store = await Store.open(self.db_path)
        self.relay = ScriptedRelay(Catalog([DEMO_COUNT]))
        gateway = WorkerGateway(store=store, relay=self.relay, user_group="primary", poll_seconds=0.02)
        app.state.services = Services(store, self.authenticator, gateway, sse_heartbeat_seconds=0.2)
        task = asyncio.create_task(gateway.run())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.wait([task])
            await store.close()

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(SHUTDOWN_GRACE_SECONDS + 10)
        assert not self._thread.is_alive(), "the controller did not stop"

    def restart(self) -> None:
        """Stop, then serve the same store at the same address again."""
        self.stop()
        self.start()


@pytest.fixture
def live(db_path: Path) -> Iterator[LiveController]:
    controller = LiveController(db_path)
    controller.start()
    yield controller
    controller.stop()


class ScriptedTransport(httpx.AsyncBaseTransport):
    """Passes requests on to ``inner``, recording those to ``path``; each of those takes the next step of
    ``script``, if any is left: ``"pass"``, ``"lose"`` (the server acts, then its reply is lost, as when a
    gateway times out), or a response to return without sending the request, as a proxy does."""

    def __init__(self, inner: httpx.AsyncBaseTransport, path: str, script: list[str | httpx.Response]) -> None:
        self.inner, self.path, self.script = inner, path, script
        self.requests: list[tuple[float, httpx.Request]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != self.path:
            return await self.inner.handle_async_request(request)
        self.requests.append((time.monotonic(), request))
        step = self.script.pop(0) if self.script else "pass"
        if isinstance(step, httpx.Response):
            return step
        response = await self.inner.handle_async_request(request)
        if step == "lose":
            await response.aread()
            await response.aclose()
            raise httpx.ReadTimeout("reply lost after the server acted", request=request)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def scripted(client: AsyncQueueServerClient, path: str, *script: str | httpx.Response) -> ScriptedTransport:
    """Send ``client``'s requests through a `ScriptedTransport`."""
    transport = ScriptedTransport(client._http._transport, path, list(script))
    client._http._transport = transport
    return transport


@pytest.mark.asyncio
async def test_queue_edits_track_the_revision(live: LiveController):
    async with (
        AsyncQueueServerClient(live.url, "alice") as editor,
        AsyncQueueServerClient(live.url, "alice") as rival,
        AsyncQueueServerClient(live.url, "bob") as bob,
    ):
        await editor.acquire_lease(60)
        assert editor.revision is None
        assert (await editor.queue()).revision == 0
        assert editor.revision == 0
        first = await editor.submit("demo.count", "1", {"num": 1})
        assert editor.revision == 1

        await rival.submit("demo.count", "1", {"num": 2}, revision=1)
        assert rival.revision == 2
        with pytest.raises(StaleRevision) as stale:
            await editor.cancel(first.operation_uid)
        assert (stale.value.current_revision, stale.value.status_code) == (2, 412)
        assert editor.revision == 1
        assert [o.parameters["num"] for o in (await editor.queue()).operations] == [1, 2]
        assert editor.revision == 2
        await editor.cancel(first.operation_uid)
        assert editor.revision == 3

        with pytest.raises(StaleRevision):
            await editor.submit("demo.count", "1", {"num": 3}, revision=1)
        await bob.queue()
        with pytest.raises(ApiError) as refused:
            await bob.submit("demo.count", "1", {"num": 3})
        assert (refused.value.code, refused.value.status_code) == ("lease_required", 403)
        assert refused.value.request_id


@pytest.mark.asyncio
async def test_edit_queue_redecides_on_a_stale_revision(live: LiveController):
    async with (
        AsyncQueueServerClient(live.url, "alice") as editor,
        AsyncQueueServerClient(live.url, "alice") as rival,
    ):
        await editor.acquire_lease(60)
        seen: list[int] = []

        async def edit(queue: Queue) -> Operation:
            seen.append(len(queue.operations))
            if len(seen) == 1:
                await rival.submit("demo.count", "1", {"num": 5}, revision=queue.revision)
            return await editor.submit("demo.count", "1", {"num": len(queue.operations) + 1})

        operation = await editor.edit_queue(edit)
        assert seen == [0, 1]
        assert operation.parameters == {"num": 2}
        assert [o.parameters["num"] for o in (await editor.queue()).operations] == [5, 2]


@pytest.mark.asyncio
async def test_submit_is_checked_against_the_catalog(live: LiveController):
    async with AsyncQueueServerClient(live.url, "alice") as client:
        await client.acquire_lease(60)
        await client.queue()
        with pytest.raises(InvalidParameters) as invalid:
            await client.submit("demo.count", "1", {"num": 0})
        assert [e["path"] for e in invalid.value.errors] == ["$.num"]
        assert invalid.value.request_id is None
        with pytest.raises(InvalidParameters) as invalid:
            await client.submit("demo.count", "1", {"num": 1, "detectors": ["tertiary"]})
        assert [e["path"] for e in invalid.value.errors] == ["$.detectors[0]"]
        with pytest.raises(UnknownOperation):
            await client.submit("demo.missing", "1", {})
        # Nothing was written.
        assert [e.event_type for e in await client.events()] == ["lease.acquired"]


@pytest.mark.asyncio
async def test_hold_lease_renews_releases_and_notices_loss(live: LiveController):
    async with (
        AsyncQueueServerClient(live.url, "alice") as alice,
        AsyncQueueServerClient(live.url, "admin") as admin,
    ):
        async with alice.hold_lease(ttl_seconds=5) as holder:
            expires_at = holder.lease.expires_at
            await asyncio.sleep(2.5)
            assert holder.lease.expires_at > expires_at
            lease = await alice.lease()
            assert lease is not None and lease.holder == "alice"
        assert await alice.lease() is None

        await alice.acquire_lease(60)
        async with alice.hold_lease(ttl_seconds=5) as holder:  # adopts the lease alice holds
            await admin.override_lease("maintenance", 60)
            async with asyncio.timeout(5):
                while holder.lost is None:
                    await asyncio.sleep(0.05)
            assert holder.lost.code == "lease_required"
        lease = await alice.lease()
        assert lease is not None and lease.holder == "admin"


@pytest.mark.asyncio
async def test_event_stream_resumes_across_a_restart(live: LiveController):
    async with (
        AsyncQueueServerClient(live.url, "reader") as reader,
        AsyncQueueServerClient(live.url, "alice") as editor,
    ):
        await editor.acquire_lease(3600)
        await editor.queue()
        await editor.submit("demo.count", "1", {"num": 1})
        async with contextlib.aclosing(reader.stream_events(reconnect_seconds=0.1)) as stream:
            events = [await anext(stream), await anext(stream)]
            # Shutdown ends the open stream, after the grace period.
            await asyncio.to_thread(live.restart)
            await editor.submit("demo.count", "1", {"num": 2})
            async with asyncio.timeout(10):
                events.append(await anext(stream))
        assert [e.event_id for e in events] == [1, 2, 3]
        assert [e.event_type for e in events] == ["lease.acquired", "operation.submitted", "operation.submitted"]


@pytest.mark.asyncio
async def test_event_stream_reconnects_with_a_fresh_token_when_it_ends(live: LiveController):
    calls = 0

    def token() -> str:
        nonlocal calls
        calls += 1
        return "reader"

    async def first(events: AsyncGenerator[Event, None]) -> Event:
        return await anext(events)

    # Every stream ends as soon as it has sent its backlog.
    live.authenticator.expires_at = int(time.time())
    async with (
        AsyncQueueServerClient(live.url, token) as reader,
        AsyncQueueServerClient(live.url, "alice") as editor,
    ):
        async with contextlib.aclosing(reader.stream_events(reconnect_seconds=0.05)) as stream:
            task = asyncio.create_task(first(stream))
            await asyncio.sleep(0.3)
            await editor.acquire_lease(60)
            assert (await task).event_type == "lease.acquired"
        assert calls >= 2


def test_walkthrough(live: LiveController):
    """Spec C1's walkthrough: take control, submit, start, follow the events, see a failure, acknowledge."""
    live.relay.outcomes.extend(["failed", "environment_lost"])
    transcript: list[str] = []
    with QueueServerClient(live.url, "alice") as client, client.hold_lease(ttl_seconds=60):
        descriptor = client.catalog().descriptor("demo.count", "1")
        client.queue()
        for n in (1, 2):
            parameters = {"num": n, "detectors": ["primary"]}
            client.submit(descriptor.operation_id, descriptor.operation_version, parameters)
            transcript.append(f"submitted {n}")
        execution = client.start_execution("continue_on_failure")
        for event in client.stream_events():
            if event.queue_execution_uid != execution.queue_execution_uid:
                continue
            transcript.append(event.event_type)
            if event.event_type == "operation.failed":
                transcript.append(f"failed: {event.payload['diagnostic']}")
            elif event.event_type == "queue_execution.blocked":
                # Based on the queue as it is now: the scheduler has changed it since this thread's last edit.
                client.queue()
                client.acknowledge_recovery("checked that the worker is idle")
            elif event.event_type in ("queue_execution.stopped", "queue_execution.completed"):
                break
    assert transcript == [
        "submitted 1",
        "submitted 2",
        "queue_execution.started",
        "operation.claimed",
        "operation.running",
        "operation.failed",
        "failed: boom",
        "operation.claimed",
        "operation.running",
        "operation.unknown",
        "queue_execution.blocked",
        "worker.fenced",
        "queue_execution.stopped",
    ]


def test_interrupted_stream_leaves_the_client_usable(live: LiveController):
    main_ident = threading.main_thread().ident
    assert main_ident is not None
    with QueueServerClient(live.url, "reader") as client:
        timer = threading.Timer(0.5, signal.pthread_kill, args=(main_ident, signal.SIGINT))
        timer.start()
        with pytest.raises(KeyboardInterrupt):
            for _ in client.stream_events():
                pass
        timer.join()
        assert client.queue().revision == 0


async def _first(stream: AsyncGenerator[Event, None], event_type: str) -> Event:
    async with asyncio.timeout(10):
        async for event in stream:
            if event.event_type == event_type:
                return event
    raise AssertionError("the stream ended")


@pytest.mark.asyncio
@pytest.mark.parametrize("seen_through", ["events", "stream"])
async def test_seeing_a_change_through_events_does_not_rebase_edits(live: LiveController, seen_through: str):
    async with (
        AsyncQueueServerClient(live.url, "alice") as agent,
        AsyncQueueServerClient(live.url, "alice") as other_session,
    ):
        await agent.acquire_lease(60)
        read = await agent.queue()
        await other_session.submit("demo.count", "1", {"num": 99}, revision=read.revision)
        if seen_through == "events":
            assert "operation.submitted" in [e.event_type for e in await agent.events()]
        else:
            async with contextlib.aclosing(agent.stream_events(reconnect_seconds=0.05)) as stream:
                await _first(stream, "operation.submitted")
        assert agent.revision == read.revision
        with pytest.raises(StaleRevision) as stale:
            await agent.submit("demo.count", "1", {"num": 1})
        assert (stale.value.status_code, stale.value.revision) == (412, read.revision)
        assert [o.parameters["num"] for o in (await agent.queue()).operations] == [99]


@pytest.mark.asyncio
async def test_tasks_sharing_a_client_edit_from_their_own_reads(live: LiveController):
    async with AsyncQueueServerClient(live.url, "alice") as client:
        await client.acquire_lease(60)
        await client.queue()
        uids = [(await client.submit("demo.count", "1", {"num": n})).operation_uid for n in (1, 2)]
        bases: list[tuple[int, int | None]] = []

        async def remove_unless_last(uid: str, think_seconds: float) -> int:
            async def decide(queue: Queue) -> int:
                if len(queue.operations) <= 1:
                    return 0
                await asyncio.sleep(think_seconds)  # e.g. a model call between reading and writing
                bases.append((queue.revision, client.revision))
                await client.cancel(uid)
                return 1

            return await client.edit_queue(decide)

        removed = await asyncio.gather(remove_unless_last(uids[0], 0.3), remove_unless_last(uids[1], 0))
        assert removed == [0, 1]
        # The slower task still sent the revision it read, though the faster one's cancel had moved on.
        assert [read for read, sent in bases] == [sent for read, sent in bases] == [2, 2]
        assert [o.operation_uid for o in (await client.queue()).operations] == [uids[0]]


def test_threads_sharing_a_client_edit_from_their_own_reads(live: LiveController):
    with QueueServerClient(live.url, "alice") as client:
        client.acquire_lease(60)
        read = client.queue()

        def other_thread() -> None:
            client.queue()
            client.submit("demo.count", "1", {"num": 2})

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        assert client.revision == read.revision
        with pytest.raises(StaleRevision):
            client.submit("demo.count", "1", {"num": 1})
        assert [o.parameters["num"] for o in client.queue().operations] == [2]


@pytest.mark.asyncio
async def test_an_edit_without_a_read_is_refused_before_sending(live: LiveController):
    sent: list[str] = []

    async def record(request: httpx.Request) -> None:
        sent.append(f"{request.method} {request.url.path}")

    async with AsyncQueueServerClient(live.url, "alice") as client:
        await client.acquire_lease(60)
        client._http.event_hooks = {"request": [record]}
        edits = [
            client.submit("demo.count", "1", {"num": 1}),
            client.replace("op", "demo.count", "1", {"num": 1}),
            client.cancel("op"),
            client.reorder([]),
            client.start_execution(),
            client.stop_after_current("execution"),
            client.acknowledge_recovery("checked"),
        ]
        for edit in edits:
            with pytest.raises(ApiError) as refused:
                await edit
            assert (refused.value.status_code, refused.value.code) == (428, "precondition_required")
            assert (refused.value.request_id, refused.value.idempotency_key) == (None, None)
        assert sent == []


@pytest.mark.asyncio
async def test_a_mutation_is_resent_with_one_key_and_revision(live: LiveController):
    async with AsyncQueueServerClient(live.url, "alice", retry_seconds=0.01) as agent:
        await agent.acquire_lease(60)
        read = await agent.queue()
        bad_gateway = httpx.Response(502, html="<html>Bad Gateway</html>")
        unavailable = httpx.Response(503, headers={"Retry-After": "0"}, text="busy")
        transport = scripted(agent, "/api/v2/operations", "lose", bad_gateway, unavailable)
        operation = await agent.submit("demo.count", "1", {"num": 7})
        assert [o.operation_uid for o in (await agent.queue()).operations] == [operation.operation_uid]
        requests = [request for _, request in transport.requests]
        assert len(requests) == 4
        assert len({request.headers["Idempotency-Key"] for request in requests}) == 1
        assert {request.headers["If-Match"] for request in requests} == {f'"qrev-{read.revision}"'}


@pytest.mark.asyncio
async def test_a_failed_mutation_can_be_resent_from_its_error(live: LiveController):
    async with (
        AsyncQueueServerClient(live.url, "alice", retries=1, retry_seconds=0.01) as agent,
        AsyncQueueServerClient(live.url, "alice") as other_session,
    ):
        await agent.acquire_lease(60)
        read = await agent.queue()
        scripted(agent, "/api/v2/operations", "lose", "lose")
        with pytest.raises(OutcomeUnknown) as unknown:
            await agent.submit("demo.count", "1", {"num": 7})
        assert isinstance(unknown.value, httpx.TransportError)
        assert isinstance(unknown.value.__cause__, httpx.ReadTimeout)
        assert unknown.value.revision == read.revision
        # The queue moves on before the agent resends.
        await other_session.queue()
        await other_session.submit("demo.count", "1", {"num": 8})

        resent = await agent.submit(
            "demo.count",
            "1",
            {"num": 7},
            idempotency_key=unknown.value.idempotency_key,
            revision=unknown.value.revision,
        )
        queue = await agent.queue()
        assert [o.parameters["num"] for o in queue.operations] == [7, 8]
        assert resent.operation_uid == queue.operations[0].operation_uid

        # A refusal carries them too.
        with pytest.raises(StaleRevision) as stale:
            await agent.cancel(resent.operation_uid, revision=read.revision, idempotency_key="cancel-1")
        assert (stale.value.idempotency_key, stale.value.revision) == ("cancel-1", read.revision)


@pytest.mark.asyncio
async def test_event_stream_reconnects_after_transient_refusals(live: LiveController):
    tokens = iter(["reader", "idp-down", "reader", "reader", "reader", "expired"])
    # Every stream ends as soon as it has sent its backlog.
    live.authenticator.expires_at = int(time.time())
    async with (
        AsyncQueueServerClient(live.url, lambda: next(tokens)) as reader,
        AsyncQueueServerClient(live.url, "alice") as editor,
    ):
        await editor.acquire_lease(60)
        transport = scripted(
            reader,
            "/api/v2/events/stream",
            "pass",
            "pass",  # 503 identity_provider_unavailable, from the server
            httpx.Response(502, html="<html>Bad Gateway</html>"),
            httpx.Response(429, headers={"Retry-After": "1"}, text="slow down"),
        )
        stream = reader.stream_events(reconnect_seconds=0.01, max_reconnect_seconds=0.05)
        try:
            assert (await anext(stream)).event_type == "lease.acquired"
            await editor.queue()
            await editor.submit("demo.count", "1", {"num": 1})
            async with asyncio.timeout(10):
                assert (await anext(stream)).event_type == "operation.submitted"
            with pytest.raises(ApiError) as refused:
                async with asyncio.timeout(10):
                    await anext(stream)
            assert (refused.value.status_code, refused.value.code) == (401, "unauthenticated")
        finally:
            await stream.aclose()
        times = [at for at, _ in transport.requests]
        assert [request.headers["Last-Event-ID"] for _, request in transport.requests] == [
            "0",
            "1",
            "1",
            "1",
            "1",
            "2",
        ]
        assert times[4] - times[3] >= 1  # the 429's Retry-After


@pytest.mark.asyncio
async def test_stop_now_ends_the_active_execution(live: LiveController):
    live.relay.deferred_pause_lands = False  # the plan is in a long move: only an immediate pause lands
    async with (
        AsyncQueueServerClient(live.url, "alice") as alice,
        AsyncQueueServerClient(live.url, "carol") as carol,
    ):
        idle = await carol.active_execution()
        assert (idle.queue_execution, idle.live_attempt) == (None, None)
        await alice.acquire_lease(60)
        await alice.queue()
        operation = await alice.submit("demo.count", "1", {"num": 1})
        execution = await alice.start_execution()
        async with asyncio.timeout(10):
            while (active := await carol.active_execution()).live_attempt is None:
                await asyncio.sleep(0.02)
        assert active.queue_execution is not None and active.live_attempt is not None
        assert active.queue_execution.queue_execution_uid == execution.queue_execution_uid
        assert active.live_attempt.operation_uid == operation.operation_uid

        # carol has the Run role but not the lease.
        attempt = await carol.stop_now(active.live_attempt.attempt_uid)
        assert attempt.stop_now_requested_at is not None and attempt.safe_stop_requested_at is None
        async with asyncio.timeout(10):
            while (await carol.active_execution()).queue_execution is not None:
                await asyncio.sleep(0.02)
        assert (await carol.attempt(attempt.attempt_uid)).stop_now_requested_at == attempt.stop_now_requested_at
        assert (await carol.queue_execution(execution.queue_execution_uid)).state == "stopped"
        with pytest.raises(ApiError) as refused:
            await carol.stop_now(attempt.attempt_uid)
        assert (refused.value.status_code, refused.value.code) == (409, "attempt_not_live")
