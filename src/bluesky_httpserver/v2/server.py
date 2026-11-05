"""The v2 controller assembled from its settings."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable

import httpx
from fastapi import FastAPI

from .api import Services, create_app
from .gateway import WorkerGateway
from .identity import OidcAuthenticator
from .relay import ManagerRelay
from .settings import Settings
from .store import Store

logger = logging.getLogger(__name__)


def create_server_app(
    settings: Settings, relay: ManagerRelay, *, on_gateway_failure: Callable[[BaseException], None]
) -> FastAPI:
    """Build the v2 controller's HTTP API, which runs the controller for as long as it serves.

    The app's lifespan opens the store, owns the HTTP client that fetches the identity provider's signing
    keys, and runs the worker gateway; on shutdown it stops the gateway, closes the client, then closes the
    store. Startup raises whatever `Store.open` raises: `StoreLocked`, `MigrationRequired`, `NotAStore` or
    `SchemaMismatch`.

    Parameters
    ----------
    settings : Settings
        The controller's settings.
    relay : ManagerRelay
        RE Manager.
    on_gateway_failure : callable
        Called once, on the event loop, with the error if the gateway stops with one while the app serves;
        nothing more can run, so the caller should stop the server. Not called when shutdown cancels the
        gateway.

    Returns
    -------
    FastAPI
    """

    def report(task: asyncio.Task[None]) -> None:
        if task.cancelled() or (error := task.exception()) is None:
            return
        # `WorkerGateway.run` uses a TaskGroup, so ``error`` is an exception group.
        logger.critical("the worker gateway stopped; the controller cannot run work", exc_info=error)
        on_gateway_failure(error)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        store = await Store.open(settings.store_path)
        try:
            async with httpx.AsyncClient() as http:
                gateway = WorkerGateway(
                    store=store,
                    relay=relay,
                    user_group=settings.user_group,
                    grace_seconds=settings.gateway.grace_seconds,
                    poll_seconds=settings.gateway.poll_seconds,
                    environment_retry_seconds=settings.gateway.environment_retry_seconds,
                )
                authenticator = OidcAuthenticator(settings.identity.oidc_settings(), http)
                app.state.services = Services(
                    store, authenticator, gateway, sse_heartbeat_seconds=settings.sse_heartbeat_seconds
                )
                # Not a TaskGroup around the yield: a gateway failure would cancel the server's lifespan task.
                task = asyncio.create_task(gateway.run(), name="worker-gateway")
                task.add_done_callback(report)
                try:
                    yield
                finally:
                    task.cancel()
                    # Not ``await task``: a gateway that already failed would raise again during shutdown.
                    await asyncio.wait([task])
        finally:
            await store.close()

    return create_app(lifespan)
