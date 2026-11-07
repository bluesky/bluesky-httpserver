"""Stress the lab's v2 controller: concurrent editors, readers and event streams while the queue runs.

    pixi run stress [--operations 40] [--editors 4] [--readers 8] [--streams 4] [--kinds change_energy,count]

Needs the whole lab up and nobody else using it: it holds the control lease as "stress" and starts queue
executions until every operation it queued has ended. It reorders the queue only while the queue holds nothing but
its own operations, and cancels or replaces only its own.

While the queue runs, it
- edits the queue from several clients at once (submit, replace, cancel, reorder), so edits race each other and the
  scheduler and are retried on stale revisions;
- sends bursts of the same submission with one idempotency key, which must queue one operation;
- reads the queue, events, operations, catalog, lease and readiness as fast as the controller answers;
- follows the event stream from several clients;
- checks that a principal without the lease, and one with only the Read role, are refused.

Then it checks what the controller promises, prints request latencies, and exits 1 if any check failed: every
operation it queued ended succeeded, cancelled or replaced, was submitted once, and if it ran, ran once (claimed,
running, succeeded); each event stream delivered exactly the event log, in order; the queue revision in the log
never went backwards; the lease was never lost.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from lab import CONTROLLER_URL, mint

from bluesky_httpserver.v2.client import ApiError, AsyncQueueServerClient, Event, Operation, Queue, StaleRevision

T = TypeVar("T")

_DETECTORS = ("diag1", "diag2", "nano_bpm", "xrd_det", "xas_det")
# Operation id (every one is version 1) and a random valid parameters generator, by --kinds name.
_KINDS: dict[str, tuple[str, Callable[[random.Random], dict[str, Any]]]] = {
    "change_energy": (
        "bmm.change_energy",
        lambda rng: {"energy": round(rng.uniform(6000, 8000), 1), "crystal": rng.choice(["si111", "si311"])},
    ),
    "count": ("tst.count", lambda rng: {"detectors": [rng.choice(_DETECTORS)], "num": 1}),
}
_EDIT_WEIGHTS = {"submit": 55, "duplicate": 5, "replace": 15, "cancel": 10, "reorder": 15}
_DUPLICATES = 4
_GOOD_ENDS = {"succeeded", "cancelled", "replaced"}
_ENDS = _GOOD_ENDS | {"failed", "aborted", "unknown"}
_ATTEMPT_EVENTS = {
    "operation.claimed",
    "operation.running",
    "operation.succeeded",
    "operation.failed",
    "operation.aborted",
    "operation.unknown",
}
_RAN = ["operation.claimed", "operation.running", "operation.succeeded"]
_MAX_UNEXPECTED = 100
_TOKEN_SECONDS = 4 * 3600


@dataclass
class Run:
    """What one stress run did and found; shared by all its tasks, which run on one event loop."""

    rng: random.Random
    kinds: list[str]
    start_cursor: int = 0
    created: set[str] = field(default_factory=set)
    submitted: int = 0
    edits: Counter[str] = field(default_factory=Counter)
    stale: int = 0
    gave_up: int = 0
    executions: int = 0
    refusals: Counter[str] = field(default_factory=Counter)
    unexpected: Counter[str] = field(default_factory=Counter)
    failures: list[str] = field(default_factory=list)
    latencies: defaultdict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    stopped: str | None = None

    async def timed(self, kind: str, awaitable: Awaitable[T]) -> T:
        start = time.perf_counter()
        try:
            return await awaitable
        finally:
            self.latencies[kind].append(time.perf_counter() - start)

    def new_operation(self) -> tuple[str, dict[str, Any]]:
        operation_id, parameters = _KINDS[self.rng.choice(self.kinds)]
        return operation_id, parameters(self.rng)

    def error(self, where: str, error: Exception) -> None:
        """Record an error nothing in this run should cause; stop the run once there are too many."""
        code = error.code if isinstance(error, ApiError) else type(error).__name__
        self.unexpected[f"{where}: {code}"] += 1
        if self.unexpected.total() > _MAX_UNEXPECTED:
            self.stopped = f"more than {_MAX_UNEXPECTED} unexpected errors"


async def _pause(done: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(done.wait(), seconds)


async def _cancel(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def check_idempotency(run: Run, client: AsyncQueueServerClient) -> None:
    """A repeated request is answered from the record even after the queue moved on; reusing its key with another
    If-Match is refused."""
    operation_id, parameters = run.new_operation()
    key = str(uuid.uuid4())
    revision = (await client.queue()).revision
    first = await run.timed(
        "submit", client.submit(operation_id, "1", parameters, revision=revision, idempotency_key=key)
    )
    run.created.add(first.operation_uid)
    run.submitted += 1
    again = await run.timed(
        "submit", client.submit(operation_id, "1", parameters, revision=revision, idempotency_key=key)
    )
    if again.operation_uid != first.operation_uid:
        run.failures.append("a repeated submission queued a second operation")
    try:
        await client.submit(operation_id, "1", parameters, revision=revision + 1, idempotency_key=key)
    except ApiError as error:
        if error.code != "idempotency_conflict":
            run.failures.append(
                f"a reused idempotency key was refused with {error.code}, not idempotency_conflict"
            )
        run.refusals[error.code] += 1
    else:
        run.failures.append("a reused idempotency key with another If-Match was accepted")


async def duplicate_burst(run: Run, client: AsyncQueueServerClient, revision: int) -> None:
    """Send one submission several times at once with one idempotency key; exactly one operation must be queued."""
    operation_id, parameters = run.new_operation()
    key = str(uuid.uuid4())
    results = await asyncio.gather(
        *(
            run.timed(
                "submit", client.submit(operation_id, "1", parameters, revision=revision, idempotency_key=key)
            )
            for _ in range(_DUPLICATES)
        ),
        return_exceptions=True,
    )
    operations = [result for result in results if isinstance(result, Operation)]
    errors = [result for result in results if isinstance(result, BaseException)]
    if not operations:
        raise errors[0]  # all stale, say; edit_queue retries a StaleRevision with a fresh queue
    uids = {operation.operation_uid for operation in operations}
    if errors or len(uids) != 1:
        run.failures.append(
            f"duplicate burst queued {len(uids)} operation(s) and raised {[repr(e) for e in errors]}"
        )
    run.created |= uids
    run.submitted += 1


async def editor(run: Run, client: AsyncQueueServerClient, target: int, pace: float) -> None:
    """Edit the queue until ``target`` operations have been submitted, retrying edits that lose a race."""

    async def edit(queue: Queue) -> None:
        action = run.rng.choices(list(_EDIT_WEIGHTS), weights=list(_EDIT_WEIGHTS.values()))[0]
        ours = [o.operation_uid for o in queue.operations if o.operation_uid in run.created]
        try:
            if action == "reorder" and 1 < len(ours) == len(queue.operations):
                run.rng.shuffle(ours)
                await run.timed("reorder", client.reorder(ours))
            elif action == "cancel" and ours:
                await run.timed("cancel", client.cancel(run.rng.choice(ours)))
            elif action == "replace" and ours:
                operation_id, parameters = run.new_operation()
                new = await run.timed(
                    "replace", client.replace(run.rng.choice(ours), operation_id, "1", parameters)
                )
                run.created.add(new.operation_uid)
            elif action == "duplicate":
                await duplicate_burst(run, client, queue.revision)
            else:
                action = "submit"
                operation_id, parameters = run.new_operation()
                operation = await run.timed("submit", client.submit(operation_id, "1", parameters))
                run.created.add(operation.operation_uid)
                run.submitted += 1
        except StaleRevision:
            run.stale += 1
            raise
        run.edits[action] += 1

    while run.submitted < target and run.stopped is None:
        try:
            await client.edit_queue(edit, attempts=5)
        except StaleRevision:
            run.gave_up += 1
        except (ApiError, httpx.HTTPError) as error:
            run.error("edit", error)
        await asyncio.sleep(run.rng.uniform(0, 2 * pace))


async def keep_running(run: Run, client: AsyncQueueServerClient, done: asyncio.Event) -> None:
    """Start a queue execution whenever operations are queued and none runs."""
    while not done.is_set():
        try:
            queue = await run.timed("queue", client.queue())
            if queue.operations:
                await run.timed(
                    "start_execution", client.start_execution("continue_on_failure", revision=queue.revision)
                )
                run.executions += 1
        except StaleRevision:
            pass
        except ApiError as error:
            if error.code == "dispatch_blocked":
                run.stopped = "dispatch blocked: an operation's outcome could not be proven"
                run.failures.append(run.stopped)
                return
            if error.code not in ("execution_active", "queue_empty"):
                run.error("start_execution", error)
        except httpx.HTTPError as error:
            run.error("start_execution", error)
        await _pause(done, 0.5)


async def reader(run: Run, client: AsyncQueueServerClient, done: asyncio.Event) -> None:
    """Read as fast as the controller answers."""
    reads: dict[str, Callable[[], Awaitable[object]]] = {
        "queue": client.queue,
        "events": lambda: client.events(after=run.start_cursor + run.rng.randrange(200), limit=100),
        "operation": lambda: client.operation(run.rng.choice(tuple(run.created))),
        "catalog": client.catalog,
        "lease": client.lease,
        "readiness": client.readiness,
    }
    while not done.is_set() and run.stopped is None:
        kind = run.rng.choice(list(reads))
        if kind == "operation" and not run.created:
            continue
        try:
            await run.timed(kind, reads[kind]())
        except (ApiError, httpx.HTTPError) as error:
            run.error(kind, error)


async def _read_then_submit(
    client: AsyncQueueServerClient, operation_id: str, parameters: dict[str, Any]
) -> object:
    await client.queue()
    return await client.submit(operation_id, "1", parameters)


async def outsiders(
    run: Run, intruder: AsyncQueueServerClient, viewer: AsyncQueueServerClient, done: asyncio.Event
) -> None:
    """While the lease is held, a principal without it and one with only the Read role must be refused."""
    attempts: list[tuple[str, Callable[[str, dict[str, Any]], Awaitable[object]], str]] = [
        ("take the lease while it is held", lambda _, __: intruder.acquire_lease(60), "lease_held"),
        ("submit without the lease", lambda o, p: _read_then_submit(intruder, o, p), "lease_required"),
        ("submit with only the Read role", lambda o, p: _read_then_submit(viewer, o, p), "insufficient_role"),
    ]
    while not done.is_set() and run.stopped is None:
        for name, attempt, expected in attempts:
            try:
                await attempt(*run.new_operation())
            except ApiError as error:
                if error.code != expected:
                    run.failures.append(f"{name}: refused with {error.code}, expected {expected}")
                run.refusals[error.code] += 1
            except httpx.HTTPError as error:
                run.error(name, error)
            else:
                run.failures.append(f"{name}: accepted")
        await _pause(done, 1.0)


async def follow(client: AsyncQueueServerClient, after: int, ids: list[int]) -> None:
    """Record the id of every event the stream delivers; runs until cancelled."""
    async with contextlib.aclosing(client.stream_events(after, reconnect_seconds=0.5)) as stream:
        async for event in stream:
            ids.append(event.event_id)


async def progress(run: Run, target: int, started: float, done: asyncio.Event) -> None:
    while not done.is_set():
        await _pause(done, 5.0)
        print(
            f"  {time.monotonic() - started:6.0f} s  submitted {run.submitted}/{target},"
            f" edits {run.edits.total()}, stale {run.stale}, executions {run.executions},"
            f" requests {sum(map(len, run.latencies.values()))}",
            flush=True,
        )


async def drain(run: Run, client: AsyncQueueServerClient, timeout: float) -> dict[str, str]:
    """Wait until every operation this run queued has ended; return the last state read of each."""
    states: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    while run.stopped is None:
        pending = [uid for uid in run.created if states.get(uid) not in _ENDS]
        if not pending:
            break
        if time.monotonic() > deadline:
            run.failures.append(f"{len(pending)} operation(s) had not ended {timeout:g} s after the edits stopped")
            break
        for uid in pending:
            try:
                states[uid] = (await run.timed("operation", client.operation(uid))).state
            except (ApiError, httpx.HTTPError) as error:
                run.error("operation", error)
        await asyncio.sleep(1.0)
    return states


async def newest_event_id(client: AsyncQueueServerClient) -> int:
    cursor = 0
    while events := await client.events(after=cursor, limit=1000):
        cursor = events[-1].event_id
    return cursor


async def event_log(client: AsyncQueueServerClient, after: int) -> list[Event]:
    log: list[Event] = []
    while events := await client.events(after=log[-1].event_id if log else after, limit=1000):
        log.extend(events)
    return log


def check(run: Run, log: list[Event], streams: list[list[int]], states: dict[str, str]) -> None:
    """Add a failure for each promise the event log, the streams or the final states break."""
    ids = [event.event_id for event in log]
    if any(b <= a for a, b in zip(ids, ids[1:], strict=False)):
        run.failures.append("event ids in the log are not strictly increasing")
    if any(b.queue_revision < a.queue_revision for a, b in zip(log, log[1:], strict=False)):
        run.failures.append("the queue revision went backwards in the event log")
    for number, delivered in enumerate(streams, 1):
        delivered = [i for i in delivered if not ids or i <= ids[-1]]
        if delivered != ids:
            run.failures.append(
                f"event stream {number} delivered {len(delivered)} events, the log has {len(ids)}: missing"
                f" {len(set(ids) - set(delivered))}, repeated {len(delivered) - len(set(delivered))}"
            )
    by_operation: defaultdict[str, list[str]] = defaultdict(list)
    for event in log:
        if event.operation_uid is not None:
            by_operation[event.operation_uid].append(event.event_type)
    for uid in sorted(run.created):
        state, types = states.get(uid), by_operation[uid]
        if state not in _GOOD_ENDS:
            run.failures.append(f"operation {uid} ended {state}")
        if types.count("operation.submitted") != 1:
            run.failures.append(f"operation {uid} was submitted {types.count('operation.submitted')} times")
        attempt = [t for t in types if t in _ATTEMPT_EVENTS]
        if attempt != (_RAN if state == "succeeded" else []):
            run.failures.append(f"operation {uid} ({state}) has attempt events {attempt}")


def report(run: Run, args: argparse.Namespace, seconds: float, log: list[Event], states: dict[str, str]) -> None:
    requests = sum(map(len, run.latencies.values()))
    ends = Counter(states.get(uid, "unread") for uid in run.created)
    print()
    print(f"seed {args.seed}: {args.editors} editors, {args.readers} readers, {args.streams} streams", end="")
    print(f", {seconds:.1f} s")
    print(f"operations: {len(run.created)} queued; ended {dict(ends)}; {run.executions} queue execution(s)")
    print(
        f"edits: {dict(run.edits)}; {run.stale} refused as stale and retried with a fresh queue,"
        f" {run.gave_up} given up after 5 tries"
    )
    print(f"refused as expected: {dict(run.refusals)}")
    print(f"events: {len(log)} in the log, followed by {args.streams} stream(s)")
    print(f"requests: {requests} in {seconds:.1f} s ({requests / seconds:.0f}/s)")
    print(f"  {'latency ms':<16}{'count':>7}{'p50':>9}{'p95':>9}{'max':>9}")
    for kind, samples in sorted(run.latencies.items()):
        ordered = sorted(samples)
        p50, p95 = (ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000 for q in (0.5, 0.95))
        print(f"  {kind:<16}{len(ordered):>7}{p50:>9.1f}{p95:>9.1f}{ordered[-1] * 1000:>9.1f}")
    print(f"unexpected errors: {dict(run.unexpected) or 'none'}")
    if run.failures:
        print(f"FAILED: {len(run.failures)} check(s)")
        for failure in run.failures[:20]:
            print(f"  - {failure}")
        if len(run.failures) > 20:
            print(f"  ... and {len(run.failures) - 20} more")
    else:
        print("all checks passed")


async def stress(args: argparse.Namespace) -> bool:
    run = Run(rng=random.Random(args.seed), kinds=args.kinds)
    stresser = mint("stress", "run", _TOKEN_SECONDS)
    async with contextlib.AsyncExitStack() as stack:

        async def client(token: str) -> AsyncQueueServerClient:
            return await stack.enter_async_context(AsyncQueueServerClient(args.url, token))

        lead = await client(stresser)
        if not (readiness := await lead.readiness()).ready:
            print(f"the controller is not ready: {readiness}")
            return False
        run.start_cursor = await newest_event_id(lead)
        viewer_token = mint("stress-viewer", "read", _TOKEN_SECONDS)
        stream_ids: list[list[int]] = [[] for _ in range(args.streams)]
        streams = [
            asyncio.create_task(follow(await client(viewer_token), run.start_cursor, ids)) for ids in stream_ids
        ]
        # Registered after the streams' clients, so it runs before they close.
        stack.push_async_callback(_cancel, streams)
        editors = [await client(stresser) for _ in range(args.editors)]
        readers = [await client(viewer_token) for _ in range(args.readers)]
        intruder = await client(mint("stress-intruder", "edit", _TOKEN_SECONDS))
        viewer = await client(viewer_token)

        started = time.monotonic()
        done = asyncio.Event()
        states: dict[str, str] = {}
        background: list[asyncio.Task[None]] = []
        try:
            async with lead.hold_lease(ttl_seconds=30) as holder:
                await check_idempotency(run, lead)
                background = [
                    asyncio.create_task(keep_running(run, lead, done)),
                    asyncio.create_task(outsiders(run, intruder, viewer, done)),
                    asyncio.create_task(progress(run, args.operations, started, done)),
                    *(asyncio.create_task(reader(run, c, done)) for c in readers),
                ]
                await asyncio.gather(*(editor(run, c, args.operations, args.pace) for c in editors))
                print("  edits done; waiting for the queued operations to end", flush=True)
                states = await drain(run, lead, args.drain_timeout)
                done.set()
                await asyncio.gather(*background)
                if holder.lost is not None:
                    run.failures.append(f"the control lease was lost: {holder.lost}")
        except ApiError as error:
            if error.code != "lease_held":
                raise
            print(f"someone else holds the control lease: {error.details}")
            return False
        finally:
            done.set()
            await _cancel(background)
        seconds = time.monotonic() - started

        log = await event_log(lead, run.start_cursor)
        # The streams may lag the log by a moment.
        last = log[-1].event_id if log else run.start_cursor
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(30):
                while any((ids[-1] if ids else run.start_cursor) < last for ids in stream_ids):
                    await asyncio.sleep(0.1)
        await _cancel(streams)
        if run.stopped is not None and run.stopped not in run.failures:
            run.failures.append(f"stopped early: {run.stopped}")
        check(run, log, stream_ids, states)
        report(run, args, seconds, log, states)
        return not run.failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=CONTROLLER_URL, help=f"the controller (default: {CONTROLLER_URL})")
    parser.add_argument("--operations", type=int, default=40, help="operations to submit (default: 40)")
    parser.add_argument("--editors", type=int, default=4, help="clients editing the queue at once (default: 4)")
    parser.add_argument("--readers", type=int, default=8, help="clients reading as fast as they can (default: 8)")
    parser.add_argument("--streams", type=int, default=4, help="clients following the event stream (default: 4)")
    parser.add_argument(
        "--kinds",
        type=lambda value: value.split(","),
        default=["change_energy"],
        help=f"operations to queue, comma-separated, from {', '.join(_KINDS)} (default: change_energy, fastest)",
    )
    parser.add_argument(
        "--pace", type=float, default=0.5, help="mean seconds between one editor's edits (default: 0.5)"
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=900,
        help="seconds to wait for queued operations to end (default: 900)",
    )
    parser.add_argument("--seed", type=int, default=None, help="random seed, to repeat a run (default: random)")
    args = parser.parse_args()
    if unknown := set(args.kinds) - set(_KINDS):
        parser.error(f"unknown --kinds {sorted(unknown)}; choose from {', '.join(_KINDS)}")
    if args.seed is None:
        args.seed = random.randrange(2**32)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"stressing {args.url} with seed {args.seed}", flush=True)
    raise SystemExit(0 if asyncio.run(stress(args)) else 1)


if __name__ == "__main__":
    main()
