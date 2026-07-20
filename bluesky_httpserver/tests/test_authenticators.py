import asyncio
import logging
import os
import time
from datetime import timedelta
from typing import Any, Tuple
from unittest.mock import MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import SecurityScopes
from jose import ExpiredSignatureError, jwt
from respx import MockRouter
from starlette.datastructures import URL, QueryParams
from starlette.requests import Request

from bluesky_httpserver import authentication as _auth

from ..authenticators import (
    EntraAuthenticator,
    LDAPAuthenticator,
    OIDCAuthenticator,
    ProxiedOIDCAuthenticator,
    UserSessionState,
)

LDAP_TEST_HOST = os.environ.get("QSERVER_TEST_LDAP_HOST", "localhost")
LDAP_TEST_PORT = int(os.environ.get("QSERVER_TEST_LDAP_PORT", "1389"))
LDAP_TEST_ALT_HOST = os.environ.get("QSERVER_TEST_LDAP_ALT_HOST")
if not LDAP_TEST_ALT_HOST:
    LDAP_TEST_ALT_HOST = "127.0.0.1" if LDAP_TEST_HOST == "localhost" else LDAP_TEST_HOST


# fmt: off


@pytest.mark.parametrize("ldap_server_address, ldap_server_port", [
    (LDAP_TEST_HOST, LDAP_TEST_PORT),
    (f"{LDAP_TEST_HOST}:{LDAP_TEST_PORT}", 904),  # Random port, ignored
    (f"{LDAP_TEST_HOST}:{LDAP_TEST_PORT}", None),
    (LDAP_TEST_ALT_HOST, LDAP_TEST_PORT),
    (f"{LDAP_TEST_ALT_HOST}:{LDAP_TEST_PORT}", 904),
    ([LDAP_TEST_HOST], LDAP_TEST_PORT),
    ([LDAP_TEST_HOST, LDAP_TEST_ALT_HOST], LDAP_TEST_PORT),
    ([LDAP_TEST_HOST, f"{LDAP_TEST_ALT_HOST}:{LDAP_TEST_PORT}"], LDAP_TEST_PORT),
    ([f"{LDAP_TEST_HOST}:{LDAP_TEST_PORT}", f"{LDAP_TEST_ALT_HOST}:{LDAP_TEST_PORT}"], None),
])
# fmt: on
@pytest.mark.parametrize("use_tls,use_ssl", [(False, False)])
def test_LDAPAuthenticator_01(use_tls, use_ssl, ldap_server_address, ldap_server_port):
    """
    Basic test for ``LDAPAuthenticator``.

    TODO: The test could be extended with enabled TLS or SSL, but it requires configuration
    of the LDAP server.
    """
    authenticator = LDAPAuthenticator(
        ldap_server_address,
        ldap_server_port,
        bind_dn_template="cn={username},ou=users,dc=example,dc=org",
        use_tls=use_tls,
        use_ssl=use_ssl,
    )

    async def testing():
        assert await authenticator.authenticate("user01", "password1") == UserSessionState("user01", {})
        assert await authenticator.authenticate("user02", "password2") == UserSessionState("user02", {})
        assert await authenticator.authenticate("user02a", "password2") is None
        assert await authenticator.authenticate("user02", "password2a") is None

    asyncio.run(testing())


def token(issued: bool, expired: bool) -> dict[str, str]:
    now = time.time()
    return {
        "aud": "tiled",
        "exp": (now - 1500) if expired else (now + 1500),
        "iat": (now - 1500) if issued else (now + 1500),
        "iss": "https://example.com/realms/example",
        "sub": "Jane Doe",
    }


def encrypted_token(token_data: dict[str, str], private_key: rsa.RSAPrivateKey) -> str:
    return jwt.encode(
        token_data,
        key=private_key,
        algorithm="RS256",
        headers={"kid": "secret"},
    )


