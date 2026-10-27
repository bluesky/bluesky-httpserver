import pytest

from bluesky_httpserver.v2.store import (
    ActorKind,
    AttemptNotFound,
    AttemptState,
    EventType,
    ExecutionActive,
    ExecutionNotFound,
    ExecutionNotRunning,
    FailurePolicy,
    InvalidAttemptTransition,
    InvalidReorder,
    LeaseRequired,
    OperationNotPending,
    OperationState,
    QueueEmpty,
    QueueExecutionState,
    StaleRevision,
)

from .conftest import (
    acquire,
    cancel,
    claim,
    claimed,
    finish,
    mark_running,
    reorder,
    replace,
    start,
    stop,
    submit,
)


async def execution_state(store, queue_execution_uid):
    return (await store.read(lambda tx: tx.queue_execution(queue_execution_uid))).state


@pytest.mark.asyncio
async def test_live_edits_are_claimed_in_order(store):
    a, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    execution, revision = await start(store, revision)
    assert revision == 3
    x = execution.queue_execution_uid
    started = (await store.events_after(0))[-1]
    assert started.event_type is EventType.QUEUE_EXECUTION_STARTED
    assert started.payload["operation_uids"] == [a.operation_uid, b.operation_uid]

    attempt, operation, revision = await claimed(store)
    assert revision == 4
    assert await claim(store) is None

    c, revision = await submit(store, revision)
    b2, revision = await replace(store, revision, b.operation_uid)
    await reorder(store, revision, [c.operation_uid, b2.operation_uid])
    submitted = {
        event.operation_uid: event.queue_execution_uid
        for event in await store.events_after(0)
        if event.event_type is EventType.OPERATION_SUBMITTED
    }
    assert submitted == {a.operation_uid: None, b.operation_uid: None, c.operation_uid: x, b2.operation_uid: x}

    claimed_uids = [operation.operation_uid]
    await finish(store, attempt.attempt_uid)
    for _ in range(2):
        attempt, operation, _ = await claimed(store)
        claimed_uids.append(operation.operation_uid)
        await finish(store, attempt.attempt_uid)
    assert claimed_uids == [a.operation_uid, c.operation_uid, b2.operation_uid]
    assert await execution_state(store, x) is QueueExecutionState.COMPLETED
    assert await claim(store) is None


@pytest.mark.asyncio
async def test_lease_expiry_does_not_stop_admitted_work(store, clock):
    _, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    execution, revision = await start(store, revision)
    attempt, _, revision = await claimed(store)

    clock.advance(3600)
    with pytest.raises(LeaseRequired):
        await submit(store, revision)
    await finish(store, attempt.attempt_uid)
    attempt, operation, _ = await claimed(store)
    assert operation.operation_uid == b.operation_uid
    _, revision = await finish(store, attempt.attempt_uid)
    assert await execution_state(store, execution.queue_execution_uid) is QueueExecutionState.COMPLETED

    await acquire(store)
    await submit(store, revision)


@pytest.mark.asyncio
async def test_stop_after_current(store):
    _, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    execution, revision = await start(store, revision)
    x = execution.queue_execution_uid
    attempt, _, revision = await claimed(store)

    stopping, _ = await stop(store, revision, x)
    assert stopping.state is QueueExecutionState.STOPPING
    assert await claim(store) is None
    await finish(store, attempt.attempt_uid)
    assert await execution_state(store, x) is QueueExecutionState.STOPPED
    pending = (await store.read(lambda tx: tx.queue())).pending
    assert [(record.operation_uid, record.state) for record in pending] == [
        (b.operation_uid, OperationState.QUEUED)
    ]


@pytest.mark.asyncio
async def test_stop_with_nothing_claimed_stops_at_once(store):
    _, revision = await submit(store, 0)
    execution, revision = await start(store, revision)
    stopped, revision = await stop(store, revision, execution.queue_execution_uid)
    assert stopped.state is QueueExecutionState.STOPPED
    requested, ended = (await store.events_after(0))[-2:]
    assert requested.event_type is EventType.QUEUE_EXECUTION_STOP_REQUESTED
    assert ended.event_type is EventType.QUEUE_EXECUTION_STOPPED
    assert requested.queue_revision == ended.queue_revision == revision


