"""Immutable records returned by the v2 durable store.

Timestamps are integer UTC microseconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class OperationState(str, Enum):
    """Where an operation is in its lifecycle.

    Attributes
    ----------
    QUEUED
        Waiting in the queue; can still be edited.
    CANCELLED
        Withdrawn from the queue; will not run.
    REPLACED
        Superseded by another operation that took its place; will not run.
    """

    QUEUED = "queued"
    CANCELLED = "cancelled"
    REPLACED = "replaced"


class ActorKind(str, Enum):
    """Who caused an event.

    Attributes
    ----------
    PRINCIPAL
        A client acting through the API.
    SCHEDULER
        TODO: not written yet. The controller's scheduler running the queue.
    SYSTEM
        TODO: not written yet. The controller acting on its own behalf.
    """

    PRINCIPAL = "principal"
    SCHEDULER = "scheduler"
    SYSTEM = "system"


class EventType(str, Enum):
    """Kind of event.

    Attributes
    ----------
    OPERATION_SUBMITTED
        An operation was added to the queue, by submission or as a replacement.
    OPERATION_REPLACED
        A queued operation was replaced by a new one at the same position.
    OPERATION_CANCELLED
        A queued operation was cancelled and removed from the queue.
    QUEUE_REORDERED
        The queue order was changed.
    """

    OPERATION_SUBMITTED = "operation.submitted"
    OPERATION_REPLACED = "operation.replaced"
    OPERATION_CANCELLED = "operation.cancelled"
    QUEUE_REORDERED = "queue.reordered"


@dataclass(frozen=True)
class OperationRecord:
    """One submitted unit of work; what to run never changes after submission.

    Attributes
    ----------
    operation_uid : str
        Identifies the operation for its whole life.
    operation_id : str
        Which catalog operation to run.
    operation_version : str
        Which version of that operation to run.
    parameters : dict
        Arguments to run it with.
    descriptor_sha256 : str
        Identifies the descriptor the parameters were validated against.
    submitted_by : str
        Who submitted it.
    submitted_at : int
        When it was submitted.
    state : OperationState
        Where it is in its lifecycle.
    updated_at : int
        When its state last changed.
    replaced_by : str or None
        The operation that superseded it, if replaced.
    """

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
    """The queue at one point in time.

    Attributes
    ----------
    revision : int
        Version of the queue; clients edit against it to detect concurrent changes.
    pending : tuple of OperationRecord
        Operations waiting to run, in the order they will run.
    """

    revision: int
    pending: tuple[OperationRecord, ...]


@dataclass(frozen=True)
class EventRecord:
    """Something that happened to the queue.

    Attributes
    ----------
    event_id : int
        Position in the event log; clients resume reading after it.
    timestamp : int
        When it happened.
    actor_kind : ActorKind
        What kind of actor caused it.
    actor_id : str
        Who caused it.
    event_type : EventType
        What happened.
    operation_uid : str or None
        The operation it happened to; None if it concerns the whole queue.
    queue_revision : int
        Queue revision it produced.
    payload : dict
        Details of what happened.
    """

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
    """Identifies a request so that a retry gets the original response.

    Attributes
    ----------
    principal : str
        Who sent the request.
    method : str
        HTTP method of the request.
    path : str
        Resource the request targets.
    key : str
        Client-chosen key, the same on every retry of one request.
    """

    principal: str
    method: str
    path: str
    key: str


@dataclass(frozen=True)
class StoredResponse:
    """The original response to a request, replayed on retries.

    Attributes
    ----------
    status_code : int
        HTTP status of the response.
    body : dict
        Body of the response.
    etag : str or None
        Queue revision the response reflects, as an ETag, if any.
    """

    status_code: int
    body: dict[str, Any]
    etag: str | None


@dataclass(frozen=True)
class IdempotentResult:
    """Response to an idempotent request, and whether the request was a retry.

    Attributes
    ----------
    response : StoredResponse
        Response to send to the client.
    replayed : bool
        True if the request was a retry and nothing was executed.
    """

    response: StoredResponse
    replayed: bool
