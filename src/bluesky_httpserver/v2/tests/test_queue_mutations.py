import asyncio

import pytest

from bluesky_httpserver.v2.store import (
    EventType,
    InvalidReorder,
    OperationNotFound,
    OperationNotPending,
    OperationState,
    StaleRevision,
)

from .conftest import cancel, reorder, replace, revision_and_event_count, submit


def pending_uids(snapshot):
    return [record.operation_uid for record in snapshot.pending]


@pytest.mark.asyncio
async def test_concurrent_writers_with_same_revision(store):
    first, second = await asyncio.gather(submit(store, 0), submit(store, 0), return_exceptions=True)
    assert not isinstance(first, BaseException)
    assert first[1] == 1
    assert isinstance(second, StaleRevision)
    assert (second.expected, second.current) == (0, 1)
    assert len((await store.queue()).pending) == 1
    assert [e.event_type for e in await store.events_after(0)] == [
        EventType.LEASE_ACQUIRED,
        EventType.OPERATION_SUBMITTED,
    ]


@pytest.mark.asyncio
async def test_rejected_cancel_writes_nothing(store):
    a, revision = await submit(store, 0)
    _, revision = await cancel(store, revision, a.operation_uid)
    before = await revision_and_event_count(store)

    with pytest.raises(OperationNotFound):
        await cancel(store, revision, "missing")
    assert await revision_and_event_count(store) == before

    with pytest.raises(OperationNotPending) as exc_info:
        await cancel(store, revision, a.operation_uid)
    assert exc_info.value.state is OperationState.CANCELLED
    assert await revision_and_event_count(store) == before


@pytest.mark.asyncio
async def test_stale_revision_is_checked_before_operation(store):
    with pytest.raises(StaleRevision):
        await cancel(store, 99, "missing")


@pytest.mark.asyncio
async def test_replace_keeps_position_and_links_operations(store):
    a, _ = await submit(store, 0)
    b, revision = await submit(store, 1)
    assert revision == 2

    c, revision = await replace(store, revision, a.operation_uid)
    assert revision == 3
    assert pending_uids(await store.queue()) == [c.operation_uid, b.operation_uid]
    old = await store.operation(a.operation_uid)
    assert old.state is OperationState.REPLACED
    assert old.replaced_by == c.operation_uid

    replaced, submitted = (await store.events_after(0))[-2:]
    assert replaced.event_type is EventType.OPERATION_REPLACED
    assert replaced.operation_uid == a.operation_uid
    assert replaced.payload == {"replaced_by": c.operation_uid}
    assert submitted.event_type is EventType.OPERATION_SUBMITTED
    assert submitted.operation_uid == c.operation_uid
    assert submitted.payload["replaces"] == a.operation_uid
    assert replaced.queue_revision == submitted.queue_revision == 3


@pytest.mark.asyncio
async def test_replacing_cancelled_operation_is_refused(store):
    a, revision = await submit(store, 0)
    _, revision = await cancel(store, revision, a.operation_uid)
    with pytest.raises(OperationNotPending):
        await replace(store, revision, a.operation_uid)


@pytest.mark.asyncio
async def test_reorder(store):
    a, _ = await submit(store, 0)
    b, _ = await submit(store, 1)
    c, revision = await submit(store, 2)

    snapshot = await reorder(store, revision, [c.operation_uid, a.operation_uid, b.operation_uid])
    assert pending_uids(snapshot) == [c.operation_uid, a.operation_uid, b.operation_uid]
    assert pending_uids(await store.queue()) == pending_uids(snapshot)
    revision = snapshot.revision

    for invalid in (
        [c.operation_uid, a.operation_uid],
        [c.operation_uid, a.operation_uid, a.operation_uid],
        [c.operation_uid, a.operation_uid, "missing"],
    ):
        with pytest.raises(InvalidReorder):
            await reorder(store, revision, invalid)
        assert (await store.queue()).revision == revision


@pytest.mark.asyncio
async def test_submit_after_cancel_appends(store):
    a, _ = await submit(store, 0)
    b, _ = await submit(store, 1)
    c, revision = await submit(store, 2)
    _, revision = await cancel(store, revision, b.operation_uid)
    d, _ = await submit(store, revision)
    assert pending_uids(await store.queue()) == [a.operation_uid, c.operation_uid, d.operation_uid]


@pytest.mark.asyncio
async def test_every_mutation_bumps_revision_once(store):
    a, revision = await submit(store, 0)
    b, revision = await submit(store, revision)
    c, revision = await replace(store, revision, a.operation_uid)
    revision = (await reorder(store, revision, [b.operation_uid, c.operation_uid])).revision
    _, revision = await cancel(store, revision, b.operation_uid)
    _, revision = await submit(store, revision)

    mutations = 6
    assert revision == (await store.queue()).revision == mutations
    events = [e for e in await store.events_after(0) if e.event_type is not EventType.LEASE_ACQUIRED]
    assert {event.queue_revision for event in events} == set(range(1, mutations + 1))