@pytest.mark.asyncio
async def test_start_and_stop_guards(store):
    with pytest.raises(QueueEmpty):
        await start(store, 0)
    _, revision = await submit(store, 0)
    execution, revision = await start(store, revision)
    x = execution.queue_execution_uid
    with pytest.raises(ExecutionActive) as active:
        await start(store, revision)
    assert active.value.queue_execution_uid == x

    _, revision = await stop(store, revision, x)
    with pytest.raises(ExecutionNotRunning) as not_running:
        await stop(store, revision, x)
    assert not_running.value.state is QueueExecutionState.STOPPED
    with pytest.raises(ExecutionNotFound):
        await stop(store, revision, "missing")
    await start(store, revision)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "outcome", "expected_state", "continues"),
    [
        (FailurePolicy.STOP_ON_FAILURE, AttemptState.FAILED, QueueExecutionState.STOPPED, False),
        (FailurePolicy.CONTINUE_ON_FAILURE, AttemptState.FAILED, QueueExecutionState.RUNNING, True),
        (FailurePolicy.CONTINUE_ON_FAILURE, AttemptState.ABORTED, QueueExecutionState.STOPPED, False),
        (FailurePolicy.STOP_ON_FAILURE, AttemptState.SUCCEEDED, QueueExecutionState.RUNNING, True),
    ],
)
async def test_failure_policy(store, policy, outcome, expected_state, continues):
    a, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    execution, revision = await start(store, revision, policy)
    attempt, _, _ = await claimed(store)
    await mark_running(store, attempt.attempt_uid)

    finished, _ = await finish(store, attempt.attempt_uid, outcome, "boom")
    assert finished.state is outcome
    assert finished.diagnostic == "boom"
    assert (await store.read(lambda tx: tx.operation(a.operation_uid))).state.value == outcome.value
    assert await execution_state(store, execution.queue_execution_uid) is expected_state
    next_claim = await claim(store)
    claimed_next = None if next_claim is None else next_claim[1].operation_uid
    assert claimed_next == (b.operation_uid if continues else None)


@pytest.mark.asyncio
async def test_claim_makes_stale_edit_fail(store):
    _, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    _, revision = await start(store, revision)
    _, _, claimed_revision = await claimed(store)
    assert claimed_revision == revision + 1

    with pytest.raises(StaleRevision) as exc_info:
        await cancel(store, revision, b.operation_uid)
    assert (exc_info.value.expected, exc_info.value.current) == (revision, revision + 1)
    assert (await store.read(lambda tx: tx.operation(b.operation_uid))).state is OperationState.QUEUED


@pytest.mark.asyncio
async def test_claimed_operation_cannot_be_edited(store):
    a, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    _, revision = await start(store, revision)
    _, _, revision = await claimed(store)

    for edit in (cancel, replace):
        with pytest.raises(OperationNotPending) as exc_info:
            await edit(store, revision, a.operation_uid)
        assert exc_info.value.state is OperationState.CLAIMED
    with pytest.raises(InvalidReorder):
        await reorder(store, revision, [a.operation_uid, b.operation_uid])


@pytest.mark.asyncio
async def test_cancelling_last_queued_operation_completes_execution(store):
    _, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    execution, revision = await start(store, revision)
    x = execution.queue_execution_uid
    attempt, _, _ = await claimed(store)
    _, revision = await finish(store, attempt.attempt_uid)
    assert await execution_state(store, x) is QueueExecutionState.RUNNING

    _, revision = await cancel(store, revision, b.operation_uid)
    assert await execution_state(store, x) is QueueExecutionState.COMPLETED
    completed = (await store.events_after(0))[-1]
    assert completed.event_type is EventType.QUEUE_EXECUTION_COMPLETED
    assert (completed.actor_kind, completed.actor_id) == (ActorKind.PRINCIPAL, "alice")
    assert completed.queue_revision == revision


@pytest.mark.asyncio
async def test_attempt_transitions(store):
    _, revision = await submit(store, 0)
    await start(store, revision)
    attempt, _, _ = await claimed(store)
    uid = attempt.attempt_uid

    running, _ = await mark_running(store, uid, "item-7")
    assert (running.state, running.item_uid) == (AttemptState.RUNNING, "item-7")
    with pytest.raises(InvalidAttemptTransition):
        await mark_running(store, uid)
    with pytest.raises(ValueError, match="not a terminal"):
        await finish(store, uid, AttemptState.RUNNING)

    await finish(store, uid)
    with pytest.raises(InvalidAttemptTransition) as exc_info:
        await finish(store, uid, AttemptState.FAILED)
    assert (exc_info.value.current, exc_info.value.target) == (AttemptState.SUCCEEDED, AttemptState.FAILED)
    with pytest.raises(AttemptNotFound):
        await store.read(lambda tx: tx.attempt("missing"))


@pytest.mark.asyncio
async def test_scheduler_changes_bump_revision_as_scheduler(store):
    _, revision = await submit(store, 0)
    execution, revision = await start(store, revision)
    x = execution.queue_execution_uid
    cursor = (await store.events_after(0))[-1].event_id

    attempt, operation, claimed_revision = await claimed(store)
    _, running_revision = await mark_running(store, attempt.attempt_uid)
    _, finished_revision = await finish(store, attempt.attempt_uid)
    assert [claimed_revision, running_revision, finished_revision] == [revision + 1, revision + 2, revision + 3]

    events = await store.events_after(cursor)
    assert [(event.event_type, event.queue_revision) for event in events] == [
        (EventType.OPERATION_CLAIMED, claimed_revision),
        (EventType.OPERATION_RUNNING, running_revision),
        (EventType.OPERATION_SUCCEEDED, finished_revision),
        (EventType.QUEUE_EXECUTION_COMPLETED, finished_revision),
    ]
    assert {(event.actor_kind, event.actor_id) for event in events} == {(ActorKind.SCHEDULER, x)}
    claim_event = events[0]
    assert (claim_event.operation_uid, claim_event.queue_execution_uid, claim_event.attempt_uid) == (
        operation.operation_uid,
        x,
        attempt.attempt_uid,
    )
