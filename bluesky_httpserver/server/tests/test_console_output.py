import requests
import json
import threading
import pprint
import pytest
import time as ttime

from bluesky_httpserver.server.tests.conftest import SERVER_ADDRESS, SERVER_PORT, request_to_json
from bluesky_httpserver.server.tests.conftest import fastapi_server_fs  # noqa F401
from bluesky_queueserver.manager.tests.common import re_manager_cmd  # noqa F401


class _ReceiveStreamedConsoleOutput(threading.Thread):
    """
    Catch streaming console output and save messages to the buffer. The method is intended
    for testing and may not be well suited for production code. Stop the thread first call
    ``stop`` method then send some request to the server so that it prints some output.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.received_data_buffer = []
        self._exit = False

    def run(self):
        with requests.get(f"http://{SERVER_ADDRESS}:{SERVER_PORT}/stream_console_output", stream=True) as r:
            r.encoding = "utf-8"

            characters = []
            n_brackets = 0

            for ch in r.iter_content(decode_unicode=True):
                # Note, that some output must be received from the server before the loop exits
                if self._exit:
                    break

                characters.append(ch)
                if ch == "{":
                    n_brackets += 1
                elif ch == "}":
                    n_brackets -= 1

                # If the received buffer ('characters') is not empty and the message contains
                #   equal number of opening and closing brackets then consider the message complete.
                if characters and not n_brackets:
                    line = "".join(characters)
                    characters = []

                    print(f"{line}")
                    self.received_data_buffer.append(json.loads(line))

    def stop(self):
        """
        Call this method to stop the thread. Then send a request to the server so that some output
        is printed in ``stdout``.
        """
        self._exit = True

    def __del__(self):
        self.stop()


@pytest.mark.parametrize("zmq_port", (None, 60619))
def test_http_server_stream_console_output_1(
    monkeypatch, re_manager_cmd, fastapi_server_fs, zmq_port  # noqa F811
):
    """
    Test for ``stream_console_output`` API
    """
    # Start HTTP Server
    if zmq_port is not None:
        monkeypatch.setenv("QSERVER_ZMQ_ADDRESS_CONSOLE", f"tcp://localhost:{zmq_port}")
    fastapi_server_fs()

    # Start RE Manager
    params = ["--zmq-publish-console", "ON"]
    if zmq_port is not None:
        params.extend(["--zmq-publish-console-addr", f"tcp://*:{zmq_port}"])
    re_manager_cmd(params)

    rsc = _ReceiveStreamedConsoleOutput()
    rsc.start()

    resp1 = request_to_json(
        "post",
        "/queue/item/add",
        json={"item": {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}},
    )
    assert resp1["success"] is True
    assert resp1["qsize"] == 1
    assert resp1["item"]["name"] == "count"
    assert resp1["item"]["args"] == [["det1", "det2"]]
    assert "item_uid" in resp1["item"]

    # Wait until capture is complete (2 message are expected) or timetout expires
    t_start, t_timeout = ttime.time(), 10
    while (len(rsc.received_data_buffer) < 2) and (ttime.time() - t_start < t_timeout):
        pass
    rsc.stop()
    # Note, that some output from the server is is needed in order to exit the loop in the thread.

    resp2 = request_to_json("get", "/queue/get")
    assert resp2["items"] != []
    assert len(resp2["items"]) == 1
    assert resp2["items"][0] == resp1["item"]
    assert resp2["running_item"] == {}

    rsc.join()

    # Verify that expected messages ('strings') are contained in captured messages.
    expected_messages = {"Adding new item to the queue", "Item added"}
    buffer = rsc.received_data_buffer
    for msg in buffer:
        for emsg in expected_messages.copy():
            if emsg in msg["msg"]:
                expected_messages.remove(emsg)

    assert (
        not expected_messages
    ), f"Messages {expected_messages} were not found in captured output: {pprint.pformat(buffer)}"
