"""`ManagerRelay` over RE Manager's 0MQ control socket."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import zmq
import zmq.asyncio

from .catalog import Catalog, OperationDescriptor
from .relay import (
    AllowedOperations,
    History,
    HistoryEntry,
    ManagerStatus,
    RelayRejected,
    RelayUnreachable,
)


class ZmqManagerRelay:
    """RE Manager reached through its 0MQ control socket, with JSON encoding.

    Each request uses a new socket, so a request that timed out leaves nothing behind and the next request
    finds the manager as soon as it answers again. Call `close` when done.

    Parameters
    ----------
    address : str
        RE Manager's control address, e.g. ``ipc:///run/bluesky/manager-control``.
    timeout_seconds : float
        How long to wait for an answer before raising `RelayUnreachable`.
    """

    def __init__(self, address: str, *, timeout_seconds: float):
        self._address = address
        self._timeout_ms = int(timeout_seconds * 1000)
        self._context = zmq.asyncio.Context()

    def close(self) -> None:
        """Release the 0MQ context; the relay cannot be used afterwards."""
        self._context.destroy(linger=0)

    async def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        socket = self._context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        try:
            socket.connect(self._address)
            await socket.send_json({"method": method, "params": dict(params)})
            if not await socket.poll(self._timeout_ms):
                raise RelayUnreachable(method)
            return await socket.recv_json()
        finally:
            socket.close()

    async def _call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        reply = await self._request(method, params or {})
        if not reply["success"]:
            raise RelayRejected(method, reply["msg"])
        return reply

    async def status(self) -> ManagerStatus:
        reply = await self._request("status", {})
        return ManagerStatus(
            manager_state=reply["manager_state"],
            running_item_uid=reply["running_item_uid"],
            worker_environment_exists=reply["worker_environment_exists"],
            plan_history_uid=reply["plan_history_uid"],
            plans_allowed_uid=reply["plans_allowed_uid"],
            # A manager that does not report the mode predates it, so it is not controller-fronted.
            controller_fronted=reply.get("controller_fronted", False),
        )

    async def plans_allowed(self, user_group: str) -> AllowedOperations:
        """Return the operations ``user_group`` may run: its allowed plans that are operations.

        Raises
        ------
        RelayRejected
            RE Manager refused the request, or its operations do not form a catalog.
        """
        reply = await self._call("plans_allowed", {"user_group": user_group})
        try:
            catalog = Catalog(
                OperationDescriptor(
                    operation_id=operation["id"],
                    operation_version=operation["version"],
                    parameters_schema=operation["request_schema"],
                    plan_name=name,
                    devices=operation["devices"],
                )
                for name, plan in reply["plans_allowed"].items()
                if (operation := plan.get("operation")) is not None
            )
        except ValueError as error:
            raise RelayRejected(
                "plans_allowed", f"the allowed operations are not a valid catalog: {error}"
            ) from None
        return AllowedOperations(reply["plans_allowed_uid"], catalog)

    async def item_execute(self, item: Mapping[str, Any], *, user: str, user_group: str) -> str:
        reply = await self._call(
            "queue_item_execute", {"item": dict(item), "user": user, "user_group": user_group}
        )
        return reply["item"]["item_uid"]

    async def history(self) -> History:
        reply = await self._call("history_get")
        entries = []
        for item in reply["items"]:
            # RE Manager also accepts a list of dicts as metadata; the controller sends a dict.
            meta = item.get("meta")
            result = item["result"]
            entries.append(
                HistoryEntry(
                    item_uid=item["item_uid"],
                    attempt_uid=meta.get("attempt_uid") if isinstance(meta, dict) else None,
                    exit_status=result["exit_status"],
                    msg=result["msg"],
                    run_uids=tuple(result["run_uids"]),
                )
            )
        return History(reply["plan_history_uid"], tuple(entries))

    async def environment_open(self) -> None:
        await self._call("environment_open")

    async def re_pause(self, option: Literal["deferred", "immediate"]) -> None:
        await self._call("re_pause", {"option": option})

    async def re_abort(self) -> None:
        await self._call("re_abort")
