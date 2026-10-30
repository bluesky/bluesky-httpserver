from __future__ import annotations

import pytest

from bluesky_httpserver.v2.catalog import Catalog

from .conftest import headers
from .fake_manager import DEMO_COUNT


@pytest.mark.asyncio
async def test_catalog(client):
    response = await client.get("/api/v2/catalog", headers=headers("reader"))
    assert response.status_code == 200
    body = response.json()
    assert body["catalog_sha256"] == Catalog([DEMO_COUNT]).sha256
    assert [(o["operation_id"], o["descriptor_sha256"]) for o in body["operations"]] == [
        ("demo.count", DEMO_COUNT.sha256)
    ]


@pytest.mark.asyncio
async def test_control_lease(client):
    response = await client.get("/api/v2/control-lease", headers=headers("reader"))
    assert response.status_code == 200
    lease = response.json()["lease"]
    assert lease["holder"] == "alice"
    assert lease["expires_at"].endswith("Z")


@pytest.mark.asyncio
async def test_empty_queue(client):
    response = await client.get("/api/v2/queue", headers=headers("reader"))
    assert response.status_code == 200
    assert response.json() == {"revision": 0, "operations": []}
    assert response.headers["ETag"] == '"qrev-0"'


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["operations", "attempts", "queue-executions"])
async def test_unknown_resource(client, collection):
    response = await client.get(f"/api/v2/{collection}/no-such-uid", headers=headers("reader"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_events(client):
    response = await client.get("/api/v2/events", params={"after": 0}, headers=headers("reader"))
    assert response.status_code == 200
    assert [e["event_type"] for e in response.json()["events"]] == ["lease.acquired"]


@pytest.mark.asyncio
async def test_event_limit_must_be_positive(client):
    response = await client.get("/api/v2/events", params={"limit": 0}, headers=headers("reader"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
