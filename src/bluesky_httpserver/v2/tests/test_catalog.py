from __future__ import annotations

import pytest

from bluesky_httpserver.v2.catalog import Catalog, OperationDescriptor

SCHEMA = {"type": "object", "properties": {"num": {"type": "integer"}}}


def test_duplicate_operation_is_refused():
    with pytest.raises(ValueError, match="duplicate operation"):
        Catalog([OperationDescriptor("count", "1", SCHEMA), OperationDescriptor("count", "1", {})])


def test_invalid_schema_is_refused():
    with pytest.raises(ValueError, match="invalid parameters schema"):
        OperationDescriptor("count", "1", {"type": 5})


def test_hashes_follow_content_not_order():
    count = OperationDescriptor("count", "1", SCHEMA)
    loose = OperationDescriptor("count", "1", {"type": "object"})
    scan = OperationDescriptor("scan", "1", SCHEMA)
    assert count.sha256 != loose.sha256
    assert Catalog([count, scan]).sha256 == Catalog([scan, count]).sha256
    assert Catalog([count, scan]).sha256 != Catalog([loose, scan]).sha256
