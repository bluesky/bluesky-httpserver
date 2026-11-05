from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from bluesky_httpserver.v2.cli import main
from bluesky_httpserver.v2.store import (
    EventRecord,
    NotAStore,
    SchemaMismatch,
    Store,
    StoreError,
    StoreLocked,
    StoreNotFound,
)
from bluesky_httpserver.v2.store.admin import backup, migrate, restore
from bluesky_httpserver.v2.store.migrations import MIGRATIONS, Migration

from .conftest import acquire, submit

EXTENDED = MIGRATIONS + (Migration(2, ("CREATE TABLE extra(x INTEGER)",)),)


async def populated(path: Path) -> list[EventRecord]:
    store = await Store.open(path)
    try:
        await acquire(store)
        await submit(store, 0)
        return await store.events_after(0)
    finally:
        await store.close()


async def events(path: Path) -> list[EventRecord]:
    store = await Store.open(path)
    try:
        return await store.events_after(0)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backup_and_restore_round_trip(db_path, tmp_path):
    original = await populated(db_path)
    copy = tmp_path / "backup.sqlite"
    backup(db_path, copy)
    restored = tmp_path / "restored.sqlite"

    assert restore(copy, restored) == 1

    assert await events(restored) == original
    store = await Store.open(restored)
    try:
        assert (await store.read(lambda tx: tx.queue())).revision == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_admin_refuses_an_open_store(db_path, tmp_path):
    other = tmp_path / "other.sqlite"
    await populated(other)
    other_backup = tmp_path / "other-backup.sqlite"
    backup(other, other_backup)
    destination = tmp_path / "backup.sqlite"

    store = await Store.open(db_path)
    try:
        with pytest.raises(StoreLocked):
            backup(db_path, destination)
        assert not destination.exists()
        with pytest.raises(StoreLocked):
            restore(other_backup, db_path, replace=True)
        with pytest.raises(StoreLocked):
            await migrate(db_path)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_restore_over_an_existing_store_needs_replace(tmp_path):
    a, b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    a_events, b_events = await populated(a), await populated(b)
    assert a_events != b_events
    a_backup = tmp_path / "a-backup.sqlite"
    backup(a, a_backup)

    with pytest.raises(FileExistsError):
        restore(a_backup, b)
    assert await events(b) == b_events

    restore(a_backup, b, replace=True)
    assert await events(b) == a_events


async def foreign(path: Path) -> None:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t(x)")


async def tampered(path: Path) -> None:
    await (await Store.open(path)).close()
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE schema_migrations SET checksum='0'")
        conn.commit()


async def newer(path: Path) -> None:
    await (await Store.open(path, migrations=EXTENDED, allow_migrate=True)).close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make", "error", "match"),
    [(foreign, NotAStore, None), (tampered, SchemaMismatch, "checksum"), (newer, SchemaMismatch, "newer")],
)
async def test_restore_refuses_bad_backups(
    db_path, tmp_path, make: Callable[[Path], Awaitable[None]], error: type[StoreError], match: str | None
):
    bad = tmp_path / "bad.sqlite"
    await make(bad)

    with pytest.raises(error, match=match):
        restore(bad, db_path)
    assert not db_path.exists()


@pytest.mark.asyncio
async def test_backup_preconditions(db_path, tmp_path):
    destination = tmp_path / "backup.sqlite"
    with pytest.raises(StoreNotFound):
        backup(db_path, destination)

    await populated(db_path)
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        backup(db_path, destination)
    assert destination.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_migrate(db_path, tmp_path):
    await (await Store.open(db_path)).close()

    assert await migrate(db_path, migrations=EXTENDED) == 2
    await (await Store.open(db_path, migrations=EXTENDED)).close()

    missing = tmp_path / "missing.sqlite"
    with pytest.raises(StoreNotFound):
        await migrate(missing)
    assert not missing.exists()


@pytest.mark.asyncio
async def test_cli(db_path, tmp_path, capsys):
    missing = tmp_path / "missing.sqlite"
    destination = tmp_path / "backup.sqlite"

    assert main(["backup", str(missing), str(destination)]) == 1
    assert capsys.readouterr().err.startswith("error: no store at")

    await populated(db_path)
    assert main(["backup", str(db_path), str(destination)]) == 0
    assert capsys.readouterr().out == f"backed up {db_path} to {destination}\n"

    assert main(["restore", str(destination), str(db_path)]) == 1
    assert capsys.readouterr().err.endswith("; pass --replace to overwrite it\n")
