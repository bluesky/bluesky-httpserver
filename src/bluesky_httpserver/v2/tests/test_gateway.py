from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from bluesky_httpserver.v2.catalog import Catalog, OperationDescriptor
from bluesky_httpserver.v2.gateway import CatalogUnavailable, NoFenceEvidence, WorkerGateway
from bluesky_httpserver.v2.relay import HistoryEntry
from bluesky_httpserver.v2.store import (
    AttemptRecord,
    AttemptState,
    EventType,
    FailurePolicy,
    OperationState,
    QueueExecutionRecord,
    QueueExecutionState,
    Store,
)

from .conftest import CLIENT, FakeMonotonic, claimed, start
from .fake_relay import DEMO_COUNT, FakeRelay

_FINISHED = {
    EventType.OPERATION_SUCCEEDED,
    EventType.OPERATION_FAILED,
    EventType.OPERATION_ABORTED,
    EventType.OPERATION_UNKNOWN,
}


async def queue_and_start(
    store: Store, count: int = 1, policy: FailurePolicy = FailurePolicy.STOP_ON_FAILURE
) -> tuple[list[str], str]:
    """Submit ``count`` operations and start a queue execution; return the operation and execution uids."""

    async def submit(revision: int) -> str:
        record, _ = await store.write(
            lambda tx: tx.submit(
                principal="alice",
                client_id=CLIENT,
                expected_revision=revision,
                operation_id="demo.count",
                operation_version="1",
                parameters={"num": 1, "detectors": ["primary"]},
                descriptor_sha256=DEMO_COUNT.sha256,
            )
        )
        return record.operation_uid

    operation_uids = [await submit(revision) for revision in range(count)]
    execution, _ = await start(store, count, policy)
    return operation_uids, execution.queue_execution_uid


def set_state(relay: FakeRelay, **changes: Any) -> None:
    relay.state = dataclasses.replace(relay.state, **changes)


async def attempts(store: Store) -> list[AttemptRecord]:
    """Every attempt, in claim order."""
    uids = [
        e.attempt_uid for e in await store.events_after(0, 1000) if e.event_type is EventType.OPERATION_CLAIMED
    ]
    return [await store.read(lambda tx, uid=uid: tx.attempt(uid)) for uid in uids if uid is not None]


async def only_attempt(store: Store) -> AttemptRecord:
    (attempt,) = await attempts(store)
    return attempt


async def execution_state(store: Store, queue_execution_uid: str) -> QueueExecutionState:
    return (await store.read(lambda tx: tx.queue_execution(queue_execution_uid))).state


async def operation_state(store: Store, operation_uid: str) -> OperationState:
    return (await store.read(lambda tx: tx.operation(operation_uid))).state


async def event_types(store: Store) -> list[EventType]:
    return [event.event_type for event in await store.events_after(0, 1000)]


async def acknowledge(store: Store, gateway: WorkerGateway) -> tuple[QueueExecutionRecord, int]:
    revision = await store.read(lambda tx: tx.revision())
    return await store.write(
        lambda tx: tx.acknowledge_recovery(
            principal="alice",
            client_id=CLIENT,
            expected_revision=revision,
            note="checked",
            fence=gateway.fence_evidence,
        )
    )


# Faults after which the outcome of the first attempt cannot be proven.
Fault = Callable[[FakeRelay, WorkerGateway, FakeMonotonic], Awaitable[None]]


async def lost(relay: FakeRelay, gateway: WorkerGateway, monotonic: FakeMonotonic) -> None:
    relay.execute_fault = "lost"
    await gateway.step()
    monotonic.advance(61)
    await gateway.step()


async def environment_lost(relay: FakeRelay, gateway: WorkerGateway, monotonic: FakeMonotonic) -> None:
    await gateway.step()
    set_state(relay, manager_state="idle", running_item_uid=None, worker_environment_exists=False)
    await gateway.step()


async def foreign_item(relay: FakeRelay, gateway: WorkerGateway, monotonic: FakeMonotonic) -> None:
    await gateway.step()
    set_state(relay, running_item_uid="item-foreign")
    await gateway.step()


async def silence(relay: FakeRelay, gateway: WorkerGateway, monotonic: FakeMonotonic) -> None:
    await gateway.step()
    relay.reachable = False
    await gateway.step()
    monotonic.advance(61)
    await gateway.step()


