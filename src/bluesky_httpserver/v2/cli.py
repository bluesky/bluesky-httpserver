"""Command line of the v2 controller."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections.abc import Sequence

from .store import StoreError, admin
from .store.migrations import MIGRATIONS


def main(argv: Sequence[str] | None = None) -> int:
    """Run one ``bluesky-httpserver-v2`` command.

    Parameters
    ----------
    argv : sequence of str, optional
        Arguments after the program name; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit status: 0 on success, 1 if the command failed.
    """
    parser = argparse.ArgumentParser(
        prog="bluesky-httpserver-v2",
        description="Maintain the v2 controller's store; the controller must be stopped.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
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
