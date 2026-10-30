from __future__ import annotations

import httpx
import pytest

from ..conftest import acquire, claimed
from .conftest import headers, new_key, queue, submit

EXECUTIONS = "/api/v2/queue-executions"


async def start(client: httpx.AsyncClient, revision: int, *, token: str = "alice") -> httpx.Response:
    return await client.post(EXECUTIONS, json={}, headers=headers(token, if_match=revision, key=new_key()))


async def stop(client: httpx.AsyncClient, queue_execution_uid: str, revision: int) -> httpx.Response:
    return await client.post(
        f"{EXECUTIONS}/{queue_execution_uid}/stop", headers=headers("alice", if_match=revision, key=new_key())
    )


@pytest.mark.asyncio
async def test_start_guards(client):
    empty = await start(client, 0)
    assert empty.status_code == 409
    assert empty.json()["error"]["code"] == "queue_empty"

    await submit(client, 0)
    await submit(client, 1)
    started = await start(client, 2)
    assert started.status_code == 201
    assert (started.json()["state"], started.json()["policy"]) == ("running", "stop_on_failure")
    assert started.headers["ETag"] == '"qrev-3"'

    second = await start(client, 3)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "execution_active"


@pytest.mark.asyncio
async def test_start_requires_run_role(client, store):
    await store.write(lambda tx: tx.release_lease(principal="alice"))
    await acquire(store, "bob")
    assert (await submit(client, 0, token="bob")).status_code == 201
    response = await start(client, 1, token="bob")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"
    assert response.json()["error"]["details"]["required_role"] == "QueueServer.Run"


@pytest.mark.asyncio
async def test_claim_between_read_and_write_then_stop(client, store):
    await submit(client, 0)
    b = (await submit(client, 1)).json()["operation_uid"]
    queue_execution_uid = (await start(client, 2)).json()["queue_execution_uid"]
    assert (await queue(client)).headers["ETag"] == '"qrev-3"'

    attempt, _, _ = await claimed(store)
    stale = await client.delete(f"/api/v2/operations/{b}", headers=headers("alice", if_match=3, key=new_key()))
    assert stale.status_code == 412
    assert stale.headers["ETag"] == '"qrev-4"'

    current = await client.get(f"/api/v2/attempts/{attempt.attempt_uid}", headers=headers("reader"))
    assert current.status_code == 200
    assert current.json()["state"] == "claimed"
    assert current.headers["ETag"] == '"qrev-4"'

    stopping = await stop(client, queue_execution_uid, 4)
    assert stopping.status_code == 200
    assert stopping.json()["state"] == "stopping"
    again = await stop(client, queue_execution_uid, 5)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "execution_not_running"
    execution = await client.get(f"{EXECUTIONS}/{queue_execution_uid}", headers=headers("reader"))
    assert execution.json()["state"] == "stopping"
