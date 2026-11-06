from __future__ import annotations

import asyncio

import httpx
import pytest

from bluesky_httpserver.v2.catalog import Catalog
from bluesky_httpserver.v2.cli import main
from bluesky_httpserver.v2.relay import ManagerStatus
from bluesky_httpserver.v2.server import create_server_app, serve
from bluesky_httpserver.v2.store import Store, StoreLocked

from .conftest import server_settings
from .fake_relay import DEMO_COUNT, FakeRelay


@pytest.mark.asyncio
async def test_lifespan_runs_the_controller(db_path, relay):
    failures: list[BaseException] = []
    app = create_server_app(server_settings(db_path, poll_seconds=0.01), relay, on_gateway_failure=failures.append)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            async with asyncio.timeout(5):
                while (await client.get("/ready")).status_code != 200:
                    await asyncio.sleep(0.01)
            # The real verifier rejects a malformed token without fetching keys.
            response = await client.get("/api/v2/catalog", headers={"Authorization": "Bearer not-a-jwt"})
            assert response.status_code == 401
            assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
            with pytest.raises(StoreLocked):
                await Store.open(db_path)

    await (await Store.open(db_path)).close()
    assert failures == []


class BrokenRelay(FakeRelay):
    async def status(self) -> ManagerStatus:
        raise RuntimeError("relay bug")


@pytest.mark.asyncio
async def test_gateway_failure_is_reported(db_path):
    failures: list[BaseException] = []
    reported = asyncio.Event()

    def record(error: BaseException) -> None:
        failures.append(error)
        reported.set()

    app = create_server_app(
        server_settings(db_path), BrokenRelay(Catalog([DEMO_COUNT])), on_gateway_failure=record
    )

    async with app.router.lifespan_context(app):
        async with asyncio.timeout(5):
            await reported.wait()

    (error,) = failures
    assert isinstance(error, BaseExceptionGroup)
    assert error.subgroup(RuntimeError) is not None
    await (await Store.open(db_path)).close()


@pytest.mark.asyncio
async def test_serve_stops_when_the_gateway_fails(db_path):
    async with asyncio.timeout(10):
        assert await serve(server_settings(db_path), BrokenRelay(Catalog([DEMO_COUNT]))) is False
    await (await Store.open(db_path)).close()


@pytest.mark.asyncio
async def test_serve_fails_to_start_on_a_store_open_elsewhere(db_path, relay):
    store = await Store.open(db_path)
    try:
        async with asyncio.timeout(10):
            with pytest.raises(SystemExit) as startup_failure:
                await serve(server_settings(db_path), relay)
        assert startup_failure.value.code == 3
    finally:
        await store.close()


def test_serve_command_refuses_invalid_settings(tmp_path, capsys):
    config = tmp_path / "controller.toml"
    config.write_text('store_path = "relative.sqlite"\n')
    assert main(["serve", "--config", str(config)]) == 1
    assert capsys.readouterr().err.startswith(f"error: {config}: ")
