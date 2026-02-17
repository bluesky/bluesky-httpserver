"""Tests for OIDC Authenticator functionality."""

import time
from typing import Any, Tuple

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import ExpiredSignatureError, jwt
from jose.backends import RSAKey
from respx import MockRouter

from bluesky_httpserver.authenticators import OIDCAuthenticator, ProxiedOIDCAuthenticator


@pytest.fixture
def oidc_well_known_url(oidc_base_url: str) -> str:
    return f"{oidc_base_url}.well-known/openid-configuration"


@pytest.fixture
def keys() -> Tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    """Generate RSA key pair for testing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return (private_key, public_key)


@pytest.fixture
def json_web_keyset(keys: Tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]) -> list[dict[str, Any]]:
    """Create a JSON Web Key Set from the test keys."""
    _, public_key = keys
    return [RSAKey(key=public_key, algorithm="RS256").to_dict()]


@pytest.fixture
def mock_oidc_server(
    respx_mock: MockRouter,
    oidc_well_known_url: str,
    well_known_response: dict[str, Any],
    json_web_keyset: list[dict[str, Any]],
) -> MockRouter:
    """Set up mock OIDC server endpoints."""
    respx_mock.get(oidc_well_known_url).mock(
        return_value=httpx.Response(httpx.codes.OK, json=well_known_response)
    )
    respx_mock.get(well_known_response["jwks_uri"]).mock(
        return_value=httpx.Response(httpx.codes.OK, json={"keys": json_web_keyset})
    )
    return respx_mock


def create_token(issued: bool, expired: bool) -> dict[str, Any]:
    """Create a test JWT token."""
    now = time.time()
    return {
        "aud": "test_client",
        "exp": (now - 1500) if expired else (now + 1500),
        "iat": (now - 1500) if issued else (now + 1500),
        "iss": "https://example.com/realms/example",
        "sub": "test_user",
    }


def encrypt_token(token: dict[str, Any], private_key: rsa.RSAPrivateKey) -> str:
    """Encrypt a token with the test private key."""
    return jwt.encode(
        token,
        key=private_key,
        algorithm="RS256",
        headers={"kid": "test_key"},
    )


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
class TestOIDCAuthenticator:
    """Tests for OIDCAuthenticator class."""

    def test_oidc_authenticator_caching(
        self,
        mock_oidc_server: MockRouter,
        oidc_well_known_url: str,
        well_known_response: dict[str, Any],
        json_web_keyset: list[dict[str, Any]],
    ):
        """Test that OIDC configuration is cached after first fetch."""
        authenticator = OIDCAuthenticator(
            audience="test_client",
            client_id="test_client",
            client_secret="secret",
            well_known_uri=oidc_well_known_url,
        )

        # Access multiple properties to ensure caching works
        assert authenticator.client_id == "test_client"
        assert authenticator.authorization_endpoint == well_known_response["authorization_endpoint"]
        assert (
            authenticator.id_token_signing_alg_values_supported
            == well_known_response["id_token_signing_alg_values_supported"]
        )
        assert authenticator.issuer == well_known_response["issuer"]
        assert authenticator.jwks_uri == well_known_response["jwks_uri"]
        assert authenticator.token_endpoint == well_known_response["token_endpoint"]
        assert (
            authenticator.device_authorization_endpoint
            == well_known_response["device_authorization_endpoint"]
        )
        assert authenticator.end_session_endpoint == well_known_response["end_session_endpoint"]

        # Should only call well-known endpoint once due to caching
        assert len(mock_oidc_server.calls) == 1
        call_request = mock_oidc_server.calls[0].request
        assert call_request.method == "GET"
        assert call_request.url == oidc_well_known_url

        # Keys should also be cached
        assert authenticator.keys() == json_web_keyset
        assert len(mock_oidc_server.calls) == 2  # Now also fetched JWKS

        # Multiple calls should still be cached
        for _ in range(5):
            assert authenticator.keys() == json_web_keyset
        assert len(mock_oidc_server.calls) == 2  # No new calls

    @pytest.mark.parametrize("issued", [True, False])
    @pytest.mark.parametrize("expired", [True, False])
    def test_oidc_token_decoding(
        self,
        mock_oidc_server: MockRouter,
        oidc_well_known_url: str,
        issued: bool,
        expired: bool,
        keys: Tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
    ):
        """Test token decoding with various validity scenarios."""
        private_key, _ = keys
        authenticator = OIDCAuthenticator(
            audience="test_client",
            client_id="test_client",
            client_secret="secret",
            well_known_uri=oidc_well_known_url,
        )

        token = create_token(issued, expired)
        encrypted = encrypt_token(token, private_key)

        if not expired:
            # Non-expired tokens should decode successfully
            decoded = authenticator.decode_token(encrypted)
            assert decoded["sub"] == "test_user"
            assert decoded["aud"] == "test_client"
        else:
            # Expired tokens should raise an error
            with pytest.raises(ExpiredSignatureError):
                authenticator.decode_token(encrypted)

    def test_oidc_authenticator_properties(
        self,
        mock_oidc_server: MockRouter,
        oidc_well_known_url: str,
        well_known_response: dict[str, Any],
    ):
        """Test that all authenticator properties are correctly set."""
        authenticator = OIDCAuthenticator(
            audience="my_audience",
            client_id="my_client_id",
            client_secret="my_secret",
            well_known_uri=oidc_well_known_url,
            confirmation_message="Logged in as {id}",
            redirect_on_success="https://app.example.com/success",
            redirect_on_failure="https://app.example.com/failure",
        )

        assert authenticator.client_id == "my_client_id"
        assert authenticator.confirmation_message == "Logged in as {id}"
        assert authenticator.redirect_on_success == "https://app.example.com/success"
        assert authenticator.redirect_on_failure == "https://app.example.com/failure"


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
class TestProxiedOIDCAuthenticator:
    """Tests for ProxiedOIDCAuthenticator class."""

    @pytest.mark.asyncio
    async def test_proxied_oidc_oauth2_schema(
        self,
        mock_oidc_server: MockRouter,
        oidc_well_known_url: str,
    ):
        """Test that ProxiedOIDCAuthenticator extracts bearer token correctly."""
        authenticator = ProxiedOIDCAuthenticator(
            audience="test_client",
            client_id="test_client",
            well_known_uri=oidc_well_known_url,
            device_flow_client_id="test_cli_client",
        )

        # Create a mock request with Authorization header
        test_request = httpx.Request(
            "GET",
            "http://example.com/api/test",
            headers={"Authorization": "Bearer TEST_TOKEN"},
        )

        # The oauth2_schema should extract the bearer token
        token = await authenticator.oauth2_schema(test_request)
        assert token == "TEST_TOKEN"

    def test_proxied_oidc_with_scopes(
        self,
        mock_oidc_server: MockRouter,
        oidc_well_known_url: str,
    ):
        """Test ProxiedOIDCAuthenticator with custom scopes."""
        authenticator = ProxiedOIDCAuthenticator(
            audience="test_client",
            client_id="test_client",
            well_known_uri=oidc_well_known_url,
            device_flow_client_id="test_cli_client",
            scopes=["openid", "profile", "email"],
        )

        assert authenticator.scopes == ["openid", "profile", "email"]
        assert authenticator.device_flow_client_id == "test_cli_client"
