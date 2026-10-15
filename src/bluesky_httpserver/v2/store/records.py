"""Immutable records returned by the v2 durable store.

Timestamps are integer UTC microseconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class OperationState(str, Enum):
    QUEUED = "queued"
    CANCELLED = "cancelled"
    REPLACED = "replaced"


class ActorKind(str, Enum):
    PRINCIPAL = "principal"
    SCHEDULER = "scheduler"
    SYSTEM = "system"


class EventType(str, Enum):
    OPERATION_SUBMITTED = "operation.submitted"
    OPERATION_REPLACED = "operation.replaced"
    OPERATION_CANCELLED = "operation.cancelled"
    QUEUE_REORDERED = "queue.reordered"


@dataclass(frozen=True)
class OperationRecord:
    operation_uid: str
    operation_id: str
    operation_version: str
    parameters: dict[str, Any]
    descriptor_sha256: str
    submitted_by: str
    submitted_at: int
    state: OperationState
    updated_at: int
    replaced_by: str | None


@dataclass(frozen=True)
class QueueSnapshot:
    revision: int
    pending: tuple[OperationRecord, ...]
    """Queued operations in queue order."""


@dataclass(frozen=True)
class EventRecord:
    event_id: int
    timestamp: int
    actor_kind: ActorKind
    actor_id: str
    event_type: EventType
    operation_uid: str | None
    queue_revision: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class IdempotencyScope:
    principal: str
    method: str
    path: str
    key: str


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]
    etag: str | None


@dataclass(frozen=True)
class IdempotentResult:
    response: StoredResponse
    replayed: bool