def test_oidc_authenticator_caching(
    mock_oidc_server: MockRouter,
    oidc_well_known_url: str,
    well_known_response: dict[str, Any],
    json_web_keyset: list[dict[str, Any]],
):
    authenticator = OIDCAuthenticator("tiled", "tiled", "secret", well_known_uri=oidc_well_known_url)
    assert authenticator.client_id == "tiled"
    assert authenticator.authorization_endpoint == well_known_response["authorization_endpoint"]
    assert authenticator.id_token_signing_alg_values_supported == well_known_response[
        "id_token_signing_alg_values_supported"
    ]
    assert authenticator.issuer == well_known_response["issuer"]
    assert authenticator.jwks_uri == well_known_response["jwks_uri"]
    assert authenticator.token_endpoint == well_known_response["token_endpoint"]
    assert authenticator.device_authorization_endpoint == well_known_response["device_authorization_endpoint"]
    assert authenticator.end_session_endpoint == well_known_response["end_session_endpoint"]

    assert len(mock_oidc_server.calls) == 1
    call_request = mock_oidc_server.calls[0].request
    assert call_request.method == "GET"
    assert call_request.url == oidc_well_known_url

    assert authenticator.keys() == json_web_keyset
    assert len(mock_oidc_server.calls) == 2
    keys_request = mock_oidc_server.calls[1].request
    assert keys_request.method == "GET"
    assert keys_request.url == well_known_response["jwks_uri"]

    for _ in range(10):
        assert authenticator.keys() == json_web_keyset

    assert len(mock_oidc_server.calls) == 2


@pytest.mark.parametrize("issued", [True, False])
@pytest.mark.parametrize("expired", [True, False])
def test_oidc_decoding(
    mock_oidc_server: MockRouter,
    oidc_well_known_url: str,
    issued: bool,
    expired: bool,
    keys: Tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
):
    private_key, _ = keys
    authenticator = OIDCAuthenticator("tiled", "tiled", "secret", well_known_uri=oidc_well_known_url)
    access_token = token(issued, expired)
    encrypted_access_token = encrypted_token(access_token, private_key)

    if not expired:
        assert authenticator.decode_token(encrypted_access_token) == access_token
    else:
        with pytest.raises(ExpiredSignatureError):
            authenticator.decode_token(encrypted_access_token)


def test_entra_decoding_ignores_unmapped_scopes(caplog):
    def mock_decode_token(self, id_token, access_token):
        return {
            "iss": "https://login.microsoftonline.com/example-tenant/v2.0",
            "sub": "opaque-sub",
            "preferred_username": "alice@example.org",
            "scp": "known.scope unknown.scope",
        }

    original_decode_token = OIDCAuthenticator.decode_token
    OIDCAuthenticator.decode_token = mock_decode_token
    try:
        caplog.set_level(logging.WARNING)

        authenticator = object.__new__(EntraAuthenticator)
        authenticator.scopes_map = {"known.scope": ["read:metadata"]}
        claims = authenticator.decode_token("id-token", "access-token")

        assert claims["entra_sub"] == "opaque-sub"
        assert claims["entra_username"] == "alice@example.org"
        assert claims["user"] == "alice"
        assert claims["scope"] == "read:metadata"
        assert any(
            "Unmapped Entra scope in 'scp': unknown.scope" in record.message
            for record in caplog.records
        )
    finally:
        OIDCAuthenticator.decode_token = original_decode_token


@pytest.mark.asyncio
async def test_proxied_oidc_token_retrieval(oidc_well_known_url: str, mock_oidc_server: MockRouter):
    authenticator = ProxiedOIDCAuthenticator("tiled", "tiled", oidc_well_known_url,
                                             device_flow_client_id="tiled-cli")
    test_request = httpx.Request("GET", "http://example.com", headers={"Authorization": "bearer FOO"})

    assert "FOO" == await authenticator.oauth2_schema(test_request)


