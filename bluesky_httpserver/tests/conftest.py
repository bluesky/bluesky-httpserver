import os
import time as ttime

import pytest
import requests
from bluesky_queueserver.manager.comms import zmq_single_request
from xprocess import ProcessStarter

import bluesky_httpserver.server as bqss

SERVER_ADDRESS = "localhost"
SERVER_PORT = "60610"

# Single-user API key used for most of the tests
API_KEY_FOR_TESTS = "APIKEYFORTESTS"

_user_group = "primary"


@pytest.fixture(scope="module")
def fastapi_server(xprocess):
    class Starter(ProcessStarter):
        env = dict(os.environ)
        env["QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"] = API_KEY_FOR_TESTS

        pattern = "Bluesky HTTP Server started successfully"
        args = f"uvicorn --host={SERVER_ADDRESS} --port {SERVER_PORT} {bqss.__name__}:app".split()
        # args = f"start-bluesky-httpserver --host={SERVER_ADDRESS} --port {SERVER_PORT}".split()

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

    def start(http_server_host=SERVER_ADDRESS, http_server_port=SERVER_PORT, api_key=API_KEY_FOR_TESTS):
        class Starter(ProcessStarter):
            env = dict(os.environ)
            if api_key:
                env["QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"] = api_key

            pattern = "Bluesky HTTP Server started successfully"
            args = f"uvicorn --host={http_server_host} --port {http_server_port} {bqss.__name__}:app".split()

        xprocess.ensure("fastapi_server", Starter)

    yield start

    xprocess.getinfo("fastapi_server").terminate()


def setup_server_with_config_file(*, config_file_str, tmpdir, monkeypatch):
    """
    Creates config file for the server in ``tmpdir/config/`` directory and
    sets up the respective environment variable. Sets ``tmpdir`` as a current directory.
    """
    print(f"SERVER CONFIGURATION:\n{'-'*50}\n{config_file_str}\n{'-'*50}")
    config_fln = "config_httpserver.yml"
    config_dir = os.path.join(tmpdir, "config")
    config_path = os.path.join(config_dir, config_fln)
    os.makedirs(config_dir)
    with open(config_path, "wt") as f:
        f.writelines(config_file_str)

    sqlite_path = os.path.join(tmpdir, "bluesky_httpserver.sqlite")
    sqlite_path = "sqlite:///" + sqlite_path

    monkeypatch.setenv("QSERVER_HTTP_SERVER_CONFIG", config_path)
    monkeypatch.setenv("QSERVER_HTTP_SERVER_DATABASE_URI", sqlite_path)
    monkeypatch.chdir(tmpdir)

    return config_path


def add_plans_to_queue():
    """
    Clear the queue and add 3 fixed plans to the queue.
    Raises an exception if clearing the queue or adding plans fails.
    """
    resp1, _ = zmq_single_request("queue_clear")
    assert resp1["success"] is True, str(resp1)

    user_group = _user_group
    user = "HTTP unit test setup"
    plan1 = {"name": "count", "args": [["det1", "det2"]], "kwargs": {"num": 10, "delay": 1}, "item_type": "plan"}
    plan2 = {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}
    for plan in (plan1, plan2, plan2):
        resp2, _ = zmq_single_request("queue_item_add", {"item": plan, "user": user, "user_group": user_group})
        assert resp2["success"] is True, str(resp2)


def request_to_json(
    request_type, path, *, request_prefix="/api", api_key=API_KEY_FOR_TESTS, token=None, login=None, **kwargs
):
    if login:
        auth = None
        data = {"username": login[0], "password": login[1]}
        kwargs.setdefault("data", {})
        kwargs.update({"data": data})
    elif token:
        auth = None
        headers = {"Authorization": f"Bearer {token}"}
        kwargs.update({"auth": auth, "headers": headers})
    elif api_key:
        auth = None
        headers = {"Authorization": f"ApiKey {api_key}"}
        kwargs.update({"auth": auth, "headers": headers})

    method = getattr(requests, request_type)
    resp = method(f"http://{SERVER_ADDRESS}:{SERVER_PORT}{request_prefix}{path}", **kwargs)
    resp = resp.json()
    return resp


def wait_for_environment_to_be_created(timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS):
    """Wait for environment to be created with timeout."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if resp["worker_environment_exists"] and (resp["manager_state"] == "idle"):
            return True

    return False


def wait_for_environment_to_be_closed(timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS):
    """Wait for environment to be closed with timeout."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if (not resp["worker_environment_exists"]) and (resp["manager_state"] == "idle"):
            return True

    return False


def wait_for_queue_execution_to_complete(timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS):
    """Wait for for queue execution to complete."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if (resp["manager_state"] == "idle") and (resp["items_in_queue"] == 0):
            return True

    return False


def wait_for_manager_state_idle(timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS):
    """Wait until manager is in 'idle' state."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if resp["manager_state"] == "idle":
            return True

    return False
