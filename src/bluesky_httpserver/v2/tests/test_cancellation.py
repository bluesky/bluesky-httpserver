"""Cancelling a writer must leave the store consistent with what SQLite actually did.

aiosqlite runs every queued request even if its awaiter is cancelled. Each test holds the awaiter of one
request after SQLite has executed it, then cancels the writer in that window.
"""

import asyncio

import pytest

from .conftest import submit


class Stall:
    """Hold awaiters of ``conn.<method>`` (when ``when(*args)``) after the call completes, until released."""

    def __init__(self, monkeypatch, conn, method, when=lambda *args: True):
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        original = getattr(conn, method)

        async def stalled(*args, **kwargs):
            result = await original(*args, **kwargs)
            if when(*args):
                self.reached.set()
                await self.release.wait()
            return result

        monkeypatch.setattr(conn, method, stalled)

    async def cancel_inside(self, task):
        await asyncio.wait_for(self.reached.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        self.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_writer_cancelled_after_begin_rolls_back(store, monkeypatch):
    stall = Stall(monkeypatch, store._conn, "execute", when=lambda sql, *args: sql == "BEGIN IMMEDIATE")
    await stall.cancel_inside(asyncio.create_task(submit(store, 0)))

    assert (await submit(store, 0))[1] == 1


@pytest.mark.asyncio
async def test_writer_cancelled_during_commit_still_publishes_events(store, monkeypatch):
    stall = Stall(monkeypatch, store._conn, "commit")
    cursor = (await store.events_after(0))[-1].event_id
    waiter = asyncio.create_task(store.wait_for_event(cursor))
    await stall.cancel_inside(asyncio.create_task(submit(store, 0)))

    assert await asyncio.wait_for(waiter, 5) == cursor + 1
    assert (await submit(store, 1))[1] == 2
