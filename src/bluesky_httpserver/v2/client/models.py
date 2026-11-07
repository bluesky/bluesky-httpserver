"""What the v2 API returns, as the client parses it.

States and event types are plain strings, so a client keeps working when the server adds one.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from ..catalog import parameter_errors
from .errors import InvalidParameters, UnknownOperation


class _View(BaseModel):
    # Fields the server adds later are ignored, so an older client keeps parsing its responses.
    model_config = ConfigDict(frozen=True, extra="ignore")


class Descriptor(_View):
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


class OperationCatalog(_View):
    """The operations clients may submit."""

    catalog_sha256: str
    """Identifies the whole catalog; changes when any operation is added, removed or changed."""
    operations: list[Descriptor]
    """Sorted by `operation_id`, then `operation_version`."""

    def descriptor(self, operation_id: str, operation_version: str) -> Descriptor:
        """Return the descriptor of an operation.

        Raises
        ------
        UnknownOperation
            The operation is not in the catalog.
        """
        for descriptor in self.operations:
            if descriptor.operation_id == operation_id and descriptor.operation_version == operation_version:
                return descriptor
        raise UnknownOperation(
            422, "unknown_operation", f"operation {operation_id} version {operation_version} is not in the catalog"
        )

    def check_parameters(
        self, operation_id: str, operation_version: str, parameters: Mapping[str, Any]
    ) -> Descriptor:
        """Check, as the server does, that an operation is in the catalog and ``parameters`` match it.

        Parameters
        ----------
        operation_id, operation_version : str
            Operation to look up.
        parameters : mapping
            JSON parameters object.

        Returns
        -------
        Descriptor
            The descriptor the parameters were checked against.

        Raises
        ------
        UnknownOperation
            The operation is not in the catalog.
        InvalidParameters
            ``parameters`` contain NaN or infinite numbers, do not match the schema, or name a device the
            operation does not accept.
        """
        descriptor = self.descriptor(operation_id, operation_version)
        errors = parameter_errors(
            Draft202012Validator(descriptor.parameters_schema), descriptor.devices, parameters
        )
        if errors:
            raise InvalidParameters(
                422, "invalid_parameters", "parameters do not match the operation's schema", {"errors": errors}
            )
        return descriptor


class Operation(_View):
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
    submitted_via: str
    """Client application it was submitted through: the principal's own client, or an agent acting on its
    behalf."""
    submitted_at: datetime
    """When it was submitted."""
    state: str
    """Where it is in its lifecycle, e.g. `queued`, `running` or `succeeded`."""
    updated_at: datetime
    """When its state last changed."""
    replaced_by: str | None
    """The operation that took its place, if it was replaced."""


class Queue(_View):
    """The queued operations in the order they will run, and the revision they were read at."""

    revision: int
    """Queue revision."""
    operations: list[Operation]
    """Queued operations, first to run first."""


class Lease(_View):
    """Control of the instrument, held by one principal and shared by every client acting as it."""

    lease_uid: str
    """Identifies this grant of control."""
    holder: str
    """Principal in control."""
    acquired_at: datetime
    """When control was taken."""
    expires_at: datetime
    """When control lapses unless renewed."""


class QueueExecution(_View):
    """One run of the queue."""

    queue_execution_uid: str
    """Identifies the run."""
    state: str
    """Where it is in its lifecycle, e.g. `running`, `blocked` or `stopped`."""
    policy: str
    """Whether a failed operation stops it: `stop_on_failure` or `continue_on_failure`."""
    started_by: str
    """Principal that started it."""
    started_via: str
    """Client application it was started through."""
    started_at: datetime
    """When it started."""
    updated_at: datetime
    """When its state last changed."""


class Attempt(_View):
    """One authorized execution of an operation."""

    attempt_uid: str
    """Identifies the attempt."""
    operation_uid: str
    """Operation being executed."""
    queue_execution_uid: str
    """Queue execution that authorized it."""
    state: str
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
    """When a safe stop was requested; None if none was."""
    stop_now_requested_at: datetime | None
    """When a stop-now was requested; None if none was."""
    current_run_uids: list[str] | None
    """Bluesky runs a running attempt has opened so far; None for an attempt that is not running, or whose runs
    RE Manager has not reported yet."""


class ActiveExecution(_View):
    """What runs now: the queue execution and its live attempt, which the stop methods take."""

    queue_execution: QueueExecution | None
    """The running or stopping queue execution; None if the queue is not running."""
    live_attempt: Attempt | None
    """The claimed or running attempt; None if no operation is being executed."""


class Event(_View):
    """Something that happened to the queue."""

    event_id: int
    """Position in the event log; pass the last one received as ``after`` to resume."""
    timestamp: datetime
    """When it happened."""
    actor_kind: str
    """What kind of actor caused it."""
    actor_id: str
    """Who caused it: a principal, or the queue execution the scheduler acted for."""
    client_id: str | None
    """Client application a principal acted through; None for the scheduler and the system."""
    event_type: str
    """What happened, e.g. `operation.submitted`."""
    operation_uid: str | None
    """The operation it happened to; None if it concerns the whole queue."""
    queue_execution_uid: str | None
    """The queue execution it concerns, if any."""
    attempt_uid: str | None
    """The attempt it concerns, if any."""
    queue_revision: int
    """Queue revision after it happened."""
    payload: dict[str, Any]
    """Details, as recorded; timestamps in it are integer microseconds since the Unix epoch."""


class Readiness(_View):
    """Whether the controller can run work now, and if not, why."""

    ready: bool
    """True only when every other field is in its ready state."""
    worker: str
    """Whether RE Manager answers and is fronted by the controller; ready when `reachable`."""
    environment: str
    """Whether the worker environment is open; ready when `present`."""
    catalog: str
    """Whether queued operations match the worker's catalog; ready when `compatible`."""
    dispatch: str
    """`blocked` after an attempt's outcome could not be proven, until recovery is acknowledged; ready when
    `open`."""
