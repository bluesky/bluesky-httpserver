from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import zmq
import zmq.asyncio

from bluesky_httpserver.v2.catalog import OperationDescriptor
from bluesky_httpserver.v2.relay import HistoryEntry, ManagerStatus, RelayRejected, RelayUnreachable
from bluesky_httpserver.v2.zmq_relay import ZmqManagerRelay

STATUS = {
    "manager_state": "executing_queue",
    "running_item_uid": "item-1",
    "worker_environment_exists": True,
    "plan_history_uid": "history-1",
    "plans_allowed_uid": "allowed-1",
    "controller_fronted": True,
    "items_in_queue": 0,
}

COUNT_SCHEMA = {
    "type": "object",
    "properties": {"detectors": {"type": "array", "items": {"type": "string"}}},
    "required": ["detectors"],
    "additionalProperties": False,
}
COUNT_DEVICES = {"detectors": {"primary": "det1", "secondary": "det2"}}


def plans_allowed(*operations: tuple[str, str, str]) -> dict[str, Any]:
    """A 'plans_allowed' reply with plain 'count' and one plan per ``(plan, id, version)`` operation."""
    plans: dict[str, Any] = {"count": {"name": "count", "parameters": []}}
    for name, operation_id, version in operations:
        operation = {
            "id": operation_id,
            "version": version,
            "request_schema": COUNT_SCHEMA,
            "devices": COUNT_DEVICES,
        }
        plans[name] = {"name": name, "parameters": [], "operation": operation}
    return {"success": True, "msg": "", "plans_allowed": plans, "plans_allowed_uid": "allowed-1"}


def history_item(item_uid: str, meta: Any, exit_status: str, msg: str = "", run_uids: list[str] | None = None):
    result = {"exit_status": exit_status, "msg": msg, "run_uids": run_uids or [], "traceback": ""}
    item: dict[str, Any] = {"item_uid": item_uid, "name": "count", "result": result}
    if meta is not None:
        item["meta"] = meta
    return item


class FakeManager:
    """Answers each request on a REP socket with the reply set for its method, and records the request."""

    def __init__(self, address: str):
        self.address = address
        self.replies: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []

    async def serve(self) -> None:
        context = zmq.asyncio.Context()
        socket = context.socket(zmq.REP)
        socket.bind(self.address)
        try:
            while True:
                request = await socket.recv_json()
                self.requests.append(request)
                await socket.send_json(self.replies[request["method"]])
        finally:
            socket.close(linger=0)
            context.term()


@pytest.fixture
def address(tmp_path: Path) -> str:
    return f"ipc://{tmp_path}/control"


@pytest_asyncio.fixture
async def manager(address: str) -> AsyncIterator[FakeManager]:
    fake = FakeManager(address)
    task = asyncio.create_task(fake.serve())
    yield fake
    task.cancel()
    await asyncio.wait([task])


@pytest.fixture
def relay(address: str) -> Iterator[ZmqManagerRelay]:
    relay = ZmqManagerRelay(address, timeout_seconds=2.0)
    yield relay
    relay.close()


@pytest.mark.asyncio
async def test_reads_status_catalog_and_history(manager, relay):
    manager.replies = {
        "status": STATUS,
        "plans_allowed": plans_allowed(("count_operation", "demo.count", "1")),
        "history_get": {
            "success": True,
            "msg": "",
            "plan_history_uid": "history-2",
            "items": [
                history_item("item-0", [{"note": "from another client"}], "completed"),
                history_item("item-1", None, "stopped"),
                history_item("item-2", {"attempt_uid": "attempt-2"}, "failed", "boom", ["run-a", "run-b"]),
            ],
        },
    }

    assert await relay.status() == ManagerStatus("executing_queue", "item-1", True, "history-1", "allowed-1", True)

    allowed = await relay.plans_allowed("primary")
    assert allowed.uid == "allowed-1"
    expected = OperationDescriptor("demo.count", "1", COUNT_SCHEMA, "count_operation", COUNT_DEVICES)
    assert allowed.catalog.descriptor("demo.count", "1") == expected

    history = await relay.history()
    assert history.uid == "history-2"
    assert history.entries == (
        HistoryEntry("item-0", None, "completed", "", ()),
        HistoryEntry("item-1", None, "stopped", "", ()),
        HistoryEntry("item-2", "attempt-2", "failed", "boom", ("run-a", "run-b")),
    )

    assert manager.requests == [
        {"method": "status", "params": {}},
        {"method": "plans_allowed", "params": {"user_group": "primary"}},
        {"method": "history_get", "params": {}},
    ]


@pytest.mark.asyncio
async def test_status_of_a_manager_without_controller_fronted_mode(manager, relay):
    manager.replies["status"] = {key: value for key, value in STATUS.items() if key != "controller_fronted"}
    assert (await relay.status()).controller_fronted is False


@pytest.mark.asyncio
async def test_commands(manager, relay):
    ok = {"success": True, "msg": ""}
    manager.replies = {
        "queue_item_execute": {**ok, "item": {"item_uid": "item-9", "name": "count_operation"}},
        "environment_open": ok,
        "re_pause": ok,
        "re_abort": ok,
    }
    item = {"item_type": "plan", "name": "count_operation", "kwargs": {}, "meta": {"attempt_uid": "attempt-9"}}

    assert await relay.item_execute(item, user="alice", user_group="primary") == "item-9"
    await relay.environment_open()
    await relay.re_pause("deferred")
    await relay.re_abort()

    assert manager.requests == [
        {"method": "queue_item_execute", "params": {"item": item, "user": "alice", "user_group": "primary"}},
        {"method": "environment_open", "params": {}},
        {"method": "re_pause", "params": {"option": "deferred"}},
        {"method": "re_abort", "params": {}},
    ]


@pytest.mark.asyncio
async def test_refusals(manager, relay):
    manager.replies = {
        "queue_item_execute": {"success": False, "msg": "plan not allowed", "item": {}},
        # Two plans for one operation: the reply cannot become a catalog.
        "plans_allowed": plans_allowed(("count_a", "demo.count", "1"), ("count_b", "demo.count", "1")),
    }

    with pytest.raises(RelayRejected) as refused:
        await relay.item_execute({"name": "count"}, user="alice", user_group="primary")
    assert (refused.value.method, refused.value.message) == ("queue_item_execute", "plan not allowed")

    with pytest.raises(RelayRejected, match="not a valid catalog: duplicate operation demo.count version 1"):
        await relay.plans_allowed("primary")


@pytest.mark.asyncio
async def test_unreachable_until_the_manager_answers(address):
    relay = ZmqManagerRelay(address, timeout_seconds=0.2)
    try:
        with pytest.raises(RelayUnreachable) as unreachable:
            await relay.status()
        assert unreachable.value.method == "status"

        manager = FakeManager(address)
        manager.replies["status"] = STATUS
        task = asyncio.create_task(manager.serve())
        try:
            assert (await relay.status()).manager_state == "executing_queue"
        finally:
            task.cancel()
            await asyncio.wait([task])
    finally:
        relay.close()
