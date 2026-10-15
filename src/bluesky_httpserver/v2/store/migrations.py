"""Checksummed schema migrations for the v2 durable store.

Migration 1 may still change until the first v2 release; afterwards schema changes go only into new
migrations, because stored checksums pin every applied migration.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

APPLICATION_ID = 0x42485632
"""SQLite ``application_id`` of a store ("BHV2"); distinguishes it from any other SQLite file."""


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        """SHA-256 hex digest of the stripped statements; pins an applied migration."""
        payload = "\n;\n".join(statement.strip() for statement in self.statements)
        return hashlib.sha256(payload.encode()).hexdigest()


# Queue positions may have gaps (cancel deletes an entry without renumbering); order is ``ORDER BY position``.
_MIGRATION_1 = Migration(
    1,
    (
        """
        CREATE TABLE schema_migrations(
            version INTEGER PRIMARY KEY CHECK(version > 0),
            checksum TEXT NOT NULL,
            applied_at INTEGER NOT NULL CHECK(applied_at >= 0)
        )
        """,
        """
        CREATE TABLE store_metadata(
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            queue_revision INTEGER NOT NULL CHECK(queue_revision >= 0)
        )
        """,
        """
        INSERT INTO store_metadata(singleton, queue_revision) VALUES (1, 0)
        """,
        """
        CREATE TABLE operations(
            operation_uid TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            operation_version TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            descriptor_sha256 TEXT NOT NULL,
            submitted_by TEXT NOT NULL,
            submitted_at INTEGER NOT NULL CHECK(submitted_at >= 0),
            state TEXT NOT NULL CHECK(state IN ('queued', 'cancelled', 'replaced')),
            updated_at INTEGER NOT NULL CHECK(updated_at >= submitted_at),
            replaced_by TEXT UNIQUE REFERENCES operations(operation_uid),
            CHECK((state = 'replaced') = (replaced_by IS NOT NULL))
        )
        """,
        """
        CREATE TABLE queue_entries(
            operation_uid TEXT PRIMARY KEY REFERENCES operations(operation_uid),
            position INTEGER NOT NULL UNIQUE CHECK(position >= 0)
        )
        """,
        """
        CREATE TABLE events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL CHECK(timestamp >= 0),
            actor_kind TEXT NOT NULL CHECK(actor_kind IN ('principal', 'scheduler', 'system')),
            actor_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            operation_uid TEXT REFERENCES operations(operation_uid),
            queue_revision INTEGER NOT NULL CHECK(queue_revision >= 0),
            payload_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE idempotency_records(
            principal TEXT NOT NULL,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            body_json TEXT NOT NULL,
            etag TEXT,
            created_at INTEGER NOT NULL CHECK(created_at >= 0),
            PRIMARY KEY(principal, method, path, idempotency_key)
        )
        """,
        """
        CREATE TRIGGER operations_identity_immutable
        BEFORE UPDATE OF operation_uid, operation_id, operation_version, parameters_json,
                         descriptor_sha256, submitted_by, submitted_at
        ON operations
        BEGIN
            SELECT RAISE(ABORT, 'operation identity is immutable');
        END
        """,
        """
        CREATE TRIGGER operations_history_preserved
        BEFORE DELETE ON operations
        BEGIN
            SELECT RAISE(ABORT, 'operations cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER events_append_only_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END
        """,
        """
        CREATE TRIGGER events_append_only_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END
        """,
    ),
)

MIGRATIONS: tuple[Migration, ...] = (_MIGRATION_1,)
