import httpx2
import pytest

from bluesky_httpserver.authorization import ServerBasedAPIAccessControl, api_access


@pytest.mark.asyncio
async def test_server_based_access_control_httpx2(monkeypatch):
    def handle_request(request):
        assert request.method == "GET"
        assert request.url == "https://access.example.com/instrument/tst/qserver/access"
        return httpx2.Response(200, json={"user": {"alice": {"first_name": "Alice"}}})

    client_class = httpx2.AsyncClient
    transport = httpx2.MockTransport(handle_request)
    monkeypatch.setattr(
        api_access.httpx2,
        "AsyncClient",
        lambda **kwargs: client_class(transport=transport, **kwargs),
    )

    manager = ServerBasedAPIAccessControl(instrument="TST", base_url="https://access.example.com")
    await manager.update_access_info()
    assert manager.get_user_roles("alice") == {"user"}
    assert manager.get_displayed_user_name("alice") == 'alice "Alice"'
