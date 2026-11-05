"""Offline maintenance of a store: each operation takes the store's lock, so it refuses to run while a
controller has the store open."""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from .errors import NotAStore, StoreNotFound
from .files import applied_version, check_application_id, lock_store, store_paths
from .migrations import APPLICATION_ID, MIGRATIONS, Migration
from .store import Store

# Backup and restore use plain sqlite3: `Store.open` refuses a store with pending migrations, and a store
# must be backed up before it is migrated.


def _identity(conn: sqlite3.Connection) -> tuple[int, bool]:
    """``PRAGMA application_id`` of the database, and whether it holds any table."""
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return application_id, table is not None


def _history(conn: sqlite3.Connection) -> list[tuple[int, str]] | None:
    """``(version, checksum)`` rows of ``schema_migrations`` in version order; None if the table is missing."""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if table is None:
        return None
    return conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()


def _require_store(path: Path, conn: sqlite3.Connection) -> None:
    """Refuse anything but a store with a schema; an empty file is not a store to copy from."""
    application_id, has_tables = _identity(conn)
    if application_id != APPLICATION_ID or not has_tables:
        raise NotAStore(path, application_id)


def backup(path: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """Copy the store at ``path`` to the new file ``destination``.

    Parameters
    ----------
    path : str or os.PathLike
        The store; its parent directory must exist.
    destination : str or os.PathLike
        The copy; must not exist.

    Raises
    ------
    StoreNotFound
        ``path`` does not exist.
    FileExistsError
        ``destination`` exists.
    StoreLocked
        The store is open.
    NotAStore
        ``path`` is not a store.
    """
    store_path, lock_path = store_paths(path)
    target = Path(destination).expanduser().absolute()
    if not store_path.exists():
        raise StoreNotFound(store_path)
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    lock_fd = lock_store(store_path, lock_path)
    try:
        with contextlib.closing(sqlite3.connect(store_path)) as source:
            _require_store(store_path, source)
            try:
                with contextlib.closing(sqlite3.connect(target)) as target_conn:
                    source.backup(target_conn)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
    finally:
        os.close(lock_fd)


def restore(
    source: str | os.PathLike[str],
    path: str | os.PathLike[str],
    *,
    replace: bool = False,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> int:
    """Replace the store at ``path`` with the backup ``source``.

    The backup is checked before the store is touched. A backup older than ``migrations`` is restored as is;
    `migrate` brings it up to date.

    Parameters
    ----------
    source : str or os.PathLike
        A backup made by `backup`.
    path : str or os.PathLike
        The store; its parent directory must exist.
    replace : bool, optional
        Overwrite an existing store.
    migrations : sequence of Migration, optional
        Schema migrations in version order.

    Returns
    -------
    int
        Schema version of the backup.

    Raises
    ------
    StoreNotFound
        ``source`` does not exist.
    NotAStore
        ``source``, or the existing file at ``path``, is not a store.
    SchemaMismatch
        The backup's migration history was altered or is newer than ``migrations``.
    FileExistsError
        ``path`` exists and ``replace`` is false.
    StoreLocked
        The store is open.
    """
    backup_path = Path(source).expanduser().absolute()
    store_path, lock_path = store_paths(path)
    if not backup_path.exists():
        raise StoreNotFound(backup_path)
    with contextlib.closing(sqlite3.connect(backup_path)) as backup_conn:
        _require_store(backup_path, backup_conn)
        version = applied_version(backup_path, _history(backup_conn), migrations)
        lock_fd = lock_store(store_path, lock_path)
        try:
            existed = store_path.exists()
            if existed and not replace:
                raise FileExistsError(f"{store_path} already exists")
            try:
                with contextlib.closing(sqlite3.connect(store_path)) as target:
                    if existed:
                        check_application_id(store_path, *_identity(target))
                    # Replaces the target in one transaction, after recovering any leftover WAL.
                    backup_conn.backup(target)
            except BaseException:
                # A new, empty file would later open as a fresh store.
                if not existed:
                    store_path.unlink(missing_ok=True)
                raise
        finally:
            os.close(lock_fd)
    return version


async def migrate(path: str | os.PathLike[str], *, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Apply pending migrations to the existing store at ``path``.

    Parameters
    ----------
    path : str or os.PathLike
        The store; its parent directory must exist.
    migrations : sequence of Migration, optional
        Schema migrations in version order.

    Returns
    -------
    int
        Schema version of the store.

    Raises
    ------
    StoreNotFound
        ``path`` does not exist.
    StoreLocked
        The store is open.
    NotAStore
        ``path`` is not a store.
    SchemaMismatch
        The store's migration history was altered or is newer than ``migrations``.
    """
    store_path, _ = store_paths(path)
    if not store_path.exists():
        raise StoreNotFound(store_path)
    store = await Store.open(store_path, allow_migrate=True, migrations=migrations)
    await store.close()
    return len(migrations)
