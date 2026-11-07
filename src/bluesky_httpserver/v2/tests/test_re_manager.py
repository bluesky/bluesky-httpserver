"""The controller against a real RE Manager in controller-fronted mode.

Runs in the ``v2-*`` pixi environments, whose RE Manager has controller-fronted mode, and needs Redis on
localhost, as RE Manager's own tests do. Skipped where RE Manager lacks the mode.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import zmq

from bluesky_httpserver.v2.gateway import WorkerGateway
from bluesky_httpserver.v2.store import AttemptState, EventRecord, EventType, OperationState, Store
from bluesky_httpserver.v2.zmq_relay import ZmqManagerRelay

from .conftest import CLIENT, acquire, start

# Startup code of the worker, which runs in RE Manager's environment.
STARTUP = """
from bluesky import RunEngine
from bluesky.plans import count
from ophyd.sim import det1, det2

from bluesky_queueserver import operation

RE = RunEngine({})


@operation(
    id="demo.count",
    version="1",
    request_model={
        "type": "object",
        "properties": {
            "detectors": {"type": "array", "items": {"type": "string"}},
            "num": {"type": "integer", "minimum": 1},
            "delay": {"type": "number", "minimum": 0},
        },
        "required": ["detectors"],
        "additionalProperties": False,
    },
    devices={"detectors": {"primary": "det1", "secondary": "det2"}},
)
def count_operation(detectors, num=1, delay=0):
    yield from count(detectors, num=num, delay=delay)
"""

CONTROLLER = """\
store_path = "{store_path}"
user_group = "primary"

[identity]
provider = "oidc"
issuer = "https://idp.example/realms/facility"
audience = "queueserver-api"
jwks_uri = "https://idp.example/realms/facility/keys"

[manager]
control_address = "{control}"
info_address = "{info}"

[server]
port = 0
"""

PERMISSIONS = """
user_groups:
  root:
    allowed_plans: [null]
    allowed_devices: [null]
  primary:
    allowed_plans: [":.*"]
    allowed_devices: [":?.*"]
