from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bluesky_httpserver.v2.store import OperationRecord, Store


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "controller.sqlite"


@pytest_asyncio.fixture
async def store(db_path: Path) -> AsyncIterator[Store]:
    s = await Store.open(db_path)
    yield s
    await s.close()


async def submit(
    store: Store,
    revision: int,
    operation_id: str = "demo.count",
    parameters: dict[str, Any] | None = None,
) -> tuple[OperationRecord, int]:
    return await store.write(
        lambda tx: tx.submit(
            principal="alice",
            expected_revision=revision,
            operation_id=operation_id,
            operation_version="1",
            parameters=parameters or {"num": 1},
            descriptor_sha256="0" * 64,
        )
    )
