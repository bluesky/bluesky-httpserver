import contextlib
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from bluesky_httpserver.v2.store import MigrationRequired, NotAStore, SchemaMismatch, Store, StoreLocked
from bluesky_httpserver.v2.store.migrations import APPLICATION_ID, MIGRATIONS, Migration


@pytest.mark.asyncio
async def test_second_open_is_refused_until_close(db_path):
    store = await Store.open(db_path)
    try:
        with pytest.raises(StoreLocked):
            await Store.open(db_path)
    finally:
        await store.close()
    reopened = await Store.open(db_path)
    await reopened.close()


@pytest.mark.asyncio
async def test_lock_is_released_when_process_dies(db_path):
    script = textwrap.dedent(
        f"""
        import asyncio
        import os

        from bluesky_httpserver.v2.store import Store

        async def main():
            store = await Store.open({str(db_path)!r})
            await store.write(lambda tx: tx.acquire_lease(principal="alice", ttl_seconds=3600))
            await store.write(
                lambda tx: tx.submit(
                    principal="alice",
                    expected_revision=0,
                    operation_id="demo.count",
                    operation_version="1",
                    parameters={{"num": 1}},
                    descriptor_sha256="0" * 64,
                )
            )
            os._exit(0)

        asyncio.run(main())
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
    store = await Store.open(db_path)
    try:
        snapshot = await store.queue()
    finally:
        await store.close()
    assert snapshot.revision == 1
    assert len(snapshot.pending) == 1


@pytest.mark.asyncio
async def test_foreign_sqlite_file_is_refused(db_path):
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE t(x)")
    with pytest.raises(NotAStore):
        await Store.open(db_path)
    # The failed open released its lock.
    with pytest.raises(NotAStore):
        await Store.open(db_path)


@pytest.mark.asyncio
async def test_tampered_migration_checksum_is_refused(db_path):
    await (await Store.open(db_path)).close()
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE schema_migrations SET checksum='0'")
        conn.commit()
    with pytest.raises(SchemaMismatch, match="checksum"):
        await Store.open(db_path)


@pytest.mark.asyncio
async def test_pending_migrations_require_permission(db_path):
    extended = MIGRATIONS + (Migration(2, ("CREATE TABLE extra(x INTEGER)",)),)
    await (await Store.open(db_path)).close()

    with pytest.raises(MigrationRequired) as exc_info:
        await Store.open(db_path, migrations=extended)
    assert (exc_info.value.current, exc_info.value.target) == (1, 2)

    await (await Store.open(db_path, migrations=extended, allow_migrate=True)).close()

    with pytest.raises(SchemaMismatch, match="newer"):
        await Store.open(db_path)


@pytest.mark.asyncio
async def test_new_store_is_tagged_and_uses_wal(db_path):
    await (await Store.open(db_path)).close()
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