def create_mock_oidc_request(query_params=None):
    if query_params is None:
        query_params = {}

    class MockRequest:
        def __init__(self, request_query_params):
            self.query_params = QueryParams(request_query_params)
            self.scope = {
                "type": "http",
                "scheme": "http",
                "server": ("localhost", 8000),
                "path": "/api/v1/auth/provider/orcid/code",
                "headers": [],
            }
            self.headers = {"host": "localhost:8000"}
            self.url = URL("http://localhost:8000/api/v1/auth/provider/orcid/code")

    return MockRequest(query_params)


@pytest.mark.asyncio
async def test_OIDCAuthenticator_mock(
    mock_oidc_server: MockRouter,
    oidc_well_known_url: str,
    well_known_response: dict[str, Any],
    monkeypatch,
):
    mock_jwt_payload = {
        "sub": "0009-0008-8698-7745",
        "aud": "APP-TEST-CLIENT-ID",
        "iss": well_known_response["issuer"],
        "exp": 9999999999,
        "iat": 1000000000,
        "given_name": "Test User",
    }

    mock_oidc_server.post(well_known_response["token_endpoint"]).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "mock-access-token",
                "id_token": "mock-id-token",
                "token_type": "bearer",
            },
        )
    )

    authenticator = OIDCAuthenticator(
        audience="APP-TEST-CLIENT-ID",
        client_id="APP-TEST-CLIENT-ID",
        client_secret="test-secret",
        well_known_uri=oidc_well_known_url,
    )

    mock_request = create_mock_oidc_request({"code": "test-auth-code"})

    def mock_jwt_decode(*args, **kwargs):
        return mock_jwt_payload

    def mock_jwk_construct(*args, **kwargs):
        class MockJWK:
            pass

        return MockJWK()

    monkeypatch.setattr("jose.jwt.decode", mock_jwt_decode)
    monkeypatch.setattr("jose.jwk.construct", mock_jwk_construct)

    user_session = await authenticator.authenticate(mock_request)

    assert user_session is not None
    assert user_session.user_name == "0009-0008-8698-7745"


@pytest.mark.asyncio
async def test_OIDCAuthenticator_missing_code_parameter(oidc_well_known_url: str):
    authenticator = OIDCAuthenticator(
        audience="APP-TEST-CLIENT-ID",
        client_id="APP-TEST-CLIENT-ID",
        client_secret="test-secret",
        well_known_uri=oidc_well_known_url,
    )

    mock_request = create_mock_oidc_request({})

    result = await authenticator.authenticate(mock_request)
    assert result is None


@pytest.mark.asyncio
async def test_OIDCAuthenticator_token_exchange_failure(
    oidc_well_known_url: str,
    mock_oidc_server,
    well_known_response,
):
    mock_oidc_server.post(well_known_response["token_endpoint"]).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_client",
                "error_description": "Client not found: APP-TEST-CLIENT-ID",
            },
        )
    )

    authenticator = OIDCAuthenticator(
        audience="APP-TEST-CLIENT-ID",
        client_id="APP-TEST-CLIENT-ID",
        client_secret="test-secret",
        well_known_uri=oidc_well_known_url,
    )

    mock_request = create_mock_oidc_request({"code": "invalid-code"})

    result = await authenticator.authenticate(mock_request)
    assert result is None


def _encode_hs(payload, key):
    return jwt.encode(payload, key, algorithm="HS256")


def test_decode_token_tries_hmac_keys_first():
    """The bluesky-httpserver HMAC keys must be tried before any proxied
    authenticator fallback.  Otherwise a stolen OIDC key could impersonate
    a locally-minted API-key session."""
    payload = {"sub": "u1", "sub_typ": "user", "ids": []}
    token = _encode_hs(payload, "k-primary")

    fake_proxied = MagicMock(spec=ProxiedOIDCAuthenticator)
    fake_proxied.decode_token.side_effect = AssertionError("must not be called")

    result = _auth.decode_token(token, ["k-primary", "k-secondary"], fake_proxied)
    assert result == payload
    fake_proxied.decode_token.assert_not_called()


