"""Compare bluesky-httpserver v1 and v2 on the same lab, through their HTTP APIs, without a user interface.

    pixi run compare [--scenarios edits,retries,audit,sync,validation,surface] [--kill-worker] [--json out.json]

Needs both stacks up and idle (see pixi.toml): v2 (manager, issuer, controller) and v1 (v1-manager, v1-server),
both queues empty and nobody holding v2's control lease. compare.py opens v1's worker environment if it is closed;
v2's controller opens its own. Both run the same plans from profile/10-operations.py, which carries v1's parameter
annotations as well as v2's catalog, so each validates as well as it can.

Each scenario does the same job on both systems, each the best way its API allows, and counts outcomes against what
the server reports afterwards:

edits       Several clients of one user each remove their own plan, but only while more than editors-1 plans are
            queued. v1 clients re-check plan_queue_uid just before removing; v2 clients send the revision they
            decided on (If-Match) and decide again when it is stale. Counts trials that broke the limit.
retries     A proxy loses the reply to each submission after the server acted, as a gateway timeout does; the
            client sends the same request again (v2: the same Idempotency-Key). Counts duplicate plans queued.
audit       alice and bob submit, move, update and remove plans; then asks each server who did what.
sync        GUIs follow the queue: v1 through its status websocket and a queue read on each new plan_queue_uid,
            v2 through its event stream and a queue read on each new queue revision. Measures how fast a change
            reaches every GUI, idle traffic, and what a GUI that was disconnected learns when it reconnects.
validation  Sends the same invalid requests to both: are they refused, and is the refusal machine-readable?
surface     Reads each OpenAPI document as an agent's tool list: size, operations, operations that run code or
            administer the server, typed request bodies, and whether it is readable without credentials.
fail-closed (only with --kill-worker) SIGKILLs RE Worker while a plan runs, then restarts the queue as an operator
            would: is the interrupted plan run again, and must anyone acknowledge first?

Prints a table per scenario; --json also writes every measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import re
import signal
import socket
import statistics
import time
import tomllib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
import lab
import uvicorn
import websockets
import yaml
from ui.server import re_manager_processes

from bluesky_httpserver.v2.client import ApiError, AsyncQueueServerClient, Queue

# The same plans on both: v1 queues them by name, v2 by catalog operation (every one version 1).
V1_PLANS = {"energy": "change_energy_operation", "count": "count_operation"}
V2_OPERATIONS = {"energy": "bmm.change_energy", "count": "tst.count"}
_TOKEN_SECONDS = 4 * 3600


# --- Results ---


@dataclass
class Metric:
    label: str
    v1: Any
    v2: Any
    better: str | None = None  # "lower", "higher", or None when neither is better


@dataclass
class Result:
    name: str
    title: str
    metrics: list[Metric] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, label: str, v1: Any, v2: Any, better: str | None = None) -> None:
        self.metrics.append(Metric(label, v1, v2, better))


def _cell(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.1f}"
    return "—" if value is None else str(value)


def print_result(result: Result) -> None:
    print(f"\n== {result.title}")
    width = max([len(m.label) for m in result.metrics] + [10])
    print(f"   {'':<{width}}  {'v1':>14}  {'v2':>14}")
    for metric in result.metrics:
        mark = ""
        if metric.better and isinstance(metric.v1, int | float) and isinstance(metric.v2, int | float):
            if metric.v1 != metric.v2:
                v2_better = (metric.v2 < metric.v1) == (metric.better == "lower")
                mark = "  v2 better" if v2_better else "  v1 better"
        print(f"   {metric.label:<{width}}  {_cell(metric.v1):>14}  {_cell(metric.v2):>14}{mark}")
    for note in result.notes:
        print(f"   * {note}")


# --- v1 over HTTP ---


class V1Error(Exception):
    """v1 refused a request: an HTTP error status, or HTTP 200 with ``success: false``."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body.get('msg', body) if isinstance(body, dict) else body}")


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def v1_item(plan: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    return {"item_type": "plan", "name": V1_PLANS[plan], "kwargs": kwargs}


class V1:
    """One user of bluesky-httpserver v1."""

    def __init__(self, url: str, authorization: str) -> None:
        self.http = httpx.AsyncClient(base_url=url, headers={"Authorization": authorization}, timeout=30)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, body: Any = None) -> httpx.Response:
        return await self.http.request(method, f"/api{path}", json=body)

    async def call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        response = await self.request(method, path, body)
        data = _json(response)
        if response.status_code != 200 or not isinstance(data, dict) or not data.get("success", True):
            raise V1Error(response.status_code, data)
        return data

    async def status(self) -> dict[str, Any]:
        return await self.call("GET", "/status")

    async def queue(self) -> dict[str, Any]:
        return await self.call("GET", "/queue/get")

    async def uids(self) -> list[str]:
        return [item["item_uid"] for item in (await self.queue())["items"]]

    async def add(self, plan: str, kwargs: dict[str, Any]) -> str:
        return (await self.call("POST", "/queue/item/add", {"item": v1_item(plan, kwargs)}))["item"]["item_uid"]

    async def remove(self, uid: str) -> None:
        await self.call("POST", "/queue/item/remove", {"uid": uid})

    async def move_to_front(self, uid: str) -> None:
        await self.call("POST", "/queue/item/move", {"uid": uid, "pos_dest": "front"})

    async def update(self, uid: str, plan: str, kwargs: dict[str, Any]) -> None:
        await self.call("POST", "/queue/item/update", {"item": {**v1_item(plan, kwargs), "item_uid": uid}})

    async def clear(self) -> None:
        await self.call("POST", "/queue/clear", {})

    async def history(self) -> list[dict[str, Any]]:
        return (await self.call("GET", "/history/get"))["items"]

    async def wait_for(
        self, condition: Callable[[dict[str, Any]], bool], what: str, timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while not condition(status := await self.status()):
            if time.monotonic() > deadline:
                raise TimeoutError(f"v1: timed out waiting for {what}")
            await asyncio.sleep(0.2)
        return status

    async def open_environment(self) -> None:
        await self.call("POST", "/environment/open", {})
        await self.wait_for(
            lambda s: s["worker_environment_exists"] and s["manager_state"] == "idle", "the environment", 300
        )


# --- The lab ---


class Lab:
    """Where both systems are, and how to act as their users."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rng = random.Random(args.seed)
        v1 = yaml.safe_load(lab.V1_SETTINGS.read_text())
        self.v1_passwords: dict[str, str] = v1["authentication"]["providers"][0]["args"]["users_to_passwords"]
        self.v1_control: str = v1["qserver_zmq_configuration"]["control_address"]
        self.v2_control: str = tomllib.loads(lab.SETTINGS.read_text())["manager"]["control_address"]
        self._v2_tokens: dict[str, str] = {}
        self.stack = contextlib.AsyncExitStack()

    def energy(self) -> dict[str, Any]:
        return {
            "energy": float(self.rng.randrange(6000, 8001, 100)),
            "crystal": self.rng.choice(["si111", "si311"]),
        }

    async def v1_token(self, user: str) -> str:
        async with httpx.AsyncClient(base_url=lab.V1_URL, timeout=30) as http:
            response = await http.post(
                "/api/auth/provider/lab/token", data={"username": user, "password": self.v1_passwords[user]}
            )
            response.raise_for_status()
            return response.json()["access_token"]

    async def v1_api_key(self, user: str) -> str:
        token = await self.v1_token(user)
        async with httpx.AsyncClient(base_url=lab.V1_URL, timeout=30) as http:
            response = await http.post(
                "/api/auth/apikey", json={"expires_in": 3600}, headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
            return response.json()["secret"]

    async def v1(self, user: str, url: str = lab.V1_URL) -> V1:
        client = V1(url, f"Bearer {await self.v1_token(user)}")
        self.stack.push_async_callback(client.aclose)
        return client

    def v2_token(self, user: str) -> str:
        if user not in self._v2_tokens:
            self._v2_tokens[user] = lab.mint(user, "read" if user == "viewer" else "run", _TOKEN_SECONDS)
        return self._v2_tokens[user]

    async def v2(self, user: str, url: str = lab.CONTROLLER_URL) -> AsyncQueueServerClient:
        return await self.stack.enter_async_context(AsyncQueueServerClient(url, self.v2_token(user)))


async def v2_clear(client: AsyncQueueServerClient) -> None:
    """Cancel every queued operation; ``client`` holds the lease."""
    for operation in (await client.queue()).operations:
        await client.cancel(operation.operation_uid)


async def v2_events(client: AsyncQueueServerClient, after: int) -> list[Any]:
    events: list[Any] = []
    while page := await client.events(after=events[-1].event_id if events else after, limit=1000):
        events.extend(page)
    return events


async def prepare(ctx: Lab) -> None:
    """Check that both systems are idle and empty; open v1's worker environment if it is closed."""
    v1 = await ctx.v1("alice")
    status = await v1.status()
    if status["manager_state"] != "idle" or status["items_in_queue"]:
        raise SystemExit(f"v1 is not idle and empty: {status['manager_state']}, {status['items_in_queue']} queued")
    if not status["worker_environment_exists"]:
        print("opening v1's worker environment", flush=True)
        await v1.open_environment()
    v2 = await ctx.v2("alice")
    deadline = time.monotonic() + 300
    while not (readiness := await v2.readiness()).ready:
        if time.monotonic() > deadline:
            raise SystemExit(f"v2 is not ready: {readiness}")
        await asyncio.sleep(1)
    if (await v2.queue()).operations or (lease := await v2.lease()) is not None:
        raise SystemExit("v2 is not empty and free: its queue holds operations or someone holds the control lease")
    del lease


# --- A proxy that loses replies ---


class LosingProxy:
    """Forwards every request. Of the mutations ``loses`` selects, it loses the reply to every other one, starting
    with the first: the server acts, and the client gets 502, as from a gateway whose upstream timed out. A client
    that retries each lost request once thus has every first attempt's reply lost and no retry's."""

    def __init__(self, upstream: str, loses: Callable[[str, str], bool]) -> None:
        self._http = httpx.AsyncClient(base_url=upstream, timeout=30)
        self._loses = loses
        self._selected = 0
        self.lost = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = [
            (k.decode(), v.decode())
            for k, v in scope["headers"]
            if k not in (b"host", b"content-length", b"connection")
        ]
        target = scope["path"] + (f"?{scope['query_string'].decode()}" if scope["query_string"] else "")
        response = await self._http.request(scope["method"], target, headers=headers, content=body)
        if self._loses(scope["method"], scope["path"]) and (selected := self._selected) % 2 == 0:
            self._selected = selected + 1
            self.lost += 1
            status, content, out = 502, b"upstream reply lost", [(b"content-type", b"text/plain")]
        else:
            if self._loses(scope["method"], scope["path"]):
                self._selected += 1
            status, content = response.status_code, response.content
            hop = ("content-length", "transfer-encoding", "connection", "content-encoding")
            out = [(k.encode(), v.encode()) for k, v in response.headers.items() if k.lower() not in hop]
        out.append((b"content-length", str(len(content)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": content})


@contextlib.asynccontextmanager
async def serving(app: LosingProxy) -> AsyncIterator[str]:
    """Serve ``app`` on a free local port for the duration of the context; yields its URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await task
        await app.aclose()


# --- Scenarios ---


async def edits(ctx: Lab) -> Result:
    trials, editors = ctx.args.trials, ctx.args.editors
    floor = editors - 1
    result = Result(
        "edits",
        f"Concurrent edits: {trials} trials; {editors} clients of one user each remove their own plan, only while"
        f" more than {floor} are queued",
    )

    async def jitter() -> None:
        await asyncio.sleep(ctx.rng.uniform(0, 0.02))

    seeder = await ctx.v1("alice")
    clients = [await ctx.v1("alice") for _ in range(editors)]

    async def v1_edit(client: V1, mine: str) -> int:
        await jitter()
        queue = await client.queue()
        if len(queue["items"]) <= floor:
            return 0
        # The best a v1 client can do: re-check that nothing changed since it decided.
        if (await client.status())["plan_queue_uid"] != queue["plan_queue_uid"]:
            return 0
        try:
            await client.remove(mine)
        except V1Error:
            return 0
        return 1

    v1_left = []
    for _ in range(trials):
        mine = [await seeder.add("energy", ctx.energy()) for _ in clients]
        await asyncio.gather(*(v1_edit(c, m) for c, m in zip(clients, mine, strict=True)))
        v1_left.append(len(await seeder.uids()))
        await seeder.clear()

    lead = await ctx.v2("alice")
    v2_clients = [await ctx.v2("alice") for _ in range(editors)]

    async def v2_edit(client: AsyncQueueServerClient, mine: str) -> int:
        await jitter()

        async def decide(queue: Queue) -> int:
            if len(queue.operations) <= floor:
                return 0
            await client.cancel(mine)
            return 1

        return await client.edit_queue(decide, attempts=editors + 1)

    v2_left = []
    async with lead.hold_lease(ttl_seconds=60):
        for _ in range(trials):
            await lead.queue()
            mine = [
                (await lead.submit(V2_OPERATIONS["energy"], "1", ctx.energy())).operation_uid for _ in v2_clients
            ]
            await asyncio.gather(*(v2_edit(c, m) for c, m in zip(v2_clients, mine, strict=True)))
            v2_left.append(len((await lead.queue()).operations))
            await v2_clear(lead)

    for label, measure in [
        (f"trials that left fewer than {floor} queued", lambda left: sum(n < floor for n in left)),
        (f"plans removed beyond the limit of {floor}", lambda left: sum(max(0, floor - n) for n in left)),
    ]:
        result.add(label, measure(v1_left), measure(v2_left), "lower")
    result.notes.append(
        "v1 clients re-check plan_queue_uid just before removing; the check and the removal are two requests, so"
        " clients that read the same queue all pass it. v2 clients send the revision they read (If-Match); all but"
        " one get 412, read again and decide not to remove."
    )
    return result


async def retries(ctx: Lab) -> Result:
    n = ctx.args.submissions
    result = Result(
        "retries", f"Retry after a lost reply: {n} submissions, each reply lost once after the server acted"
    )

    v1_proxy = LosingProxy(lab.V1_URL, lambda method, path: method == "POST" and path == "/api/queue/item/add")
    direct = await ctx.v1("alice")
    async with serving(v1_proxy) as url:
        client = await ctx.v1("alice", url)
        for _ in range(n):
            kwargs = ctx.energy()
            try:
                await client.add("energy", kwargs)
            except V1Error as error:
                if error.status != 502:
                    raise
                await client.add("energy", kwargs)  # the same request again: v1 has nothing else to offer
    v1_queued = len(await direct.uids())
    await direct.clear()

    v2_proxy = LosingProxy(
        lab.CONTROLLER_URL, lambda method, path: method == "POST" and path == "/api/v2/operations"
    )
    lead = await ctx.v2("alice")
    async with lead.hold_lease(ttl_seconds=60), serving(v2_proxy) as url:
        client = await ctx.v2("alice", url)
        for _ in range(n):
            parameters, key = ctx.energy(), str(uuid.uuid4())
            revision = (await client.queue()).revision
            try:
                await client.submit(
                    V2_OPERATIONS["energy"], "1", parameters, revision=revision, idempotency_key=key
                )
            except ApiError as error:
                if error.status_code != 502:
                    raise
                await client.submit(
                    V2_OPERATIONS["energy"], "1", parameters, revision=revision, idempotency_key=key
                )
        v2_queued = len((await lead.queue()).operations)
        await v2_clear(lead)

    result.add("replies lost", v1_proxy.lost, v2_proxy.lost)
    result.add("plans queued", v1_queued, v2_queued)
    result.add("duplicate plans queued", v1_queued - n, v2_queued - n, "lower")
    result.notes.append(
        "v1 always mints a new item uid on add, so the retry queues a second plan. v2 replays the response it"
        " recorded for the Idempotency-Key, even though the queue revision moved on."
    )
    return result


AUDIT_STEPS = [
    ("alice", "submits A"),
    ("bob", "submits B"),
    ("bob", "moves B to the front"),
    ("bob", "updates A's parameters"),
    ("alice", "removes B"),
    ("alice", "submits C"),
]


async def audit(ctx: Lab) -> Result:
    result = Result("audit", "Audit: alice and bob take turns editing; can the server say who made each change?")

    alice, bob = await ctx.v1("alice"), await ctx.v1("bob")
    a = await alice.add("energy", ctx.energy())
    b = await bob.add("energy", ctx.energy())
    await bob.move_to_front(b)
    await bob.update(a, "energy", ctx.energy())
    await alice.remove(b)
    c = await alice.add("energy", ctx.energy())
    # What v1 keeps: each queued or finished item names the user who last submitted or updated it. A move or a
    # removal leaves no record.
    records = (await alice.queue())["items"] + await alice.history()

    def holds(uid: str, user: str) -> bool:
        return any(r.get("item_uid") == uid and r.get("user") == user for r in records)

    v1_found = [holds(a, "alice"), holds(b, "bob"), False, holds(a, "bob"), False, holds(c, "alice")]
    await alice.clear()

    alice2, bob2 = await ctx.v2("alice"), await ctx.v2("bob")
    cursor = (await v2_events(alice2, 0))[-1].event_id
    async with alice2.hold_lease(ttl_seconds=60):
        await alice2.queue()
        a2 = (await alice2.submit(V2_OPERATIONS["energy"], "1", ctx.energy())).operation_uid
    async with bob2.hold_lease(ttl_seconds=60):
        await bob2.queue()
        b2 = (await bob2.submit(V2_OPERATIONS["energy"], "1", ctx.energy())).operation_uid
        await bob2.reorder([b2, a2])
        await bob2.replace(a2, V2_OPERATIONS["energy"], "1", ctx.energy())
    async with alice2.hold_lease(ttl_seconds=60):
        await alice2.queue()
        await alice2.cancel(b2)
        c2 = (await alice2.submit(V2_OPERATIONS["energy"], "1", ctx.energy())).operation_uid
        events = await v2_events(alice2, cursor)
        await v2_clear(alice2)

    def recorded(event_type: str, user: str, uid: str | None) -> bool:
        return any(
            e.event_type == event_type and e.actor_id == user and (uid is None or e.operation_uid == uid)
            for e in events
        )

    v2_found = [
        recorded("operation.submitted", "alice", a2),
        recorded("operation.submitted", "bob", b2),
        recorded("queue.reordered", "bob", None),
        recorded("operation.replaced", "bob", a2),
        recorded("operation.cancelled", "alice", b2),
        recorded("operation.submitted", "alice", c2),
    ]
    for (user, action), found_v1, found_v2 in zip(AUDIT_STEPS, v1_found, v2_found, strict=True):
        result.add(f"{user} {action}", found_v1, found_v2)
    result.add("changes the server can attribute", sum(v1_found), sum(v2_found), "higher")
    result.notes.append(
        "v1's only record is the user on each queued or finished item, overwritten by an update; moves and"
        " removals leave none. v2 appends an event with the actor for every change, in the same transaction."
    )
    return result


@dataclass
class Watcher:
    """A GUI following one system: what it showed and when, and what it received."""

    views: list[tuple[float, tuple[str, ...]]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    bytes: int = 0
    messages: int = 0
    connected: asyncio.Event = field(default_factory=asyncio.Event)


async def watch_v1(api_key: str, watcher: Watcher) -> None:
    headers = {"Authorization": f"ApiKey {api_key}"}
    ws_url = lab.V1_URL.replace("http", "ws", 1) + "/api/status/ws"
    async with (
        httpx.AsyncClient(base_url=lab.V1_URL, headers=headers, timeout=30) as http,
        websockets.connect(ws_url, additional_headers=headers) as ws,
    ):
        watcher.connected.set()
        last = None
        async for raw in ws:
            text = raw if isinstance(raw, str) else raw.decode()
            watcher.bytes += len(text.encode())
            watcher.messages += 1
            status = json.loads(text).get("msg", {}).get("status")
            if status is None or status["plan_queue_uid"] == last:
                continue
            last = status["plan_queue_uid"]
            items = (await http.get("/api/queue/get")).json()["items"]
            watcher.views.append((time.monotonic(), tuple(item["item_uid"] for item in items)))


async def watch_v2(token: str, after: int, watcher: Watcher) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(30, read=None)
    async with httpx.AsyncClient(base_url=lab.CONTROLLER_URL, headers=headers, timeout=timeout) as http:
        stream = http.stream("GET", "/api/v2/events/stream", headers={"Last-Event-ID": str(after)})
        async with stream as response:
            watcher.connected.set()
            revision = None
            async for line in response.aiter_lines():
                watcher.bytes += len(line.encode()) + 1
                if line.startswith(":"):
                    watcher.messages += 1
                if not line.startswith("data:"):
                    continue
                watcher.messages += 1
                event = json.loads(line[5:])
                watcher.events.append(event)
                if event["queue_revision"] == revision:
                    continue
                revision = event["queue_revision"]
                queue = (await http.get("/api/v2/queue")).json()
                watcher.views.append((time.monotonic(), tuple(o["operation_uid"] for o in queue["operations"])))


@contextlib.asynccontextmanager
async def watching(
    start: Callable[[Watcher], Coroutine[Any, Any, None]], count: int
) -> AsyncIterator[list[Watcher]]:
    watchers = [Watcher() for _ in range(count)]
    tasks = [asyncio.create_task(start(w)) for w in watchers]
    try:
        async with asyncio.timeout(30):
            await asyncio.gather(*(w.connected.wait() for w in watchers))
        yield watchers
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


Change = tuple[float, tuple[str, ...]]  # when the change was requested, and the queue it leaves


async def drive(
    add: Callable[[], Awaitable[str]], remove: Callable[[str], Awaitable[None]], changes: int, pace: float
) -> list[Change]:
    """Make ``changes`` changes, half adds then removals of those, ``pace`` seconds apart."""
    queue: tuple[str, ...] = ()
    made: list[Change] = []
    for _ in range(changes // 2):
        start = time.monotonic()
        queue = (*queue, await add())
        made.append((start, queue))
        await asyncio.sleep(pace)
    for uid in list(queue):
        start = time.monotonic()
        await remove(uid)
        queue = tuple(u for u in queue if u != uid)
        made.append((start, queue))
        await asyncio.sleep(pace)
    return made


def delays(made: list[Change], watchers: list[Watcher]) -> tuple[list[float], int]:
    """Seconds from each change to each watcher showing it, and how many (change, watcher) pairs never did."""
    found, missed = [], 0
    for start, queue in made:
        for watcher in watchers:
            shown = next((t for t, view in watcher.views if t >= start and view == queue), None)
            if shown is None:
                missed += 1
            else:
                found.append(shown - start)
    return found, missed


async def sync(ctx: Lab) -> Result:
    count, changes, pace, idle = ctx.args.watchers, ctx.args.changes, ctx.args.pace, ctx.args.idle
    gap = 5
    result = Result(
        "sync",
        f"GUI sync: {count} GUIs follow the queue while one user makes {changes} changes {pace:g} s apart",
    )
    measured: dict[str, dict[str, Any]] = {}

    async def run(
        system: str,
        start: Callable[[Watcher], Coroutine[Any, Any, None]],
        restart: Callable[[Watcher], Callable[[Watcher], Coroutine[Any, Any, None]]],
        add: Callable[[], Awaitable[str]],
        remove: Callable[[str], Awaitable[None]],
    ) -> None:
        async with watching(start, count) as watchers:
            made = await drive(add, remove, changes, pace)
            await asyncio.sleep(1)
            before = [(w.bytes, w.messages) for w in watchers]
            await asyncio.sleep(idle)
            idle_bytes = [w.bytes - b for w, (b, _) in zip(watchers, before, strict=True)]
            idle_messages = [w.messages - m for w, (_, m) in zip(watchers, before, strict=True)]
            found, missed = delays(made, watchers)
        # A GUI that was away while `gap` plans were added: what does it learn when it comes back?
        async with watching(start, 1) as (away,):
            await asyncio.sleep(0.5)
        queue: tuple[str, ...] = ()
        expected = []
        for _ in range(gap):
            queue = (*queue, await add())
            expected.append(queue)
        async with watching(restart(away), 1) as (back,):
            await asyncio.sleep(2)
        if system == "v1":
            # Each state it shows after reconnecting is one it can tell happened.
            learned = sum(any(view == q for _, view in back.views) for q in expected)
        else:
            submitted = {e["operation_uid"] for e in back.events if e["event_type"] == "operation.submitted"}
            learned = sum(q[-1] in submitted for q in expected)
        for uid in queue:
            await remove(uid)
        per_minute = 60 / idle
        measured[system] = {
            "median_ms": statistics.median(found) * 1000 if found else None,
            "p95_ms": sorted(found)[int(0.95 * (len(found) - 1))] * 1000 if found else None,
            "missed": missed,
            "idle_bytes_per_min": statistics.mean(idle_bytes) * per_minute,
            "idle_messages_per_min": statistics.mean(idle_messages) * per_minute,
            "learned_after_gap": learned,
        }

    driver = await ctx.v1("alice")
    api_key = await ctx.v1_api_key("viewer")
    await run(
        "v1",
        lambda w: watch_v1(api_key, w),
        lambda _: lambda w: watch_v1(api_key, w),
        lambda: driver.add("energy", ctx.energy()),
        driver.remove,
    )

    lead = await ctx.v2("alice")
    token = ctx.v2_token("viewer")
    cursor = (await v2_events(lead, 0))[-1].event_id

    async def v2_add() -> str:
        await lead.queue()
        return (await lead.submit(V2_OPERATIONS["energy"], "1", ctx.energy())).operation_uid

    async def v2_remove(uid: str) -> None:
        await lead.queue()
        await lead.cancel(uid)

    async with lead.hold_lease(ttl_seconds=60):
        await run(
            "v2",
            lambda w: watch_v2(token, cursor, w),
            # The reconnecting GUI resumes after the last event it received.
            lambda away: lambda w: watch_v2(token, away.events[-1]["event_id"] if away.events else cursor, w),
            v2_add,
            v2_remove,
        )

    v1, v2 = measured["v1"], measured["v2"]
    result.add("change → every GUI, median ms", v1["median_ms"], v2["median_ms"], "lower")
    result.add("change → every GUI, p95 ms", v1["p95_ms"], v2["p95_ms"], "lower")
    result.add("(change, GUI) pairs never shown", v1["missed"], v2["missed"], "lower")
    result.add("idle traffic per GUI, bytes/min", v1["idle_bytes_per_min"], v2["idle_bytes_per_min"], "lower")
    result.add("idle messages per GUI, per min", v1["idle_messages_per_min"], v2["idle_messages_per_min"], "lower")
    result.add(
        f"of {gap} changes while a GUI was away, learned on reconnect",
        v1["learned_after_gap"],
        v2["learned_after_gap"],
        "higher",
    )
    result.notes.append(
        "v1 GUIs authenticate the status websocket with an API key: with an access token the handshake fails (HTTP"
        " 500) in this checkout. v1 pushes the full status every second whether or not anything changed."
    )
    result.notes.append(
        "On reconnect a v1 GUI can only read the queue as it is now; a v2 GUI resumes with Last-Event-ID and"
        " receives every event it missed."
    )
    return result


# The same invalid requests to both: (case, v1 add body, v2 submission body, and what each error should name).
def validation_cases() -> list[tuple[str, Any, Any, str | None, str | None]]:
    ok = {"energy": 7000.0, "crystal": "si111"}

    def both(kwargs: dict[str, Any], v1_name: str = "change_energy_operation", v2_id: str = "bmm.change_energy"):
        v1_body = {"item": {"item_type": "plan", "name": v1_name, "kwargs": kwargs}}
        v2_body = {"operation_id": v2_id, "operation_version": "1", "parameters": kwargs}
        return v1_body, v2_body

    return [
        ("valid request (control)", *both(ok), None, None),
        ("unknown plan", *both(ok, "no_such_plan", "no.such_plan"), "no_such_plan", "no.such_plan"),
        ("missing required parameter", *both({"crystal": "si111"}), "energy", "energy"),
        ("wrong type", *both({"energy": "high", "crystal": "si111"}), "energy", "energy"),
        ("out of range", *both({"energy": 9000.0, "crystal": "si111"}), "energy", "energy"),
        ("unknown device", *both({"energy": 7000.0, "crystal": "si999"}), "crystal", "crystal"),
        ("unexpected parameter", *both({**ok, "bogus": 1}), "bogus", "bogus"),
        ("malformed request", {"itemx": 1}, {"operation": 1}, None, None),
    ]


async def validation(ctx: Lab) -> Result:
    result = Result("validation", "Validation: the same invalid requests to both; refused, and how?")
    v1 = await ctx.v1("alice")
    lead = await ctx.v2("alice")
    rows: list[dict[str, Any]] = []
    accepted_v1: list[str] = []
    async with lead.hold_lease(ttl_seconds=60):
        raw = httpx.AsyncClient(
            base_url=lab.CONTROLLER_URL, headers={"Authorization": f"Bearer {ctx.v2_token('alice')}"}, timeout=30
        )
        async with raw:
            for case, v1_body, v2_body, v1_target, v2_target in validation_cases():
                response = await v1.request("POST", "/queue/item/add", v1_body)
                body = _json(response)
                ok = response.status_code == 200 and body.get("success") is True
                if ok:
                    accepted_v1.append(body["item"]["item_uid"])
                message = str(body.get("msg", "")) if isinstance(body, dict) else ""
                v1_row = {
                    "accepted": ok,
                    "status": response.status_code,
                    "code": None,
                    "located": False,
                    "named": bool(v1_target) and v1_target in message,
                }
                queue = await raw.get("/api/v2/queue")
                response = await raw.post(
                    "/api/v2/operations",
                    json=v2_body,
                    headers={"If-Match": queue.headers["ETag"], "Idempotency-Key": str(uuid.uuid4())},
                )
                error = _json(response).get("error", {})
                located = (
                    any(
                        v2_target in f"{e.get('path', '')} {e.get('message', '')} {e.get('loc', '')}"
                        for e in (error.get("details") or {}).get("errors", [])
                    )
                    if v2_target
                    else bool((error.get("details") or {}).get("errors"))
                )
                v2_row = {
                    "accepted": response.status_code == 201,
                    "status": response.status_code,
                    "code": error.get("code"),
                    "located": located,
                    "named": bool(v2_target)
                    and v2_target in str(error.get("message", "")) + json.dumps(error.get("details")),
                }
                rows.append({"case": case, "v1": v1_row, "v2": v2_row})
        for uid in accepted_v1:
            await v1.remove(uid)
        await v2_clear(lead)

    for row in rows:
        v1_row, v2_row = row["v1"], row["v2"]
        result.add(
            row["case"],
            f"{v1_row['status']} {'accepted' if v1_row['accepted'] else 'refused'}",
            f"{v2_row['status']} {v2_row['code'] or ('accepted' if v2_row['accepted'] else 'refused')}",
        )
    invalid = rows[1:]

    def total(system: str, key: str) -> int:
        return sum(bool(row[system][key]) for row in invalid)

    def error_status(system: str) -> int:
        return sum(row[system]["status"] >= 400 for row in invalid)

    result.add(
        f"invalid requests accepted (of {len(invalid)})", total("v1", "accepted"), total("v2", "accepted"), "lower"
    )
    result.add("refused with an HTTP error status", error_status("v1"), error_status("v2"), "higher")
    result.add("refused with a machine-readable code", total("v1", "code"), total("v2", "code"), "higher")
    result.add("problem located in a structured field", total("v1", "located"), total("v2", "located"), "higher")
    result.add("problem named somewhere in the reply", total("v1", "named"), total("v2", "named"), "higher")
    result.notes.append(
        "v1 validates the annotated plans when they are added, but answers every refusal, even a malformed body,"
        " with HTTP 200 and success: false and a free-text message. v2 answers 4xx with a stable code and, for"
        " parameters, the JSON path of each problem; its client runs the same check before sending."
    )
    return result


_RUNS_CODE = re.compile(r"script|function|/queue/item/execute")
_ADMINISTERS = re.compile(r"environment|permissions|kernel|/manager|/lock|/unlock|/config|history/clear|/test/")


def _resolve(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        node: Any = document
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def describe(document: dict[str, Any], raw_bytes: int) -> dict[str, Any]:
    operations = [
        (path, method, spec)
        for path, item in document.get("paths", {}).items()
        for method, spec in item.items()
        if method in ("get", "post", "put", "delete", "patch")
    ]
    mutating = [(p, m, s) for p, m, s in operations if m != "get"]

    def typed(spec: dict[str, Any]) -> bool:
        content = spec.get("requestBody", {}).get("content", {}).get("application/json")
        if content is None:
            return True  # nothing to type
        schema = _resolve(document, content.get("schema", {}))
        return bool(schema.get("properties"))

    def documents_errors(spec: dict[str, Any]) -> bool:
        return any(code[0] in "45" and code != "422" for code in spec.get("responses", {}))

    return {
        "bytes": raw_bytes,
        "tokens": raw_bytes // 4,
        "operations": len(operations),
        "mutating": len(mutating),
        "runs_code": sorted({p for p, _, _ in operations if _RUNS_CODE.search(p)}),
        "administers": sorted({p for p, _, _ in operations if _ADMINISTERS.search(p)}),
        "untyped_bodies": sum(not typed(s) for _, _, s in mutating),
        "documented_errors": sum(documents_errors(s) for _, _, s in operations),
    }


async def surface(ctx: Lab) -> Result:
    result = Result("surface", "Surface for an agent: each OpenAPI document read as a tool list")
    v1_token = await ctx.v1_token("alice")
    async with httpx.AsyncClient(timeout=30) as http:
        v1_anonymous = (await http.get(f"{lab.V1_URL}/openapi.json")).status_code
        v1_doc = await http.get(f"{lab.V1_URL}/openapi.json", headers={"Authorization": f"Bearer {v1_token}"})
        v2_anonymous = (await http.get(f"{lab.CONTROLLER_URL}/api/v2/openapi.json")).status_code
        v2_doc = await http.get(
            f"{lab.CONTROLLER_URL}/api/v2/openapi.json",
            headers={"Authorization": f"Bearer {ctx.v2_token('alice')}"},
        )
    v1, v2 = describe(v1_doc.json(), len(v1_doc.content)), describe(v2_doc.json(), len(v2_doc.content))
    result.add("readable without credentials", v1_anonymous == 200, v2_anonymous == 200)
    result.add("document size, approx. tokens", v1["tokens"], v2["tokens"])
    result.add("operations", v1["operations"], v2["operations"])
    result.add("operations that change state", v1["mutating"], v2["mutating"])
    result.add("operations that run code", len(v1["runs_code"]), len(v2["runs_code"]), "lower")
    result.add(
        "operations that administer worker or server", len(v1["administers"]), len(v2["administers"]), "lower"
    )
    result.add("state changes with an untyped body", v1["untyped_bodies"], v2["untyped_bodies"], "lower")
    result.add("operations documenting their errors", v1["documented_errors"], v2["documented_errors"], "higher")
    if v1["runs_code"]:
        result.notes.append(f"v1 operations that run code: {', '.join(v1['runs_code'])}")
    result.notes.append(
        "Websocket routes are not in either document; v1's status and console websockets are extra. v2's document"
        " includes its usage guide and the error codes of each route."
    )
    return result


async def fail_closed(ctx: Lab) -> Result:
    num = ctx.args.count_num
    result = Result(
        "fail-closed",
        f"Fail-closed: RE Worker is SIGKILLed during a {num}-point count with another plan queued behind it",
    )
    count_kwargs = {"detectors": ["xas_det"], "num": num, "delay": 1}

    def kill_worker(control_address: str) -> int:
        worker = re_manager_processes(control_address)["worker"]
        if worker is None:
            raise SystemExit(f"no RE Worker found for {control_address}")
        os.kill(worker, signal.SIGKILL)
        return worker

    # v1
    v1 = await ctx.v1("alice")
    p1 = await v1.add("count", count_kwargs)
    p2_kwargs = ctx.energy()
    await v1.add("energy", p2_kwargs)
    await v1.call("POST", "/queue/start", {})
    await v1.wait_for(lambda s: s["running_item_uid"] == p1, "the count to run", 120)
    await asyncio.sleep(1.5)
    kill_worker(ctx.v1_control)
    await v1.wait_for(
        lambda s: not s["worker_environment_exists"] and s["manager_state"] == "idle", "recovery", 120
    )
    last = (await v1.history())[-1]
    queued = (await v1.queue())["items"]
    v1_requeued = bool(queued) and queued[0]["name"] == V1_PLANS["count"] and queued[0]["kwargs"] == count_kwargs
    v1_outcome = last["result"]["exit_status"]
    result.notes.append(f"v1 recorded the interrupted plan as {v1_outcome}: {last['result']['msg']}")
    # The operator brings the worker back and restarts the queue.
    await v1.open_environment()
    v1_restart = (await v1.request("POST", "/queue/start", {})).json().get("success", False)
    await v1.wait_for(lambda s: s["running_item_uid"] is not None, "the next plan to run", 120)
    running = (await v1.queue())["running_item"]
    v1_rerun = running.get("name") == V1_PLANS["count"] and running.get("kwargs") == count_kwargs
    await v1.wait_for(
        lambda s: s["manager_state"] == "idle" and not s["items_in_queue"], "the queue to finish", 300
    )
    v1_next_ran = any(
        item["name"] == V1_PLANS["energy"]
        and item["kwargs"] == p2_kwargs
        and item["result"]["exit_status"] == "completed"
        for item in await v1.history()
    )

    # v2
    v2 = await ctx.v2("alice")
    async with v2.hold_lease(ttl_seconds=60):
        await v2.queue()
        q1 = (await v2.submit(V2_OPERATIONS["count"], "1", count_kwargs)).operation_uid
        q2 = (await v2.submit(V2_OPERATIONS["energy"], "1", ctx.energy())).operation_uid
        await v2.queue()
        await v2.start_execution("stop_on_failure")
        deadline = time.monotonic() + 120
        while (await v2.operation(q1)).state != "running":
            if time.monotonic() > deadline:
                raise TimeoutError("v2: timed out waiting for the count to run")
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.5)
        kill_worker(ctx.v2_control)
        deadline = time.monotonic() + 180
        while (state := (await v2.operation(q1)).state) == "running":
            if time.monotonic() > deadline:
                raise TimeoutError("v2: timed out waiting for the controller to judge the count")
            await asyncio.sleep(0.5)
        v2_outcome = state
        v2_requeued = q1 in [o.operation_uid for o in (await v2.queue()).operations]
        await v2.queue()
        try:
            await v2.start_execution("stop_on_failure")
            v2_restart = True
        except ApiError as error:
            v2_restart = False
            result.notes.append(f"v2 refused to start the queue: {error.code}")
        await v2.queue()
        await v2.acknowledge_recovery("compare.py: the worker was killed on purpose; nothing runs")
        deadline = time.monotonic() + 300
        while not (await v2.readiness()).ready:
            if time.monotonic() > deadline:
                raise TimeoutError("v2: timed out waiting for the controller to be ready again")
            await asyncio.sleep(1)
        await v2.queue()
        await v2.start_execution("stop_on_failure")
        while (await v2.operation(q2)).state not in ("succeeded", "failed", "aborted", "unknown"):
            if time.monotonic() > deadline:
                raise TimeoutError("v2: timed out waiting for the queued plan to end")
            await asyncio.sleep(0.5)
        v2_rerun = (await v2.operation(q1)).state != v2_outcome
        v2_next_ran = (await v2.operation(q2)).state == "succeeded"

    result.add("outcome recorded for the interrupted plan", v1_outcome, v2_outcome)
    result.add("interrupted plan put back in the queue", v1_requeued, v2_requeued)
    result.add("queue restarts without anyone acknowledging", bool(v1_restart), v2_restart)
    result.add("interrupted plan ran again", v1_rerun, v2_rerun)
    result.add("plan queued behind it ran after the restart", v1_next_ran, v2_next_ran)
    result.notes.append(
        "Both RE Managers come from the clone that detects a dead worker (A2); a released 0.x manager would keep"
        " reporting the plan as running. v1 pushes the interrupted plan back to the front of the queue with a new"
        " uid, so the next queue start runs it again. v2 records it as unknown and refuses to dispatch until an"
        " operator acknowledges; it never runs it again."
    )
    return result


SCENARIOS: dict[str, Callable[[Lab], Awaitable[Result]]] = {
    "edits": edits,
    "retries": retries,
    "audit": audit,
    "sync": sync,
    "validation": validation,
    "surface": surface,
    "fail-closed": fail_closed,
}


async def compare(args: argparse.Namespace) -> list[Result]:
    ctx = Lab(args)
    results: list[Result] = []
    async with ctx.stack:
        await prepare(ctx)
        for name in args.scenarios:
            print(f"\nrunning {name} ...", flush=True)
            results.append(await SCENARIOS[name](ctx))
            print_result(results[-1])
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default = [name for name in SCENARIOS if name != "fail-closed"]
    parser.add_argument(
        "--scenarios",
        type=lambda value: value.split(","),
        default=default,
        help=f"comma-separated, from {', '.join(SCENARIOS)} (default: all but fail-closed)",
    )
    parser.add_argument(
        "--kill-worker", action="store_true", help="also run fail-closed, which SIGKILLs RE Worker"
    )
    parser.add_argument("--trials", type=int, default=20, help="edits: trials (default: 20)")
    parser.add_argument("--editors", type=int, default=4, help="edits: concurrent clients (default: 4)")
    parser.add_argument("--submissions", type=int, default=10, help="retries: submissions (default: 10)")
    parser.add_argument("--watchers", type=int, default=5, help="sync: GUIs following the queue (default: 5)")
    parser.add_argument("--changes", type=int, default=10, help="sync: queue changes (default: 10)")
    parser.add_argument("--pace", type=float, default=0.5, help="sync: seconds between changes (default: 0.5)")
    parser.add_argument(
        "--idle", type=float, default=30, help="sync: idle seconds measured; v2 heartbeats every 15 (default: 30)"
    )
    parser.add_argument("--count-num", type=int, default=6, help="fail-closed: points of the count (default: 6)")
    parser.add_argument("--seed", type=int, default=None, help="random seed (default: random)")
    parser.add_argument("--json", help="also write every measurement to this file")
    args = parser.parse_args()
    if unknown := set(args.scenarios) - set(SCENARIOS):
        parser.error(f"unknown scenarios {sorted(unknown)}; choose from {', '.join(SCENARIOS)}")
    if "fail-closed" in args.scenarios and not args.kill_worker:
        parser.error("fail-closed SIGKILLs RE Worker; pass --kill-worker to run it")
    if args.kill_worker and "fail-closed" not in args.scenarios:
        args.scenarios.append("fail-closed")
    if args.editors < 2:
        parser.error("--editors must be at least 2")
    if args.seed is None:
        args.seed = random.randrange(2**32)
    print(f"comparing v1 at {lab.V1_URL} with v2 at {lab.CONTROLLER_URL}, seed {args.seed}", flush=True)
    results = asyncio.run(compare(args))
    if args.json:
        with open(args.json, "w") as file:
            json.dump({"seed": args.seed, "results": [asdict(r) for r in results]}, file, indent=2, default=str)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
