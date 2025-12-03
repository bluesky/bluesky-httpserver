import json
import pprint
import re
import threading
import time as ttime
from websockets.sync.client import connect

import pytest
import requests
from bluesky_queueserver.manager.tests.common import re_manager_cmd  # noqa F401

from bluesky_httpserver.tests.conftest import (  # noqa F401
    API_KEY_FOR_TESTS,
    SERVER_ADDRESS,
    SERVER_PORT,
    fastapi_server_fs,
    request_to_json,
    set_qserver_zmq_encoding,
    wait_for_environment_to_be_closed,
    wait_for_environment_to_be_created,
    wait_for_manager_state_idle,
)


class _ReceiveSystemInfoSocket(threading.Thread):
    """
    Catch streaming console output by connecting to /console_output/ws socket and 
    save messages to the buffer.
    """

    def __init__(self, *, endpoint, api_key=API_KEY_FOR_TESTS, **kwargs):
        super().__init__(**kwargs)
        self.received_data_buffer = []
        self._exit = False
        self._api_key = api_key
        self._endpoint = endpoint

    def run(self):
        websocket_uri = f"ws://{SERVER_ADDRESS}:{SERVER_PORT}/api{self._endpoint}"
        with connect(websocket_uri) as websocket:
            while not self._exit:
                try:
                    msg_json = websocket.recv(timeout=0.1, decode=False)
                    try:
                        msg = json.loads(msg_json)
                        self.received_data_buffer.append(msg)
                    except json.JSONDecodeError as e:
                        pass
                except TimeoutError:
                    pass

    def stop(self):
        """
        Call this method to stop the thread. Then send a request to the server so that some output
        is printed in ``stdout``.
        """
        self._exit = True

    def __del__(self):
        self.stop()


@pytest.mark.parametrize("zmq_port", (None, 60619))
@pytest.mark.parametrize("endpoint", ["/info/ws", "/status/ws"])
def test_http_server_system_info_socket_1(
    monkeypatch, re_manager_cmd, fastapi_server_fs, zmq_port, endpoint  # noqa F811
):
    """
    Test for ``/info/ws`` and ``/status/ws`` websockets
    """
    # Start HTTP Server
    if zmq_port is not None:
        monkeypatch.setenv("QSERVER_ZMQ_INFO_ADDRESS", f"tcp://localhost:{zmq_port}")
    fastapi_server_fs()

    # Start RE Manager
    params = ["--zmq-publish-console", "ON"]
    if zmq_port is not None:
        params.extend(["--zmq-info-addr", f"tcp://*:{zmq_port}"])
    re_manager_cmd(params)

    rsc = _ReceiveSystemInfoSocket(endpoint=endpoint)
    rsc.start()
    ttime.sleep(1)  # Wait until the client connects to the socket

    resp1 = request_to_json("post", "/environment/open")
    assert resp1["success"] is True, pprint.pformat(resp1)

    assert wait_for_environment_to_be_created(timeout=10)

    resp2b = request_to_json("post", "/environment/close")
    assert resp2b["success"] is True, pprint.pformat(resp2b)

    assert wait_for_environment_to_be_closed(timeout=10)

    # Wait until capture is complete
    ttime.sleep(2)
    rsc.stop()
    rsc.join()

    buffer = rsc.received_data_buffer
    assert len(buffer) > 0
    for msg in buffer:
        assert "time" in msg, msg
        assert isinstance(msg["time"], float), msg
        assert "msg"  in msg
        assert isinstance(msg["msg"], dict)
        
    if endpoint == "/status/ws":
        for msg in buffer:
            assert "status" in msg["msg"], msg
            assert isinstance(msg["msg"]["status"], dict), msg
    elif endpoint == "/info/ws":
        for msg in buffer:
            if "status" in msg["msg"]:
                assert isinstance(msg["msg"]["status"], dict), msg
    else:
        assert False, f"Unknown endpoint: {endpoint}"

    # In the test we opened and then closed the environment, so let's check if it is reflected in
    #   the collected streamed status.
    wrk_env_exists = [_["msg"]["status"]["worker_environment_exists"] for _ in buffer if "status" in _["msg"]]
    assert wrk_env_exists.count(True) >= 0, wrk_env_exists
    assert wrk_env_exists.count(False) >= 0, wrk_env_exists