@pytest.mark.asyncio
async def test_dispatch_sends_the_plan_with_worker_device_names(store, relay, gateway):
    (o,), _ = await queue_and_start(store)
    await gateway.step()
    a = await only_attempt(store)
    assert relay.executed == [
        (
            {
                "item_type": "plan",
                "name": "count",
                "kwargs": {"num": 1, "detectors": ["det1"]},
                "meta": {"attempt_uid": a.attempt_uid, "operation_uid": o},
            },
            "alice",
            "primary",
        )
    ]
    assert (a.state, a.item_uid) == (AttemptState.RUNNING, "item-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_status", "msg", "attempt_state", "queue_execution_state"),
    [
        ("completed", "", AttemptState.SUCCEEDED, QueueExecutionState.COMPLETED),
        ("stopped", "", AttemptState.SUCCEEDED, QueueExecutionState.COMPLETED),
        ("failed", "boom", AttemptState.FAILED, QueueExecutionState.STOPPED),
        ("aborted", "", AttemptState.ABORTED, QueueExecutionState.STOPPED),
        ("halted", "", AttemptState.ABORTED, QueueExecutionState.STOPPED),
        ("unknown", "", AttemptState.UNKNOWN, QueueExecutionState.BLOCKED),
        ("failed", "RE Worker process terminated unexpectedly", AttemptState.UNKNOWN, QueueExecutionState.BLOCKED),
        # The worker may have started the plan when the manager's run request timed out.
        (
            "failed",
            "Timeout occurred while processing the request",
            AttemptState.UNKNOWN,
            QueueExecutionState.BLOCKED,
        ),
    ],
)
async def test_history_entry_decides_the_outcome(
    store, relay, gateway, exit_status, msg, attempt_state, queue_execution_state
):
    _, x = await queue_and_start(store)
    await gateway.step()
    relay.finish_running(exit_status, msg, ("run-1",))
    await gateway.step()

    a = await only_attempt(store)
    assert a.state is attempt_state
    if attempt_state is AttemptState.UNKNOWN:
        assert a.diagnostic is not None
        assert a.diagnostic.startswith(f"RE Manager recorded exit status '{exit_status}'")
    else:
        assert a.diagnostic == (msg or None)
    assert await execution_state(store, x) is queue_execution_state
    (finished,) = [e for e in await store.events_after(0, 1000) if e.event_type in _FINISHED]
    assert finished.payload["run_uids"] == ["run-1"]


@pytest.mark.asyncio
async def test_refused_execute_fails_the_attempt(store, relay, gateway):
    _, x = await queue_and_start(store)
    relay.execute_fault = "reject"
    await gateway.step()
    a = await only_attempt(store)
    assert a.state is AttemptState.FAILED
    assert a.diagnostic is not None and a.diagnostic.startswith("RE Manager refused the operation")
    assert await execution_state(store, x) is QueueExecutionState.STOPPED


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [lost, environment_lost, foreign_item, silence], ids=lambda f: f.__name__)
async def test_unprovable_outcome_blocks_dispatch(store, relay, gateway, monotonic, fault: Fault):
    (_, second), x = await queue_and_start(store, count=2)
    await fault(relay, gateway, monotonic)
    relay.reachable = True
    monotonic.advance(120)
    for _ in range(3):
        await gateway.step()

    assert [a.state for a in await attempts(store)] == [AttemptState.UNKNOWN]
    assert (await event_types(store)).count(EventType.QUEUE_EXECUTION_BLOCKED) == 1
    assert await execution_state(store, x) is QueueExecutionState.BLOCKED
    assert relay.calls.count("item_execute") == 1
    assert await operation_state(store, second) is OperationState.QUEUED
    assert "environment_open" not in relay.calls
    assert gateway.readiness().dispatch == "blocked"


@pytest.mark.asyncio
async def test_short_silence_keeps_the_attempt(store, relay, gateway, monotonic):
    _, x = await queue_and_start(store)
    await gateway.step()
    relay.reachable = False
    await gateway.step()
    monotonic.advance(59)
    relay.reachable = True
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.RUNNING

    relay.finish_running("completed")
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.SUCCEEDED
    assert await execution_state(store, x) is QueueExecutionState.COMPLETED
    assert EventType.QUEUE_EXECUTION_BLOCKED not in await event_types(store)


@pytest.mark.asyncio
async def test_outcome_recorded_during_silence_is_found(store, relay, gateway, monotonic):
    await queue_and_start(store)
    await gateway.step()
    relay.reachable = False
    await gateway.step()
    relay.finish_running("completed")
    monotonic.advance(30)
    relay.reachable = True
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.SUCCEEDED
    assert EventType.QUEUE_EXECUTION_BLOCKED not in await event_types(store)


