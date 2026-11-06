"""The controller against a real RE Manager in controller-fronted mode.

Runs in the ``v2-*`` pixi environments, whose RE Manager has controller-fronted mode, and needs Redis on
localhost, as RE Manager's own tests do. Skipped where RE Manager lacks the mode.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import zmq

from bluesky_httpserver.v2.gateway import WorkerGateway
from bluesky_httpserver.v2.store import AttemptState, EventRecord, EventType, Store
from bluesky_httpserver.v2.zmq_relay import ZmqManagerRelay

from .conftest import acquire, start

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
        },
        "required": ["detectors"],
        "additionalProperties": False,
    },
    devices={"detectors": {"primary": "det1", "secondary": "det2"}},
)
def count_operation(detectors, num=1):
    yield from count(detectors, num=num)
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


@pytest.fixture
def re_manager(tmp_path: Path) -> Iterator[str]:
    """Start RE Manager in controller-fronted mode with a worker that offers ``demo.count``; yield its address."""
    executable = shutil.which("start-re-manager")
    assert executable is not None, "start-re-manager is not installed"
    usage = subprocess.run([executable, "-h"], capture_output=True, text=True, timeout=60).stdout
    if "--controller-fronted" not in usage:
        pytest.skip("this RE Manager has no controller-fronted mode; run in a v2-* pixi environment")
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "00-startup.py").write_text(STARTUP)
    (startup / "user_group_permissions.yaml").write_text(PERMISSIONS)
    address = f"ipc://{tmp_path}/control"
    log = tmp_path / "manager.log"

    with log.open("w") as output:
        process = subprocess.Popen(
            [
                executable,
                "--controller-fronted=ON",
                f"--zmq-control-addr={address}",
                f"--zmq-info-addr=ipc://{tmp_path}/info",
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
        while (status := request(address, "status", timeout=0.5)) is None or status["manager_state"] != "idle":
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"RE Manager did not start (is Redis running?):\n{log.read_text()[-3000:]}")
        # The history persists in Redis between runs; the test reads only what it ran.
        assert request(address, "history_clear")["success"]
        yield address
    finally:
        if process.poll() is None:
            request(address, "manager_stop", {"option": "safe_off"})
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
    relay = ZmqManagerRelay(re_manager, timeout_seconds=5.0)
    try:
        gateway = WorkerGateway(store=store, relay=relay, user_group="primary", poll_seconds=0.1)
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
        (item,) = request(re_manager, "history_get")["items"]
        assert item["item_uid"] == attempt.item_uid
        assert item["meta"]["attempt_uid"] == attempt_uid
        assert (item["name"], item["user"]) == ("count_operation", "alice")
        assert item["kwargs"] == {"detectors": ["det2"], "num": 2}
        assert item["result"]["exit_status"] == "completed"
    finally:
        relay.close()
        await store.close()
