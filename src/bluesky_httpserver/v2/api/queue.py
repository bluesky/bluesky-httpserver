"""Routes of the catalog, the queue, its operations, queue executions, attempts and recovery.

Every queue mutation takes ``Idempotency-Key`` and ``If-Match``, except the stops of an attempt (safe stop and
stop now), which take only ``Idempotency-Key``; its response carries the new revision as ``ETag``. Stops need the
Run role but not the lease. Parameters are validated against the catalog inside the mutation's transaction, after
any replay, so a retried request gets its original response even if the catalog has changed since. Likewise,
recovery's fence check runs after any replay.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..store import AttemptRecord, QueueExecutionRecord, ReadTransaction, Store, StoredResponse, Transaction
from .dependencies import EditPrincipal, ReadPrincipal, RunPrincipal, ServicesDep
from .errors import error_responses, queue_etag
from .models import (
    ActiveExecutionView,
    AttemptView,
    CatalogView,
    OperationSubmission,
    OperationView,
    QueueExecutionStart,
    QueueExecutionView,
    QueueView,
    RecoveryAcknowledgement,
    ReorderRequest,
)
from .preconditions import (
    IDEMPOTENCY_ERRORS,
    QUEUE_EDIT_ERRORS,
    REVISION_ERRORS,
    expected_revision,
    mutation_headers,
    require_idempotency_key,
    run_idempotent,
)

T = TypeVar("T")

router = APIRouter()

_ETAG_HEADER = {
    "description": 'Queue revision the response reflects, e.g. `"qrev-7"`; send it as `If-Match` on the next'
    " edit.",
    "schema": {"type": "string"},
}


def _tagged_responses(status_code: int, *codes: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` of a route whose success carries the queue revision as ``ETag``."""
    return {status_code: {"headers": {"ETag": _ETAG_HEADER}}, **error_responses(*codes)}


