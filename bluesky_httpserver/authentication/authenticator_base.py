from abc import ABC
from dataclasses import dataclass
from typing import Optional

from fastapi import Request


@dataclass
class UserSessionState:
    """Data transfer class to communicate custom session state information."""

    user_name: str
    state: dict = None


class InternalAuthenticator(ABC):
    """
    Base class for authenticators that use username/password credentials.

    Subclasses must implement the authenticate method which takes a username
    and password and returns a UserSessionState on success or None on failure.
    """

    async def authenticate(
        self, username: str, password: str
    ) -> Optional[UserSessionState]:
        raise NotImplementedError


class ExternalAuthenticator(ABC):
    """
    Base class for authenticators that use external identity providers.

    Subclasses must implement the authenticate method which takes a FastAPI
    Request object and returns a UserSessionState on success or None on failure.
    """

    async def authenticate(self, request: Request) -> Optional[UserSessionState]:
        raise NotImplementedError
