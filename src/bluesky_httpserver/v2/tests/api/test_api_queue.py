from __future__ import annotations

import pytest

from bluesky_httpserver.v2.catalog import Catalog

from .conftest import headers, new_key, operation_uids, queue, submit
from .fake_manager import DEMO_COUNT

OPERATIONS = "/api/v2/operations"
SUBMISSION = {"operation_id": "demo.count", "operation_version": "1", "parameters": {"num": 1}}
NAN_SUBMISSION = '{"operation_id": "demo.count", "operation_version": "1", "parameters": {"num": NaN}}'


@pytest.mark.asyncio
async def test_submit(client):
    response = await submit(client, 0, num=2)
    assert response.status_code == 201
    assert response.headers["ETag"] == '"qrev-1"'
    operation = response.json()
    assert operation["submitted_by"] == "alice"
    assert operation["descriptor_sha256"] == DEMO_COUNT.sha256
    assert operation["submitted_at"].endswith("Z")

    current = await queue(client)
    assert operation_uids(current) == [operation["operation_uid"]]
    assert current.headers["ETag"] == '"qrev-1"'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token, if_match, body, status, code, paths",
    [
        ("alice", '"qrev-0"', {**SUBMISSION, "operation_version": "2"}, 422, "unknown_operation", None),
        (
            "alice",
            '"qrev-0"',
            {**SUBMISSION, "parameters": {"num": 0, "x": 1}},
            422,
            "invalid_parameters",
            ["$", "$.num"],
        ),
        ("alice", '"qrev-0"', NAN_SUBMISSION, 422, "invalid_parameters", ["$"]),
        ("alice", '"qrev-0"', {**SUBMISSION, "submitted_by": "eve"}, 422, "invalid_request", None),
        ("alice", None, SUBMISSION, 428, "precondition_required", None),
        ("alice", "*", SUBMISSION, 400, "invalid_if_match", None),
        ("bob", '"qrev-0"', SUBMISSION, 403, "lease_required", None),
        ("reader", '"qrev-0"', SUBMISSION, 403, "insufficient_role", None),
    ],
)
async def test_rejected_submission_writes_nothing(client, token, if_match, body, status, code, paths):
    request_headers = headers(token, key=new_key())
    if if_match is not None:
        request_headers["If-Match"] = if_match
    if isinstance(body, str):
        request_headers["Content-Type"] = "application/json"
        response = await client.post(OPERATIONS, content=body, headers=request_headers)
    else:
        response = await client.post(OPERATIONS, json=body, headers=request_headers)
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    if paths is not None:
        assert [e["path"] for e in error["details"]["errors"]] == paths

    current = await queue(client)
    assert current.headers["ETag"] == '"qrev-0"'
    assert operation_uids(current) == []


@pytest.mark.asyncio
async def test_stale_write_is_refused(client):
    assert (await submit(client, 0)).status_code == 201
    stale = await submit(client, 0)
    assert stale.status_code == 412
    assert stale.headers["ETag"] == '"qrev-1"'
    assert stale.json()["error"]["code"] == "stale_revision"
    assert stale.json()["error"]["details"]["current_revision"] == 1
    assert len(operation_uids(await queue(client))) == 1


@pytest.mark.asyncio
async def test_replay(client, manager):
    first = await submit(client, 0, key="k1")
    repeat = await submit(client, 0, key="k1")
    assert (repeat.status_code, repeat.json()) == (first.status_code, first.json())
    assert repeat.headers["ETag"] == '"qrev-1"'
    assert len(operation_uids(await queue(client))) == 1

    conflict = await submit(client, 0, num=3, key="k1")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    manager.current = Catalog([])
    after_reload = await submit(client, 0, key="k1")
    assert (after_reload.status_code, after_reload.json()) == (201, first.json())
    unknown = await submit(client, 1)
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_operation"


@pytest.mark.asyncio
async def test_replace(client):
    a = (await submit(client, 0)).json()["operation_uid"]
    b = (await submit(client, 1)).json()["operation_uid"]
    replacement = {**SUBMISSION, "parameters": {"num": 5}}

    response = await client.put(
        f"{OPERATIONS}/{a}", json=replacement, headers=headers("alice", if_match=2, key=new_key())
    )
    assert response.status_code == 200
    assert response.headers["ETag"] == '"qrev-3"'
    r = response.json()["operation_uid"]
    assert r not in (a, b)
    assert operation_uids(await queue(client)) == [r, b]

    replaced = (await client.get(f"{OPERATIONS}/{a}", headers=headers("reader"))).json()
    assert (replaced["state"], replaced["replaced_by"]) == ("replaced", r)

    again = await client.put(
        f"{OPERATIONS}/{a}", json=replacement, headers=headers("alice", if_match=3, key=new_key())
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "operation_not_queued"


@pytest.mark.asyncio
async def test_cancel_and_reorder(client):
    a, b, c = [(await submit(client, revision)).json()["operation_uid"] for revision in range(3)]

    cancelled = await client.delete(f"{OPERATIONS}/{b}", headers=headers("alice", if_match=3, key=new_key()))
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert operation_uids(await queue(client)) == [a, c]

    reordered = await client.post(
        "/api/v2/queue/reorder",
        json={"operation_uids": [c, a]},
        headers=headers("alice", if_match=4, key=new_key()),
    )
    assert reordered.status_code == 200
    assert operation_uids(reordered) == [c, a]
    assert reordered.headers["ETag"] == '"qrev-5"'

    invalid = await client.post(
        "/api/v2/queue/reorder", json={"operation_uids": [c]}, headers=headers("alice", if_match=5, key=new_key())
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_reorder"


@pytest.mark.asyncio
async def test_expired_lease(client, clock):
    clock.advance(3601)
    response = await submit(client, 0)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "lease_required"
