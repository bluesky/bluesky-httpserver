from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from bluesky_httpserver.v2.api.dependencies import authenticated_principal

from .conftest import headers


def dependency_calls(dependant: Dependant) -> Iterator[Callable[..., Any] | None]:
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from dependency_calls(dependency)


def test_every_route_but_health_requires_authentication(app: FastAPI):
    # Included routers stay nested in app.routes; the contexts carry each route's effective path and dependant.
    for context in iter_route_contexts(app.routes):
        assert isinstance(context.original_route, APIRoute), context.path
        if context.path == "/health":
            continue
        assert authenticated_principal in set(dependency_calls(context.dependant)), context.path


@pytest.mark.asyncio
async def test_openapi_has_no_plan_script_or_manager_routes(client):
    response = await client.get("/api/v2/openapi.json", headers=headers("reader"))
    assert response.status_code == 200
    banned = ("plan", "script", "function", "environment", "permissions", "zmq")
    assert [path for path in response.json()["paths"] if any(word in path for word in banned)] == []


@pytest.mark.asyncio
async def test_openapi_describes_authentication_errors_and_preconditions(client):
    # Clients and agent tools are generated from this document, so it must state what the API requires.
    document = (await client.get("/api/v2/openapi.json", headers=headers("reader"))).json()
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            where = f"{method.upper()} {path}"
            if path != "/health":
                assert operation["security"] == [{"bearer": []}], where
            for status, response in operation["responses"].items():
                if int(status) >= 400:
                    schema = response["content"]["application/json"]["schema"]
                    assert schema == {"$ref": "#/components/schemas/ErrorEnvelope"}, (where, status)
            required_headers = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "header" and parameter["required"]
            }
            if method in ("post", "put", "delete"):
                assert "Idempotency-Key" in required_headers, where
            if "412" in operation["responses"]:
                assert "If-Match" in required_headers, where


@pytest.mark.asyncio
async def test_health_needs_no_token(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_unknown_route_gets_error_envelope(client):
    response = await client.get("/api/v2/no-such-route", headers=headers("reader"))
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["X-Request-ID"]
