import asyncio
import os
import time
from typing import Any, Tuple

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import ExpiredSignatureError, jwt
from jose.backends import RSAKey
from respx import MockRouter
from starlette.datastructures import URL, QueryParams

from ..authenticators import LDAPAuthenticator, OIDCAuthenticator, ProxiedOIDCAuthenticator, UserSessionState

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


@pytest.fixture
def oidc_well_known_url(oidc_base_url: str) -> str:
    return f"{oidc_base_url}.well-known/openid-configuration"


@pytest.fixture
def keys() -> Tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return (private_key, public_key)


@pytest.fixture
def json_web_keyset(keys: Tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]) -> list[dict[str, Any]]:
    _, public_key = keys
    return [RSAKey(key=public_key, algorithm="RS256").to_dict()]


@pytest.fixture
def mock_oidc_server(
    respx_mock: MockRouter,
    oidc_well_known_url: str,
    well_known_response: dict[str, Any],
    json_web_keyset: list[dict[str, Any]],
) -> MockRouter:
    respx_mock.get(oidc_well_known_url).mock(return_value=httpx.Response(httpx.codes.OK, json=well_known_response))
    respx_mock.get(well_known_response["jwks_uri"]).mock(
        return_value=httpx.Response(httpx.codes.OK, json={"keys": json_web_keyset})
    )
    return respx_mock


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
