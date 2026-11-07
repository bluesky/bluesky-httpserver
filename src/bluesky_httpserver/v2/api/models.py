"""Request bodies and response views of the v2 HTTP API.

Timestamps in views are RFC 3339 UTC strings; event payloads are passed through as the store recorded them.
Attribute docstrings become field descriptions in the OpenAPI document.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ..catalog import Catalog, OperationDescriptor
from ..gateway import CatalogState, DispatchState, EnvironmentPresence, Readiness, WorkerReachability
from ..store import (
    LEASE_TTL_MAX_SECONDS,
    LEASE_TTL_MIN_SECONDS,
    ActorKind,
    AttemptRecord,
    AttemptState,
    EventRecord,
    EventType,
    FailurePolicy,
    LeaseRecord,
    OperationRecord,
    OperationState,
    QueueExecutionRecord,
    QueueExecutionState,
    QueueSnapshot,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc(microseconds: int) -> datetime:
    """Convert a store timestamp, in UTC microseconds since the Unix epoch, to an aware datetime."""
    return _EPOCH + timedelta(microseconds=microseconds)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


_Note = Annotated[str, Field(min_length=1, max_length=1000), AfterValidator(_not_blank)]
"""Free text an operator records with an action; must not be blank."""


class _Model(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)


class _RequestBody(_Model):
    # Unknown fields are refused, so a client cannot name an actor or any other server-owned field.
    model_config = ConfigDict(extra="forbid")


class LeaseRequest(_RequestBody):
    """Take or extend control of the instrument."""

    ttl_seconds: int = Field(ge=LEASE_TTL_MIN_SECONDS, le=LEASE_TTL_MAX_SECONDS)
    """Seconds from now until control lapses unless renewed."""


class LeaseOverrideRequest(_RequestBody):
    """Take control of the instrument from its holder."""

    reason: _Note
    """Why control is taken; recorded in the `lease.overridden` event. Must not be blank."""
    ttl_seconds: int = Field(ge=LEASE_TTL_MIN_SECONDS, le=LEASE_TTL_MAX_SECONDS)
    """Seconds from now until control lapses unless renewed."""


class RecoveryAcknowledgement(_RequestBody):
    """Clear the dispatch block after an attempt's outcome could not be proven."""

    note: _Note
    """What the operator checked before clearing the block; recorded in the `worker.fenced` event."""


class OperationSubmission(_RequestBody):
    """An operation to queue: which catalog operation, and its parameters."""

    operation_id: str = Field(min_length=1)
    """`operation_id` of an operation in `GET /api/v2/catalog`."""
    operation_version: str = Field(min_length=1)
    """`operation_version` of that operation."""
    parameters: dict[str, Any]
    """Arguments; must match the operation's `parameters_schema`. NaN and infinite numbers are refused."""


class ReorderRequest(_RequestBody):
    """A new order for the queue."""

    operation_uids: list[str]
    """Every queued operation exactly once, in the order they should run."""


class QueueExecutionStart(_RequestBody):
    """Start running the queue."""

    policy: FailurePolicy = FailurePolicy.STOP_ON_FAILURE
    """Whether a failed operation stops the queue."""


class OperationView(_Model):
    """One submitted operation, in any state."""

    operation_uid: str
    """Identifies the operation for its whole life."""
    operation_id: str
    """Which catalog operation it runs."""
    operation_version: str
    """Which version of that operation it runs."""
    parameters: dict[str, Any]
    """Arguments it runs with; never change after submission."""
    descriptor_sha256: str
    """Identifies the catalog descriptor the parameters were validated against."""
    submitted_by: str
    """Principal that submitted it."""
    submitted_at: datetime
    """When it was submitted."""
    state: OperationState
    """Where it is in its lifecycle."""
    updated_at: datetime
    """When its state last changed."""
    replaced_by: str | None
    """The operation that took its place, if it was replaced."""

    @classmethod
    def from_record(cls, record: OperationRecord) -> OperationView:
        return cls(
            operation_uid=record.operation_uid,
            operation_id=record.operation_id,
            operation_version=record.operation_version,
            parameters=record.parameters,
            descriptor_sha256=record.descriptor_sha256,
            submitted_by=record.submitted_by,
            submitted_at=utc(record.submitted_at),
            state=record.state,
            updated_at=utc(record.updated_at),
            replaced_by=record.replaced_by,
        )