def test_decode_token_supports_key_rotation():
    """Older tokens minted with a rotated-out key must still decode if the
    old key is present in secret_keys."""
    token = _encode_hs({"sub": "u1"}, "old-key")
    result = _auth.decode_token(token, ["new-key", "old-key"], None)
    assert result["sub"] == "u1"


def test_decode_token_falls_back_to_proxied_authenticator():
    """When no HMAC key accepts the token, delegate to a
    ProxiedOIDCAuthenticator.decode_token.  This enables OIDC-minted access
    tokens (device-code flow) to be accepted by protected endpoints."""
    # Encode with a key that is not in secret_keys, so HMAC decoding fails.
    token = _encode_hs({"sub": "external-u", "scp": "read:queue"}, "unknown-key")

    fake_proxied = MagicMock(spec=ProxiedOIDCAuthenticator)
    fake_proxied.decode_token.return_value = {
        "sub": "external-u",
        "scp": "read:queue",
    }

    result = _auth.decode_token(token, ["hmac-key"], fake_proxied)
    assert result == {"sub": "external-u", "scp": "read:queue"}
    fake_proxied.decode_token.assert_called_once_with(token)


def test_decode_token_raises_when_no_key_matches():
    token = _encode_hs({"sub": "u1"}, "unknown")
    with pytest.raises(HTTPException) as excinfo:
        _auth.decode_token(token, ["a", "b"], None)
    assert excinfo.value.status_code == 401


def test_decode_token_propagates_expired_signature():
    """Expired tokens raise ExpiredSignatureError verbatim so the caller can
    return a distinct 401 with 'refresh token' guidance rather than a
    generic 'invalid credentials'."""
    past = int(time.time()) - 3600
    token = jwt.encode({"sub": "u1", "exp": past}, "k", algorithm="HS256")
    with pytest.raises(ExpiredSignatureError):
        _auth.decode_token(token, ["k"], None)


def _make_token(private_key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": "tiled",
        "exp": now + 1500,
        "iat": now - 10,
        "iss": "https://example.com/realms/example",
        "sub": "abc-123",
    }
    claims.update(overrides)
    return jwt.encode(claims, key=private_key, algorithm="RS256", headers={"kid": "secret"})


def test_oidc_decode_token_accepts_access_token_kwarg(mock_oidc_server, oidc_well_known_url, keys):
    """After the port, decode_token must accept an optional second positional
    argument (the access_token, used for at_hash validation)."""
    priv, _ = keys
    auth = OIDCAuthenticator("tiled", "tiled", "secret", well_known_uri=oidc_well_known_url)
    id_token = _make_token(priv)
    # Both calling conventions must work.
    single = auth.decode_token(id_token)
    dual = auth.decode_token(id_token, access_token=None)
    assert single == dual


def test_oidc_keys_cache_ttl_is_one_hour():
    """The @cached decorator on OIDCAuthenticator.keys() must use a 1h TTL."""
    # cachetools stores the TTL on the cache attached to the wrapped function.
    method = OIDCAuthenticator.keys
    # ``cachetools.func.ttl_cache`` or ``cachetools.cached(TTLCache(...))``
    # both expose the underlying cache via the wrapped function.  We only
    # need to check that the TTL is one hour, not seven days.
    cache = getattr(method, "cache", None)
    if cache is None:
        # cachetools>=5 uses __wrapped__.cache or the closure.  Fall back to
        # inspecting closures.
        closures = getattr(method, "__closure__", None) or ()
        for cell in closures:
            obj = cell.cell_contents
            if hasattr(obj, "ttl"):
                cache = obj
                break
    assert cache is not None, "Unable to locate TTLCache on OIDCAuthenticator.keys"
    # 1 h == 3600 s. Assert it's an hour, definitely not 7 days.
    assert cache.ttl == pytest.approx(timedelta(hours=1).total_seconds())
    assert cache.ttl < timedelta(days=1).total_seconds()


