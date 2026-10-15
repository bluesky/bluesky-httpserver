import asyncio
import contextlib
import sqlite3

import pytest

from bluesky_httpserver.v2.store import EventType, StaleRevision, Store

from .conftest import submit


@pytest.mark.asyncio
async def test_queue_and_events_survive_reopen(db_path):
    store = await Store.open(db_path)
    try:
        a, revision = await submit(store, 0)
        b, revision = await submit(store, revision)
        c, revision = await store.write(
            lambda tx: tx.replace(
                principal="alice",
                expected_revision=revision,
                operation_uid=a.operation_uid,
                operation_id="demo.count",
                operation_version="1",
                parameters={"num": 2},
                descriptor_sha256="1" * 64,
            )
        )
        _, revision = await store.write(
            lambda tx: tx.cancel(principal="alice", expected_revision=revision, operation_uid=b.operation_uid)
        )
        d, revision = await submit(store, revision)
        await store.write(
            lambda tx: tx.reorder(
                principal="alice", expected_revision=revision, operation_uids=[d.operation_uid, c.operation_uid]
            )
        )
        snapshot = await store.queue()
        events = await store.events_after(0)
    finally:
        await store.close()

    assert {event.event_type for event in events} == set(EventType)
    ids = [event.event_id for event in events]
    assert ids == sorted(set(ids))

    reopened = await Store.open(db_path)
    try:
        assert await reopened.queue() == snapshot
        assert await reopened.events_after(0) == events
        page = await reopened.events_after(after=2, limit=1)
    finally:
        await reopened.close()
    assert [event.event_id for event in page] == [3]
    assert page == [events[2]]


@pytest.mark.asyncio
async def test_events_are_append_only(db_path):
    store = await Store.open(db_path)
    try:
        await submit(store, 0)
    finally:
        await store.close()
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE events SET actor_id='x'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM events")


@pytest.mark.asyncio
async def test_wait_for_event(store):
    waiter = asyncio.create_task(store.wait_for_event(0))
    await asyncio.sleep(0)
    assert not waiter.done()
    await submit(store, 0)
    assert await asyncio.wait_for(waiter, 5) == 1
    assert await asyncio.wait_for(store.wait_for_event(0), 5) == 1

    waiter = asyncio.create_task(store.wait_for_event(1))
    with pytest.raises(StaleRevision):
        await submit(store, 0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(waiter, 0.2)


@pytest.mark.asyncio
async def test_store_is_not_reentrant_and_transactions_expire(store):
    with pytest.raises(RuntimeError, match="inside Store.write"):
        await store.write(lambda tx: store.queue())

    kept = []

    async def keep(tx):
        kept.append(tx)

    await store.write(keep)
    with pytest.raises(RuntimeError, match="outside Store.write"):
        await kept[0].queue()