class QueueView(_Model):
    """The queued operations in the order they will run, and the revision they were read at."""

    revision: int
    """Queue revision; the response's `ETag` is `"qrev-<revision>"`."""
    operations: list[OperationView]
    """Queued operations, first to run first."""

    @classmethod
    def from_snapshot(cls, snapshot: QueueSnapshot) -> QueueView:
        return cls(
            revision=snapshot.revision,
            operations=[OperationView.from_record(record) for record in snapshot.pending],
        )


class LeaseView(_Model):
    """Control of the instrument held by one principal."""

    lease_uid: str
    """Identifies this grant of control."""
    holder: str
    """Principal in control."""
    acquired_at: datetime
    """When control was taken."""
    expires_at: datetime
    """When control lapses unless renewed."""

    @classmethod
    def from_record(cls, record: LeaseRecord) -> LeaseView:
        return cls(
            lease_uid=record.lease_uid,
            holder=record.holder,
            acquired_at=utc(record.acquired_at),
            expires_at=utc(record.expires_at),
        )


class ControlLeaseView(_Model):
    """Who controls the instrument."""

    lease: LeaseView | None
    """The lease; null when nobody holds control."""


class QueueExecutionView(_Model):
    """One run of the queue."""

    queue_execution_uid: str
    """Identifies the run."""
    state: QueueExecutionState
    """Where it is in its lifecycle."""
    policy: FailurePolicy
    """Whether a failed operation stops it."""
    started_by: str
    """Principal that started it."""
    started_at: datetime
    """When it started."""
    updated_at: datetime
    """When its state last changed."""

    @classmethod
    def from_record(cls, record: QueueExecutionRecord) -> QueueExecutionView:
        return cls(
            queue_execution_uid=record.queue_execution_uid,
            state=record.state,
            policy=record.policy,
            started_by=record.started_by,
            started_at=utc(record.started_at),
            updated_at=utc(record.updated_at),
        )


class AttemptView(_Model):
    """One authorized execution of an operation."""

    attempt_uid: str
    """Identifies the attempt."""
    operation_uid: str
    """Operation being executed."""
    queue_execution_uid: str
    """Queue execution that authorized it."""
    state: AttemptState
    """Where it is in its lifecycle."""
    claimed_at: datetime
    """When it was authorized."""
    updated_at: datetime
    """When its state last changed."""
    item_uid: str | None
    """The manager's identifier for the running item, once started."""
    diagnostic: str | None
    """Why it ended as it did, if known."""
    safe_stop_requested_at: datetime | None
    """When a safe stop was requested; null if none was."""
    stop_now_requested_at: datetime | None
    """When a stop now was requested; null if none was."""
    current_run_uids: list[str] | None
    """Bluesky runs a running attempt has opened so far, as RE Manager last reported them; null for an attempt
    that is not running, or whose runs RE Manager has not reported yet. A finished attempt's runs are the
    `run_uids` of its last event."""

    @classmethod
    def from_record(cls, record: AttemptRecord, current_run_uids: Sequence[str] | None = None) -> AttemptView:
        return cls(
            attempt_uid=record.attempt_uid,
            operation_uid=record.operation_uid,
            queue_execution_uid=record.queue_execution_uid,
            state=record.state,
            claimed_at=utc(record.claimed_at),
            updated_at=utc(record.updated_at),
            item_uid=record.item_uid,
            diagnostic=record.diagnostic,
            safe_stop_requested_at=(
                None if record.safe_stop_requested_at is None else utc(record.safe_stop_requested_at)
            ),
            stop_now_requested_at=(
                None if record.stop_now_requested_at is None else utc(record.stop_now_requested_at)
            ),
            current_run_uids=None if current_run_uids is None else list(current_run_uids),
        )


class ActiveExecutionView(_Model):
    """What runs now: the queue execution and its live attempt, which the stop routes take."""

    queue_execution: QueueExecutionView | None
    """The running or stopping queue execution; null if the queue is not running."""
    live_attempt: AttemptView | None
    """The claimed or running attempt; null if no operation is being executed."""


