"""The controller's view of the RE Worker, reached through RE Manager, which only relays."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .catalog import Catalog, UnknownOperation
from .relay import HistoryEntry, ManagerRelay, ManagerStatus, RelayRejected, RelayUnreachable
from .store import AttemptRecord, AttemptState, OperationRecord, ReadTransaction, Store, Transaction

logger = logging.getLogger(__name__)

WorkerReachability = Literal["reachable", "unreachable", "not_controller_fronted"]
EnvironmentPresence = Literal["present", "absent", "unknown"]
CatalogState = Literal["compatible", "incompatible", "unavailable"]
DispatchState = Literal["open", "blocked"]
PauseOption = Literal["deferred", "immediate"]

# ``failed`` history messages that do not prove the plan failed. The first is the A2 diagnostic of a dead
# worker; the others are manager paths where the worker's real outcome is unknown, including a run request
# that timed out after the worker may have started the plan.
_UNPROVEN_FAILURES = frozenset(
    {
        "RE Worker process terminated unexpectedly",
        "RE Worker environment was destroyed",
        "Internal RE Manager error occurred. Report the error to the development team",
        "Timeout occurred while processing the request",
    }
)


class CatalogUnavailable(Exception):
    """The operation catalog has not been loaded from the worker yet."""

    def __init__(self) -> None:
        super().__init__("the operation catalog has not been loaded from the worker yet")


class NoFenceEvidence(Exception):
    """RE Manager does not prove that the worker runs nothing."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"the worker is not provably stopped: {reason}")


@dataclass(frozen=True)
class Readiness:
    """Whether the controller can run work now, and if not, why.

    Attributes
    ----------
    worker : str
        ``reachable``; ``unreachable`` if RE Manager does not answer; ``not_controller_fronted`` if it answers
        but still accepts queue control from others.
    environment : str
        ``present`` or ``absent``: whether the worker environment is open; ``unknown`` while RE Manager is
        unreachable.
    catalog : str
        ``compatible``; ``incompatible`` if a queued or live operation was validated against a descriptor the
        worker no longer offers; ``unavailable`` before the catalog is first loaded.
    dispatch : str
        ``open``, or ``blocked`` until recovery is acknowledged.
    """

    worker: WorkerReachability
    environment: EnvironmentPresence
    catalog: CatalogState
    dispatch: DispatchState

    @property
    def ready(self) -> bool:
        """Whether every field allows work to run."""
        return (self.worker, self.environment, self.catalog, self.dispatch) == (
            "reachable",
            "present",
            "compatible",
            "open",
        )


def _outcome(entry: HistoryEntry) -> tuple[AttemptState, str | None]:
    """Return the attempt state a history entry proves, and its diagnostic."""
    if entry.exit_status in ("completed", "stopped"):
        state = AttemptState.SUCCEEDED
    elif entry.exit_status == "failed" and entry.msg not in _UNPROVEN_FAILURES:
        state = AttemptState.FAILED
    elif entry.exit_status in ("aborted", "halted"):
        state = AttemptState.ABORTED
    else:
        diagnostic = f"RE Manager recorded exit status '{entry.exit_status}'"
        return AttemptState.UNKNOWN, f"{diagnostic}: {entry.msg}" if entry.msg else diagnostic
    return state, entry.msg or None


def _incompatible(
    catalog: Catalog, descriptors: Iterable[tuple[str, str, str]]
) -> tuple[tuple[str, str, str], ...]:
    """Return the ``(operation_id, operation_version, descriptor_sha256)`` that ``catalog`` does not offer."""
    incompatible: list[tuple[str, str, str]] = []
    for operation_id, operation_version, sha256 in descriptors:
        try:
            offered = catalog.descriptor(operation_id, operation_version).sha256 == sha256
        except UnknownOperation:
            offered = False
        if not offered:
            incompatible.append((operation_id, operation_version, sha256))
    return tuple(incompatible)


