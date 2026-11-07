"""A web console for the v2 lab: one page that operates the controller and demonstrates what v2 guarantees.

    pixi run ui [--port 8080] [--config state/controller.toml]      then open http://127.0.0.1:8080

The page talks to the controller's public API through this server, which forwards requests unchanged (same origin,
so the browser needs no CORS) and adds lab-only helpers under /lab, none of which the controller offers:

- POST /lab/token         stands in for signing in at the identity provider: returns an access token minted with
                          the lab's signing key, or, for the identity demos, a deliberately bad one;
- GET  /lab/processes     finds the RE Manager watchdog, manager and worker processes serving the controller;
- POST /lab/faults/kill-worker
                          SIGKILLs RE Worker, so the controller cannot prove how the running operation ended;
- POST /lab/faults/freeze-manager
                          SIGSTOPs RE Manager and its watchdog for FREEZE_SECONDS, so RE Manager stops answering
                          for longer than the controller's request timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

LAB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB))

import lab  # noqa: E402  (lab.py lives one directory up)

STATIC = Path(__file__).resolve().parent / "static"

# Request headers the API reads, and response headers the page shows; hop-by-hop headers are never forwarded.
_REQUEST_HEADERS = ("authorization", "content-type", "accept", "if-match", "idempotency-key", "last-event-id")
_RESPONSE_HEADERS = ("content-type", "etag", "x-request-id", "www-authenticate", "cache-control")

# Longer than the controller's 5 s request timeout, shorter than its 60 s grace window for a live operation.
FREEZE_SECONDS = 15

PERSONAS = [
    {"sub": "alice", "role": "admin", "title": "Instrument scientist"},
    {"sub": "bob", "role": "run", "title": "Beamline operator"},
    {"sub": "carol", "role": "edit", "title": "Visiting user"},
    {"sub": "dave", "role": "read", "title": "Remote observer"},
    {"sub": "agent-7", "role": "run", "title": "Autonomous agent"},
]

# The client application the browser UI's tokens are issued to.
CLIENT = "lab-ui"

# Tokens the identity demos send; each must be refused with 401 before any handler runs.
BAD_TOKENS = {
    "expired": "Signed by the identity provider, but expired an hour ago",
    "wrong_audience": "Signed by the identity provider for a different API",
    "wrong_issuer": "Signed with the right key, but claiming another issuer",
    "missing_client": "Signed by the identity provider, but naming no client application (azp)",
    "hs256": "HS256 with a shared secret, the way v1 minted its own tokens",
    "alg_none": "Unsigned (alg: none)",
    "foreign_key": "Signed by an attacker's RSA key, reusing the provider's key id",
}


class Lab:
    """Where the controller and its RE Manager are, read from the controller's settings file."""

    def __init__(self, config: Path) -> None:
        settings = tomllib.loads(config.read_text())
        server = settings.get("server", {})
        self.config = config
        self.controller_url = f"http://{server.get('host', '127.0.0.1')}:{server.get('port', 8000)}"
        self.control_address: str = settings["manager"]["control_address"]
        self.store_path: str = settings["store_path"]


def _claims(sub: str, role: str, ttl_seconds: int, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": lab.ISSUER,
        "aud": lab.AUDIENCE,
        "sub": sub,
        "azp": CLIENT,
        "scope": f"QueueServer.{role.capitalize()}",
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
    }
    claims.update(overrides)
    return claims


def bad_token(kind: str, sub: str, role: str) -> str:
    """Return a token of ``kind`` (a key of `BAD_TOKENS`) for ``sub`` with ``role``."""
    key = lab._signing_key()
    kid = lab._public_jwk(key)["kid"]
    if kind == "expired":
        now = int(time.time())
        claims = _claims(sub, role, 0, iat=now - 7200, nbf=now - 7200, exp=now - 3600)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
    if kind == "wrong_audience":
        return jwt.encode(
            _claims(sub, role, 600, aud="some-other-api"), key, algorithm="RS256", headers={"kid": kid}
        )
    if kind == "wrong_issuer":
        claims = _claims(sub, role, 600, iss="https://idp.example.org")
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
    if kind == "missing_client":
        claims = _claims(sub, role, 600)
        del claims["azp"]
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
    if kind == "hs256":
        return jwt.encode(_claims(sub, role, 600), "secret", algorithm="HS256")
    if kind == "alg_none":
        return jwt.encode(_claims(sub, role, 600), "", algorithm="none", headers={"kid": kid})
    if kind == "foreign_key":
        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return jwt.encode(_claims(sub, role, 600), attacker, algorithm="RS256", headers={"kid": kid})
    raise KeyError(kind)


