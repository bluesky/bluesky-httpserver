"""Authenticated callers and the roles that bound what they may do."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol


class Role(IntEnum):
    """Right to use the controller; each role includes every role before it.

    Attributes
    ----------
    READ
        Read the queue, operations, executions, attempts and events.
    EDIT
        Hold the control lease and edit the queue.
    RUN
        Start and stop queue executions.
    ADMIN
        Take the control lease from its holder.
    """

    READ = 1
    EDIT = 2
    RUN = 3
    ADMIN = 4

    @property
    def claim_value(self) -> str:
        """Value that grants this role in the access token's rights claim."""
        return f"QueueServer.{self.name.capitalize()}"


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    Attributes
    ----------
    id : str
        Stable identifier of the caller; recorded as lease holder, submitter and event actor.
    role : Role or None
        Highest role held; None if the caller holds no controller role.
    expires_at : int
        When the credential stops being valid, in Unix seconds; long-lived responses such as the event
        stream end then.
    """

    id: str
    role: Role | None
    expires_at: int


class AuthenticationFailed(Exception):
    """The request carries no valid credential. ``reason`` names the failed check and never contains token
    content."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class IdentityProviderUnavailable(Exception):
    """The token could not be checked because the identity provider's signing keys could not be fetched."""

    def __init__(self) -> None:
        super().__init__("identity provider signing keys are unavailable")


class InsufficientRole(Exception):
    """The caller is authenticated but does not hold the role an action requires."""

    def __init__(self, required: Role, held: Role | None):
        self.required = required
        self.held = held
        super().__init__(f"requires the {required.claim_value} role")


class Authenticator(Protocol):
    async def authenticate(self, token: str) -> Principal:
        """Verify a bearer token and return the caller it identifies.

        Raises
        ------
        AuthenticationFailed
            The token is not a valid credential.
        IdentityProviderUnavailable
            The token could not be checked because signing keys are unavailable.
        """
        ...


def bearer_token(authorization: str | None) -> str:
    """Return the token of an ``Authorization: Bearer`` header value.

    Raises
    ------
    AuthenticationFailed
        The header is absent or not a bearer credential.
    """
    if not authorization:
        raise AuthenticationFailed("missing bearer token")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthenticationFailed("malformed authorization header")
    return parts[1]


def authorize(principal: Principal, required: Role) -> None:
    """Allow the action only if ``principal`` holds ``required`` or a higher role.

    Raises
    ------
    InsufficientRole
        The principal holds a lower role or none.
    """
    if principal.role is None or principal.role < required:
        raise InsufficientRole(required, principal.role)
