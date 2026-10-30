"""Routes of the control lease."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..store import StoredResponse, Transaction
from .dependencies import AdminPrincipal, EditPrincipal, ReadPrincipal, ServicesDep
from .errors import error_responses
from .models import ControlLeaseView, LeaseOverrideRequest, LeaseRequest, LeaseView
from .preconditions import IDEMPOTENCY_ERRORS, mutation_headers, require_idempotency_key, run_idempotent

router = APIRouter(tags=["lease"])


@router.get("/control-lease", response_model=ControlLeaseView, responses=error_responses())
async def get_control_lease(services: ServicesDep, principal: ReadPrincipal) -> ControlLeaseView:
    """Who controls the instrument, and until when."""
    # No ETag: lease changes do not bump the queue revision.
    lease = await services.store.read(lambda tx: tx.lease())
    return ControlLeaseView(lease=None if lease is None else LeaseView.from_record(lease))


@router.post(
    "/control-lease",
    response_model=LeaseView,
    status_code=201,
    responses=error_responses("invalid_request", *IDEMPOTENCY_ERRORS, "lease_held"),
    openapi_extra=mutation_headers(if_match=False),
)
async def acquire_control_lease(
    request: Request, body: LeaseRequest, services: ServicesDep, principal: EditPrincipal
) -> JSONResponse:
    """Take control of the instrument. Needs the Edit role.

    Only the holder may edit the queue or start and stop queue executions. Fails with `lease_held` if anyone
    holds control, the caller included; the holder renews instead.
    """
    key = require_idempotency_key(request)

    async def action(tx: Transaction) -> StoredResponse:
        lease = await tx.acquire_lease(principal=principal.id, ttl_seconds=body.ttl_seconds)
        return StoredResponse(201, LeaseView.from_record(lease).model_dump(mode="json"), None)

    return await run_idempotent(request, services, principal, key=key, body=body, revision=None, action=action)


@router.post(
    "/control-lease/renew",
    response_model=LeaseView,
    responses=error_responses("invalid_request", *IDEMPOTENCY_ERRORS, "lease_required"),
    openapi_extra=mutation_headers(if_match=False),
)
async def renew_control_lease(
    request: Request, body: LeaseRequest, services: ServicesDep, principal: EditPrincipal
) -> JSONResponse:
    """Extend the caller's control to `ttl_seconds` from now. Needs the Edit role and the lease."""
    key = require_idempotency_key(request)

    async def action(tx: Transaction) -> StoredResponse:
        lease = await tx.renew_lease(principal=principal.id, ttl_seconds=body.ttl_seconds)
        return StoredResponse(200, LeaseView.from_record(lease).model_dump(mode="json"), None)

    return await run_idempotent(request, services, principal, key=key, body=body, revision=None, action=action)


@router.post(
    "/control-lease/release",
    response_model=ControlLeaseView,
    responses=error_responses(*IDEMPOTENCY_ERRORS, "lease_required"),
    openapi_extra=mutation_headers(if_match=False),
)
async def release_control_lease(request: Request, services: ServicesDep, principal: EditPrincipal) -> JSONResponse:
    """Give up the caller's control. Needs the Edit role and the lease.

    A running queue execution keeps running.
    """
    key = require_idempotency_key(request)

    async def action(tx: Transaction) -> StoredResponse:
        await tx.release_lease(principal=principal.id)
        return StoredResponse(200, ControlLeaseView(lease=None).model_dump(mode="json"), None)

    return await run_idempotent(request, services, principal, key=key, body=None, revision=None, action=action)


@router.post(
    "/control-lease/override",
    response_model=LeaseView,
    responses=error_responses("invalid_request", *IDEMPOTENCY_ERRORS),
    openapi_extra=mutation_headers(if_match=False),
)
async def override_control_lease(
    request: Request, body: LeaseOverrideRequest, services: ServicesDep, principal: AdminPrincipal
) -> JSONResponse:
    """Take control from whoever holds it. Needs the Admin role.

    The reason and the previous holder are recorded in a `lease.overridden` event.
    """
    key = require_idempotency_key(request)

    async def action(tx: Transaction) -> StoredResponse:
        lease = await tx.override_lease(principal=principal.id, reason=body.reason, ttl_seconds=body.ttl_seconds)
        return StoredResponse(200, LeaseView.from_record(lease).model_dump(mode="json"), None)

    return await run_idempotent(request, services, principal, key=key, body=body, revision=None, action=action)
