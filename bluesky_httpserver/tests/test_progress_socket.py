import json
import pprint
import threading
import time as ttime

import pytest
from bluesky_queueserver.manager.tests.common import (  # noqa F401
    append_code_to_last_startup_file,
    copy_default_profile_collection,
    re_manager_cmd,
)
from websockets.sync.client import connect

from bluesky_httpserver.tests.conftest import (  # noqa F401
    API_KEY_FOR_TESTS,
    SERVER_ADDRESS,
    SERVER_PORT,
    fastapi_server_fs,
    request_to_json,
    set_qserver_zmq_encoding,
    wait_for_environment_to_be_closed,
    wait_for_environment_to_be_created,
    wait_for_queue_execution_to_complete,
)

# Startup code that defines a slow (delayed) motor and a plan that moves it. Moving a delayed
# motor makes the RE 'waiting_hook' emit incremental watcher updates (name/current/fraction/...),
# as opposed to the instantaneous demo motors which only produce completion messages.
_progress_test_startup_code = """
from ophyd.sim import SynAxis as _SynAxisProgressTest
from bluesky.plan_stubs import mv as _mv_progress_test

motor_slow = _SynAxisProgressTest(name="motor_slow", delay=0.6)


def progress_test_plan(npts: int = 5):
    for _i in range(npts):
        yield from _mv_progress_test(motor_slow, _i + 1)
"""


class _ReceiveProgressSocket(threading.Thread):
    """
    Catch streaming progress updates by connecting to the ``/progress/ws`` socket and
    save messages to the buffer.
    """

    def __init__(self, *, endpoint="/progress/ws", api_key=API_KEY_FOR_TESTS, **kwargs):
        super().__init__(**kwargs)
        self.received_data_buffer = []
        self._exit = False
        self._api_key = api_key
        self._endpoint = endpoint

    def run(self):
        websocket_uri = f"ws://{SERVER_ADDRESS}:{SERVER_PORT}/api{self._endpoint}"
        additional_headers = {"Authorization": f"ApiKey {self._api_key}"}
        try:
            with connect(websocket_uri, additional_headers=additional_headers) as websocket:
                while not self._exit:
                    try:
                        msg_json = websocket.recv(timeout=0.1, decode=False)
                        try:
                            msg = json.loads(msg_json)
                            self.received_data_buffer.append(msg)
                        except json.JSONDecodeError:
                            pass
                    except TimeoutError:
                        pass
        except Exception as ex:
            print(f"Failed to connect to server: {ex}")

    def stop(self):
        self._exit = True

    def __del__(self):
        self.stop()


@pytest.mark.parametrize("zmq_port", (None, 60619))
def test_http_server_progress_socket_1(
    monkeypatch, re_manager_cmd, fastapi_server_fs, zmq_port  # noqa F811
):
    """
    Test for the ``/progress/ws`` websocket. Runs a plan that moves a motor so that the
    RE Manager publishes ``waiting_hook`` (watcher) progress updates over 0MQ, and verifies
    that the updates are streamed to the connected websocket client.
    """
    # Start HTTP Server
    if zmq_port is not None:
        monkeypatch.setenv("QSERVER_ZMQ_INFO_ADDRESS", f"tcp://localhost:{zmq_port}")
    fastapi_server_fs()

    # Start RE Manager with progress publishing enabled
    params = ["--zmq-publish-progress", "ON"]
    if zmq_port is not None:
        params.extend(["--zmq-info-addr", f"tcp://*:{zmq_port}"])
    re_manager_cmd(params)

    rps = _ReceiveProgressSocket()
    rps.start()
    ttime.sleep(1)  # Wait until the client connects to the socket

    resp1 = request_to_json("post", "/environment/open")
    assert resp1["success"] is True, pprint.pformat(resp1)
    assert wait_for_environment_to_be_created(timeout=10)

    # A 'scan' moves 'motor', which makes the RE wait on status objects and publish progress updates
    plan = {"name": "scan", "args": [["det"], "motor", -1, 1, 10], "item_type": "plan"}
    resp2 = request_to_json("post", "/queue/item/add", json={"item": plan})
    assert resp2["success"] is True, pprint.pformat(resp2)

    resp3 = request_to_json("post", "/queue/start")
    assert resp3["success"] is True, pprint.pformat(resp3)

    assert wait_for_queue_execution_to_complete(timeout=30)

    resp4 = request_to_json("post", "/environment/close")
    assert resp4["success"] is True, pprint.pformat(resp4)
    assert wait_for_environment_to_be_closed(timeout=10)

    # Wait until capture is complete
    ttime.sleep(2)
    rps.stop()
    rps.join()

    buffer = rps.received_data_buffer
    assert len(buffer) > 0, "No progress updates were received"
    for msg in buffer:
        assert "time" in msg, msg
        assert isinstance(msg["time"], float), msg
        assert "msg" in msg, msg
        assert isinstance(msg["msg"], dict), msg

    # The RE sends a completion message each time it finishes waiting on status objects.
    completion_msgs = [_ for _ in buffer if _["msg"].get("completed") is True]
    assert len(completion_msgs) > 0, pprint.pformat(buffer)


