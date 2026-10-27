"""Immutable records returned by the v2 durable store.

Timestamps are integer UTC microseconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class OperationState(StrEnum):
    """Where an operation is in its lifecycle.

    Attributes
    ----------
    QUEUED
        Waiting in the queue; can still be edited.
    CANCELLED
        Withdrawn from the queue; will not run.
    REPLACED
        Superseded by another operation that took its place; will not run.
    CLAIMED
        Taken off the queue to run; can no longer be edited.
    RUNNING
        Running on the worker.
    SUCCEEDED
        Ran to completion.
    FAILED
        Ran and failed; will not be retried.
    ABORTED
        Stopped before finishing.
    """

    QUEUED = "queued"
    CANCELLED = "cancelled"
    REPLACED = "replaced"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class ActorKind(StrEnum):
    """Who caused an event.

    Attributes
    ----------
    PRINCIPAL
        A client acting through the API.
    SCHEDULER
        The controller's scheduler, acting for a queue execution.
    SYSTEM
        TODO: not written yet. The controller acting on its own behalf.
    """

    PRINCIPAL = "principal"
    SCHEDULER = "scheduler"
    SYSTEM = "system"


class EventType(StrEnum):
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
    OPERATION_CLAIMED
        A queued operation was taken off the queue to run.
    OPERATION_RUNNING
        A claimed operation started running on the worker.
    OPERATION_SUCCEEDED
        An operation ran to completion.
    OPERATION_FAILED
        An operation ran and failed.
    OPERATION_ABORTED
        An operation was stopped before finishing.
    QUEUE_EXECUTION_STARTED
        The queue started running.
    QUEUE_EXECUTION_STOP_REQUESTED
        The queue was asked to stop after the current operation.
    QUEUE_EXECUTION_COMPLETED
        The queue finished; no admitted operation is left to run.
    QUEUE_EXECUTION_STOPPED
        The queue stopped before running all admitted operations.
    LEASE_ACQUIRED
        A principal took control of the instrument.
    LEASE_RENEWED
        The controlling principal extended its control.
    LEASE_RELEASED
        The controlling principal gave up control.
    LEASE_OVERRIDDEN
        An administrator took control, displacing any holder.
    """

    OPERATION_SUBMITTED = "operation.submitted"
    OPERATION_REPLACED = "operation.replaced"
    OPERATION_CANCELLED = "operation.cancelled"
    QUEUE_REORDERED = "queue.reordered"
    OPERATION_CLAIMED = "operation.claimed"
    OPERATION_RUNNING = "operation.running"
    OPERATION_SUCCEEDED = "operation.succeeded"
    OPERATION_FAILED = "operation.failed"
    OPERATION_ABORTED = "operation.aborted"
    QUEUE_EXECUTION_STARTED = "queue_execution.started"
    QUEUE_EXECUTION_STOP_REQUESTED = "queue_execution.stop_requested"
    QUEUE_EXECUTION_COMPLETED = "queue_execution.completed"
    QUEUE_EXECUTION_STOPPED = "queue_execution.stopped"
    LEASE_ACQUIRED = "lease.acquired"
    LEASE_RENEWED = "lease.renewed"
    LEASE_RELEASED = "lease.released"
    LEASE_OVERRIDDEN = "lease.overridden"


class QueueExecutionState(StrEnum):
    """Where a queue execution is in its lifecycle.

    Attributes
    ----------
    RUNNING
        Running admitted operations, one at a time.
    STOPPING
        Will stop once the current operation finishes.
    COMPLETED
        Finished; no admitted operation is left to run.
    STOPPED
        Stopped before running all admitted operations.
    """

    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STOPPED = "stopped"


class FailurePolicy(StrEnum):
    """What a failed operation does to its queue execution.

    Attributes
    ----------
    STOP_ON_FAILURE
        A failed operation stops the queue.
    CONTINUE_ON_FAILURE
        A failed operation does not stop the queue.
    """

    STOP_ON_FAILURE = "stop_on_failure"
    CONTINUE_ON_FAILURE = "continue_on_failure"


class AttemptState(StrEnum):
    """Where an attempt is in its lifecycle.

    Attributes
    ----------
    CLAIMED
        Authorized to run; not started on the worker yet.
    RUNNING
        Running on the worker.
    SUCCEEDED
        Ran to completion.
    FAILED
        Ran and failed; will not be retried.
    ABORTED
        Stopped before finishing.
    """

    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


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
class LeaseRecord:
    """Control of the instrument held by one principal.

    Attributes
    ----------
    lease_uid : str
        Identifies this grant of control.
    holder : str
        Principal in control.
    acquired_at : int
        When control was taken.
    expires_at : int
        When control lapses unless renewed.
    """

    lease_uid: str
    holder: str
    acquired_at: int
    expires_at: int


@dataclass(frozen=True)
class QueueExecutionRecord:
    """One run of the queue; authorizes the scheduler to execute admitted operations.

    Attributes
    ----------
    queue_execution_uid : str
        Identifies the run.
    state : QueueExecutionState
        Where it is in its lifecycle.
    policy : FailurePolicy
        What a failed operation does to the run.
    started_by : str
        Who started it.
    started_at : int
        When it started.
    updated_at : int
        When its state last changed.
    """

    queue_execution_uid: str
    state: QueueExecutionState
    policy: FailurePolicy
    started_by: str
    started_at: int
    updated_at: int


@dataclass(frozen=True)
class AttemptRecord:
    """One authorized execution of an operation.

    Attributes
    ----------
    attempt_uid : str
        Identifies the attempt.
    operation_uid : str
        Operation being executed.
    queue_execution_uid : str
        Queue execution that authorized it.
    state : AttemptState
        Where it is in its lifecycle.
    claimed_at : int
        When it was authorized.
    updated_at : int
        When its state last changed.
    item_uid : str or None
        The manager's identifier for the running item, once started.
    diagnostic : str or None
        Why it ended as it did, if known.
    """

    attempt_uid: str
    operation_uid: str
    queue_execution_uid: str
    state: AttemptState
    claimed_at: int
    updated_at: int
    item_uid: str | None
    diagnostic: str | None


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
    queue_execution_uid : str or None
        The queue execution it concerns, if any.
    attempt_uid : str or None
        The attempt it concerns, if any.
    queue_revision : int
        Queue revision after it happened.
    payload : dict
        Details of what happened.
    """

    event_id: int
    timestamp: int
    actor_kind: ActorKind
    actor_id: str
    event_type: EventType
    operation_uid: str | None
    queue_execution_uid: str | None
    attempt_uid: str | None
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