"""


def request(address: str, method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> Any:
    """Send one request to RE Manager; None if it does not answer within ``timeout`` seconds."""
    with zmq.Context() as context, context.socket(zmq.REQ) as socket:
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(address)
        socket.send_json({"method": method, "params": params or {}})
        if not socket.poll(int(timeout * 1000)):
            return None
        return socket.recv_json()


class Manager(NamedTuple):
    control: str
    info: str


@pytest.fixture
def re_manager(tmp_path: Path) -> Iterator[Manager]:
    """Start RE Manager in controller-fronted mode with a worker that offers ``demo.count``."""
    executable = shutil.which("start-re-manager")
    assert executable is not None, "start-re-manager is not installed"
    usage = subprocess.run([executable, "-h"], capture_output=True, text=True, timeout=60).stdout
    if "--controller-fronted" not in usage:
        pytest.skip("this RE Manager has no controller-fronted mode; run in a v2-* pixi environment")
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "00-startup.py").write_text(STARTUP)
    (startup / "user_group_permissions.yaml").write_text(PERMISSIONS)
    manager = Manager(f"ipc://{tmp_path}/control", f"ipc://{tmp_path}/info")
    log = tmp_path / "manager.log"

    with log.open("w") as output:
        process = subprocess.Popen(
            [
                executable,
                "--controller-fronted=ON",
                f"--zmq-control-addr={manager.control}",
                f"--zmq-info-addr={manager.info}",
                "--zmq-publish-info=ON",
                f"--startup-dir={startup}",
                f"--existing-plans-devices={startup}/existing_plans_and_devices.yaml",
                f"--user-group-permissions={startup}/user_group_permissions.yaml",
                "--update-existing-plans-devices=NEVER",
                "--redis-name-prefix=bluesky_httpserver_v2_tests",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 30
        while (status := request(manager.control, "status", timeout=0.5)) is None or status[
            "manager_state"
        ] != "idle":
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"RE Manager did not start (is Redis running?):\n{log.read_text()[-3000:]}")
        # The history persists in Redis between runs; the test reads only what it ran.
        assert request(manager.control, "history_clear")["success"]
        yield manager
    finally:
        # A controller-fronted manager refuses `manager_stop`; it is stopped as a service manager would stop it.
        # SIGTERM kills the worker as well.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


async def events_until(store: Store, event_type: EventType) -> list[EventRecord]:
    """Return the events up to and including the first of ``event_type``."""
    events: list[EventRecord] = []
    while True:
        for event in await store.events_after(events[-1].event_id if events else 0):
            events.append(event)
            if event.event_type is event_type:
                return events
        await store.wait_for_event(events[-1].event_id if events else 0)


@pytest.mark.asyncio
async def test_gateway_runs_an_operation_on_re_manager(re_manager, db_path):
    store = await Store.open(db_path)
    relay = ZmqManagerRelay(re_manager.control, re_manager.info, timeout_seconds=5.0)
    try:
        # Polling is a minute away: what the gateway sees promptly, it learns from published status.
        gateway = WorkerGateway(store=store, relay=relay, user_group="primary", poll_seconds=60)
        task = asyncio.create_task(gateway.run())
        try:
            # The gateway opens the worker environment and loads the catalog from the manager.
            async with asyncio.timeout(60):
                while not gateway.readiness().ready:
                    await asyncio.sleep(0.1)
            descriptor = gateway.catalog().descriptor("demo.count", "1")
            assert descriptor.plan_name == "count_operation"

            await acquire(store)
            _, revision = await store.write(
                lambda tx: tx.submit(
                    principal="alice",
                    client_id=CLIENT,
                    expected_revision=0,
                    operation_id="demo.count",
                    operation_version="1",
                    parameters={"detectors": ["secondary"], "num": 2},
                    descriptor_sha256=descriptor.sha256,
                )
            )
            await start(store, revision)
            async with asyncio.timeout(60):
                events = await events_until(store, EventType.QUEUE_EXECUTION_COMPLETED)
        finally:
            task.cancel()
            await asyncio.wait([task])

        (succeeded,) = [event for event in events if event.event_type is EventType.OPERATION_SUCCEEDED]
        attempt_uid = succeeded.attempt_uid
        assert attempt_uid is not None
        attempt = await store.read(lambda tx: tx.attempt(attempt_uid))
        assert attempt.state is AttemptState.SUCCEEDED
        assert len(succeeded.payload["run_uids"]) == 1

        # The manager ran the plan as the submitter, with device ids replaced by the worker's names.
        (item,) = request(re_manager.control, "history_get")["items"]
        assert item["item_uid"] == attempt.item_uid
        assert item["meta"]["attempt_uid"] == attempt_uid
        assert (item["name"], item["user"]) == ("count_operation", "alice")
        assert item["kwargs"] == {"detectors": ["det2"], "num": 2}
        assert item["result"]["exit_status"] == "completed"
    finally:
        relay.close()
        await store.close()


async def wait_until(condition: Callable[[], bool], what: str, timeout: float = 60) -> None:
    try:
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.1)
    except TimeoutError:
        pytest.fail(f"timed out waiting for {what}")


@pytest.mark.asyncio
async def test_killed_controller_resumes_the_running_attempt(re_manager, db_path, tmp_path):
    """Kill the controller process while its attempt runs, restart it: nothing committed is lost, the
    attempt is finished from the manager's history, and the plan is not sent again."""
    executable = shutil.which("bluesky-httpserver-v2")
    assert executable is not None
    relay = ZmqManagerRelay(re_manager.control, re_manager.info, timeout_seconds=5.0)
    try:
        # Open the environment, so that the operation can be submitted against the manager's catalog.
        assert request(re_manager.control, "environment_open")["success"]
        await wait_until(
            lambda: (
                "count_operation"
                in request(re_manager.control, "plans_allowed", {"user_group": "primary"})["plans_allowed"]
            ),
            "the catalog",
        )
        descriptor = (await relay.plans_allowed("primary")).catalog.descriptor("demo.count", "1")
        store = await Store.open(db_path)
        try:
            await acquire(store)
            _, revision = await store.write(
                lambda tx: tx.submit(
                    principal="alice",
                    client_id=CLIENT,
                    expected_revision=0,
                    operation_id="demo.count",
                    operation_version="1",
                    parameters={"detectors": ["primary"], "num": 5, "delay": 1},
                    descriptor_sha256=descriptor.sha256,
                )
            )
            await start(store, revision)
        finally:
            await store.close()

        config = tmp_path / "controller.toml"
        config.write_text(CONTROLLER.format(store_path=db_path, control=re_manager.control, info=re_manager.info))
        log = tmp_path / "controller.log"
        with log.open("w") as output:
            controller = subprocess.Popen(
                [executable, "serve", "--config", str(config)], stdout=output, stderr=subprocess.STDOUT
            )
        try:

            def running() -> bool:
                assert controller.poll() is None, log.read_text()
                return request(re_manager.control, "status")["running_item_uid"] is not None

            await wait_until(running, "the controller to start the plan")
            item_uid = request(re_manager.control, "status")["running_item_uid"]
            await asyncio.sleep(1)  # the controller records the item it started
        finally:
            controller.kill()
            controller.wait()

        store = await Store.open(db_path)
        try:
            attempt = await store.read(lambda tx: tx.live_attempt())
            assert attempt is not None
            assert (attempt.state, attempt.item_uid) == (AttemptState.RUNNING, item_uid)
            committed = await store.events_after(0)

            gateway = WorkerGateway(store=store, relay=relay, user_group="primary", poll_seconds=60)
            task = asyncio.create_task(gateway.run())
            try:
                async with asyncio.timeout(60):
                    events = await events_until(store, EventType.QUEUE_EXECUTION_COMPLETED)
            finally:
                task.cancel()
                await asyncio.wait([task])

            assert events[: len(committed)] == committed
            assert [event.event_id for event in events] == list(range(1, len(events) + 1))
            finished = await store.read(lambda tx: tx.attempt(attempt.attempt_uid))
            assert finished.state is AttemptState.SUCCEEDED
        finally:
            await store.close()

        (item,) = request(re_manager.control, "history_get")["items"]
        assert (item["item_uid"], item["result"]["exit_status"]) == (item_uid, "completed")
    finally:
        relay.close()


