from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from bluesky_httpserver.v2.identity import OidcSettings
from bluesky_httpserver.v2.settings import OidcIdentity, Settings

from .test_oidc import AUDIENCE, CLIENT, ISSUER, JWKS_URI, TENANT

VALID: dict[str, Any] = {
    "store_path": "/var/lib/bluesky-httpserver/controller.sqlite",
    "user_group": "primary",
    "identity": {"provider": "oidc", "issuer": ISSUER, "audience": AUDIENCE, "jwks_uri": JWKS_URI},
    "manager": {
        "control_address": "ipc:///run/bluesky/manager-control",
        "info_address": "ipc:///run/bluesky/manager-info",
    },
}


def test_entra_preset_from_toml(tmp_path):
    path = tmp_path / "controller.toml"
    path.write_text(
        f"""\
store_path = "{tmp_path}/c.sqlite"
user_group = "primary"

[identity]
provider = "entra"
tenant_id = "{TENANT}"
client_id = "{CLIENT}"

[manager]
control_address = "ipc:///run/bluesky/manager-control"
info_address = "ipc:///run/bluesky/manager-info"

[gateway]
grace_seconds = 60.0
"""
    )

    settings = Settings.load(path)

    assert settings.identity.oidc_settings() == OidcSettings.entra(tenant_id=TENANT, client_id=CLIENT)
    assert settings.gateway.grace_seconds == 60.0
    assert (settings.server.host, settings.server.port) == ("127.0.0.1", 8000)


def test_generic_provider_defaults_come_from_oidc_settings():
    identity = OidcIdentity.model_validate(
        {"provider": "oidc", "issuer": ISSUER, "audience": AUDIENCE, "jwks_uri": JWKS_URI}
    )
    assert identity.oidc_settings() == OidcSettings(issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI)


def _jwks_over_http(settings: dict[str, Any]) -> None:
    settings["identity"]["jwks_uri"] = "http://idp.example/realms/facility/keys"


def _signing_secret(settings: dict[str, Any]) -> None:
    settings["signing_secret"] = "x"


def _relative_store(settings: dict[str, Any]) -> None:
    settings["store_path"] = "relative.sqlite"


def _zero_poll(settings: dict[str, Any]) -> None:
    settings["gateway"] = {"poll_seconds": 0}


def _tcp_manager(settings: dict[str, Any]) -> None:
    settings["manager"]["control_address"] = "tcp://localhost:60615"


def _abstract_socket(settings: dict[str, Any]) -> None:
    settings["manager"]["info_address"] = "ipc://@bluesky-manager-info"


def _relative_socket(settings: dict[str, Any]) -> None:
    settings["manager"]["control_address"] = "ipc://run/manager-control"


@pytest.mark.parametrize(
    ("change", "loc"),
    [
        (_jwks_over_http, ("identity",)),
        (_signing_secret, ("signing_secret",)),
        (_relative_store, ("store_path",)),
        (_zero_poll, ("gateway", "poll_seconds")),
        (_tcp_manager, ("manager", "control_address")),
        (_abstract_socket, ("manager", "info_address")),
        (_relative_socket, ("manager", "control_address")),
    ],
)
def test_invalid_settings_are_refused(change, loc):
    settings = copy.deepcopy(VALID)
    Settings.model_validate(settings)
    change(settings)

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(settings)
    assert exc_info.value.errors()[0]["loc"][: len(loc)] == loc
