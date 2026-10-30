from __future__ import annotations

from bluesky_httpserver.v2.catalog import Catalog, OperationDescriptor

DEMO_COUNT = OperationDescriptor(
    "demo.count",
    "1",
    {
        "type": "object",
        "properties": {
            "num": {"type": "integer", "minimum": 1},
            "detectors": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["num"],
        "additionalProperties": False,
    },
)


class FakeManager:
    """Stands in for the manager gateway: serves a catalog the test can replace."""

    def __init__(self, catalog: Catalog):
        self.current = catalog

    def catalog(self) -> Catalog:
        return self.current
