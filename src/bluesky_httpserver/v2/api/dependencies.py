"""FastAPI dependencies of the v2 HTTP API: shared services, authentication and role checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.openapi.models import HTTPBearer as HTTPBearerModel
from fastapi.security.base import SecurityBase

from ..gateway import ManagerGateway
from ..identity import Authenticator, Principal, Role, authorize, bearer_token
from ..store import Store


@dataclass(frozen=True)
class Services:
    """What the routes of one app use.

    Attributes
    ----------
    store : Store
        The controller queue.
    authenticator : Authenticator
        Verifies bearer tokens.
    manager : ManagerGateway
        The queueserver manager.
    clock : callable
        Unix seconds; ends event streams at token expiry.
    sse_heartbeat_seconds : float
        Longest silence on an event stream before a heartbeat comment is sent.
    """

    store: Store
    authenticator: Authenticator
    manager: ManagerGateway
    clock: Callable[[], float]
    sse_heartbeat_seconds: float


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


class _BearerAuthorization(SecurityBase):
    """Declares bearer authentication in the OpenAPI document and yields the raw ``Authorization`` header.

    FastAPI's ``HTTPBearer`` would judge the header itself, and could not tell a missing header from a malformed
    one; `bearer_token` does that instead.
    """

    def __init__(self) -> None:
        self.model = HTTPBearerModel(
            bearerFormat="JWT",
            description=(
                "Access token from the facility identity provider. Its roles claim must grant"
                " `QueueServer.Read`, `QueueServer.Edit`, `QueueServer.Run` or `QueueServer.Admin`."
            ),
        )
        self.scheme_name = "bearer"

    async def __call__(self, request: Request) -> str | None:
        return request.headers.get("Authorization")


_bearer_authorization = _BearerAuthorization()


async def authenticated_principal(
    services: ServicesDep, authorization: Annotated[str | None, Depends(_bearer_authorization)]
) -> Principal:
    """The caller identified by the request's bearer token; every route except ``/health`` depends on it."""
    return await services.authenticator.authenticate(bearer_token(authorization))


def require_role(role: Role) -> Callable[..., Awaitable[Principal]]:
    """Return a dependency yielding the authenticated caller if it holds ``role`` or a higher one."""

    async def dependency(principal: Annotated[Principal, Depends(authenticated_principal)]) -> Principal:
        authorize(principal, role)
        return principal

    return dependency


ReadPrincipal = Annotated[Principal, Depends(require_role(Role.READ))]
EditPrincipal = Annotated[Principal, Depends(require_role(Role.EDIT))]
RunPrincipal = Annotated[Principal, Depends(require_role(Role.RUN))]
AdminPrincipal = Annotated[Principal, Depends(require_role(Role.ADMIN))]
