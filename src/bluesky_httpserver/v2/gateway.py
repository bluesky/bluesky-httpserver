"""The controller's view of the RE Worker, reached through RE Manager, which only relays."""

from __future__ import annotations

from typing import Protocol

from .catalog import Catalog


class WorkerGateway(Protocol):
    def catalog(self) -> Catalog:
        """The operations clients may submit now; may change when the worker's profile is reloaded."""
        ...
