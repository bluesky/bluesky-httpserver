from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bluesky_httpserver.v2.store import (
    AttemptRecord,
    AttemptState,
    FailurePolicy,
    LeaseRecord,
    OperationRecord,
    QueueExecutionRecord,
    QueueSnapshot,
    Store,
)


class FakeClock:
    """Store clock that moves only when told to."""

    def __init__(self, now: int = 1_700_000_000_000_000):
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1_000_000)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "controller.sqlite"


@pytest_asyncio.fixture
async def store(db_path: Path, clock: FakeClock) -> AsyncIterator[Store]:
    s = await Store.open(db_path, clock=clock)
    try:
        await acquire(s)
        yield s
    finally:
        await s.close()


async def acquire(store: Store, principal: str = "alice", ttl_seconds: int = 3600) -> LeaseRecord:
    return await store.write(lambda tx: tx.acquire_lease(principal=principal, ttl_seconds=ttl_seconds))


async def submit(
    store: Store,
    revision: int,
    operation_id: str = "demo.count",
    parameters: dict[str, Any] | None = None,
    *,
    principal: str = "alice",
) -> tuple[OperationRecord, int]:
    return await store.write(
        lambda tx: tx.submit(
            principal=principal,
            expected_revision=revision,
            operation_id=operation_id,
            operation_version="1",
            parameters=parameters or {"num": 1},
            descriptor_sha256="0" * 64,
        )
    )


async def cancel(
    store: Store, revision: int, operation_uid: str, *, principal: str = "alice"
) -> tuple[OperationRecord, int]:
    return await store.write(
        lambda tx: tx.cancel(principal=principal, expected_revision=revision, operation_uid=operation_uid)
    )


async def replace(
    store: Store, revision: int, operation_uid: str, *, principal: str = "alice"
) -> tuple[OperationRecord, int]:
    return await store.write(
        lambda tx: tx.replace(
            principal=principal,
            expected_revision=revision,
            operation_uid=operation_uid,
            operation_id="demo.count",
            operation_version="1",
            parameters={"num": 2},
            descriptor_sha256="1" * 64,
        )
    )


async def reorder(
    store: Store, revision: int, operation_uids: Sequence[str], *, principal: str = "alice"
) -> QueueSnapshot:
    return await store.write(
        lambda tx: tx.reorder(principal=principal, expected_revision=revision, operation_uids=operation_uids)
    )


async def revision_and_event_count(store: Store) -> tuple[int, int]:
    return (await store.read(lambda tx: tx.queue())).revision, len(await store.events_after(0))


async def start(
    store: Store, revision: int, policy: FailurePolicy = FailurePolicy.STOP_ON_FAILURE
) -> tuple[QueueExecutionRecord, int]:
    return await store.write(
        lambda tx: tx.start_queue_execution(principal="alice", expected_revision=revision, policy=policy)
    )


async def stop(store: Store, revision: int, queue_execution_uid: str) -> tuple[QueueExecutionRecord, int]:
    return await store.write(
        lambda tx: tx.stop_queue_execution(
            principal="alice", expected_revision=revision, queue_execution_uid=queue_execution_uid
        )
    )


async def claim(store: Store) -> tuple[AttemptRecord, OperationRecord, int] | None:
    return await store.write(lambda tx: tx.claim_next())


async def claimed(store: Store) -> tuple[AttemptRecord, OperationRecord, int]:
    result = await claim(store)
    assert result is not None
    return result


async def mark_running(store: Store, attempt_uid: str, item_uid: str = "item-1") -> tuple[AttemptRecord, int]:
    return await store.write(lambda tx: tx.mark_attempt_running(attempt_uid=attempt_uid, item_uid=item_uid))


async def finish(
    store: Store,
    attempt_uid: str,
    state: AttemptState = AttemptState.SUCCEEDED,
    diagnostic: str | None = None,
) -> tuple[AttemptRecord, int]:
    return await store.write(
        lambda tx: tx.finish_attempt(attempt_uid=attempt_uid, state=state, diagnostic=diagnostic)
    )
