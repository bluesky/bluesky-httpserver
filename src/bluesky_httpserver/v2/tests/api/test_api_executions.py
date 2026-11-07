from __future__ import annotations

import httpx
import pytest

from ..conftest import CLIENT, acquire, claimed
from .conftest import headers, new_key, queue, submit

EXECUTIONS = "/api/v2/queue-executions"


async def start(client: httpx.AsyncClient, revision: int, *, token: str = "alice") -> httpx.Response:
    return await client.post(EXECUTIONS, json={}, headers=headers(token, if_match=revision, key=new_key()))


async def stop(
    client: httpx.AsyncClient, queue_execution_uid: str, revision: int, *, token: str = "alice"
) -> httpx.Response:
    return await client.post(
        f"{EXECUTIONS}/{queue_execution_uid}/stop", headers=headers(token, if_match=revision, key=new_key())
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
    await store.write(lambda tx: tx.release_lease(principal="alice", client_id=CLIENT))
    await acquire(store, "bob")
    assert (await submit(client, 0, token="bob")).status_code == 201
    response = await start(client, 1, token="bob")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"
    assert response.json()["error"]["details"]["required_role"] == "QueueServer.Run"


@pytest.mark.asyncio
async def test_an_agent_acting_for_the_holder_uses_her_lease_and_is_recorded(client):
    # The store fixture gave alice the lease through her own client; the agent's token names alice as the caller.
    submitted = await submit(client, 0, token="agent-for-alice")
    assert submitted.status_code == 201
    assert (submitted.json()["submitted_by"], submitted.json()["submitted_via"]) == ("alice", "agent")
    started = await start(client, 1, token="agent-for-alice")
    assert started.status_code == 201
    assert (started.json()["started_by"], started.json()["started_via"]) == ("alice", "agent")
    events = (await client.get("/api/v2/events", headers=headers("reader"))).json()["events"]
    assert [(e["event_type"], e["actor_id"], e["client_id"]) for e in events[:3]] == [
        ("lease.acquired", "alice", CLIENT),
        ("operation.submitted", "alice", "agent"),
        ("queue_execution.started", "alice", "agent"),
    ]


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

    # Carol has the Run role but not the lease, which alice holds: a stop needs only the role.
    stopping = await stop(client, queue_execution_uid, 4, token="carol")
    assert stopping.status_code == 200
    assert stopping.json()["state"] == "stopping"
    again = await stop(client, queue_execution_uid, 5)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "execution_not_running"
    execution = await client.get(f"{EXECUTIONS}/{queue_execution_uid}", headers=headers("reader"))
    assert execution.json()["state"] == "stopping"


@pytest.mark.asyncio
async def test_running_attempt_shows_its_runs(client, relay, worker):
    await submit(client, 0)
    await start(client, 1)
    await worker.step()
    ((item, _, _),) = relay.executed
    attempt = f"/api/v2/attempts/{item['meta']['attempt_uid']}"

    relay.open_run("run-a")
    await worker.step()
    running = (await client.get(attempt, headers=headers("reader"))).json()
    assert (running["state"], running["current_run_uids"]) == ("running", ["run-a"])

    relay.finish_running("completed", run_uids=["run-a"])
    await worker.step()
    finished = (await client.get(attempt, headers=headers("reader"))).json()
    assert (finished["state"], finished["current_run_uids"]) == ("succeeded", None)