def test_entra_authenticator_decode_token_signature(mock_oidc_server, oidc_well_known_url, keys, monkeypatch):
    """Regression test for the fork-local defect where
    EntraAuthenticator.decode_token called super().decode_token(id_token,
    access_token) against an OIDCAuthenticator whose decode_token only
    accepted a single argument.  After the port the parent accepts an
    optional access_token."""
    priv, _ = keys
    auth = EntraAuthenticator(
        audience="tiled",
        client_id="tiled",
        well_known_uri=oidc_well_known_url,
        device_flow_client_id="tiled-cli",
        scopes_map={"User.Read": ["read:queue"]},
    )
    id_token = _make_token(
        priv,
        preferred_username="jane@example.com",
        scp="User.Read",
    )
    # Must not raise TypeError from arg-count mismatch, nor JWTError.
    claims = auth.decode_token(id_token, access_token="opaque-access-token")
    # UUID5 rewrites 'sub', preserves entra_sub, resolves user, maps scopes.
    assert claims["entra_sub"] == "abc-123"
    assert claims["user"] == "jane"
    assert "read:queue" in claims["scope"].split()


class TestExtractScopes:
    def test_scp_as_space_separated_string(self):
        assert _auth._extract_scopes({"scp": "read:queue write:queue:edit"}) == {
            "read:queue",
            "write:queue:edit",
        }

    def test_scp_as_list(self):
        assert _auth._extract_scopes({"scp": ["read:queue", "read:status"]}) == {
            "read:queue",
            "read:status",
        }

    def test_scope_as_space_separated_string(self):
        assert _auth._extract_scopes({"scope": "read:queue read:status"}) == {
            "read:queue",
            "read:status",
        }

    def test_empty_or_missing(self):
        assert _auth._extract_scopes({}) == set()
        assert _auth._extract_scopes({"scp": "", "scope": ""}) == {""}


class _FakeAuthorizationEndpoint:
    """Stand-in for the ``authorization_endpoint`` cached_property.  We do not
    want to hit an actual OIDC well-known URL from a unit test."""

    def __init__(self):
        self.captured_params: dict | None = None

    def copy_with(self, params):
        self.captured_params = params
        # Return an httpx.URL so RedirectResponse can str() it cleanly.
        return httpx.URL("https://idp.example.com/authorize").copy_with(params=params)


@pytest.mark.asyncio
async def test_authorize_route_requests_offline_access_and_prompts_login():
    """Verify that the browser-facing /authorize redirect asks the IdP for
    offline_access (to guarantee a refresh_token) and always prompts the
    user (avoids surprising silent SSO)."""
    fake_endpoint = _FakeAuthorizationEndpoint()

    class FakeAuthenticator:
        client_id = "test-client"
        authorization_endpoint = fake_endpoint
        extra_scopes = ["api://tiled/access_as_user"]

    class FakeRequest:
        headers = {"host": "localhost:8000"}
        scope = {"scheme": "http", "root_path": ""}

    route = _auth.build_authorize_route(FakeAuthenticator(), "orcid")
    resp = await route(FakeRequest(), state=None)
    assert resp.status_code == 307
    params = fake_endpoint.captured_params
    assert params["prompt"] == "login"
    scopes = set(params["scope"].split())
    assert {"openid", "offline_access", "api://tiled/access_as_user"}.issubset(scopes)


def _make_request(*, query_string: bytes = b"", path: str = "/api/status") -> Request:
    return Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("localhost", 8000),
            "path": path,
            "query_string": query_string,
            "root_path": "",
            "headers": [(b"host", b"localhost:8000")],
        }
    )


def test_headers_for_401_includes_scope_and_root():
    request = _make_request()
    headers = _auth.headers_for_401(request, SecurityScopes(scopes=["read:status"]))
    assert headers["WWW-Authenticate"] == 'Bearer scope="read:status"'
    assert headers["X-Tiled-Root"] == "http://localhost:8000/api"


