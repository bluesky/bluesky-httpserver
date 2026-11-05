from __future__ import annotations

import pytest

from bluesky_httpserver.v2.catalog import Catalog, OperationDescriptor

SCHEMA = {"type": "object", "properties": {"num": {"type": "integer"}}}


def test_duplicate_operation_is_refused():
    with pytest.raises(ValueError, match="duplicate operation"):
        Catalog(
            [
                OperationDescriptor("count", "1", SCHEMA, plan_name="count"),
                OperationDescriptor("count", "1", {}, plan_name="count"),
            ]
        )


def test_invalid_schema_is_refused():
    with pytest.raises(ValueError, match="invalid parameters schema"):
        OperationDescriptor("count", "1", {"type": 5}, plan_name="count")


def test_hashes_follow_content_not_order():
    count = OperationDescriptor("count", "1", SCHEMA, plan_name="count")
    loose = OperationDescriptor("count", "1", {"type": "object"}, plan_name="count")
    scan = OperationDescriptor("scan", "1", SCHEMA, plan_name="scan")
    assert count.sha256 != loose.sha256
    assert count.sha256 != OperationDescriptor("count", "1", SCHEMA, plan_name="other").sha256
    assert (
        count.sha256
        != OperationDescriptor("count", "1", SCHEMA, plan_name="count", devices={"det": {"a": "det1"}}).sha256
    )
    assert Catalog([count, scan]).sha256 == Catalog([scan, count]).sha256
    assert Catalog([count, scan]).sha256 != Catalog([loose, scan]).sha256
