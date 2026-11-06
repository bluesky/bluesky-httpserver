"""RE Manager as the worker gateway uses it: a relay that runs one item at a time on the worker."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .catalog import Catalog


class RelayUnreachable(Exception):
    """RE Manager did not answer; whether the request took effect is unknown."""

    def __init__(self, method: str):
        self.method = method
        super().__init__(f"RE Manager did not answer {method}")


class RelayRejected(Exception):
    """RE Manager answered and refused the request; nothing took effect."""

    def __init__(self, method: str, message: str):
        self.method = method
        self.message = message
        super().__init__(f"RE Manager refused {method}: {message}")


@dataclass(frozen=True)
class ManagerStatus:
    """What RE Manager reports about itself and the worker.

    Attributes
    ----------
    manager_state : str
        One of ``initializing``, ``idle``, ``paused``, ``creating_environment``, ``starting_queue``,
        ``executing_queue``, ``executing_task``, ``closing_environment`` and ``destroying_environment``.
    running_item_uid : str or None
        The item the worker is running; None if it runs none.
    worker_environment_exists : bool
        Whether the worker environment is open.
    plan_history_uid : str
        Changes whenever the history changes.
    plans_allowed_uid : str
        Changes whenever the allowed operations change.
    controller_fronted : bool
        Whether the manager refuses every queue, script and permission method (A3).
    """

    manager_state: str
    running_item_uid: str | None
    worker_environment_exists: bool
    plan_history_uid: str
    plans_allowed_uid: str
    controller_fronted: bool


@dataclass(frozen=True)
class AllowedOperations:
    """The operations the worker allows a user group to run.

    Attributes
    ----------
    uid : str
        The manager's ``plans_allowed_uid`` they were read at.
    catalog : Catalog
        The operations, with their plans and devices.
    """

    uid: str
    catalog: Catalog


@dataclass(frozen=True)
class HistoryEntry:
    """How one item the manager ran ended.

    Attributes
    ----------
    item_uid : str
        The manager's identifier for the item.
    attempt_uid : str or None
        The ``meta.attempt_uid`` the item was sent with; None for items the controller did not send.
    exit_status : str
        ``completed``, ``stopped``, ``failed``, ``aborted``, ``halted`` or ``unknown``.
    msg : str
        The manager's explanation; empty if it gave none.
    run_uids : tuple of str
        Bluesky runs the item produced.
    """

    item_uid: str
    attempt_uid: str | None
    exit_status: str
    msg: str
    run_uids: tuple[str, ...]


@dataclass(frozen=True)
class History:
    """The items the manager ran.

    Attributes
    ----------
    uid : str
        The manager's ``plan_history_uid`` it was read at.
    entries : tuple of HistoryEntry
        Oldest first.
    """

    uid: str
    entries: tuple[HistoryEntry, ...]


class ManagerRelay(Protocol):
    """RE Manager methods the worker gateway calls.

    Every request method raises `RelayUnreachable` when the manager does not answer, and `RelayRejected` when
    it answers with a refusal.
    """

    async def status(self) -> ManagerStatus:
        """Return the manager's status."""
        ...

    def status_updates(self) -> AsyncIterator[ManagerStatus]:
        """Yield the status the manager publishes when it changes, and periodically; never ends.

        Delivery is best effort: updates may be lost, and none arrive while the manager is down, so callers
        still poll `status`.
        """
        ...

    async def plans_allowed(self, user_group: str) -> AllowedOperations:
        """Return the operations ``user_group`` may run."""
        ...

    async def item_execute(self, item: Mapping[str, Any], *, user: str, user_group: str) -> str:
        """Run ``item`` on the worker now; needs an idle manager with an open environment.

        Returns
        -------
        str
            The ``item_uid`` the manager assigned.
        """
        ...

    async def history(self) -> History:
        """Return the items the manager ran."""
        ...

    async def environment_open(self) -> None:
        """Start opening the worker environment; needs an idle manager without one."""
        ...

    async def re_pause(self, option: Literal["deferred", "immediate"]) -> None:
        """Ask the running plan to pause, at its next checkpoint if ``deferred``."""
        ...

    async def re_abort(self) -> None:
        """Abort the paused plan; its history entry then ends ``aborted``."""
        ...