def test_check_scopes_raises_for_missing_scope():
    principal = _auth.schemas.Principal(
        uuid="123e4567-e89b-12d3-a456-426614174000",
        type="user",
        scopes={"read:status"},
    )
    request = _make_request()
    with pytest.raises(HTTPException) as excinfo:
        _auth.check_scopes(request, SecurityScopes(scopes=["admin:read:principals"]), principal)
    assert excinfo.value.status_code == 401
    assert "Not enough permissions" in excinfo.value.detail


def test_cleanup_principal_scopes_assigns_sorted_fields():
    principal = _auth.schemas.Principal(
        uuid="123e4567-e89b-12d3-a456-426614174000",
        type="user",
        identities=[_auth.schemas.Identity(id="alice", provider="internal")],
    )
    result = _auth.cleanup_principal_scopes(
        roles={"expert", "admin"},
        scopes={"read:status", "read:queue"},
        api_key_scopes={"read:status"},
        principal=principal,
    )
    assert result.roles == ["admin", "expert"]
    assert result.scopes == ["read:queue", "read:status"]
    assert result.api_key_scopes == ["read:status"]


def test_get_current_principal_rejects_invalid_single_user_api_key(monkeypatch):
    class _DummySessionMaker:
        def __call__(self):
            class _DummyCtx:
                def __enter__(self):
                    return MagicMock()

                def __exit__(self, exc_type, exc, tb):
                    return False

            return _DummyCtx()

    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _db_settings: _DummySessionMaker())

    settings = MagicMock()
    settings.database_settings = MagicMock()
    settings.single_user_api_key = "expected-key"

    api_access_manager = MagicMock()
    api_access_manager.get_user_scopes.return_value = {"read:status"}
    api_access_manager.get_user_roles.return_value = {"single_user"}

    request = _make_request()
    with pytest.raises(HTTPException) as excinfo:
        _auth.get_current_principal(
            request=request,
            security_scopes=SecurityScopes(scopes=[]),
            access_token=None,
            decoded_access_token=None,
            api_key="wrong-key",
            settings=settings,
            authenticators={},
            api_access_manager=api_access_manager,
        )
    assert excinfo.value.status_code == 401
    assert "Invalid API key" in excinfo.value.detail


def test_get_current_principal_preserves_api_key_scopes(sqlite_session, monkeypatch):
    import hashlib
    import secrets as py_secrets

    from sqlalchemy.orm import sessionmaker

    from bluesky_httpserver.database import orm as db_orm
    from bluesky_httpserver.database.core import create_user
    from bluesky_httpserver.settings import DatabaseSettings

    db = sqlite_session
    principal = create_user(db, "internal", "alice")
    secret = py_secrets.token_bytes(4 + 32)
    apikey_orm = db_orm.APIKey(
        principal_id=principal.id,
        first_eight=secret.hex()[:8],
        hashed_secret=hashlib.sha256(secret).digest(),
        scopes=["read:status"],
    )
    db.add(apikey_orm)
    db.commit()

    engine = db.get_bind()

    def _fake_sessionmaker(_db_settings):
        return sessionmaker(bind=engine, autocommit=False, autoflush=False)

    monkeypatch.setattr(_auth, "get_sessionmaker", _fake_sessionmaker)

    settings = MagicMock()
    settings.database_settings = DatabaseSettings(uri="sqlite://", pool_size=None, pool_pre_ping=None)
    settings.authentication_provider_names = ["internal"]

    api_access_manager = MagicMock()
    api_access_manager.get_user_scopes.return_value = {"read:status", "write:queue"}
    api_access_manager.get_user_roles.return_value = {"user"}

    request = _make_request()
    resolved = _auth.get_current_principal(
        request=request,
        security_scopes=SecurityScopes(scopes=[]),
        access_token=None,
        decoded_access_token=None,
        api_key=secret.hex(),
        settings=settings,
        authenticators={"internal": MagicMock()},
        api_access_manager=api_access_manager,
    )
    assert resolved.api_key_scopes == ["read:status"]
