"""The controller's view of the queueserver manager."""

from __future__ import annotations

from typing import Protocol

from .catalog import Catalog


class ManagerGateway(Protocol):
    def catalog(self) -> Catalog:
        """The operations clients may submit now; may change when the manager reloads its profile."""
        ...
