from __future__ import annotations

from datetime import datetime

import pytest

from .conftest import headers, new_key

LEASE = "/api/v2/control-lease"


@pytest.mark.asyncio
async def test_handover(client, clock):
    held = await client.post(LEASE, json={"ttl_seconds": 60}, headers=headers("bob", key=new_key()))
    assert held.status_code == 409
    assert held.json()["error"]["code"] == "lease_held"
    assert held.json()["error"]["details"]["holder"] == "alice"

    released = await client.post(f"{LEASE}/release", headers=headers("alice", key=new_key()))
    assert released.status_code == 200
    assert released.json() == {"lease": None}

    bob_key = new_key()
    acquired = await client.post(LEASE, json={"ttl_seconds": 60}, headers=headers("bob", key=bob_key))
    assert acquired.status_code == 201
    assert acquired.json()["holder"] == "bob"

    clock.advance(10)
    renewed = await client.post(
        f"{LEASE}/renew", json={"ttl_seconds": 3600}, headers=headers("bob", key=new_key())
    )
    assert renewed.status_code == 200
    expiry = datetime.fromisoformat(renewed.json()["expires_at"])
    assert expiry > datetime.fromisoformat(acquired.json()["expires_at"])

    refused = await client.post(
        f"{LEASE}/renew", json={"ttl_seconds": 60}, headers=headers("alice", key=new_key())
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "lease_required"

    replayed = await client.post(LEASE, json={"ttl_seconds": 60}, headers=headers("bob", key=bob_key))
    assert replayed.status_code == 201
    assert replayed.json() == acquired.json()
    events = (await client.get("/api/v2/events", headers=headers("reader"))).json()["events"]
    bob_acquisitions = [e for e in events if e["event_type"] == "lease.acquired" and e["actor_id"] == "bob"]
    assert len(bob_acquisitions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, key, status, code",
    [
        ({"ttl_seconds": 60}, None, 400, "idempotency_key_required"),
        ({"ttl_seconds": 60}, "has space", 400, "invalid_idempotency_key"),
        ({"ttl_seconds": 4}, "k1", 422, "invalid_request"),
    ],
)
async def test_refused_acquire(client, body, key, status, code):
    response = await client.post(LEASE, json=body, headers=headers("bob", key=key))
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_override_requires_admin(client):
    response = await client.post(
        f"{LEASE}/override", json={"reason": "mine", "ttl_seconds": 300}, headers=headers("bob", key=new_key())
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"
    assert response.json()["error"]["details"]["required_role"] == "QueueServer.Admin"


@pytest.mark.asyncio
async def test_override_requires_reason(client):
    response = await client.post(
        f"{LEASE}/override", json={"reason": "   ", "ttl_seconds": 300}, headers=headers("admin", key=new_key())
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_override(client):
    response = await client.post(
        f"{LEASE}/override",
        json={"reason": "beamline emergency", "ttl_seconds": 300},
        headers=headers("admin", key=new_key()),
    )
    assert response.status_code == 200
    assert response.json()["holder"] == "admin"
    last = (await client.get("/api/v2/events", headers=headers("reader"))).json()["events"][-1]
    assert last["event_type"] == "lease.overridden"
    assert last["payload"]["previous_holder"] == "alice"
    assert last["payload"]["reason"] == "beamline emergency"
