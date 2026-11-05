from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from typing import Any

import httpx
import pytest

from bluesky_httpserver.v2.api import create_app
from bluesky_httpserver.v2.gateway import WorkerGateway
from bluesky_httpserver.v2.store import EventType, Store

from .conftest import headers, new_key, submit

EXECUTIONS = "/api/v2/queue-executions"
ACKNOWLEDGE = "/api/v2/recovery/acknowledge"


async def start(client: httpx.AsyncClient, revision: int) -> httpx.Response:
    return await client.post(EXECUTIONS, json={}, headers=headers("alice", if_match=revision, key=new_key()))


async def safe_stop(client: httpx.AsyncClient, attempt_uid: str, token: str = "alice") -> httpx.Response:
    return await client.post(f"/api/v2/attempts/{attempt_uid}/safe-stop", headers=headers(token, key=new_key()))


async def acknowledge(client: httpx.AsyncClient, revision: int) -> httpx.Response:
    return await client.post(
        ACKNOWLEDGE, json={"note": "checked"}, headers=headers("alice", if_match=revision, key=new_key())
    )


async def events(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return (await client.get("/api/v2/events", params={"limit": 1000}, headers=headers("reader"))).json()["events"]


async def current_revision(client: httpx.AsyncClient) -> int:
    etag = (await client.get("/api/v2/queue", headers=headers("reader"))).headers["ETag"]
    return int(etag.strip('"').removeprefix("qrev-"))


async def dispatched(client: httpx.AsyncClient, worker: WorkerGateway) -> str:
    """Queue one operation, start the queue and dispatch it; return the attempt uid."""
    assert (await submit(client, 0)).status_code == 201
    assert (await start(client, 1)).status_code == 201
    await worker.step()
    (claim,) = [e for e in await events(client) if e["event_type"] == "operation.claimed"]
    return claim["attempt_uid"]


async def block(client: httpx.AsyncClient, relay, worker: WorkerGateway) -> None:
    """Dispatch an operation, then make the manager run an item the controller did not send."""
    await dispatched(client, worker)
    relay.state = dataclasses.replace(relay.state, running_item_uid="item-foreign")
    await worker.step()


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


@pytest.mark.asyncio
async def test_safe_stop(client, relay, worker):
    a = await dispatched(client, worker)
    response = await safe_stop(client, a)
    assert response.status_code == 202
    assert response.headers["ETag"]
    assert response.json()["safe_stop_requested_at"].endswith("Z")

    forbidden = await safe_stop(client, a, token="bob")
    assert forbidden.status_code == 403
    assert error_code(forbidden) == "insufficient_role"
    assert forbidden.json()["error"]["details"]["required_role"] == "QueueServer.Run"

    missing = await safe_stop(client, "no-such-attempt")
    assert (missing.status_code, error_code(missing)) == (404, "not_found")

    relay.finish_running("completed")
    await worker.step()
    ended = await safe_stop(client, a)
    assert (ended.status_code, error_code(ended)) == (409, "attempt_not_live")


@pytest.mark.asyncio
async def test_acknowledge_recovery(client, relay, worker):
    nothing = await acknowledge(client, 0)
    assert (nothing.status_code, error_code(nothing)) == (409, "not_blocked")

    await block(client, relay, worker)
    revision = await current_revision(client)
    refused = await acknowledge(client, revision)
    assert (refused.status_code, error_code(refused)) == (409, "no_fence_evidence")
    started = await start(client, revision)
    assert (started.status_code, error_code(started)) == (409, "dispatch_blocked")

    relay.state = dataclasses.replace(relay.state, manager_state="idle", running_item_uid=None)
    await worker.step()
    acknowledged = await acknowledge(client, await current_revision(client))
    assert acknowledged.status_code == 200
    assert acknowledged.json()["state"] == "stopped"
    assert [e["event_type"] for e in await events(client)][-2:] == ["worker.fenced", "queue_execution.stopped"]


@pytest.mark.asyncio
async def test_ready(client, relay, worker):
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "worker": "reachable",
        "environment": "present",
        "catalog": "compatible",
        "dispatch": "open",
    }

    await block(client, relay, worker)
    blocked = await client.get("/ready")
    assert (blocked.status_code, blocked.json()["dispatch"]) == (503, "blocked")

    relay.reachable = False
    await worker.step()
    unreachable = await client.get("/ready")
    assert (unreachable.status_code, unreachable.json()["worker"]) == (503, "unreachable")


@pytest.mark.asyncio
async def test_catalog_unavailable_before_the_first_step(store, authenticator, gateway):
    app = create_app(store=store, authenticator=authenticator, worker=gateway)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/api/v2/catalog", headers=headers("reader"))
        assert (response.status_code, error_code(response)) == (503, "catalog_unavailable")
        ready = await client.get("/ready")
        assert (ready.status_code, ready.json()["catalog"]) == (503, "unavailable")


async def wait_for(store: Store, event_type: EventType) -> None:
    cursor = 0
    async with asyncio.timeout(5):
        while True:
            for event in await store.events_after(cursor):
                if event.event_type is event_type:
                    return
                cursor = event.event_id
            await store.wait_for_event(cursor)


@pytest.mark.asyncio
async def test_queue_runs_end_to_end(store, relay, authenticator):
    gateway = WorkerGateway(store=store, relay=relay, user_group="primary", poll_seconds=0.01)
    app = create_app(store=store, authenticator=authenticator, worker=gateway)
    task = asyncio.create_task(gateway.run())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            async with asyncio.timeout(5):
                while (await client.get("/ready")).status_code != 200:
                    await asyncio.sleep(0.01)
            submitted = await client.post(
                "/api/v2/operations",
                json={
                    "operation_id": "demo.count",
                    "operation_version": "1",
                    "parameters": {"num": 2, "detectors": ["secondary"]},
                },
                headers=headers("alice", if_match=0, key=new_key()),
            )
            assert submitted.status_code == 201
            assert (await start(client, 1)).status_code == 201

            await wait_for(store, EventType.OPERATION_RUNNING)
            assert relay.executed[0][0]["kwargs"] == {"num": 2, "detectors": ["det2"]}
            relay.finish_running("completed")
            await wait_for(store, EventType.QUEUE_EXECUTION_COMPLETED)
            (claim,) = [e for e in await events(client) if e["event_type"] == "operation.claimed"]
            attempt = await client.get(f"/api/v2/attempts/{claim['attempt_uid']}", headers=headers("reader"))
            assert attempt.json()["state"] == "succeeded"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
