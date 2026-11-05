"""Where a store lives on disk, its lock, and checks of a database file's identity and migration history;
shared by `Store.open` and the admin operations."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Sequence
from pathlib import Path

from .errors import NotAStore, SchemaMismatch, StoreLocked
from .migrations import APPLICATION_ID, Migration


def store_paths(path: str | os.PathLike[str]) -> tuple[Path, Path]:
    """Absolute paths of the store at ``path`` and of its sibling ``<name>.lock`` file.

    Parameters
    ----------
    path : str or os.PathLike
        Database file; its parent directory must exist.

    Returns
    -------
    tuple of Path
        ``(store_path, lock_path)``.

    Raises
    ------
    FileNotFoundError
        The parent directory of ``path`` does not exist.
    """
    candidate = Path(path).expanduser().absolute()
    directory = candidate.parent.resolve(strict=True)
    store_path = directory / candidate.name
    lock_path = directory / (candidate.name + ".lock")
    return store_path, lock_path


def lock_store(store_path: Path, lock_path: Path) -> int:
    """Hold the store's lock exclusively, without waiting.

    Parameters
    ----------
    store_path : Path
        The store, named in the error.
    lock_path : Path
        Its lock file, created if missing.

    Returns
    -------
    int
        File descriptor of the lock file; closing it releases the lock.

    Raises
    ------
    StoreLocked
        The lock is held, in this or another process.
    """
    # flock locks belong to the open file description: a second open in this process is refused
    # too, and the kernel releases the lock when the process dies. The lock file is never deleted.
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise StoreLocked(store_path, lock_path) from None
    except BaseException:
        os.close(lock_fd)
        raise
    return lock_fd


def check_application_id(path: Path, application_id: int, has_tables: bool) -> None:
    """Require the database at ``path`` to be a store, or a new, empty database.

    Parameters
    ----------
    path : Path
        The database, named in the error.
    application_id : int
        Its ``PRAGMA application_id``.
    has_tables : bool
        Whether it holds any table.

    Raises
    ------
    NotAStore
        The database belongs to another application.
    """
    if application_id != APPLICATION_ID and (has_tables or application_id != 0):
        raise NotAStore(path, application_id)


def applied_version(path: Path, history: Sequence[tuple[int, str]] | None, migrations: Sequence[Migration]) -> int:
    """Schema version of a store whose migration history is ``history``, checked against ``migrations``.

    Parameters
    ----------
    path : Path
        The store, named in the error.
    history : sequence of (int, str) or None
        ``(version, checksum)`` rows of ``schema_migrations`` in version order; None if that table is missing.
    migrations : sequence of Migration
        Schema migrations in version order.

    Returns
    -------
    int
        Number of applied migrations.

    Raises
    ------
    SchemaMismatch
        The history is missing, altered, or newer than ``migrations``.
    """
    if history is None:
        raise SchemaMismatch(f"{path} has no migration history")
    versions = [version for version, _ in history]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaMismatch(f"migration history is not contiguous: {versions}")
    current = len(versions)
    if current > len(migrations):
        raise SchemaMismatch(f"store schema version {current} is newer than supported version {len(migrations)}")
    for version, checksum in history:
        if checksum != migrations[version - 1].checksum:
            raise SchemaMismatch(f"migration {version} checksum does not match")
    return current
