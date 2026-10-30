from __future__ import annotations

import httpx
import pytest

from bluesky_httpserver.v2.api import create_app
from bluesky_httpserver.v2.identity import OidcAuthenticator

from ..test_oidc import JWKS_URI, SETTINGS, FakeIdentityProvider, FakeMonotonic, SigningKey, alg_none, valid
from .conftest import headers

OPENAPI = "/api/v2/openapi.json"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization, challenge",
    [
        (None, "Bearer"),
        ("Bearer wrong", 'Bearer error="invalid_token"'),
        ("Basic x", 'Bearer error="invalid_request"'),
    ],
)
async def test_unauthenticated(client, authorization, challenge):
    response = await client.get(OPENAPI, headers={} if authorization is None else {"Authorization": authorization})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["WWW-Authenticate"] == challenge


@pytest.mark.asyncio
async def test_identity_provider_unavailable(client):
    response = await client.get(OPENAPI, headers=headers("idp-down"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_provider_unavailable"


@pytest.mark.asyncio
async def test_principal_without_role_is_forbidden(client):
    response = await client.get(OPENAPI, headers=headers("nobody"))
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "insufficient_role"
    assert error["details"]["required_role"] == "QueueServer.Read"


@pytest.mark.asyncio
async def test_authentication_precedes_body_validation(client):
    response = await client.post("/api/v2/control-lease", json={"ttl_seconds": "x"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.asyncio
async def test_real_verifier(store, manager):
    key = SigningKey.generate("k1")
    fake = FakeIdentityProvider(JWKS_URI, [key])
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        authenticator = OidcAuthenticator(SETTINGS, http, monotonic=FakeMonotonic())
        app = create_app(store=store, authenticator=authenticator, manager=manager)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get(OPENAPI, headers=headers(valid(key)))).status_code == 200
            assert (await client.get(OPENAPI, headers=headers(alg_none(key)))).status_code == 401
