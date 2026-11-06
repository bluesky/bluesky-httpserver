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


@pytest.mark.asyncio
async def test_queue_edits_track_the_revision(live: LiveController):
    async with (
        AsyncQueueServerClient(live.url, "alice") as editor,
        AsyncQueueServerClient(live.url, "alice") as rival,
        AsyncQueueServerClient(live.url, "bob") as bob,
    ):
        await editor.acquire_lease(60)
        assert editor.revision is None
        first = await editor.submit("demo.count", "1", {"num": 1})
        assert editor.revision == 1

        await rival.submit("demo.count", "1", {"num": 2})
        with pytest.raises(StaleRevision) as stale:
            await editor.cancel(first.operation_uid)
        assert (stale.value.current_revision, stale.value.status_code) == (2, 412)
        assert [o.parameters["num"] for o in (await editor.queue()).operations] == [1, 2]
        assert editor.revision == 2
        await editor.cancel(first.operation_uid)
        assert editor.revision == 3

        with pytest.raises(StaleRevision):
            await editor.submit("demo.count", "1", {"num": 3}, revision=1)
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
                await rival.submit("demo.count", "1", {"num": 5})
            return await editor.submit("demo.count", "1", {"num": len(queue.operations) + 1})

        operation = await editor.edit_queue(edit)
        assert seen == [0, 1]
        assert operation.parameters == {"num": 2}
        assert [o.parameters["num"] for o in (await editor.queue()).operations] == [5, 2]


@pytest.mark.asyncio
async def test_submit_is_checked_against_the_catalog(live: LiveController):
    async with AsyncQueueServerClient(live.url, "alice") as client:
        await client.acquire_lease(60)
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
                # Based on the queue revision the streamed events raised the client's revision to.
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
