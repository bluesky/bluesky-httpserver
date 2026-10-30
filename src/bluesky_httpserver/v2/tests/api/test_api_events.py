from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from bluesky_httpserver.v2.api.models import EventView

from .conftest import headers, submit

STREAM = "/api/v2/events/stream"


async def stream(client: httpx.AsyncClient, *, last_event_id: str | None = None, after: int = 0) -> httpx.Response:
    """Read the whole stream; it ends when the reader's token expires."""
    request_headers = headers("reader")
    if last_event_id is not None:
        request_headers["Last-Event-ID"] = last_event_id
    async with asyncio.timeout(10):  # guards against a stream that never ends
        return await client.get(STREAM, params={"after": after}, headers=request_headers)


def blocks(response: httpx.Response) -> list[list[str]]:
    *complete, rest = response.text.split("\n\n")
    assert rest == ""
    return [block.split("\n") for block in complete]


def assert_event_block(block: list[str], event_id: int, event_type: str) -> None:
    id_line, event_line, data_line = block
    assert (id_line, event_line) == (f"id: {event_id}", f"event: {event_type}")
    assert EventView.model_validate_json(data_line.removeprefix("data: ")).event_id == event_id


@pytest.mark.asyncio
async def test_backlog_then_close_at_expiry(client, authenticator):
    await submit(client, 0)
    await submit(client, 1)
    authenticator.expires_at = int(time.time())
    response = await stream(client)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Request-ID"]
    received = blocks(response)
    assert len(received) == 3
    for event_id, (block, event_type) in enumerate(
        zip(received, ["lease.acquired", "operation.submitted", "operation.submitted"]), start=1
    ):
        assert_event_block(block, event_id, event_type)


@pytest.mark.asyncio
async def test_last_event_id_wins_over_after(client, authenticator):
    await submit(client, 0)
    await submit(client, 1)
    authenticator.expires_at = int(time.time())
    received = blocks(await stream(client, last_event_id="2", after=0))
    assert len(received) == 1
    assert_event_block(received[0], 3, "operation.submitted")


@pytest.mark.asyncio
async def test_invalid_last_event_id(client):
    response = await stream(client, last_event_id="abc")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_last_event_id"


@pytest.mark.asyncio
async def test_heartbeat(client, authenticator):
    authenticator.expires_at = int(time.time()) + 1
    response = await stream(client, last_event_id="1")
    assert ": heartbeat\n\n" in response.text
    assert "id:" not in response.text


@pytest.mark.asyncio
async def test_live_delivery(client, authenticator, store):
    authenticator.expires_at = int(time.time()) + 1
    task = asyncio.create_task(stream(client, last_event_id="1"))
    await store.write(lambda tx: tx.release_lease(principal="alice"))
    response = await task
    assert "id: 2\nevent: lease.released\n" in response.text