class EventView(_Model):
    """Something that happened to the queue."""

    event_id: int
    """Position in the event log; pass the last one received as `after` or `Last-Event-ID` to resume."""
    timestamp: datetime
    """When it happened."""
    actor_kind: ActorKind
    """What kind of actor caused it."""
    actor_id: str
    """Who caused it."""
    event_type: EventType
    """What happened."""
    operation_uid: str | None
    """The operation it happened to; null if it concerns the whole queue."""
    queue_execution_uid: str | None
    """The queue execution it concerns, if any."""
    attempt_uid: str | None
    """The attempt it concerns, if any."""
    queue_revision: int
    """Queue revision after it happened."""
    payload: dict[str, Any]
    """Details, as recorded; timestamps in it, such as a lease's `expires_at`, are integer microseconds since
    the Unix epoch."""

    @classmethod
    def from_record(cls, record: EventRecord) -> EventView:
        return cls(
            event_id=record.event_id,
            timestamp=utc(record.timestamp),
            actor_kind=record.actor_kind,
            actor_id=record.actor_id,
            event_type=record.event_type,
            operation_uid=record.operation_uid,
            queue_execution_uid=record.queue_execution_uid,
            attempt_uid=record.attempt_uid,
            queue_revision=record.queue_revision,
            payload=record.payload,
        )


class EventPage(_Model):
    """Consecutive events from the log."""

    events: list[EventView]
    """Events in `event_id` order; empty when there is nothing newer."""


class DescriptorView(_Model):
    """An operation clients may submit, and the JSON Schema of its parameters."""

    operation_id: str
    """Names the operation."""
    operation_version: str
    """Version of the operation; a changed parameter contract gets a new version."""
    parameters_schema: dict[str, Any]
    """JSON Schema (draft 2020-12) that the `parameters` of a submission must match."""
    devices: dict[str, list[str]]
    """For each device parameter, the device ids it accepts."""
    descriptor_sha256: str
    """Identifies this descriptor; recorded with every operation submitted against it."""

    @classmethod
    def from_descriptor(cls, descriptor: OperationDescriptor) -> DescriptorView:
        return cls(
            operation_id=descriptor.operation_id,
            operation_version=descriptor.operation_version,
            parameters_schema=dict(descriptor.parameters_schema),
            devices={name: sorted(ids) for name, ids in descriptor.devices.items()},
            descriptor_sha256=descriptor.sha256,
        )


class CatalogView(_Model):
    """The operations clients may submit now."""

    catalog_sha256: str
    """Identifies the whole catalog; changes when any operation is added, removed or changed."""
    operations: list[DescriptorView]
    """Sorted by `operation_id`, then `operation_version`."""

    @classmethod
    def from_catalog(cls, catalog: Catalog) -> CatalogView:
        return cls(
            catalog_sha256=catalog.sha256,
            operations=[DescriptorView.from_descriptor(descriptor) for descriptor in catalog.descriptors],
        )


class HealthView(_Model):
    """The server is up."""

    status: Literal["ok"]


class ReadinessView(_Model):
    """Whether the controller can run work now, and if not, why."""

    ready: bool
    """True only when every other field is in its ready state."""
    worker: WorkerReachability
    """Whether RE Manager answers and refuses queue control from anyone but the controller; ready when
    `reachable`."""
    environment: EnvironmentPresence
    """Whether the worker environment is open; `unknown` while RE Manager is unreachable. Ready when
    `present`."""
    catalog: CatalogState
    """`incompatible` if a queued or live operation was validated against a descriptor the worker no longer
    offers; `unavailable` before the catalog is first loaded. Ready when `compatible`."""
    dispatch: DispatchState
    """`blocked` after an attempt's outcome could not be proven, until `POST /api/v2/recovery/acknowledge`.
    Ready when `open`."""

    @classmethod
    def from_readiness(cls, readiness: Readiness) -> ReadinessView:
        return cls(
            ready=readiness.ready,
            worker=readiness.worker,
            environment=readiness.environment,
            catalog=readiness.catalog,
            dispatch=readiness.dispatch,
        )


class ErrorBody(_Model):
    """Why a request failed."""

    code: str
    """Stable, machine-readable reason; branch on this."""
    message: str
    """Human-readable explanation."""
    details: dict[str, Any] | None
    """Code-specific data, if any."""
    request_id: str
    """Identifies the request; the same as the response's `X-Request-ID` header."""


class ErrorEnvelope(_Model):
    """Body of every error response."""

    error: ErrorBody