def _ppid(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    # The command name in parentheses may contain spaces; the parent pid is the second field after it.
    return int(stat.rsplit(")", 1)[1].split()[1])


def _socket_inode(path: str) -> str | None:
    """Inode of the listening Unix socket bound at ``path``, from /proc/net/unix."""
    for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[7] == path:
            return fields[6]
    return None


def _holds_socket(pid: int, inode: str) -> bool:
    target = f"socket:[{inode}]"
    try:
        return any(os.readlink(fd) == target for fd in Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False


def re_manager_processes(control_address: str) -> dict[str, int | None]:
    """PIDs of the RE Manager watchdog, manager and worker bound to ``control_address``.

    start-re-manager forks its processes, so they all share its command line. The manager is the one holding the
    listening control socket, its parent is the watchdog, and the worker is the watchdog's other child. Any of
    them may be None, e.g. the worker while no environment is open.
    """
    socket_path = os.path.realpath(control_address.removeprefix("ipc://"))
    candidates: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            if not any(arg.endswith("start-re-manager") for arg in argv):
                continue
            addresses = [arg.split("=", 1)[1] for arg in argv if arg.startswith("--zmq-control-addr=")]
            if not any(os.path.realpath(a.removeprefix("ipc://")) == socket_path for a in addresses):
                continue
            candidates[int(entry.name)] = _ppid(int(entry.name))
        except (OSError, ValueError):
            continue
    inode = _socket_inode(socket_path)
    holders = [pid for pid in candidates if inode and _holds_socket(pid, inode)]
    manager = next((pid for pid in holders if candidates[pid] not in holders), None)
    watchdog = None if manager is None else candidates[manager]
    workers = [pid for pid, ppid in candidates.items() if ppid == watchdog and pid != manager]
    return {"watchdog": watchdog, "manager": manager, "worker": workers[0] if len(workers) == 1 else None}


def create_app(settings: Lab) -> Starlette:
    http = httpx.AsyncClient(base_url=settings.controller_url, timeout=httpx.Timeout(30.0, read=None))
    frozen: set[int] = set()

    def thaw(pids: list[int]) -> None:
        for pid in pids:
            frozen.discard(pid)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        try:
            yield
        finally:
            # Never leave RE Manager stopped behind us.
            thaw(list(frozen))
            await http.aclose()

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    async def info(request: Request) -> Response:
        return JSONResponse(
            {
                "controller_url": settings.controller_url,
                "controller_config": str(settings.config),
                "store_path": settings.store_path,
                "issuer": lab.ISSUER,
                "audience": lab.AUDIENCE,
                "personas": PERSONAS,
                "roles": list(lab.ROLES),
                "bad_tokens": BAD_TOKENS,
            }
        )

    async def token(request: Request) -> Response:
        body = await request.json()
        sub, role, kind = body.get("sub", "alice"), body.get("role", "admin"), body.get("kind", "valid")
        if role not in lab.ROLES:
            return JSONResponse({"error": f"role must be one of {', '.join(lab.ROLES)}"}, status_code=400)
        if kind == "valid":
            ttl = int(body.get("ttl_seconds", 3600))
            encoded = lab.mint(sub, role, ttl, CLIENT)
        elif kind in BAD_TOKENS:
            encoded = bad_token(kind, sub, role)
        else:
            return JSONResponse({"error": f"unknown token kind {kind!r}"}, status_code=400)
        return JSONResponse(
            {
                "token": encoded,
                "header": jwt.get_unverified_header(encoded),
                "claims": jwt.decode(encoded, options={"verify_signature": False}),
            }
        )

    async def processes(request: Request) -> Response:
        return JSONResponse(re_manager_processes(settings.control_address))

    async def fault(request: Request) -> Response:
        target = request.path_params["target"]
        found = re_manager_processes(settings.control_address)
        if target == "kill-worker":
            if found["worker"] is None:
                return JSONResponse(
                    {"error": f"no RE Worker found for {settings.control_address}"}, status_code=409
                )
            os.kill(found["worker"], signal.SIGKILL)
            return JSONResponse({"killed": "worker", "pid": found["worker"]})
        if target == "freeze-manager":
            if found["manager"] is None or found["watchdog"] is None:
                return JSONResponse(
                    {"error": f"no RE Manager found for {settings.control_address}"}, status_code=409
                )
            # The watchdog too: it would otherwise restart the frozen manager within seconds.
            pids = [found["watchdog"], found["manager"]]
            for pid in pids:
                os.kill(pid, signal.SIGSTOP)
            frozen.update(pids)
            asyncio.get_running_loop().call_later(FREEZE_SECONDS, thaw, pids)
            return JSONResponse({"frozen": pids, "seconds": FREEZE_SECONDS})
        return JSONResponse({"error": "target must be kill-worker or freeze-manager"}, status_code=404)

    async def proxy(request: Request) -> Response:
        headers = {name: value for name, value in request.headers.items() if name in _REQUEST_HEADERS}
        upstream = http.build_request(
            request.method,
            httpx.URL(request.url.path, query=request.url.query.encode()),
            headers=headers,
            content=await request.body(),
        )
        try:
            response = await http.send(upstream, stream=True)
        except httpx.TransportError as error:
            message = f"the controller at {settings.controller_url} does not answer: {error!r}"
            return JSONResponse({"error": {"code": "controller_unreachable", "message": message}}, status_code=502)

        async def body() -> AsyncIterator[bytes]:
            # Closed when the browser goes away too, so an abandoned event stream frees its upstream connection.
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            body(),
            status_code=response.status_code,
            headers={name: value for name, value in response.headers.items() if name in _RESPONSE_HEADERS},
        )

    methods = ["GET", "POST", "PUT", "DELETE"]
    return Starlette(
        routes=[
            Route("/", index),
            Route("/lab/info", info),
            Route("/lab/token", token, methods=["POST"]),
            Route("/lab/processes", processes),
            Route("/lab/faults/{target}", fault, methods=["POST"]),
            Route("/health", proxy),
            Route("/ready", proxy),
            Route("/api/v2/{path:path}", proxy, methods=methods),
            Mount("/static", StaticFiles(directory=STATIC)),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--config", type=Path, default=lab.SETTINGS, help="the controller's settings file")
    args = parser.parse_args()
    lab.init()
    settings = Lab(args.config.resolve())
    print(f"v2 lab console at http://{args.host}:{args.port}  (controller {settings.controller_url})", flush=True)
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
