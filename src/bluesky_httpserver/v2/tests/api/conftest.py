from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from bluesky_httpserver.v2.api import create_app
from bluesky_httpserver.v2.catalog import Catalog
from bluesky_httpserver.v2.identity import AuthenticationFailed, IdentityProviderUnavailable, Principal, Role
from bluesky_httpserver.v2.store import Store

from .fake_manager import DEMO_COUNT, FakeManager


class FakeAuthenticator:
    """Accepts each name in `ROLES` as a token for that principal; ``idp-down`` fails to verify."""

    ROLES: dict[str, Role | None] = {
        "reader": Role.READ,
        "alice": Role.RUN,
        "bob": Role.EDIT,
        "admin": Role.ADMIN,
        "nobody": None,
    }

    def __init__(self) -> None:
        self.expires_at = int(time.time()) + 3600

    async def authenticate(self, token: str) -> Principal:
        if token == "idp-down":
            raise IdentityProviderUnavailable()
        if token in self.ROLES:
            return Principal(id=token, role=self.ROLES[token], expires_at=self.expires_at)
        raise AuthenticationFailed("invalid token signature")


@pytest.fixture
def manager() -> FakeManager:
    return FakeManager(Catalog([DEMO_COUNT]))


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()


@pytest.fixture
def app(store: Store, authenticator: FakeAuthenticator, manager: FakeManager) -> FastAPI:
    return create_app(store=store, authenticator=authenticator, manager=manager, sse_heartbeat_seconds=0.05)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def headers(token: str | None, *, if_match: int | None = None, key: str | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    if token is not None:
        result["Authorization"] = f"Bearer {token}"
    if if_match is not None:
        result["If-Match"] = f'"qrev-{if_match}"'
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def new_key() -> str:
    return str(uuid.uuid4())


async def submit(
    client: httpx.AsyncClient, revision: int, *, num: int = 1, token: str = "alice", key: str | None = None
) -> httpx.Response:
    """Submit ``demo.count`` through the API against queue revision ``revision``."""
    return await client.post(
        "/api/v2/operations",
        json={"operation_id": "demo.count", "operation_version": "1", "parameters": {"num": num}},
        headers=headers(token, if_match=revision, key=key or new_key()),
    )


async def queue(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/api/v2/queue", headers=headers("reader"))


def operation_uids(response: httpx.Response) -> list[str]:
    return [operation["operation_uid"] for operation in response.json()["operations"]]