@pytest.mark.asyncio
async def test_lost_execute_reply_is_resolved_by_history(store, relay, gateway, monotonic):
    await queue_and_start(store)
    relay.execute_fault = "reply_lost"
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.CLAIMED

    relay.execute_fault = None
    relay.finish_running("completed")
    monotonic.advance(10)
    await gateway.step()
    a = await only_attempt(store)
    assert (a.state, a.item_uid) == (AttemptState.SUCCEEDED, "item-1")
    types = await event_types(store)
    assert types.index(EventType.OPERATION_RUNNING) < types.index(EventType.OPERATION_SUCCEEDED)
    assert relay.calls.count("item_execute") == 1


@pytest.mark.asyncio
async def test_claimed_attempt_at_startup_is_resolved_by_history(store, relay, gateway):
    await queue_and_start(store)
    a, _, _ = await claimed(store)
    relay.history_entries.append(HistoryEntry("item-9", a.attempt_uid, "completed", "", ()))
    set_state(relay, plan_history_uid="history-1")
    await gateway.step()
    a = await only_attempt(store)
    assert (a.state, a.item_uid) == (AttemptState.SUCCEEDED, "item-9")
    assert "item_execute" not in relay.calls


@pytest.mark.asyncio
async def test_claimed_attempt_at_startup_without_evidence_becomes_unknown(store, relay, gateway, monotonic):
    await queue_and_start(store)
    await claimed(store)
    await gateway.step()
    monotonic.advance(61)
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.UNKNOWN
    assert "item_execute" not in relay.calls


@pytest.mark.asyncio
async def test_late_evidence_resolves_unknown_attempt(store, relay, gateway, monotonic):
    _, x = await queue_and_start(store)
    await silence(relay, gateway, monotonic)
    relay.reachable = True
    relay.finish_running("completed")
    await gateway.step()

    assert (await only_attempt(store)).state is AttemptState.SUCCEEDED
    finished = [e for e in await store.events_after(0, 1000) if e.event_type in _FINISHED][-1]
    assert finished.payload["previous_state"] == "unknown"
    assert await execution_state(store, x) is QueueExecutionState.BLOCKED


@pytest.mark.asyncio
async def test_recovery_needs_an_idle_manager(store, relay, gateway, monotonic):
    (_, second), _ = await queue_and_start(store, count=2)
    await foreign_item(relay, gateway, monotonic)
    with pytest.raises(NoFenceEvidence, match="running item item-foreign"):
        await acknowledge(store, gateway)

    set_state(relay, manager_state="idle", running_item_uid=None, worker_environment_exists=False)
    await gateway.step()
    assert "environment_open" not in relay.calls
    stopped, _ = await acknowledge(store, gateway)
    assert stopped.state is QueueExecutionState.STOPPED
    fenced = [e for e in await store.events_after(0, 1000) if e.event_type is EventType.WORKER_FENCED]
    assert [e.payload["status"]["worker_environment_exists"] for e in fenced] == [False]

    await gateway.step()
    assert relay.calls.count("environment_open") == 1
    assert gateway.readiness().dispatch == "open"
    assert await operation_state(store, second) is OperationState.QUEUED


@pytest.mark.asyncio
async def test_safe_stop_pauses_then_aborts(store, relay, gateway):
    _, x = await queue_and_start(store)
    await gateway.step()
    uid = (await only_attempt(store)).attempt_uid
    await store.write(lambda tx: tx.request_safe_stop(principal="alice", client_id=CLIENT, attempt_uid=uid))

    await gateway.step()
    assert "re_pause deferred" in relay.calls
    await gateway.step()
    assert "re_abort" in relay.calls
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.ABORTED
    assert await execution_state(store, x) is QueueExecutionState.STOPPED
    assert relay.calls.count("re_pause deferred") == relay.calls.count("re_abort") == 1


@pytest.mark.asyncio
async def test_stop_now_pauses_at_once_then_aborts(store, relay, gateway):
    (_, second), x = await queue_and_start(store, count=2)
    await gateway.step()
    uid = (await only_attempt(store)).attempt_uid
    await store.write(lambda tx: tx.request_stop_now(principal="bob", client_id=CLIENT, attempt_uid=uid))

    await gateway.step()
    assert "re_pause immediate" in relay.calls
    await gateway.step()
    assert "re_abort" in relay.calls
    await gateway.step()
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.ABORTED
    assert await execution_state(store, x) is QueueExecutionState.STOPPED
    assert await operation_state(store, second) is OperationState.QUEUED
    assert relay.calls.count("re_pause immediate") == relay.calls.count("re_abort") == 1
    assert "re_pause deferred" not in relay.calls