class WorkerGateway:
    """Runs the controller's queue on the RE Worker through RE Manager, and fails closed when an outcome
    cannot be proven.

    Each `step` observes the manager, records the outcomes its history proves, then either watches the live
    attempt, opens the worker environment or dispatches the next operation. An attempt whose outcome cannot
    be proven becomes ``unknown`` and blocks dispatch until an operator acknowledges recovery; no attempt is
    ever sent twice.

    Parameters
    ----------
    store : Store
        The controller queue.
    relay : ManagerRelay
        RE Manager.
    user_group : str
        Manager user group that operations run as; its allowed operations are the catalog.
    grace_seconds : float, optional
        How long a live attempt may go without evidence, because RE Manager is unreachable or neither runs its
        item nor has a history entry for it, before it becomes ``unknown``.
    poll_seconds : float, optional
        Longest wait between steps in `run`.
    environment_retry_seconds : float, optional
        Shortest interval between requests to open the worker environment.
    monotonic : callable, optional
        Monotonic clock, in seconds.

    Raises
    ------
    ValueError
        ``poll_seconds`` is not positive.
    """

    def __init__(
        self,
        *,
        store: Store,
        relay: ManagerRelay,
        user_group: str,
        grace_seconds: float = 60.0,
        poll_seconds: float = 1.0,
        environment_retry_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._store = store
        self._relay = relay
        self._user_group = user_group
        self._grace = grace_seconds
        self._poll = poll_seconds
        self._environment_retry = environment_retry_seconds
        self._monotonic = monotonic
        self._status: ManagerStatus | None = None
        self._unreachable_since: float | None = None
        self._catalog: Catalog | None = None
        self._catalog_uid: str | None = None
        self._history_uid: str | None = None
        self._by_item: dict[str, HistoryEntry] = {}
        self._by_attempt: dict[str, HistoryEntry] = {}
        self._incompatible: tuple[tuple[str, str, str], ...] = ()
        self._blocked = False
        # Attempt uid -> monotonic time at which waiting for evidence of its outcome ends.
        self._deadlines: dict[str, float] = {}
        # The pause sent for an attempt, and the attempt an abort was sent for, so each is sent once.
        self._pause_sent: tuple[str, PauseOption] | None = None
        self._abort_sent: str | None = None
        self._environment_opened_at: float | None = None
        # The live attempt's runs as RE Manager last reported them, and the run list uid they were read at.
        self._runs: tuple[str, tuple[str, ...]] | None = None
        self._run_list_uid: str | None = None
        self._wakeup = asyncio.Event()

    def catalog(self) -> Catalog:
        """Return the operations clients may submit now.

        The last catalog loaded keeps being served while RE Manager is unreachable.

        Raises
        ------
        CatalogUnavailable
            The catalog has not been loaded yet.
        """
        if self._catalog is None:
            raise CatalogUnavailable()
        return self._catalog

    def runs(self, attempt_uid: str) -> tuple[str, ...] | None:
        """Return the uids of the Bluesky runs the running attempt ``attempt_uid`` has opened so far.

        Returns None for any other attempt, and until RE Manager has reported the runs.
        """
        if self._runs is None or self._runs[0] != attempt_uid:
            return None
        return self._runs[1]

    def fence_evidence(self) -> dict[str, Any]:
        """Return RE Manager's last status, if it proves the worker runs nothing.

        A status up to one poll old suffices: while dispatch is blocked nothing can start, so a stale idle is
        still idle.

        Raises
        ------
        NoFenceEvidence
            RE Manager is unreachable, not controller-fronted, or not idle.
        """
        status = self._status
        if status is None or self._unreachable_since is not None:
            raise NoFenceEvidence("RE Manager is unreachable")
        if not status.controller_fronted:
            raise NoFenceEvidence("RE Manager is not in controller-fronted mode")
        if status.manager_state != "idle" or status.running_item_uid is not None:
            reason = f"RE Manager is {status.manager_state}"
            if status.running_item_uid is not None:
                reason += f" and running item {status.running_item_uid}"
            raise NoFenceEvidence(reason)
        return {
            "manager_state": "idle",
            "running_item_uid": None,
            "worker_environment_exists": status.worker_environment_exists,
        }

    def readiness(self) -> Readiness:
        """Return whether the controller can run work now, as of the last step."""
        status = self._status
        worker: WorkerReachability
        environment: EnvironmentPresence
        if status is None or self._unreachable_since is not None:
            worker, environment = "unreachable", "unknown"
        else:
            worker = "reachable" if status.controller_fronted else "not_controller_fronted"
            environment = "present" if status.worker_environment_exists else "absent"
        catalog: CatalogState
        if self._catalog is None:
            catalog = "unavailable"
        elif self._incompatible:
            catalog = "incompatible"
        else:
            catalog = "compatible"
        return Readiness(worker, environment, catalog, "blocked" if self._blocked else "open")

    async def run(self) -> None:
        """Step until cancelled.

        After each step, wait ``poll_seconds``, or until the store commits an event or RE Manager publishes
        a status that differs from the last one observed, whichever comes first. The caller runs this for as
        long as the app serves.
        """
        async with asyncio.TaskGroup() as group:
            group.create_task(self._wake_on_events())
            group.create_task(self._wake_on_status())
            while True:
                self._wakeup.clear()
                await self.step()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self._poll):
                        await self._wakeup.wait()

    async def step(self) -> None:
        """Observe RE Manager once, record what it proves, then act on the live attempt or the queue.

        Must not run concurrently with itself.
        """
        now = self._monotonic()

        async def read(
            tx: ReadTransaction,
        ) -> tuple[AttemptRecord | None, list[AttemptRecord], bool, list[tuple[str, str, str]]]:
            blocked = await tx.blocked_execution()
            return (
                await tx.live_attempt(),
                await tx.unknown_attempts(),
                blocked is not None,
                await tx.active_descriptors(),
            )

        live, unknown, self._blocked, descriptors = await self._store.read(read)
        self._deadlines = {
            uid: deadline
            for uid, deadline in self._deadlines.items()
            if live is not None and uid == live.attempt_uid
        }

        try:
            status = await self._relay.status()
        except RelayUnreachable:
            since = self._note_unreachable(now)
            if live is not None and now - since >= self._grace:
                await self._mark_unknown(
                    live, f"RE Manager was unreachable for {self._grace:g} s while the attempt was live"
                )
            return
        previous, self._status, self._unreachable_since = self._status, status, None

        if not status.controller_fronted:
            if previous is None or previous.controller_fronted:
                logger.error("RE Manager is not in controller-fronted mode; the controller will not use it")
            return

        try:
            await self._refresh_catalog(status)
            await self._refresh_history(status)
            if self._catalog is not None:
                incompatible = _incompatible(self._catalog, descriptors)
                if incompatible and incompatible != self._incompatible:
                    logger.warning(
                        "operations were validated against descriptors the worker no longer offers;"
                        " nothing will be claimed until they are replaced or cancelled: %s",
                        ", ".join(f"{i} version {v} ({sha256})" for i, v, sha256 in incompatible),
                    )
                self._incompatible = incompatible
            for attempt in unknown:
                await self._resolve_late(attempt)
            if live is not None:
                await self._observe(live, status, now)
            elif self._blocked:
                pass  # never reopen the environment or dispatch while an outcome is unproven
            elif not status.worker_environment_exists:
                await self._open_environment(status, now)
            else:
                await self._dispatch(status, now)
        except RelayUnreachable:
            self._note_unreachable(now)

    async def _wake_on_events(self) -> None:
        cursor = 0
        while True:
            cursor = await self._store.wait_for_event(cursor)
            self._wakeup.set()

    async def _wake_on_status(self) -> None:
        async for status in self._relay.status_updates():
            # RE Manager republishes an unchanged status every second; only a change needs a step.
            if status != self._status:
                self._wakeup.set()

    def _note_unreachable(self, now: float) -> float:
        """Start the unreachability clock unless it runs; return when RE Manager became unreachable."""
        if self._unreachable_since is None:
            self._unreachable_since = now
            logger.warning("RE Manager unreachable")
        return self._unreachable_since

    async def _refresh_catalog(self, status: ManagerStatus) -> None:
        if self._catalog is not None and status.plans_allowed_uid == self._catalog_uid:
            return
        try:
            allowed = await self._relay.plans_allowed(self._user_group)
        except RelayRejected as error:
            logger.error("RE Manager refused the allowed operations of %s: %s", self._user_group, error.message)
            return
        self._catalog, self._catalog_uid = allowed.catalog, allowed.uid

    async def _refresh_history(self, status: ManagerStatus) -> None:
        if status.plan_history_uid == self._history_uid:
            return
        try:
            history = await self._relay.history()
        except RelayRejected as error:
            logger.error("RE Manager refused its history: %s", error.message)
            return
        # Later entries win: the history is oldest first.
        self._by_item = {entry.item_uid: entry for entry in history.entries}
        self._by_attempt = {entry.attempt_uid: entry for entry in history.entries if entry.attempt_uid is not None}
        self._history_uid = history.uid

    def _entry(self, attempt: AttemptRecord) -> HistoryEntry | None:
        if attempt.item_uid is not None:
            return self._by_item.get(attempt.item_uid)
        return self._by_attempt.get(attempt.attempt_uid)

    async def _resolve_late(self, attempt: AttemptRecord) -> None:
        entry = self._entry(attempt)
        if entry is None:
            return
        state, diagnostic = _outcome(entry)
        if state is AttemptState.UNKNOWN:
            return
        await self._store.write(
            lambda tx: tx.resolve_unknown_attempt(
                attempt_uid=attempt.attempt_uid,
                state=state,
                item_uid=entry.item_uid,
                diagnostic=diagnostic,
                run_uids=entry.run_uids,
            )
        )
        logger.warning("attempt %s was unknown; RE Manager's history now shows it %s", attempt.attempt_uid, state)

    async def _observe(self, attempt: AttemptRecord, status: ManagerStatus, now: float) -> None:
        # History evidence comes first: it beats every unknown verdict below.
        uid = attempt.attempt_uid
        entry = self._entry(attempt)
        if entry is not None:
            state, diagnostic = _outcome(entry)
            await self._finish(attempt, state, diagnostic, entry.run_uids, item_uid=entry.item_uid)
            return
        if not status.worker_environment_exists:
            await self._mark_unknown(
                attempt,
                "the worker environment closed while the attempt was live, and no history entry explains it",
            )
            return
        if attempt.state is AttemptState.RUNNING and status.running_item_uid is not None:
            if status.running_item_uid == attempt.item_uid:
                self._deadlines.pop(uid, None)
                await self._refresh_runs(attempt, status)
                if attempt.safe_stop_requested_at is not None or attempt.stop_now_requested_at is not None:
                    await self._stop(attempt, status)
                return
            logger.error(
                "RE Manager is running item %s, but the controller dispatched %s",
                status.running_item_uid,
                attempt.item_uid,
            )
            await self._mark_unknown(
                attempt,
                f"RE Manager is running item {status.running_item_uid}, which the controller did not dispatch",
            )
            return
        # A claimed attempt whose execute request may have reached the manager, or a running attempt the
        # manager neither runs nor has a history entry for: wait for evidence, then give up.
        deadline = self._deadlines.setdefault(uid, now + self._grace)
        if now >= deadline:
            await self._mark_unknown(
                attempt,
                f"after {self._grace:g} s RE Manager neither runs the attempt's item"
                " nor has a history entry for it",
            )

    async def _refresh_runs(self, attempt: AttemptRecord, status: ManagerStatus) -> None:
        uid = attempt.attempt_uid
        if self._runs is not None and self._runs[0] == uid and status.run_list_uid == self._run_list_uid:
            return
        try:
            runs = await self._relay.runs()
        except RelayRejected as error:
            logger.warning("RE Manager refused the runs of attempt %s: %s", uid, error.message)
            return
        self._runs, self._run_list_uid = (uid, runs), status.run_list_uid

    async def _stop(self, attempt: AttemptRecord, status: ManagerStatus) -> None:
        """Pause the plan, at its next checkpoint for a safe stop or at once for a stop now, then abort it.

        A stop now requested after a safe stop sends the immediate pause as well.
        """
        uid = attempt.attempt_uid
        if status.manager_state == "paused":
            if self._abort_sent == uid:
                return
            self._abort_sent = uid
            try:
                await self._relay.re_abort()
            except RelayRejected as error:
                self._abort_sent = None
                logger.warning("RE Manager refused to abort attempt %s: %s", uid, error.message)
            except RelayUnreachable:
                self._abort_sent = None
                raise
            return
        option: PauseOption = "immediate" if attempt.stop_now_requested_at is not None else "deferred"
        if self._pause_sent in ((uid, option), (uid, "immediate")):
            return
        self._pause_sent = (uid, option)
        try:
            await self._relay.re_pause(option)
        except RelayRejected as error:
            self._pause_sent = None
            logger.warning("RE Manager refused to pause attempt %s: %s", uid, error.message)
        except RelayUnreachable:
            self._pause_sent = None
            raise

    async def _open_environment(self, status: ManagerStatus, now: float) -> None:
        if status.manager_state != "idle":
            return
        if self._environment_opened_at is not None and now - self._environment_opened_at < self._environment_retry:
            return
        self._environment_opened_at = now
        logger.info("opening the worker environment")
        try:
            await self._relay.environment_open()
        except RelayRejected as error:
            logger.warning("RE Manager refused to open the worker environment: %s", error.message)

    async def _dispatch(self, status: ManagerStatus, now: float) -> None:
        catalog = self._catalog
        if (
            catalog is None
            or self._incompatible
            or status.manager_state != "idle"
            or status.running_item_uid is not None
        ):
            return

        async def claim(tx: Transaction) -> tuple[AttemptRecord, OperationRecord, int] | None:
            # Checked again in the claim's transaction: an operation submitted against an older catalog may
            # have been committed since this step read the store.
            if _incompatible(catalog, await tx.active_descriptors()):
                return None
            return await tx.claim_next()

        claimed = await self._store.write(claim)
        if claimed is None:
            return
        attempt, operation, _ = claimed
        descriptor = catalog.descriptor(operation.operation_id, operation.operation_version)
        item = {
            "item_type": "plan",
            "name": descriptor.plan_name,
            "kwargs": descriptor.plan_kwargs(operation.parameters),
            "meta": {"attempt_uid": attempt.attempt_uid, "operation_uid": operation.operation_uid},
        }
        try:
            item_uid = await self._relay.item_execute(
                item, user=operation.submitted_by, user_group=self._user_group
            )
        except RelayRejected as error:
            await self._finish(attempt, AttemptState.FAILED, f"RE Manager refused the operation: {error.message}")
            return
        except RelayUnreachable:
            # The item may be running: wait for evidence like any claimed attempt, and never send it again.
            self._deadlines[attempt.attempt_uid] = now + self._grace
            raise
        await self._store.write(
            lambda tx: tx.mark_attempt_running(attempt_uid=attempt.attempt_uid, item_uid=item_uid)
        )

    async def _mark_unknown(self, attempt: AttemptRecord, reason: str) -> None:
        await self._finish(attempt, AttemptState.UNKNOWN, reason)

    async def _finish(
        self,
        attempt: AttemptRecord,
        state: AttemptState,
        diagnostic: str | None,
        run_uids: Sequence[str] = (),
        *,
        item_uid: str | None = None,
    ) -> None:
        """Record how a live attempt ended; ``item_uid`` first marks a claimed attempt running."""
        uid = attempt.attempt_uid

        async def write(tx: Transaction) -> None:
            if item_uid is not None and attempt.state is AttemptState.CLAIMED:
                await tx.mark_attempt_running(attempt_uid=uid, item_uid=item_uid)
            await tx.finish_attempt(attempt_uid=uid, state=state, diagnostic=diagnostic, run_uids=run_uids)

        await self._store.write(write)
        self._deadlines.pop(uid, None)
        if self._runs is not None and self._runs[0] == uid:
            self._runs = None
        if self._pause_sent is not None and self._pause_sent[0] == uid:
            self._pause_sent = None
        if self._abort_sent == uid:
            self._abort_sent = None
        if state is AttemptState.UNKNOWN:
            self._blocked = True
            logger.error("attempt %s is unknown: %s; nothing runs until recovery is acknowledged", uid, diagnostic)
