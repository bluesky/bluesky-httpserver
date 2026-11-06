from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal

from bluesky_httpserver.v2.catalog import Catalog, OperationDescriptor
from bluesky_httpserver.v2.relay import (
    AllowedOperations,
    History,
    HistoryEntry,
    ManagerStatus,
    RelayRejected,
    RelayUnreachable,
)

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
    plan_name="count",
    devices={"detectors": {"primary": "det1", "secondary": "det2"}},
)


class FakeRelay:
    """Stands in for RE Manager: the test sets its state, injects faults and inspects the calls it received."""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.state = ManagerStatus("idle", None, True, "history-0", "allowed-0", True)
        self.history_entries: list[HistoryEntry] = []
        self.reachable = True
        self.execute_fault: Literal["reject", "lost", "reply_lost"] | None = None
        self.calls: list[str] = []
        self.executed: list[tuple[dict[str, Any], str, str]] = []
        self._running_attempt_uid: str | None = None
        self._published: asyncio.Queue[ManagerStatus] = asyncio.Queue()

    def _call(self, method: str, record: str | None = None) -> None:
        self.calls.append(record or method)
        if not self.reachable:
            raise RelayUnreachable(method)

    async def status(self) -> ManagerStatus:
        self._call("status")
        return self.state

    async def status_updates(self) -> AsyncIterator[ManagerStatus]:
        while True:
            yield await self._published.get()

    def publish(self) -> None:
        """Publish the current state, as RE Manager does on its info socket."""
        self._published.put_nowait(self.state)

    async def plans_allowed(self, user_group: str) -> AllowedOperations:
        self._call("plans_allowed")
        return AllowedOperations(self.state.plans_allowed_uid, self.catalog)

    async def item_execute(self, item: Mapping[str, Any], *, user: str, user_group: str) -> str:
        self._call("item_execute")
        if self.execute_fault == "lost":
            raise RelayUnreachable("item_execute")
        if self.execute_fault == "reject":
            raise RelayRejected("item_execute", "plan validation failed")
        if self.state.manager_state != "idle" or not self.state.worker_environment_exists:
            raise RelayRejected("item_execute", "RE Manager is not idle with an open environment")
        self.executed.append((dict(item), user, user_group))
        item_uid = f"item-{len(self.executed)}"
        self._running_attempt_uid = item["meta"]["attempt_uid"]
        self.state = dataclasses.replace(self.state, manager_state="executing_queue", running_item_uid=item_uid)
        if self.execute_fault == "reply_lost":
            raise RelayUnreachable("item_execute")
        return item_uid

    def finish_running(self, exit_status: str, msg: str = "", run_uids: Sequence[str] = ()) -> None:
        """End the running item as the manager does: record it in the history, then go idle."""
        item_uid = self.state.running_item_uid
        assert item_uid is not None, "no item is running"
        self.history_entries.append(
            HistoryEntry(item_uid, self._running_attempt_uid, exit_status, msg, tuple(run_uids))
        )
        self._running_attempt_uid = None
        self.state = dataclasses.replace(
            self.state,
            manager_state="idle",
            running_item_uid=None,
            plan_history_uid=f"history-{len(self.history_entries)}",
        )

    async def history(self) -> History:
        self._call("history")
        return History(self.state.plan_history_uid, tuple(self.history_entries))

    async def environment_open(self) -> None:
        self._call("environment_open")
        if self.state.worker_environment_exists:
            raise RelayRejected("environment_open", "RE Worker environment already exists")
        self.state = dataclasses.replace(self.state, worker_environment_exists=True)

    async def re_pause(self, option: Literal["deferred", "immediate"]) -> None:
        self._call("re_pause", f"re_pause {option}")
        self.state = dataclasses.replace(self.state, manager_state="paused")

    async def re_abort(self) -> None:
        self._call("re_abort")
        if self.state.manager_state != "paused":
            raise RelayRejected("re_abort", "RE Manager is not paused")
        self.finish_running("aborted")
