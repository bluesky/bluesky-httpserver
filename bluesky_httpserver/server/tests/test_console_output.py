import requests
import json
import threading
import pprint
import pytest
import re
import time as ttime

from bluesky_httpserver.server.tests.conftest import SERVER_ADDRESS, SERVER_PORT, request_to_json
from bluesky_queueserver.manager.tests.common import re_manager_cmd  # noqa F401

from bluesky_httpserver.server.tests.conftest import (  # noqa F401
    request_to_json,
    fastapi_server_fs,
    wait_for_environment_to_be_created,
    wait_for_environment_to_be_closed,
    wait_for_manager_state_idle,
)


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

    # Wait until capture is complete (at least 2 message are expected) or timetout expires
    ttime.sleep(10)
    rsc.stop()
    # Note, that some output from the server is is needed in order to exit the loop in the thread.

    resp2 = request_to_json("get", "/queue/get")
    assert resp2["items"] != []
    assert len(resp2["items"]) == 1
    assert resp2["items"][0] == resp1["item"]
    assert resp2["running_item"] == {}

    rsc.join()

    assert len(rsc.received_data_buffer) >= 2, pprint.pformat(rsc.received_data_buffer)

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


_script1 = r"""
ttime.sleep(0.5)  # Leave some time for other messages to be printed
print("=====")
print("Beginning of the line. ", end="")
print("End of the line.")
print("Print\n multiple\n\nlines\n\n"),
"""

_script1_output = """=====
Beginning of the line. End of the line.
Print
 multiple

lines

"""


@pytest.mark.parametrize("zmq_port", (None, 60619))
def test_http_server_console_output_1(monkeypatch, re_manager_cmd, fastapi_server_fs, zmq_port):  # noqa F811
    """
    Test for ``console_output`` API (not a streaming version).
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

    script = _script1
    expected_output = _script1_output

    resp1 = request_to_json("post", "/environment/open")
    assert resp1["success"] is True, pprint.pformat(resp1)

    assert wait_for_environment_to_be_created(timeout=10)

    resp2 = request_to_json("get", "/console_output/uid")
    assert resp2["success"] is True
    console_output_uid = resp2["console_output_uid"]

    resp2a = request_to_json("post", "/script/upload", json={"script": script})
    assert resp2a["success"] is True, pprint.pformat(resp2a)

    # Do not poll status while the script is executed to avoid contaminating
    #   the printed message with logging messages
    ttime.sleep(3)

    assert wait_for_manager_state_idle(timeout=10)

    resp2b = request_to_json("post", "/environment/close")
    assert resp2b["success"] is True, pprint.pformat(resp2b)

    assert wait_for_environment_to_be_closed(timeout=10)

    # The console output should be available instantly, but there could be delays
    #   when the tests are running on CI
    # ttime.sleep(5)

    resp3a = request_to_json("get", "/console_output")
    assert resp3a["success"] is True
    console_output = resp3a["text"]

    print("*******************************************************")
    print(f"console_output =\n{console_output}")
    print("*******************************************************")
    print(f"expected_output =\n{expected_output}")
    print("*******************************************************")
    print(f"script =\n{script}")
    print("*******************************************************")

    assert re.search(expected_output, console_output)

    resp3b = request_to_json("get", "/console_output/uid")
    assert resp3b["success"] is True
    assert resp3b["console_output_uid"] != console_output_uid

    resp3c = request_to_json("get", "/console_output", json={"nlines": 300})
    assert resp3c["success"] is True
    console_output = resp3c["text"]


@pytest.mark.parametrize("zmq_port", (None, 60619))
def test_http_server_console_output_update_1(
    monkeypatch, re_manager_cmd, fastapi_server_fs, zmq_port  # noqa F811
):
    """
    Test for ``console_output`` API (not a streaming version).
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

    script = _script1
    expected_output = _script1_output

    resp1 = request_to_json("post", "/environment/open")
    assert resp1["success"] is True, pprint.pformat(resp1)

    assert wait_for_environment_to_be_created(timeout=10)

    # Save initial 'last_msg_uid'
    resp2a = request_to_json("get", "/console_output_update", json={"last_msg_uid": ""})
    assert resp2a["success"] is True
    assert resp2a["msg"] == ""
    last_msg_uid_1 = resp2a["last_msg_uid"]
    assert resp2a["console_output_msgs"] == []

    # Download ALL existing messages
    resp2b = request_to_json("get", "/console_output_update", json={"last_msg_uid": "ALL"})
    assert resp2b["success"] is True
    assert resp2b["msg"] == ""
    assert resp2b["last_msg_uid"] == last_msg_uid_1
    assert isinstance(resp2b["console_output_msgs"], list)
    assert len(resp2b["console_output_msgs"]) > 0

    # Upload the script the 1st time
    resp3 = request_to_json("post", "/script/upload", json={"script": script})
    assert resp3["success"] is True, pprint.pformat(resp2a)

    # Do not poll status while the script is executed to avoid contaminating
    #   the printed message with logging messages.
    ttime.sleep(3)
    assert wait_for_manager_state_idle(timeout=10)

    resp4a = request_to_json("get", "/console_output_update", json={"last_msg_uid": last_msg_uid_1})
    last_msg_uid_2 = resp4a["last_msg_uid"]
    assert last_msg_uid_2 != last_msg_uid_1
    console_output = resp4a["console_output_msgs"]
    console_output_text = "".join([_["msg"] for _ in console_output])
    assert expected_output in console_output_text
    assert console_output_text.count(expected_output) == 1

    resp4b = request_to_json("get", "/console_output_update", json={"last_msg_uid": "ALL"})
    assert resp4b["last_msg_uid"] == last_msg_uid_2
    console_output = resp4b["console_output_msgs"]
    console_output_text = "".join([_["msg"] for _ in console_output])
    assert expected_output in console_output_text
    assert console_output_text.count(expected_output) == 1

    # Upload the script the 2nd time
    resp5 = request_to_json("post", "/script/upload", json={"script": script})
    assert resp5["success"] is True, pprint.pformat(resp2a)

    # Do not poll status while the script is executed to avoid contaminating
    #   the printed message with logging messages.
    ttime.sleep(3)
    assert wait_for_manager_state_idle(timeout=10)

    # Download the lastest updates
    resp6a = request_to_json("get", "/console_output_update", json={"last_msg_uid": last_msg_uid_2})
    last_msg_uid_3 = resp6a["last_msg_uid"]
    assert last_msg_uid_3 != last_msg_uid_2
    console_output = resp6a["console_output_msgs"]
    console_output_text = "".join([_["msg"] for _ in console_output])
    assert expected_output in console_output_text
    assert console_output_text.count(expected_output) == 1

    # Download the updates using 'old' UID. The script was uploaded twice, so the output should
    #   contain two copies of the printed output
    resp6b = request_to_json("get", "/console_output_update", json={"last_msg_uid": last_msg_uid_1})
    assert resp6b["last_msg_uid"] == last_msg_uid_3
    console_output = resp6b["console_output_msgs"]
    console_output_text = "".join([_["msg"] for _ in console_output])
    assert expected_output in console_output_text
    assert console_output_text.count(expected_output) == 2

    # No updates are expected since last request
    resp6c = request_to_json("get", "/console_output_update", json={"last_msg_uid": last_msg_uid_3})
    assert resp6c["last_msg_uid"] == last_msg_uid_3
    assert resp6c["console_output_msgs"] == []

    resp7 = request_to_json("post", "/environment/close")
    assert resp7["success"] is True, pprint.pformat(resp7)

    assert wait_for_environment_to_be_closed(timeout=10)