def _edit_responses(status_code: int, *codes: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` of a queue mutation that may also fail with ``codes``."""
    return _tagged_responses(status_code, *IDEMPOTENCY_ERRORS, *QUEUE_EDIT_ERRORS, *codes)


async def _read_with_revision(store: Store, read: Callable[[ReadTransaction], Awaitable[T]]) -> tuple[T, int]:
    """Run ``read`` and read the queue revision in the same transaction."""

    async def fn(tx: ReadTransaction) -> tuple[T, int]:
        return await read(tx), await tx.revision()

    return await store.read(fn)


def _tagged(view: BaseModel, revision: int) -> JSONResponse:
    return JSONResponse(view.model_dump(mode="json"), headers={"ETag": queue_etag(revision)})


def _stored(status_code: int, view: BaseModel, revision: int) -> StoredResponse:
    return StoredResponse(status_code, view.model_dump(mode="json"), queue_etag(revision))


@router.get(
    "/catalog", tags=["catalog"], response_model=CatalogView, responses=error_responses("catalog_unavailable")
)
async def get_catalog(services: ServicesDep, principal: ReadPrincipal) -> CatalogView:
    """The operations that may be submitted now, each with the JSON Schema of its parameters.

    The catalog can change while the server runs; `catalog_sha256` identifies the current one.
    """
    return CatalogView.from_catalog(services.worker.catalog())


@router.get("/queue", tags=["queue"], response_model=QueueView, responses=_tagged_responses(200))
async def get_queue(services: ServicesDep, principal: ReadPrincipal) -> JSONResponse:
    """The queued operations in the order they will run.

    `ETag` is the revision to send as `If-Match` on the next edit.
    """
    snapshot = await services.store.read(lambda tx: tx.queue())
    return _tagged(QueueView.from_snapshot(snapshot), snapshot.revision)


@router.post(
    "/queue/reorder",
    tags=["queue"],
    response_model=QueueView,
    responses=_edit_responses(200, "invalid_request", "invalid_reorder"),
    openapi_extra=mutation_headers(if_match=True),
)
async def reorder_queue(
    request: Request, body: ReorderRequest, services: ServicesDep, principal: EditPrincipal
) -> JSONResponse:
    """Put the queued operations in a new order. Needs the Edit role and the lease."""
    key = require_idempotency_key(request)
    revision = expected_revision(request)

    async def action(tx: Transaction) -> StoredResponse:
        snapshot = await tx.reorder(
            principal=principal.id,
            client_id=principal.client_id,
            expected_revision=revision,
            operation_uids=body.operation_uids,
        )
        return _stored(200, QueueView.from_snapshot(snapshot), snapshot.revision)

    return await run_idempotent(request, services, principal, key=key, body=body, revision=revision, action=action)


@router.get(
    "/operations/{operation_uid}",
    tags=["queue"],
    response_model=OperationView,
    responses=_tagged_responses(200, "invalid_request", "not_found"),
)
async def get_operation(operation_uid: str, services: ServicesDep, principal: ReadPrincipal) -> JSONResponse:
    """One operation in any state, including cancelled, replaced and finished ones."""
    record, revision = await _read_with_revision(services.store, lambda tx: tx.operation(operation_uid))
    return _tagged(OperationView.from_record(record), revision)


@router.post(
    "/operations",
    tags=["queue"],
    response_model=OperationView,
    status_code=201,
    responses=_edit_responses(
        201, "invalid_request", "unknown_operation", "invalid_parameters", "catalog_unavailable"
    ),
    openapi_extra=mutation_headers(if_match=True),
)
async def submit_operation(
    request: Request, body: OperationSubmission, services: ServicesDep, principal: EditPrincipal
) -> JSONResponse:
    """Append an operation to the end of the queue. Needs the Edit role and the lease.

    `parameters` are validated against the operation's `parameters_schema` from `GET /api/v2/catalog`; if they
    do not match, nothing is written. An operation submitted while a queue execution is running runs in it.
    """
    key = require_idempotency_key(request)
    revision = expected_revision(request)

    async def action(tx: Transaction) -> StoredResponse:
        descriptor = services.worker.catalog().validate(body.operation_id, body.operation_version, body.parameters)
        record, new_revision = await tx.submit(
            principal=principal.id,
            client_id=principal.client_id,
            expected_revision=revision,
            operation_id=body.operation_id,
            operation_version=body.operation_version,
            parameters=body.parameters,
            descriptor_sha256=descriptor.sha256,
        )
        return _stored(201, OperationView.from_record(record), new_revision)

    return await run_idempotent(request, services, principal, key=key, body=body, revision=revision, action=action)


@router.put(
    "/operations/{operation_uid}",
    tags=["queue"],
    response_model=OperationView,
    responses=_edit_responses(
        200,
        "invalid_request",
        "unknown_operation",
        "invalid_parameters",
        "not_found",
        "operation_not_queued",
        "catalog_unavailable",
    ),
    openapi_extra=mutation_headers(if_match=True),
)
async def replace_operation(
    operation_uid: str,
    request: Request,
    body: OperationSubmission,
    services: ServicesDep,
    principal: EditPrincipal,
) -> JSONResponse:
    """Replace a queued operation by a new one at the same position. Needs the Edit role and the lease.

    Returns the new operation. The old one becomes `replaced`, and its `replaced_by` names the new one.
    """
    key = require_idempotency_key(request)
    revision = expected_revision(request)

    async def action(tx: Transaction) -> StoredResponse:
        descriptor = services.worker.catalog().validate(body.operation_id, body.operation_version, body.parameters)
        record, new_revision = await tx.replace(
            principal=principal.id,
            client_id=principal.client_id,
            expected_revision=revision,
            operation_uid=operation_uid,
            operation_id=body.operation_id,
            operation_version=body.operation_version,
            parameters=body.parameters,
            descriptor_sha256=descriptor.sha256,
        )
        return _stored(200, OperationView.from_record(record), new_revision)

    return await run_idempotent(request, services, principal, key=key, body=body, revision=revision, action=action)


@router.delete(
    "/operations/{operation_uid}",
    tags=["queue"],
    response_model=OperationView,
    responses=_edit_responses(200, "invalid_request", "not_found", "operation_not_queued"),
    openapi_extra=mutation_headers(if_match=True),
)
async def cancel_operation(
    operation_uid: str, request: Request, services: ServicesDep, principal: EditPrincipal
) -> JSONResponse:
    """Remove a queued operation from the queue; it becomes `cancelled`. Needs the Edit role and the lease.

    Only queued operations can be cancelled: once taken to run, an operation can no longer be edited.
    """
    key = require_idempotency_key(request)
    revision = expected_revision(request)

    async def action(tx: Transaction) -> StoredResponse:
        record, new_revision = await tx.cancel(
            principal=principal.id,
            client_id=principal.client_id,
            expected_revision=revision,
            operation_uid=operation_uid,
        )
        return _stored(200, OperationView.from_record(record), new_revision)

    return await run_idempotent(request, services, principal, key=key, body=None, revision=revision, action=action)


@router.get(
    "/active-execution", tags=["executions"], response_model=ActiveExecutionView, responses=_tagged_responses(200)
)
async def get_active_execution(services: ServicesDep, principal: ReadPrincipal) -> JSONResponse:
    """What runs now: the running or stopping queue execution and its claimed or running attempt.

    Names what the stop routes take, without replaying events. Either is null when there is none.
    """

    async def read(tx: ReadTransaction) -> tuple[QueueExecutionRecord | None, AttemptRecord | None]:
        return await tx.active_execution(), await tx.live_attempt()

    (execution, attempt), revision = await _read_with_revision(services.store, read)
    view = ActiveExecutionView(
        queue_execution=None if execution is None else QueueExecutionView.from_record(execution),
        live_attempt=(
            None
            if attempt is None
            else AttemptView.from_record(attempt, services.worker.runs(attempt.attempt_uid))
        ),
    )
    return _tagged(view, revision)


@router.get(
    "/attempts/{attempt_uid}",
    tags=["executions"],
    response_model=AttemptView,
    responses=_tagged_responses(200, "invalid_request", "not_found"),
)
async def get_attempt(attempt_uid: str, services: ServicesDep, principal: ReadPrincipal) -> JSONResponse:
    """One attempt to execute an operation; event `attempt_uid` fields name attempts."""
    record, revision = await _read_with_revision(services.store, lambda tx: tx.attempt(attempt_uid))
    return _tagged(AttemptView.from_record(record, services.worker.runs(attempt_uid)), revision)


@router.post(
    "/attempts/{attempt_uid}/safe-stop",
    tags=["executions"],
    response_model=AttemptView,
    status_code=202,
    responses=_tagged_responses(202, *IDEMPOTENCY_ERRORS, "invalid_request", "not_found", "attempt_not_live"),
    openapi_extra=mutation_headers(if_match=False),
)
async def safe_stop_attempt(
    attempt_uid: str, request: Request, services: ServicesDep, principal: RunPrincipal
) -> JSONResponse:
    """Stop a live attempt safely. Needs the Run role; the lease is not needed.

    The request is recorded at once; the controller then pauses the plan at its next checkpoint and aborts it.
    A plan in the middle of a move or a wait continues until its next checkpoint; `stop-now` interrupts it. If
    the operation finishes first, its own outcome stands. Either way, the queue execution stops once the attempt
    ends. Takes no `If-Match`, so a stop never fails because the queue changed.
    """
    key = require_idempotency_key(request)

    async def action(tx: Transaction) -> StoredResponse:
        record, revision = await tx.request_safe_stop(
            principal=principal.id, client_id=principal.client_id, attempt_uid=attempt_uid
        )
        return _stored(202, AttemptView.from_record(record), revision)

    return await run_idempotent(request, services, principal, key=key, body=None, revision=None, action=action)


@router.post(
    "/attempts/{attempt_uid}/stop-now",
    tags=["executions"],
    response_model=AttemptView,
    status_code=202,
    responses=_tagged_responses(202, *IDEMPOTENCY_ERRORS, "invalid_request", "not_found", "attempt_not_live"),
    openapi_extra=mutation_headers(if_match=False),
)
async def stop_attempt_now(
    attempt_uid: str, request: Request, services: ServicesDep, principal: RunPrincipal
) -> JSONResponse:
    """Stop a live attempt at once. Needs the Run role; the lease is not needed.

    The request is recorded at once; the controller then pauses the plan immediately, interrupting a move or a
    wait in progress, and aborts it. It also escalates a safe stop that has not taken effect yet. If the
    operation finishes first, or fails while being stopped, its own outcome stands. Either way, the queue
    execution stops once the attempt ends. Takes no `If-Match`, so a stop never fails because the queue changed.
    """
    key = require_idempotency_key(request)

    async def action(tx: Transaction) -> StoredResponse:
        record, revision = await tx.request_stop_now(
            principal=principal.id, client_id=principal.client_id, attempt_uid=attempt_uid
        )
        return _stored(202, AttemptView.from_record(record), revision)

    return await run_idempotent(request, services, principal, key=key, body=None, revision=None, action=action)


@router.get(
    "/queue-executions/{queue_execution_uid}",
    tags=["executions"],
    response_model=QueueExecutionView,
    responses=_tagged_responses(200, "invalid_request", "not_found"),
)
async def get_queue_execution(
    queue_execution_uid: str, services: ServicesDep, principal: ReadPrincipal
) -> JSONResponse:
    """One run of the queue."""
    record, revision = await _read_with_revision(
        services.store, lambda tx: tx.queue_execution(queue_execution_uid)
    )
    return _tagged(QueueExecutionView.from_record(record), revision)


@router.post(
    "/queue-executions",
    tags=["executions"],
    response_model=QueueExecutionView,
    status_code=201,
    responses=_edit_responses(201, "invalid_request", "execution_active", "queue_empty", "dispatch_blocked"),
    openapi_extra=mutation_headers(if_match=True),
)
async def start_queue_execution(
    request: Request, body: QueueExecutionStart, services: ServicesDep, principal: RunPrincipal
) -> JSONResponse:
    """Start running the queue, one operation at a time in queue order. Needs the Run role and the lease.

    The execution also runs operations submitted after it started, and does not need the lease once started.
    It ends when no queued operation is left, when it is stopped, or when an operation fails under
    `stop_on_failure`. Send `{}` for the default policy.
    """
    key = require_idempotency_key(request)
    revision = expected_revision(request)

    async def action(tx: Transaction) -> StoredResponse:
        record, new_revision = await tx.start_queue_execution(
            principal=principal.id, client_id=principal.client_id, expected_revision=revision, policy=body.policy
        )
        return _stored(201, QueueExecutionView.from_record(record), new_revision)

    return await run_idempotent(request, services, principal, key=key, body=body, revision=revision, action=action)


@router.post(
    "/queue-executions/{queue_execution_uid}/stop",
    tags=["executions"],
    response_model=QueueExecutionView,
    responses=_tagged_responses(
        200, *IDEMPOTENCY_ERRORS, *REVISION_ERRORS, "invalid_request", "not_found", "execution_not_running"
    ),
    openapi_extra=mutation_headers(if_match=True),
)
async def stop_queue_execution(
    queue_execution_uid: str, request: Request, services: ServicesDep, principal: RunPrincipal
) -> JSONResponse:
    """Stop a running queue execution once its current operation finishes. Needs the Run role; the lease is
    not needed.

    The execution becomes `stopping`, or `stopped` at once if no operation is claimed or running. Operations
    not yet taken to run stay queued.
    """
    key = require_idempotency_key(request)
    revision = expected_revision(request)

    async def action(tx: Transaction) -> StoredResponse:
        record, new_revision = await tx.stop_queue_execution(
            principal=principal.id,
            client_id=principal.client_id,
            expected_revision=revision,
            queue_execution_uid=queue_execution_uid,
        )
        return _stored(200, QueueExecutionView.from_record(record), new_revision)

    return await run_idempotent(request, services, principal, key=key, body=None, revision=revision, action=action)


@router.post(
    "/recovery/acknowledge",
    tags=["executions"],
    response_model=QueueExecutionView,
    responses=_edit_responses(200, "invalid_request", "not_blocked", "no_fence_evidence"),
    openapi_extra=mutation_headers(if_match=True),
)
async def acknowledge_recovery(
    request: Request, body: RecoveryAcknowledgement, services: ServicesDep, principal: RunPrincipal
) -> JSONResponse:
    """Clear the dispatch block left by an attempt whose outcome could not be proven. Needs the Run role and
    the lease.

    Succeeds only while RE Manager reports nothing running. The blocked execution becomes `stopped`, queued
    operations stay queued, and the controller reopens the worker environment if it is gone. Start a new queue
    execution to continue.
    """
    key = require_idempotency_key(request)
    revision = expected_revision(request)

    async def action(tx: Transaction) -> StoredResponse:
        record, new_revision = await tx.acknowledge_recovery(
            principal=principal.id,
            client_id=principal.client_id,
            expected_revision=revision,
            note=body.note,
            fence=services.worker.fence_evidence,
        )
        return _stored(200, QueueExecutionView.from_record(record), new_revision)

    return await run_idempotent(request, services, principal, key=key, body=body, revision=revision, action=action)
