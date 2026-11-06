"""Command line of the v2 controller."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
import tomllib
from collections.abc import Sequence

from pydantic import ValidationError

from .server import serve
from .settings import Settings
from .store import StoreError, admin
from .store.migrations import MIGRATIONS
from .zmq_relay import ZmqManagerRelay


def _serve(config: str) -> int:
    try:
        settings = Settings.load(config)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        print(f"error: {config}: {error}", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    manager = settings.manager
    relay = ZmqManagerRelay(
        manager.control_address, manager.info_address, timeout_seconds=manager.request_timeout_seconds
    )
    try:
        served = asyncio.run(serve(settings, relay))
    except KeyboardInterrupt:
        served = True  # the server shut down gracefully, then uvicorn raised SIGINT again
    finally:
        relay.close()
    return 0 if served else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run one ``bluesky-httpserver-v2`` command.

    Parameters
    ----------
    argv : sequence of str, optional
        Arguments after the program name; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit status: 0 on success, 1 if the command failed or, for ``serve``, the worker gateway failed.
        ``serve`` exits with status 3 if the controller fails to start.
    """
    parser = argparse.ArgumentParser(
        prog="bluesky-httpserver-v2",
        description="Run the v2 controller, or maintain its store while the controller is stopped.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve_command = commands.add_parser("serve", help="run the controller until it is stopped")
    serve_command.add_argument("--config", required=True, help="settings file (TOML)")
    migrate = commands.add_parser("migrate", help="apply pending schema migrations")
    migrate.add_argument("store")
    backup = commands.add_parser("backup", help="copy the store to a new file")
    backup.add_argument("store")
    backup.add_argument("destination")
    restore = commands.add_parser("restore", help="replace the store with a backup")
    restore.add_argument("backup")
    restore.add_argument("store")
    restore.add_argument("--replace", action="store_true", help="overwrite an existing store")
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args.config)
    try:
        if args.command == "migrate":
            version = asyncio.run(admin.migrate(args.store))
            message = f"{args.store} is at schema version {version}"
        elif args.command == "backup":
            admin.backup(args.store, args.destination)
            message = f"backed up {args.store} to {args.destination}"
        else:
            version = admin.restore(args.backup, args.store, replace=args.replace)
            message = f"restored {args.store} from {args.backup} at schema version {version}"
            if version < len(MIGRATIONS):
                message += f"; run 'bluesky-httpserver-v2 migrate {args.store}' before serving"
    except (StoreError, OSError, sqlite3.Error) as error:
        hint = (
            "; pass --replace to overwrite it"
            if args.command == "restore" and isinstance(error, FileExistsError)
            else ""
        )
        print(f"error: {error}{hint}", file=sys.stderr)
        return 1
    print(message)
    return 0
