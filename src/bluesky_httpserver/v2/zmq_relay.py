"""`ManagerRelay` over RE Manager's 0MQ control socket and its info (``QS_Info``) publisher."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
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

_INFO_TOPIC = b"QS_Info"


def _manager_status(status: Mapping[str, Any]) -> ManagerStatus:
    return ManagerStatus(
        manager_state=status["manager_state"],
        running_item_uid=status["running_item_uid"],
        worker_environment_exists=status["worker_environment_exists"],
        plan_history_uid=status["plan_history_uid"],
        plans_allowed_uid=status["plans_allowed_uid"],
        run_list_uid=status["run_list_uid"],
        # A manager that does not report the mode predates it, so it is not controller-fronted.
        controller_fronted=status.get("controller_fronted", False),
    )


class ZmqManagerRelay:
    """RE Manager reached through its 0MQ control socket and info publisher, with JSON encoding.

    Each request uses a new socket, so a request that timed out leaves nothing behind and the next request
    finds the manager as soon as it answers again. Call `close` when done.

    Parameters
    ----------
    control_address : str
        RE Manager's control address, e.g. ``ipc:///run/bluesky/manager-control``.
    info_address : str
        Address where RE Manager publishes its status; it does so only if started with
        ``--zmq-publish-info=ON``.
    timeout_seconds : float
        How long to wait for an answer before raising `RelayUnreachable`.
    """

    def __init__(self, control_address: str, info_address: str, *, timeout_seconds: float):
        self._control_address = control_address
        self._info_address = info_address
        self._timeout_ms = int(timeout_seconds * 1000)
        self._context = zmq.asyncio.Context()

    def close(self) -> None:
        """Release the 0MQ context; the relay cannot be used afterwards."""
        self._context.destroy(linger=0)

    async def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        socket = self._context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        try:
            socket.connect(self._control_address)
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
        return _manager_status(await self._request("status", {}))

    async def status_updates(self) -> AsyncIterator[ManagerStatus]:
        # 0MQ reconnects the subscription by itself when RE Manager restarts.
        socket = self._context.socket(zmq.SUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.SUBSCRIBE, _INFO_TOPIC)
        try:
            socket.connect(self._info_address)
            while True:
                _, payload = await socket.recv_multipart()
                # Info messages are {"time": ..., "msg": {<kind>: ...}}; only "status" ones matter here.
                status = json.loads(payload)["msg"].get("status")
                if status is not None:
                    yield _manager_status(status)
        finally:
            socket.close()

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

    async def runs(self) -> tuple[str, ...]:
        # Without an option, 're_runs' lists every run of the running item, open or closed.
        reply = await self._call("re_runs")
        return tuple(run["uid"] for run in reply["run_list"])

    async def environment_open(self) -> None:
        await self._call("environment_open")

    async def re_pause(self, option: Literal["deferred", "immediate"]) -> None:
        await self._call("re_pause", {"option": option})

    async def re_abort(self) -> None:
        await self._call("re_abort")
