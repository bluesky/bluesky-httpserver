"""Settings of the v2 controller, read from a TOML file with `Settings.load`.

.. code-block:: toml

    store_path = "/var/lib/bluesky-httpserver/controller.sqlite"
    user_group = "primary"

    [identity]
    provider = "entra"
    tenant_id = "<directory GUID>"
    client_id = "<API application GUID>"

    [manager]
    control_address = "ipc:///run/bluesky/manager-control"
    info_address = "ipc:///run/bluesky/manager-info"

    [server]
    host = "127.0.0.1"
    port = 8000

    [gateway]
    grace_seconds = 60.0

The file holds no secret: access tokens are verified with the identity provider's public signing keys. Any
other OpenID Connect provider takes ``provider = "oidc"`` with ``issuer``, ``audience`` and ``jwks_uri``.
Unknown keys are refused.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identity import OidcSettings


class _Section(BaseModel):
    # Refusing unknown keys catches a misspelled key, and a secret that does not belong in the file.
    model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)


class OidcIdentity(_Section):
    """Any OpenID Connect provider."""

    provider: Literal["oidc"]
    issuer: str
    """Exact ``iss`` of accepted tokens."""
    audience: str
    """Value that must appear in ``aud``; identifies this API, not a client app."""
    jwks_uri: str
    """HTTPS URL of the issuer's signing keys (JWK Set)."""
    principal_claim: str | None = None
    """Claim whose string value identifies the caller; None takes the default: ``sub``."""
    roles_claim: str | None = None
    """Claim holding the caller's rights; None takes the default: ``scope``."""
    leeway_seconds: int | None = None
    """Clock skew tolerated on ``exp``, ``nbf`` and ``iat``, in seconds; None takes the default: 60."""

    @model_validator(mode="after")
    def _valid(self) -> OidcIdentity:
        self.oidc_settings()
        return self

    def oidc_settings(self) -> OidcSettings:
        """Settings of the token verifier."""
        return OidcSettings(**self.model_dump(exclude={"provider"}, exclude_none=True))


class EntraIdentity(_Section):
    """A single-tenant Microsoft Entra ID API registration; see `OidcSettings.entra`."""

    provider: Literal["entra"]
    tenant_id: str
    """Directory (tenant) ID, a GUID."""
    client_id: str
    """Application (client) ID of the API registration, a GUID."""
    leeway_seconds: int | None = None
    """Clock skew tolerated on ``exp``, ``nbf`` and ``iat``, in seconds; None takes the default: 60."""

    @model_validator(mode="after")
    def _valid(self) -> EntraIdentity:
        self.oidc_settings()
        return self

    def oidc_settings(self) -> OidcSettings:
        """Settings of the token verifier."""
        return OidcSettings.entra(**self.model_dump(exclude={"provider"}, exclude_none=True))


class GatewaySettings(_Section):
    """Pacing of the worker gateway."""

    grace_seconds: float = Field(default=60.0, gt=0)
    """How long a live attempt may go without evidence, because RE Manager is unreachable or neither runs its
    item nor has a history entry for it, before it becomes ``unknown``."""
    poll_seconds: float = Field(default=1.0, gt=0)
    """Longest wait between steps of the gateway."""
    environment_retry_seconds: float = Field(default=30.0, gt=0)
    """Shortest interval between requests to open the worker environment."""


class ManagerSettings(_Section):
    """How to reach RE Manager."""

    control_address: str
    """0MQ control address of RE Manager, an ``ipc://`` path: a controller-fronted manager binds no other."""
    info_address: str
    """0MQ address where RE Manager publishes its status, an ``ipc://`` path. Start RE Manager with
    ``--zmq-publish-info=ON``; otherwise the controller sees changes only when it polls."""
    request_timeout_seconds: float = Field(default=5.0, gt=0)
    """How long to wait for RE Manager to answer a request before treating it as unreachable."""

    @field_validator("control_address", "info_address")
    @classmethod
    def _ipc(cls, value: str) -> str:
        if not value.startswith("ipc://"):
            raise ValueError("must be an ipc:// address")
        return value


class ServerSettings(_Section):
    """Where the HTTP API listens."""

    host: str = "127.0.0.1"
    """Address to bind; the default accepts only local connections, e.g. from a TLS-terminating proxy."""
    port: int = Field(default=8000, ge=0, le=65535)
    """Port to bind; 0 picks a free port."""


class Settings(_Section):
    """Settings of the v2 controller."""

    store_path: Path
    """Database file of the store, an absolute path; its parent directory must exist."""
    user_group: str = Field(min_length=1)
    """Manager user group that operations run as; its allowed operations are the catalog."""
    identity: Annotated[OidcIdentity | EntraIdentity, Field(discriminator="provider")]
    """Identity provider that issues access tokens."""
    manager: ManagerSettings
    """How to reach RE Manager."""
    server: ServerSettings = Field(default_factory=ServerSettings)
    """Where the HTTP API listens."""
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    """Pacing of the worker gateway."""
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    """Longest silence on an event stream before a heartbeat comment is sent."""

    @field_validator("store_path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        # A relative path would open a different, empty store depending on the working directory.
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Settings:
        """Read settings from the TOML file at ``path``.

        Parameters
        ----------
        path : str or os.PathLike
            The settings file.

        Returns
        -------
        Settings

        Raises
        ------
        OSError
            The file cannot be read.
        tomllib.TOMLDecodeError
            The file is not valid TOML.
        pydantic.ValidationError
            The settings are invalid; each error locates its key.
        """
        with open(path, "rb") as file:
            return cls.model_validate(tomllib.load(file))
