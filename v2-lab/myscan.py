from bluesky_httpserver.v2.client import QueueServerClient
from lab import CONTROLLER_URL, mint

with QueueServerClient(CONTROLLER_URL, lambda: mint("alice", "admin")) as client, client.hold_lease():
    client.submit(
        "bmm.scan_energy", "1", {"detectors": ["xas_det"], "energies": [6500, 7000, 7500], "crystal": "si111"}
    )
    execution = client.start_execution()
    for event in client.stream_events():
        if event.queue_execution_uid == execution.queue_execution_uid:
            print(event.event_type, event.payload)
            if event.event_type in (
                "queue_execution.completed",
                "queue_execution.stopped",
                "queue_execution.blocked",
            ):
                break
