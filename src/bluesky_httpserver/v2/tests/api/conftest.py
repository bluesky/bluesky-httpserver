from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from bluesky_httpserver.v2.api import Services, create_app
from bluesky_httpserver.v2.gateway import WorkerGateway
from bluesky_httpserver.v2.identity import AuthenticationFailed, IdentityProviderUnavailable, Principal, Role
from bluesky_httpserver.v2.server import create_server_app
from bluesky_httpserver.v2.store import Store

from ..conftest import CLIENT, server_settings
from ..fake_relay import FakeRelay


class FakeAuthenticator:
    """Accepts each name in `ROLES` as a token for that principal, acting through `CLIENT`, and each name in
    `AGENTS` as a token of an agent acting on a principal's behalf; ``idp-down`` fails to verify.

    The store fixture gives alice the lease; carol has alice's role but not the lease.
    """

    ROLES: dict[str, Role | None] = {
        "reader": Role.READ,
        "alice": Role.RUN,
        "bob": Role.EDIT,
        "carol": Role.RUN,
        "admin": Role.ADMIN,
        "nobody": None,
    }
    AGENTS: dict[str, str] = {"agent-for-alice": "alice"}
    AGENT_CLIENT = "agent"

    def __init__(self) -> None:
        self.expires_at = int(time.time()) + 3600

    async def authenticate(self, token: str) -> Principal:
        if token == "idp-down":
            raise IdentityProviderUnavailable()
        if token in self.AGENTS:
            principal = self.AGENTS[token]
            return Principal(
                id=principal, client_id=self.AGENT_CLIENT, role=self.ROLES[principal], expires_at=self.expires_at
            )
        if token in self.ROLES:
            return Principal(id=token, client_id=CLIENT, role=self.ROLES[token], expires_at=self.expires_at)
        raise AuthenticationFailed("invalid token signature")


@pytest_asyncio.fixture
async def worker(gateway: WorkerGateway) -> WorkerGateway:
    # One step loads the catalog; in the fixture state it writes nothing.
    await gateway.step()
    return gateway


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()


def serving(services: Services) -> FastAPI:
    """App whose lifespan serves ``services``; the test opens, runs and closes them itself."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.services = services
        yield

    return create_app(lifespan)


@contextlib.asynccontextmanager
async def started(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    """A client of ``app`` inside its lifespan, which `httpx.ASGITransport` does not run."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.fixture
def app(store: Store, authenticator: FakeAuthenticator, worker: WorkerGateway) -> FastAPI:
    return serving(Services(store, authenticator, worker, sse_heartbeat_seconds=0.05))


@pytest.fixture
def server_app(db_path: Path, relay: FakeRelay) -> FastAPI:
    """The served app; its lifespan never runs, so only its routes may be inspected."""
    return create_server_app(server_settings(db_path), relay, on_gateway_failure=lambda error: None)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with started(app) as c:
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
