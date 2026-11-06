"""Run one ``tst.count`` through the v2 controller and print what happens.

    pixi run demo [DETECTOR ...]      (default: xas_det)

Waits until the controller is ready, takes control, queues the count, starts the queue and follows its events
until the queue execution ends; then releases control.
"""

from __future__ import annotations

import sys
import time

from bluesky_httpserver.v2.client import QueueServerClient

from lab import CONTROLLER_URL, mint

_ENDS = {"queue_execution.completed", "queue_execution.stopped", "queue_execution.blocked"}


def wait_until_ready(client: QueueServerClient, timeout_seconds: float = 300) -> None:
    """Wait for the controller to report itself ready; opening the worker environment takes a while."""
    deadline = time.monotonic() + timeout_seconds
    while not (readiness := client.readiness()).ready:
        if time.monotonic() > deadline:
            sys.exit(f"the controller is not ready: {readiness}")
        print(f"waiting for the controller: {readiness}")
        time.sleep(3)


def newest_event_id(client: QueueServerClient) -> int:
    cursor = 0
    while events := client.events(after=cursor, limit=1000):
        cursor = events[-1].event_id
    return cursor


def main() -> None:
    detectors = sys.argv[1:] or ["xas_det"]
    with QueueServerClient(CONTROLLER_URL, lambda: mint("alice", "admin")) as client:
        wait_until_ready(client)
        catalog = client.catalog()
        print("catalog:", ", ".join(f"{d.operation_id} v{d.operation_version}" for d in catalog.operations))
        cursor = newest_event_id(client)
        with client.hold_lease(ttl_seconds=60):
            operation = client.submit("tst.count", "1", {"detectors": detectors, "num": 2})
            print(f"queued {operation.operation_id} as {operation.operation_uid}")
            execution = client.start_execution()
            for event in client.stream_events(after=cursor):
                if event.queue_execution_uid != execution.queue_execution_uid:
                    continue
                print(f"{event.timestamp:%H:%M:%S}  {event.event_type:<26} {event.payload or ''}")
                if event.event_type == "queue_execution.blocked":
                    print(
                        "An outcome could not be proven, so nothing runs until recovery is acknowledged:"
                        " check that the worker is idle, then call acknowledge_recovery."
                    )
                if event.event_type in _ENDS:
                    break


if __name__ == "__main__":
    main()
