from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from bluesky_httpserver.v2.store import (
    AttemptNotLive,
    AttemptState,
    DispatchBlocked,
    EventType,
    InvalidAttemptTransition,
    LeaseRequired,
    NotBlocked,
    OperationState,
    QueueExecutionRecord,
    QueueExecutionState,
    Store,
)

from .conftest import (
    cancel,
    claim,
    claimed,
    finish,
    mark_running,
    revision_and_event_count,
    start,
    stop,
    submit,
)


def idle() -> dict[str, Any]:
    return {"manager_state": "idle", "running_item_uid": None}


async def blocked(store: Store, *, stopping: bool = False) -> tuple[str, str, str, str]:
    """Queue two operations and end the first one unknown.

    Returns the unknown attempt, the first and second operation, and the blocked execution.
    """
    a, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    execution, revision = await start(store, revision)
    attempt, _, _ = await claimed(store)
    _, revision = await mark_running(store, attempt.attempt_uid)
    if stopping:
        await stop(store, revision, execution.queue_execution_uid)
    await finish(store, attempt.attempt_uid, AttemptState.UNKNOWN, "lost")
    return attempt.attempt_uid, a.operation_uid, b.operation_uid, execution.queue_execution_uid


async def execution(store: Store, queue_execution_uid: str) -> QueueExecutionRecord:
    return await store.read(lambda tx: tx.queue_execution(queue_execution_uid))


async def acknowledge(
    store: Store, revision: int, note: str = "checked", fence: Callable[[], Mapping[str, Any]] = idle
) -> tuple[QueueExecutionRecord, int]:
    return await store.write(
        lambda tx: tx.acknowledge_recovery(principal="alice", expected_revision=revision, note=note, fence=fence)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stopping", [False, True])
async def test_unknown_outcome_blocks_dispatch(store, stopping):
    attempt_uid, a, b, x = await blocked(store, stopping=stopping)

    attempt = await store.read(lambda tx: tx.attempt(attempt_uid))
    assert (attempt.state, attempt.diagnostic) == (AttemptState.UNKNOWN, "lost")
    assert (await store.read(lambda tx: tx.operation(a))).state is OperationState.UNKNOWN
    assert (await execution(store, x)).state is QueueExecutionState.BLOCKED
    unknown, block = (await store.events_after(0))[-2:]
    assert (unknown.event_type, block.event_type) == (
        EventType.OPERATION_UNKNOWN,
        EventType.QUEUE_EXECUTION_BLOCKED,
    )
    assert unknown.queue_revision == block.queue_revision
    assert (block.queue_execution_uid, block.attempt_uid) == (x, attempt_uid)

    assert await claim(store) is None
    before = await revision_and_event_count(store)
    with pytest.raises(DispatchBlocked) as exc_info:
        await start(store, before[0])
    assert exc_info.value.queue_execution_uid == x
    assert await revision_and_event_count(store) == before

    cancelled, _ = await cancel(store, before[0], b)
    assert cancelled.state is OperationState.CANCELLED
    assert (await execution(store, x)).state is QueueExecutionState.BLOCKED


@pytest.mark.asyncio
async def test_late_evidence_resolves_unknown_attempt_but_not_the_block(store):
    attempt_uid, a, _, x = await blocked(store)

    def resolve(state: AttemptState):
        return store.write(
            lambda tx: tx.resolve_unknown_attempt(
                attempt_uid=attempt_uid, state=state, item_uid="item-9", run_uids=["run-1"]
            )
        )

    with pytest.raises(ValueError, match="not a proven outcome"):
        await resolve(AttemptState.UNKNOWN)
    resolved, revision = await resolve(AttemptState.SUCCEEDED)
    assert (resolved.state, resolved.item_uid) == (AttemptState.SUCCEEDED, "item-1")
    assert (await store.read(lambda tx: tx.operation(a))).state is OperationState.SUCCEEDED
    event = (await store.events_after(0))[-1]
    assert (event.event_type, event.queue_revision) == (EventType.OPERATION_SUCCEEDED, revision)
    assert event.payload == {"diagnostic": None, "run_uids": ["run-1"], "previous_state": "unknown"}
    assert (await execution(store, x)).state is QueueExecutionState.BLOCKED

    with pytest.raises(InvalidAttemptTransition):
        await resolve(AttemptState.FAILED)


@pytest.mark.asyncio
async def test_safe_stop_request(store, clock):
    _, revision = await submit(store, 0)
    await start(store, revision)
    attempt, _, _ = await claimed(store)
    uid = attempt.attempt_uid

    def request(principal: str = "alice"):
        return store.write(lambda tx: tx.request_safe_stop(principal=principal, attempt_uid=uid))

    with pytest.raises(LeaseRequired):
        await request("bob")
    revision, count = await revision_and_event_count(store)
    requested, new_revision = await request()
    assert requested.safe_stop_requested_at == clock.now
    assert requested.state is AttemptState.CLAIMED
    assert new_revision == revision + 1
    events = await store.events_after(0)
    assert len(events) == count + 1
    assert events[-1].event_type is EventType.OPERATION_SAFE_STOP_REQUESTED
    assert (events[-1].actor_id, events[-1].attempt_uid) == ("alice", uid)

    clock.advance(1)
    again, again_revision = await request()
    assert (again.safe_stop_requested_at, again_revision) == (requested.safe_stop_requested_at, new_revision)
    assert await revision_and_event_count(store) == (new_revision, count + 1)

    await finish(store, uid, AttemptState.ABORTED)
    with pytest.raises(AttemptNotLive) as exc_info:
        await request()
    assert exc_info.value.state is AttemptState.ABORTED


@pytest.mark.asyncio
async def test_acknowledge_recovery(store):
    with pytest.raises(NotBlocked):
        await acknowledge(store, 0)

    _, _, b, x = await blocked(store)
    before = await revision_and_event_count(store)
    with pytest.raises(ValueError, match="must not be blank"):
        await acknowledge(store, before[0], note="  ")

    def busy() -> Mapping[str, Any]:
        raise RuntimeError("busy")

    with pytest.raises(RuntimeError, match="busy"):
        await acknowledge(store, before[0], fence=busy)
    assert await revision_and_event_count(store) == before

    stopped, revision = await acknowledge(store, before[0])
    assert stopped.state is QueueExecutionState.STOPPED
    fenced, ended = (await store.events_after(0))[-2:]
    assert (fenced.event_type, ended.event_type) == (EventType.WORKER_FENCED, EventType.QUEUE_EXECUTION_STOPPED)
    assert fenced.payload == {"note": "checked", "status": idle()}
    assert fenced.queue_revision == ended.queue_revision == revision
    assert ended.queue_execution_uid == x
    pending = (await store.read(lambda tx: tx.queue())).pending
    assert [(record.operation_uid, record.state) for record in pending] == [(b, OperationState.QUEUED)]

    restarted, _ = await start(store, revision)
    assert restarted.state is QueueExecutionState.RUNNING
