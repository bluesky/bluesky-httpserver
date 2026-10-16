"""Durable SQLite store of the v2 controller.

Every successful queue edit is one SQLite transaction holding the domain change, exactly one
``queue_revision`` bump and the events stamped with the new revision. A rejected edit writes nothing.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

from .errors import (
    IdempotencyConflict,
    InvalidReorder,
    MigrationRequired,
    NotAStore,
    OperationNotFound,
    OperationNotPending,
    SchemaMismatch,
    StaleRevision,
    StoreError,
    StoreLocked,
)
from .migrations import APPLICATION_ID, MIGRATIONS, Migration
from .records import (
    ActorKind,
    EventRecord,
    EventType,
    IdempotencyScope,
    IdempotentResult,
    OperationRecord,
    OperationState,
    QueueSnapshot,
    StoredResponse,
)

T = TypeVar("T")


def _utc_microseconds() -> int:
    return time.time_ns() // 1_000


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


async def _run_to_completion(coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` in its own task and wait for it even if the caller is cancelled meanwhile.

    A cancellation of the caller is re-raised once ``coro`` has finished.
    """
    task = asyncio.ensure_future(coro)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        raise cancelled from (None if task.cancelled() else task.exception())
    return task.result()


async def _fetchone(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    cursor = await conn.execute(sql, params)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetchall(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    cursor = await conn.execute(sql, params)
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


def _row_to_operation(row: sqlite3.Row) -> OperationRecord:
    return OperationRecord(
        operation_uid=row["operation_uid"],
        operation_id=row["operation_id"],
        operation_version=row["operation_version"],
        parameters=json.loads(row["parameters_json"]),
        descriptor_sha256=row["descriptor_sha256"],
        submitted_by=row["submitted_by"],
        submitted_at=row["submitted_at"],
        state=OperationState(row["state"]),
        updated_at=row["updated_at"],
        replaced_by=row["replaced_by"],
    )


def _row_to_event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        event_id=row["event_id"],
        timestamp=row["timestamp"],
        actor_kind=ActorKind(row["actor_kind"]),
        actor_id=row["actor_id"],
        event_type=EventType(row["event_type"]),
        operation_uid=row["operation_uid"],
        queue_revision=row["queue_revision"],
        payload=json.loads(row["payload_json"]),
    )


async def _apply_migration(conn: aiosqlite.Connection, migration: Migration, clock: Callable[[], int]) -> None:
    await conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in migration.statements:
            await conn.execute(statement)
        await conn.execute(
            "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.checksum, clock()),
        )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise


async def _prepare_schema(
    conn: aiosqlite.Connection,
    path: Path,
    migrations: Sequence[Migration],
    allow_migrate: bool,
    clock: Callable[[], int],
) -> None:
    """Verify the identity and migration history of the database at ``path`` and bring it up to date."""
    app_id = (await _fetchone(conn, "PRAGMA application_id"))[0]
    has_tables = (
        await _fetchone(
            conn, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        )
        is not None
    )
    if app_id != APPLICATION_ID and (has_tables or app_id != 0):
        raise NotAStore(path, app_id)
    if not has_tables:
        # A crash before migration 1 commits leaves an empty, tagged file, which is treated as new.
        await conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")

    mode = (await _fetchone(conn, "PRAGMA journal_mode=WAL"))[0]
    if mode != "wal":
        raise StoreError(f"SQLite refused WAL journal mode for {path}: {mode}")
    await conn.execute("PRAGMA synchronous=FULL")

    if not has_tables:
        pending = migrations
    else:
        history_table = await _fetchone(
            conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        if history_table is None:
            raise SchemaMismatch(f"{path} has no migration history")
        rows = await _fetchall(conn, "SELECT version, checksum FROM schema_migrations ORDER BY version")
        versions = [row["version"] for row in rows]
        if versions != list(range(1, len(versions) + 1)):
            raise SchemaMismatch(f"migration history is not contiguous: {versions}")
        current = len(versions)
        if current > len(migrations):
            raise SchemaMismatch(
                f"store schema version {current} is newer than supported version {len(migrations)}"
            )
        for row in rows:
            if row["checksum"] != migrations[row["version"] - 1].checksum:
                raise SchemaMismatch(f"migration {row['version']} checksum does not match")
        pending = migrations[current:]
        if pending and not allow_migrate:
            raise MigrationRequired(current, len(migrations))

    for migration in pending:
        await _apply_migration(conn, migration, clock)


class Store:
    """Exclusive, durable owner of the controller queue. Construct with `Store.open`.

    All access is serialized on one connection. Methods must not be called from inside a
    `Store.write` callback; use its `Transaction` argument instead.
    """

    def __init__(
        self,
        path: Path,
        lock_path: Path,
        conn: aiosqlite.Connection,
        lock_fd: int,
        clock: Callable[[], int],
        last_event_id: int,
    ):
        self.path = path
        self.lock_path = lock_path
        self._conn: aiosqlite.Connection | None = conn
        self._lock_fd: int | None = lock_fd
        self._clock = clock
        self._lock = asyncio.Lock()
        self._writer_task: asyncio.Task[Any] | None = None
        self._last_event_id = last_event_id
        self._event_published = asyncio.Event()

    @classmethod
    async def open(
        cls,
        path: str | os.PathLike[str],
        *,
        allow_migrate: bool = False,
        migrations: Sequence[Migration] = MIGRATIONS,
        clock: Callable[[], int] = _utc_microseconds,
    ) -> Store:
        """Open the store at ``path``, creating it if new, and hold it exclusively until `close`.

        A sibling ``<name>.lock`` file is held with ``flock`` while the store is open.

        Parameters
        ----------
        path : str or os.PathLike
            Database file; its parent directory must exist.
        allow_migrate : bool, optional
            Apply pending migrations to an existing store.
        migrations : sequence of Migration, optional
            Schema migrations in version order.
        clock : callable, optional
            Returns the current time in UTC microseconds.

        Returns
        -------
        Store

        Raises
        ------
        StoreLocked
            The store is already open, in this or another process.
        NotAStore
            ``path`` is an SQLite file of another application.
        SchemaMismatch
            The migration history was altered or is newer than ``migrations``.
        MigrationRequired
            Migrations are pending and ``allow_migrate`` is false.
        """
        candidate = Path(path).expanduser().absolute()
        directory = candidate.parent.resolve(strict=True)
        store_path = directory / candidate.name
        lock_path = directory / (candidate.name + ".lock")

        # flock locks belong to the open file description: a second open in this process is refused
        # too, and the kernel releases the lock when the process dies. The lock file is never deleted.
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        conn: aiosqlite.Connection | None = None
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreLocked(store_path, lock_path) from None
            conn = await aiosqlite.connect(store_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            await _prepare_schema(conn, store_path, migrations, allow_migrate, clock)
            last_event_id = (await _fetchone(conn, "SELECT COALESCE(MAX(event_id), 0) FROM events"))[0]
        except BaseException:
            if conn is not None:
                await conn.close()
            os.close(lock_fd)
            raise
        return cls(store_path, lock_path, conn, lock_fd, clock, last_event_id)

    async def close(self) -> None:
        """Close the store and release its lock.

        Waits for a running transaction; further calls have no effect.
        """
        self._check_not_in_write()
        async with self._lock:
            if self._conn is None:
                return
            conn, lock_fd = self._conn, self._lock_fd
            self._conn = self._lock_fd = None
            try:
                await conn.close()
            finally:
                os.close(lock_fd)

    async def queue(self) -> QueueSnapshot:
        """Return the queue revision and the queued operations in order.

        Returns
        -------
        QueueSnapshot
        """
        return await self._read(lambda tx: tx.queue())

    async def operation(self, operation_uid: str) -> OperationRecord:
        """Return one operation in any state.

        Parameters
        ----------
        operation_uid : str
            Operation to look up.

        Returns
        -------
        OperationRecord

        Raises
        ------
        OperationNotFound
            ``operation_uid`` does not exist.
        """
        return await self._read(lambda tx: tx.operation(operation_uid))

    async def events_after(self, after: int = 0, limit: int = 100) -> list[EventRecord]:
        """Return committed events in ``event_id`` order.

        Parameters
        ----------
        after : int, optional
            Return only events with ``event_id > after``.
        limit : int, optional
            Maximum number of events.

        Returns
        -------
        list of EventRecord
        """
        self._check_not_in_write()
        async with self._lock:
            rows = await _fetchall(
                self._connection(),
                "SELECT * FROM events WHERE event_id > ? ORDER BY event_id LIMIT ?",
                (after, limit),
            )
        return [_row_to_event(row) for row in rows]

    async def write(self, fn: Callable[[Transaction], Awaitable[T]]) -> T:
        """Run ``fn`` in one transaction: commit if it returns, roll back if it raises.

        If the caller is cancelled once the commit has started, the commit and the publication of its
        events still complete before the cancellation propagates.

        Parameters
        ----------
        fn : callable
            Coroutine function receiving a `Transaction` that is valid only until ``fn`` returns.

        Returns
        -------
        T
            The result of ``fn``.
        """
        self._check_not_in_write()
        async with self._lock:
            conn = self._connection()
            self._writer_task = asyncio.current_task()
            tx = Transaction(conn, self._clock)
            # aiosqlite executes every queued request even if its awaiter is cancelled, so any exception
            # after BEGIN is queued, cancellation included, must roll back before the lock is released.
            try:
                await conn.execute("BEGIN IMMEDIATE")
                result = await fn(tx)
                last_event_id = (
                    (await _fetchone(conn, "SELECT MAX(event_id) FROM events"))[0] if tx._inserted_events else None
                )
            except BaseException:
                await conn.rollback()
                raise
            finally:
                tx._active = False
                self._writer_task = None
            await _run_to_completion(self._commit(conn, last_event_id))
        return result

    async def write_idempotent(
        self,
        scope: IdempotencyScope,
        request_hash: str,
        fn: Callable[[Transaction], Awaitable[StoredResponse]],
    ) -> IdempotentResult:
        """Run ``fn`` at most once per idempotency scope and replay its response on repeats.

        The response is recorded in the same transaction as ``fn``'s changes, so if ``fn`` raises,
        nothing is recorded and a retry runs ``fn`` again.

        Parameters
        ----------
        scope : IdempotencyScope
            Principal, method, path and idempotency key of the request.
        request_hash : str
            Opaque request fingerprint, compared for equality only.
        fn : callable
            Coroutine function receiving a `Transaction`; returns the response to record.

        Returns
        -------
        IdempotentResult
            The response, and whether it was replayed instead of running ``fn``.

        Raises
        ------
        IdempotencyConflict
            ``scope`` was already used with a different ``request_hash``.
        """

        async def run(tx: Transaction) -> IdempotentResult:
            stored = await tx._idempotent_response(scope, request_hash)
            if stored is not None:
                return IdempotentResult(stored, replayed=True)
            response = await fn(tx)
            await tx._record_idempotent_response(scope, request_hash, response)
            return IdempotentResult(response, replayed=False)

        return await self.write(run)

    async def wait_for_event(self, after: int) -> int:
        """Wait until an event newer than ``after`` is committed.

        Returns immediately if one already exists. Callers apply their own timeout.

        Parameters
        ----------
        after : int
            Event ID cursor.

        Returns
        -------
        int
            The latest committed event ID.
        """
        while self._last_event_id <= after:
            await self._event_published.wait()
        return self._last_event_id

    async def _commit(self, conn: aiosqlite.Connection, last_event_id: int | None) -> None:
        try:
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        if last_event_id is not None:
            # Synchronous, so a committed event is always published; serialized writers keep it increasing.
            self._last_event_id = last_event_id
            self._event_published.set()
            # Create a fresh event so each waiter is awoken a single time
            self._event_published = asyncio.Event()

    async def _read(self, fn: Callable[[Transaction], Awaitable[T]]) -> T:
        # Holding the store lock gives reads a consistent view: every write runs under it.
        self._check_not_in_write()
        async with self._lock:
            tx = Transaction(self._connection(), self._clock)
            try:
                return await fn(tx)
            finally:
                tx._active = False

    def _check_not_in_write(self) -> None:
        # The store lock is not reentrant: calling back into the store from a write callback would deadlock.
        if self._writer_task is asyncio.current_task():
            raise RuntimeError("Store method called inside Store.write(); use the Transaction argument")

    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(f"store {self.path} is closed")
        return self._conn


class Transaction:
    """Queue edits and reads inside one `Store.write` (or a read) call.

    Each edit checks ``expected_revision`` first, then its preconditions, then writes, bumps the queue
    revision exactly once and appends events stamped with the new revision.
    """

    def __init__(self, conn: aiosqlite.Connection, clock: Callable[[], int]):
        self._conn = conn
        self._clock = clock
        self._inserted_events = False
        self._active = True

    async def submit(
        self,
        *,
        principal: str,
        expected_revision: int,
        operation_id: str,
        operation_version: str,
        parameters: Mapping[str, Any],
        descriptor_sha256: str,
    ) -> tuple[OperationRecord, int]:
        """Append a new operation to the end of the queue.

        Parameters
        ----------
        principal : str
            Principal making the edit.
        expected_revision : int
            Queue revision the edit is based on.
        operation_id : str
            Identifier of the operation type.
        operation_version : str
            Version of the operation descriptor.
        parameters : mapping
            JSON-serializable operation parameters.
        descriptor_sha256 : str
            Hash of the operation descriptor.

        Returns
        -------
        record : OperationRecord
            The new operation.
        revision : int
            The new queue revision.

        Raises
        ------
        StaleRevision
            ``expected_revision`` is not the current revision.
        """
        self._require_active()
        await self._check_revision(expected_revision)
        record = await self._insert_operation(
            principal, operation_id, operation_version, parameters, descriptor_sha256, self._clock()
        )
        await self._conn.execute(
            "INSERT INTO queue_entries(operation_uid, position)"
            " VALUES (?, (SELECT COALESCE(MAX(position), -1) + 1 FROM queue_entries))",
            (record.operation_uid,),
        )
        revision = await self._bump_revision()
        await self._append_event(
            ActorKind.PRINCIPAL,
            principal,
            EventType.OPERATION_SUBMITTED,
            record.operation_uid,
            revision,
            _submitted_payload(record),
        )
        return record, revision

    async def replace(
        self,
        *,
        principal: str,
        expected_revision: int,
        operation_uid: str,
        operation_id: str,
        operation_version: str,
        parameters: Mapping[str, Any],
        descriptor_sha256: str,
    ) -> tuple[OperationRecord, int]:
        """Replace a queued operation by a new one at the same queue position.

        The old operation becomes ``replaced`` and links to its replacement.

        Parameters
        ----------
        principal : str
            Principal making the edit.
        expected_revision : int
            Queue revision the edit is based on.
        operation_uid : str
            Queued operation to replace.
        operation_id, operation_version, parameters, descriptor_sha256
            The replacement, as for `submit`.

        Returns
        -------
        record : OperationRecord
            The replacement.
        revision : int
            The new queue revision.

        Raises
        ------
        StaleRevision
            ``expected_revision`` is not the current revision.
        OperationNotFound
            ``operation_uid`` does not exist.
        OperationNotPending
            ``operation_uid`` is not queued.
        """
        self._require_active()
        await self._check_revision(expected_revision)
        old = await self._pending_operation(operation_uid)
        entry = await _fetchone(
            self._conn, "SELECT position FROM queue_entries WHERE operation_uid=?", (operation_uid,)
        )
        position = entry["position"]
        now = self._clock()
        # Insert before update: replaced_by references the replacement row.
        record = await self._insert_operation(
            principal, operation_id, operation_version, parameters, descriptor_sha256, now
        )
        await self._conn.execute("DELETE FROM queue_entries WHERE operation_uid=?", (operation_uid,))
        await self._conn.execute(
            "INSERT INTO queue_entries(operation_uid, position) VALUES (?, ?)", (record.operation_uid, position)
        )
        await self._conn.execute(
            "UPDATE operations SET state=?, replaced_by=?, updated_at=? WHERE operation_uid=?",
            (OperationState.REPLACED.value, record.operation_uid, max(now, old.submitted_at), operation_uid),
        )
        revision = await self._bump_revision()
        await self._append_event(
            ActorKind.PRINCIPAL,
            principal,
            EventType.OPERATION_REPLACED,
            operation_uid,
            revision,
            {"replaced_by": record.operation_uid},
        )
        await self._append_event(
            ActorKind.PRINCIPAL,
            principal,
            EventType.OPERATION_SUBMITTED,
            record.operation_uid,
            revision,
            {**_submitted_payload(record), "replaces": operation_uid},
        )
        return record, revision

    async def cancel(
        self, *, principal: str, expected_revision: int, operation_uid: str
    ) -> tuple[OperationRecord, int]:
        """Remove a queued operation from the queue and mark it cancelled.

        Parameters
        ----------
        principal : str
            Principal making the edit.
        expected_revision : int
            Queue revision the edit is based on.
        operation_uid : str
            Queued operation to cancel.

        Returns
        -------
        record : OperationRecord
            The cancelled operation.
        revision : int
            The new queue revision.

        Raises
        ------
        StaleRevision
            ``expected_revision`` is not the current revision.
        OperationNotFound
            ``operation_uid`` does not exist.
        OperationNotPending
            ``operation_uid`` is not queued.
        """
        self._require_active()
        await self._check_revision(expected_revision)
        await self._pending_operation(operation_uid)
        await self._conn.execute("DELETE FROM queue_entries WHERE operation_uid=?", (operation_uid,))
        await self._conn.execute(
            "UPDATE operations SET state=?, updated_at=MAX(?, submitted_at) WHERE operation_uid=?",
            (OperationState.CANCELLED.value, self._clock(), operation_uid),
        )
        revision = await self._bump_revision()
        await self._append_event(
            ActorKind.PRINCIPAL, principal, EventType.OPERATION_CANCELLED, operation_uid, revision, {}
        )
        return await self.operation(operation_uid), revision

    async def reorder(
        self, *, principal: str, expected_revision: int, operation_uids: Sequence[str]
    ) -> QueueSnapshot:
        """Reorder the queue; an unchanged order still counts as an edit.

        Parameters
        ----------
        principal : str
            Principal making the edit.
        expected_revision : int
            Queue revision the edit is based on.
        operation_uids : sequence of str
            Every queued operation exactly once, in the new order.

        Returns
        -------
        QueueSnapshot
            The reordered queue at the new revision.

        Raises
        ------
        StaleRevision
            ``expected_revision`` is not the current revision.
        InvalidReorder
            ``operation_uids`` omits, repeats or adds an operation.
        """
        self._require_active()
        await self._check_revision(expected_revision)
        current = [
            row["operation_uid"]
            for row in await _fetchall(self._conn, "SELECT operation_uid FROM queue_entries ORDER BY position")
        ]
        if len(operation_uids) != len(current) or set(operation_uids) != set(current):
            raise InvalidReorder()
        await self._conn.execute("DELETE FROM queue_entries")
        await self._conn.executemany(
            "INSERT INTO queue_entries(operation_uid, position) VALUES (?, ?)",
            [(uid, index) for index, uid in enumerate(operation_uids)],
        )
        revision = await self._bump_revision()
        await self._append_event(
            ActorKind.PRINCIPAL,
            principal,
            EventType.QUEUE_REORDERED,
            None,
            revision,
            {"operation_uids": list(operation_uids)},
        )
        return await self.queue()

    async def queue(self) -> QueueSnapshot:
        """Return the queue revision and the queued operations in order.

        Returns
        -------
        QueueSnapshot
        """
        self._require_active()
        rows = await _fetchall(
            self._conn,
            "SELECT o.* FROM queue_entries q JOIN operations o ON o.operation_uid = q.operation_uid"
            " ORDER BY q.position",
        )
        return QueueSnapshot(await self._revision(), tuple(_row_to_operation(row) for row in rows))

    async def operation(self, operation_uid: str) -> OperationRecord:
        """Return one operation in any state.

        Parameters
        ----------
        operation_uid : str
            Operation to look up.

        Returns
        -------
        OperationRecord

        Raises
        ------
        OperationNotFound
            ``operation_uid`` does not exist.
        """
        self._require_active()
        row = await _fetchone(self._conn, "SELECT * FROM operations WHERE operation_uid=?", (operation_uid,))
        if row is None:
            raise OperationNotFound(operation_uid)
        return _row_to_operation(row)

    def _require_active(self) -> None:
        # A stale transaction would otherwise write in autocommit mode, outside any revision guarantee.
        if not self._active:
            raise RuntimeError("Transaction used outside Store.write()")

    async def _revision(self) -> int:
        return (await _fetchone(self._conn, "SELECT queue_revision FROM store_metadata WHERE singleton=1"))[0]

    async def _check_revision(self, expected: int) -> None:
        current = await self._revision()
        if current != expected:
            raise StaleRevision(expected, current)

    async def _bump_revision(self) -> int:
        await self._conn.execute("UPDATE store_metadata SET queue_revision = queue_revision + 1 WHERE singleton=1")
        return await self._revision()

    async def _pending_operation(self, operation_uid: str) -> OperationRecord:
        record = await self.operation(operation_uid)
        if record.state is not OperationState.QUEUED:
            raise OperationNotPending(operation_uid, record.state)
        return record

    async def _insert_operation(
        self,
        principal: str,
        operation_id: str,
        operation_version: str,
        parameters: Mapping[str, Any],
        descriptor_sha256: str,
        now: int,
    ) -> OperationRecord:
        operation_uid = str(uuid.uuid4())
        parameters_json = _dumps(dict(parameters))
        await self._conn.execute(
            "INSERT INTO operations(operation_uid, operation_id, operation_version, parameters_json,"
            " descriptor_sha256, submitted_by, submitted_at, state, updated_at, replaced_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                operation_uid,
                operation_id,
                operation_version,
                parameters_json,
                descriptor_sha256,
                principal,
                now,
                OperationState.QUEUED.value,
                now,
            ),
        )
        # Return the stored representation, identical to what a later read produces.
        return OperationRecord(
            operation_uid=operation_uid,
            operation_id=operation_id,
            operation_version=operation_version,
            parameters=json.loads(parameters_json),
            descriptor_sha256=descriptor_sha256,
            submitted_by=principal,
            submitted_at=now,
            state=OperationState.QUEUED,
            updated_at=now,
            replaced_by=None,
        )

    async def _append_event(
        self,
        actor_kind: ActorKind,
        actor_id: str,
        event_type: EventType,
        operation_uid: str | None,
        queue_revision: int,
        payload: Mapping[str, Any],
    ) -> None:
        await self._conn.execute(
            "INSERT INTO events(timestamp, actor_kind, actor_id, event_type, operation_uid, queue_revision,"
            " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._clock(),
                actor_kind.value,
                actor_id,
                event_type.value,
                operation_uid,
                queue_revision,
                _dumps(dict(payload)),
            ),
        )
        self._inserted_events = True

    async def _idempotent_response(self, scope: IdempotencyScope, request_hash: str) -> StoredResponse | None:
        row = await _fetchone(
            self._conn,
            "SELECT request_hash, status_code, body_json, etag FROM idempotency_records"
            " WHERE principal=? AND method=? AND path=? AND idempotency_key=?",
            (scope.principal, scope.method, scope.path, scope.key),
        )
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict()
        return StoredResponse(row["status_code"], json.loads(row["body_json"]), row["etag"])

    async def _record_idempotent_response(
        self, scope: IdempotencyScope, request_hash: str, response: StoredResponse
    ) -> None:
        await self._conn.execute(
            "INSERT INTO idempotency_records(principal, method, path, idempotency_key, request_hash,"
            " status_code, body_json, etag, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope.principal,
                scope.method,
                scope.path,
                scope.key,
                request_hash,
                response.status_code,
                _dumps(response.body),
                response.etag,
                self._clock(),
            ),
        )


def _submitted_payload(record: OperationRecord) -> dict[str, Any]:
    return {
        "operation_id": record.operation_id,
        "operation_version": record.operation_version,
        "parameters": record.parameters,
        "descriptor_sha256": record.descriptor_sha256,
    }