@pytest.mark.asyncio
async def test_stop_now_interrupts_a_wait(re_manager, db_path):
    """A count that waits 30 s between its readings ends within seconds of a stop now; a safe stop would wait
    for the next checkpoint, after the wait. The execution stops, and the operation behind it stays queued."""
    store = await Store.open(db_path)
    relay = ZmqManagerRelay(re_manager.control, re_manager.info, timeout_seconds=5.0)
    try:
        gateway = WorkerGateway(store=store, relay=relay, user_group="primary", poll_seconds=60)
        task = asyncio.create_task(gateway.run())
        try:
            await wait_until(lambda: gateway.readiness().ready, "the controller to be ready")
            descriptor = gateway.catalog().descriptor("demo.count", "1")
            await acquire(store)
            revision = 0
            operation_uids: list[str] = []
            for parameters in ({"detectors": ["primary"], "num": 2, "delay": 30}, {"detectors": ["primary"]}):
                record, revision = await store.write(
                    lambda tx, parameters=parameters, revision=revision: tx.submit(
                        principal="alice",
                        client_id=CLIENT,
                        expected_revision=revision,
                        operation_id="demo.count",
                        operation_version="1",
                        parameters=parameters,
                        descriptor_sha256=descriptor.sha256,
                    )
                )
                operation_uids.append(record.operation_uid)
            await start(store, revision)
            async with asyncio.timeout(60):
                (*_, running) = await events_until(store, EventType.OPERATION_RUNNING)
            attempt_uid = running.attempt_uid
            assert attempt_uid is not None
            await asyncio.sleep(2)  # the first reading is taken; the plan waits

            # Bob does not hold the lease, which alice does.
            await store.write(
                lambda tx: tx.request_stop_now(principal="bob", client_id=CLIENT, attempt_uid=attempt_uid)
            )
            requested = time.monotonic()
            async with asyncio.timeout(15):
                events = await events_until(store, EventType.QUEUE_EXECUTION_STOPPED)
            elapsed = time.monotonic() - requested
        finally:
            task.cancel()
            await asyncio.wait([task])

        assert elapsed < 15
        (aborted,) = [event for event in events if event.event_type is EventType.OPERATION_ABORTED]
        assert aborted.attempt_uid == attempt_uid
        attempt = await store.read(lambda tx: tx.attempt(attempt_uid))
        assert attempt.state is AttemptState.ABORTED
        assert attempt.stop_now_requested_at is not None
        queued = await store.read(lambda tx: tx.operation(operation_uids[1]))
        assert queued.state is OperationState.QUEUED

        (item,) = request(re_manager.control, "history_get")["items"]
        assert (item["item_uid"], item["result"]["exit_status"]) == (attempt.item_uid, "aborted")
    finally:
        relay.close()
        await store.close()