def test_http_server_progress_socket_2(
    tmp_path, monkeypatch, re_manager_cmd, fastapi_server_fs  # noqa F811
):
    """
    Test that incremental (watcher) progress updates are streamed over ``/progress/ws``.
    Runs a plan that moves a delayed motor, which makes the RE Manager publish per-status
    progress updates (with ``name``/``current``/``fraction``) and not just completion messages.
    """
    # Prepare a startup profile with a delayed motor and a plan that moves it
    pc_path = copy_default_profile_collection(tmp_path)
    append_code_to_last_startup_file(pc_path, _progress_test_startup_code)

    fastapi_server_fs()

    # 'ENVIRONMENT_OPEN' regenerates the list of existing plans/devices so 'progress_test_plan' is known
    params = [
        "--zmq-publish-progress",
        "ON",
        "--startup-dir",
        pc_path,
        "--update-existing-plans-devices",
        "ENVIRONMENT_OPEN",
    ]
    re_manager_cmd(params)

    rps = _ReceiveProgressSocket()
    rps.start()
    ttime.sleep(1)  # Wait until the client connects to the socket

    resp1 = request_to_json("post", "/environment/open")
    assert resp1["success"] is True, pprint.pformat(resp1)
    assert wait_for_environment_to_be_created(timeout=10)

    plan = {"name": "progress_test_plan", "kwargs": {"npts": 5}, "item_type": "plan"}
    resp2 = request_to_json("post", "/queue/item/add", json={"item": plan})
    assert resp2["success"] is True, pprint.pformat(resp2)

    resp3 = request_to_json("post", "/queue/start")
    assert resp3["success"] is True, pprint.pformat(resp3)

    assert wait_for_queue_execution_to_complete(timeout=30)

    resp4 = request_to_json("post", "/environment/close")
    assert resp4["success"] is True, pprint.pformat(resp4)
    assert wait_for_environment_to_be_closed(timeout=10)

    # Wait until capture is complete
    ttime.sleep(2)
    rps.stop()
    rps.join()

    buffer = rps.received_data_buffer
    assert len(buffer) > 0, "No progress updates were received"
    for msg in buffer:
        assert "time" in msg, msg
        assert isinstance(msg["time"], float), msg
        assert "msg" in msg, msg
        assert isinstance(msg["msg"], dict), msg

    # Incremental watcher updates carry the moved device name (not just {"completed": True})
    incremental_msgs = [_ for _ in buffer if _["msg"].get("name") == "motor_slow"]
    assert len(incremental_msgs) > 0, pprint.pformat(buffer)
    for msg in incremental_msgs:
        m = msg["msg"]
        assert "current" in m, msg
        assert "fraction" in m, msg
        assert "done" in m, msg

    # Completion messages are still sent when each wait finishes
    completion_msgs = [_ for _ in buffer if _["msg"].get("completed") is True]
    assert len(completion_msgs) > 0, pprint.pformat(buffer)

