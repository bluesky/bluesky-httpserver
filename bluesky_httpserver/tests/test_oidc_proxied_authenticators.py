"""Tests for OIDC Authenticator functionality."""

import httpx
import pytest
from respx import MockRouter

from bluesky_httpserver.authenticators import ProxiedOIDCAuthenticator


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
