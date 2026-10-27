from __future__ import annotations

from pathlib import Path

from .records import AttemptState, OperationState, QueueExecutionState


class StoreError(Exception):
    """Base class for every error raised by the v2 durable store."""


class StoreLocked(StoreError):
    def __init__(self, path: Path, lock_path: Path):
        self.path = path
        self.lock_path = lock_path
        super().__init__(f"store {path} is already open (lock {lock_path} is held)")


class NotAStore(StoreError):
    def __init__(self, path: Path, application_id: int):
        self.path = path
        self.application_id = application_id
        super().__init__(f"{path} is not a bluesky-httpserver v2 store (application_id {application_id:#x})")


class SchemaMismatch(StoreError):
    pass


class MigrationRequired(StoreError):
    def __init__(self, current: int, target: int):
        self.current = current
        self.target = target
        super().__init__(
            f"store schema is at version {current}, version {target} is required; open with allow_migrate=True"
        )


class StaleRevision(StoreError):
    def __init__(self, expected: int, current: int):
        self.expected = expected
        self.current = current
        super().__init__(f"queue revision {expected} is stale; current revision is {current}")


class OperationNotFound(StoreError):
    def __init__(self, operation_uid: str):
        self.operation_uid = operation_uid
        super().__init__(f"operation {operation_uid} does not exist")


class OperationNotPending(StoreError):
    def __init__(self, operation_uid: str, state: OperationState):
        self.operation_uid = operation_uid
        self.state = state
        super().__init__(f"operation {operation_uid} is {state.value}, not queued")


class InvalidReorder(StoreError):
    def __init__(self) -> None:
        super().__init__("reorder must name every queued operation exactly once")


class IdempotencyConflict(StoreError):
    def __init__(self) -> None:
        super().__init__("idempotency key was already used for a different request")


class LeaseHeld(StoreError):
    def __init__(self, holder: str, expires_at: int):
        self.holder = holder
        self.expires_at = expires_at
        super().__init__(f"control lease is held by {holder}")


class LeaseRequired(StoreError):
    def __init__(self, principal: str):
        self.principal = principal
        super().__init__(f"{principal} does not hold the control lease")


class QueueEmpty(StoreError):
    def __init__(self) -> None:
        super().__init__("cannot start a queue execution on an empty queue")


class ExecutionActive(StoreError):
    def __init__(self, queue_execution_uid: str):
        self.queue_execution_uid = queue_execution_uid
        super().__init__(f"queue execution {queue_execution_uid} is already active")


class ExecutionNotFound(StoreError):
    def __init__(self, queue_execution_uid: str):
        self.queue_execution_uid = queue_execution_uid
        super().__init__(f"queue execution {queue_execution_uid} does not exist")


class ExecutionNotRunning(StoreError):
    def __init__(self, queue_execution_uid: str, state: QueueExecutionState):
        self.queue_execution_uid = queue_execution_uid
        self.state = state
        super().__init__(f"queue execution {queue_execution_uid} is {state.value}, not running")


class AttemptNotFound(StoreError):
    def __init__(self, attempt_uid: str):
        self.attempt_uid = attempt_uid
        super().__init__(f"attempt {attempt_uid} does not exist")


class InvalidAttemptTransition(StoreError):
    def __init__(self, attempt_uid: str, current: AttemptState, target: AttemptState):
        self.attempt_uid = attempt_uid
        self.current = current
        self.target = target
        super().__init__(f"attempt {attempt_uid} cannot go from {current.value} to {target.value}")