@pytest.mark.asyncio
async def test_stop_now_escalates_a_safe_stop_that_has_not_landed(store, relay, gateway):
    _, x = await queue_and_start(store)
    await gateway.step()
    uid = (await only_attempt(store)).attempt_uid
    relay.deferred_pause_lands = False
    await store.write(lambda tx: tx.request_safe_stop(principal="alice", client_id=CLIENT, attempt_uid=uid))
    await gateway.step()
    await gateway.step()
    assert relay.calls.count("re_pause deferred") == 1
    assert (await only_attempt(store)).state is AttemptState.RUNNING

    await store.write(lambda tx: tx.request_stop_now(principal="alice", client_id=CLIENT, attempt_uid=uid))
    await gateway.step()
    assert relay.calls.count("re_pause immediate") == 1
    await gateway.step()
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.ABORTED
    assert await execution_state(store, x) is QueueExecutionState.STOPPED
    assert [call for call in relay.calls if call.startswith("re_")] == [
        "re_pause deferred",
        "re_pause immediate",
        "re_abort",
    ]


@pytest.mark.asyncio
async def test_completion_before_safe_stop_wins_and_the_execution_stops(store, relay, gateway):
    (_, second), x = await queue_and_start(store, count=2)
    await gateway.step()
    uid = (await only_attempt(store)).attempt_uid
    await store.write(lambda tx: tx.request_safe_stop(principal="alice", client_id=CLIENT, attempt_uid=uid))
    relay.finish_running("completed")
    await gateway.step()
    await gateway.step()
    assert (await only_attempt(store)).state is AttemptState.SUCCEEDED
    assert "re_pause deferred" not in relay.calls
    assert await execution_state(store, x) is QueueExecutionState.STOPPED
    assert await operation_state(store, second) is OperationState.QUEUED
    assert relay.calls.count("item_execute") == 1


@pytest.mark.asyncio
async def test_incompatible_catalog_stops_claiming(store, relay, gateway):
    await queue_and_start(store)
    relay.catalog = Catalog([OperationDescriptor("demo.count", "1", {"type": "object"}, plan_name="count")])
    set_state(relay, plans_allowed_uid="allowed-1")
    await gateway.step()
    assert "item_execute" not in relay.calls
    assert await attempts(store) == []
    assert gateway.readiness().catalog == "incompatible"
    assert not gateway.readiness().ready


@pytest.mark.asyncio
async def test_manager_not_controller_fronted_is_not_used(relay, gateway):
    set_state(relay, controller_fronted=False)
    await gateway.step()
    assert relay.calls == ["status"]
    assert gateway.readiness().worker == "not_controller_fronted"


@pytest.mark.asyncio
async def test_runs_of_the_running_attempt_are_read_when_they_change(store, relay, gateway):
    await queue_and_start(store)
    await gateway.step()
    a = await only_attempt(store)
    assert gateway.runs(a.attempt_uid) is None

    relay.open_run("run-a")
    await gateway.step()
    await gateway.step()
    assert gateway.runs(a.attempt_uid) == ("run-a",)
    assert relay.calls.count("runs") == 1

    relay.open_run("run-b")
    await gateway.step()
    assert gateway.runs(a.attempt_uid) == ("run-a", "run-b")
    assert gateway.runs("another-attempt") is None

    relay.finish_running("completed", run_uids=["run-a", "run-b"])
    await gateway.step()
    assert gateway.runs(a.attempt_uid) is None


@pytest.mark.asyncio
async def test_absent_environment_is_opened_at_most_every_retry_interval(relay, gateway, monotonic):
    set_state(relay, worker_environment_exists=False)
    await gateway.step()
    assert relay.calls.count("environment_open") == 1
    set_state(relay, worker_environment_exists=False)  # e.g. opening failed
    await gateway.step()
    assert relay.calls.count("environment_open") == 1
    monotonic.advance(30)
    await gateway.step()
    assert relay.calls.count("environment_open") == 2


@pytest.mark.asyncio
async def test_catalog_is_unavailable_before_the_first_step(gateway):
    with pytest.raises(CatalogUnavailable):
        gateway.catalog()


@pytest.mark.asyncio
async def test_published_status_change_wakes_the_gateway(store, relay, monotonic):
    # Polling is an hour away, so only the published status can report that the item finished.
    gateway = WorkerGateway(store=store, relay=relay, user_group="primary", poll_seconds=3600, monotonic=monotonic)
    (o,), _ = await queue_and_start(store)
    task = asyncio.create_task(gateway.run())
    try:
        async with asyncio.timeout(5):
            while await operation_state(store, o) is not OperationState.RUNNING:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)  # let the steps woken by the dispatch's own events finish

        relay.finish_running("completed")
        relay.publish()
        async with asyncio.timeout(5):
            while await operation_state(store, o) is not OperationState.SUCCEEDED:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.wait([task])
