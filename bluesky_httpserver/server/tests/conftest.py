import pytest
from xprocess import ProcessStarter
import time as ttime
import requests

import bluesky_httpserver.server.server as bqss
from bluesky_queueserver.manager.comms import zmq_single_request

SERVER_ADDRESS = "localhost"
SERVER_PORT = "60610"


@pytest.fixture(scope="module")
def fastapi_server(xprocess):
    class Starter(ProcessStarter):
        pattern = "Bluesky HTTP Server started successfully"
        args = f"uvicorn --host={SERVER_ADDRESS} --port {SERVER_PORT} {bqss.__name__}:app".split()

    xprocess.ensure("fastapi_server", Starter)

    yield

    xprocess.getinfo("fastapi_server").terminate()


@pytest.fixture
def fastapi_server_fs(xprocess):
    """
    FastAPI server with function scope. Should not be executed in the same module as ``fastapi_server``.
    The server must be explicitly started in the unit test code as ``fastapi_server_fs()``. This allows
    to perform additional steps (such as setting environmental variables) before the server is started.
    """

    def start():
        class Starter(ProcessStarter):
            pattern = "Bluesky HTTP Server started successfully"
            args = f"uvicorn --host={SERVER_ADDRESS} --port {SERVER_PORT} {bqss.__name__}:app".split()

        xprocess.ensure("fastapi_server", Starter)

    yield start

    xprocess.getinfo("fastapi_server").terminate()


def add_plans_to_queue():
    """
    Clear the queue and add 3 fixed plans to the queue.
    Raises an exception if clearing the queue or adding plans fails.
    """
    resp1, _ = zmq_single_request("queue_clear")
    assert resp1["success"] is True, str(resp1)

    user_group = "admin"
    user = "HTTP unit test setup"
    plan1 = {"name": "count", "args": [["det1", "det2"]], "kwargs": {"num": 10, "delay": 1}, "item_type": "plan"}
    plan2 = {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}
    for plan in (plan1, plan2, plan2):
        resp2, _ = zmq_single_request("queue_item_add", {"item": plan, "user": user, "user_group": user_group})
        assert resp2["success"] is True, str(resp2)


def request_to_json(request_type, path, **kwargs):
    resp = getattr(requests, request_type)(f"http://{SERVER_ADDRESS}:{SERVER_PORT}{path}", **kwargs)
    resp = resp.json()
    return resp


def wait_for_environment_to_be_created(timeout, polling_period=0.2):
    """Wait for environment to be created with timeout."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status")
        if resp["worker_environment_exists"] and (resp["manager_state"] == "idle"):
            return True

    return False


def wait_for_environment_to_be_closed(timeout, polling_period=0.2):
    """Wait for environment to be closed with timeout."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status")
        if (not resp["worker_environment_exists"]) and (resp["manager_state"] == "idle"):
            return True

    return False


def wait_for_queue_execution_to_complete(timeout, polling_period=0.2):
    """Wait for for queue execution to complete."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status")
        if (resp["manager_state"] == "idle") and (resp["items_in_queue"] == 0):
            return True

    return False


def wait_for_manager_state_idle(timeout, polling_period=0.2):
    """Wait until manager is in 'idle' state."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status")
        if resp["manager_state"] == "idle":
            return True

    return False
