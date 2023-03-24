import os
import pprint

import pytest
from bluesky_queueserver import generate_zmq_keys
from bluesky_queueserver.manager.tests.common import (  # noqa F401
    append_code_to_last_startup_file,
    copy_default_profile_collection,
    re_manager,
    re_manager_cmd,
    re_manager_pc_copy,
    set_qserver_zmq_address,
    set_qserver_zmq_public_key,
)

from bluesky_httpserver.tests.conftest import (  # noqa F401
    SERVER_ADDRESS,
    SERVER_PORT,
    add_plans_to_queue,
    fastapi_server_fs,
    request_to_json,
    setup_server_with_config_file,
    wait_for_environment_to_be_created,
    wait_for_manager_state_idle,
    wait_for_queue_execution_to_complete,
)

# Plans used in most of the tests: '_plan1' and '_plan2' are quickly executed '_plan3' runs for 5 seconds.
_plan1 = {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}
_plan2 = {"name": "scan", "args": [["det1", "det2"], "motor", -1, 1, 10], "item_type": "plan"}
_plan3 = {"name": "count", "args": [["det1", "det2"]], "kwargs": {"num": 5, "delay": 1}, "item_type": "plan"}


_config_public_key = """
qserver_zmq_configuration:
  public_key: "{0}"
"""


# fmt: off
@pytest.mark.parametrize("test_mode", ["none", "ev", "cfg_file", "both"])
# fmt: on
def test_http_server_secure_1(monkeypatch, tmpdir, re_manager_cmd, fastapi_server_fs, test_mode):  # noqa: F811
    """
    Test operation of HTTP server with enabled encryption. Security of HTTP server can be enabled
    only by setting the environment variable to the value of the public key.
    """
    public_key, private_key = generate_zmq_keys()

    if test_mode == "none":
        # No encryption
        pass
    elif test_mode == "ev":
        # Set server private key using environment variable
        monkeypatch.setenv("QSERVER_ZMQ_PRIVATE_KEY_FOR_SERVER", private_key)  # RE Manager
        monkeypatch.setenv("QSERVER_ZMQ_PUBLIC_KEY", public_key)  # HTTP server
        set_qserver_zmq_public_key(monkeypatch, server_public_key=public_key)  # For test functions
    elif test_mode == "cfg_file":
        monkeypatch.setenv("QSERVER_ZMQ_PRIVATE_KEY_FOR_SERVER", private_key)  # RE Manager
        setup_server_with_config_file(
            config_file_str=_config_public_key.format(public_key), tmpdir=tmpdir, monkeypatch=monkeypatch
        )
        set_qserver_zmq_public_key(monkeypatch, server_public_key=public_key)  # For test functions
    elif test_mode == "both":
        monkeypatch.setenv("QSERVER_ZMQ_PRIVATE_KEY_FOR_SERVER", private_key)  # RE Manager
        setup_server_with_config_file(
            config_file_str=_config_public_key.format(public_key), tmpdir=tmpdir, monkeypatch=monkeypatch
        )
        monkeypatch.setenv("QSERVER_ZMQ_PUBLIC_KEY", "abc")  # IGNORED
        set_qserver_zmq_public_key(monkeypatch, server_public_key=public_key)  # For test functions
    else:
        raise RuntimeError(f"Unrecognized test mode '{test_mode}'")

    fastapi_server_fs()
    re_manager_cmd([])

    resp1 = request_to_json("post", "/queue/item/add", json={"item": _plan1})
    assert resp1["success"] is True, str(resp1)

    resp2 = request_to_json("post", "/queue/item/add", json={"item": _plan2})
    assert resp2["success"] is True, str(resp2)

    resp3 = request_to_json("get", "/plans/allowed")
    assert isinstance(resp3["plans_allowed"], dict)
    assert len(resp3["plans_allowed"]) > 0
    resp4 = request_to_json("get", "/devices/allowed")
    assert isinstance(resp4["devices_allowed"], dict)
    assert len(resp4["devices_allowed"]) > 0

    resp5 = request_to_json("post", "/environment/open")
    assert resp5["success"] is True
    assert wait_for_environment_to_be_created(10)

    resp6 = request_to_json("get", "/status")
    assert resp6["items_in_queue"] == 2
    assert resp6["items_in_history"] == 0

    resp7 = request_to_json("post", "/queue/start")
    assert resp7["success"] is True

    wait_for_queue_execution_to_complete(20)

    resp8 = request_to_json("get", "/status")
    assert resp8["items_in_queue"] == 0
    assert resp8["items_in_history"] == 2

    # Close the environment
    resp9 = request_to_json("post", "/environment/close")
    assert resp9 == {"success": True, "msg": ""}

    wait_for_manager_state_idle(10)


_config_zmq_address = """
qserver_zmq_configuration:
  control_address: {0}
  info_address: {1}
"""


# fmt: off
@pytest.mark.parametrize("option", ["ev", "cfg_file", "both"])
# fmt: on
def test_http_server_set_zmq_address_1(
    monkeypatch, tmpdir, re_manager_cmd, fastapi_server_fs, option  # noqa: F811
):
    """
    Test if ZMQ address of RE Manager is passed to the HTTP server using 'QSERVER_ZMQ_ADDRESS_CONTROL'
    environment variable. Start RE Manager and HTTP server with ZMQ address for control communication
    channel different from default address, add and execute a plan.
    """

    # Change ZMQ address to use port 60616 instead of the default port 60615.
    zmq_control_address_server = "tcp://*:60616"
    zmq_info_address_server = "tcp://*:60617"
    zmq_control_address = "tcp://localhost:60616"
    zmq_info_address = "tcp://localhost:60617"
    if option == "ev":
        monkeypatch.setenv("QSERVER_ZMQ_CONTROL_ADDRESS", zmq_control_address)
        monkeypatch.setenv("QSERVER_ZMQ_INFO_ADDRESS", zmq_info_address)
    elif option in ("cfg_file", "both"):
        setup_server_with_config_file(
            config_file_str=_config_zmq_address.format(zmq_control_address, zmq_info_address),
            tmpdir=tmpdir,
            monkeypatch=monkeypatch,
        )
        if option == "both":
            monkeypatch.setenv("QSERVER_ZMQ_CONTROL_ADDRESS", "something")  # Ignored
            monkeypatch.setenv("QSERVER_ZMQ_INFO_ADDRESS", "something")  # Ignored
    else:
        assert False, f"Unknown option {option!r}"
    fastapi_server_fs()

    set_qserver_zmq_address(monkeypatch, zmq_server_address=zmq_control_address)
    re_manager_cmd(
        [
            f"--zmq-control-addr={zmq_control_address_server}",
            f"--zmq-info-addr={zmq_info_address_server}",
            "--zmq-publish-console=ON",
        ]
    )

    # Now execute a plan to make sure everything works as expected
    resp1 = request_to_json("post", "/queue/item/add", json={"item": _plan1})
    assert resp1["success"] is True, str(resp1)

    resp5 = request_to_json("post", "/environment/open")
    assert resp5["success"] is True
    assert wait_for_environment_to_be_created(10)

    resp6 = request_to_json("get", "/status")
    assert resp6["items_in_queue"] == 1
    assert resp6["items_in_history"] == 0

    resp7 = request_to_json("post", "/queue/start")
    assert resp7["success"] is True

    wait_for_queue_execution_to_complete(20)

    resp8 = request_to_json("get", "/status")
    assert resp8["items_in_queue"] == 0
    assert resp8["items_in_history"] == 1

    # Close the environment
    resp9 = request_to_json("post", "/environment/close")
    assert resp9 == {"success": True, "msg": ""}

    wait_for_manager_state_idle(10)

    import time as ttime

    ttime.sleep(2)
    resp10 = request_to_json("get", "/console_output", json={"nlines": 1000})
    assert resp10["success"] is True
    assert len(resp10["text"]) > 0
    assert "RE Environment is ready" in resp10["text"], resp10["text"]


_mod1 = """
from fastapi import APIRouter
from bluesky_httpserver.resources import SERVER_RESOURCES as SR

router = APIRouter()

@router.get("/testing_custom_router_1")
async def testing_custom_router_1(payload: dict = {}):
    return {"success": True, "msg": "Response from 'testing_custom_router_1'"}
"""

_mod2 = """
from fastapi import APIRouter
from bluesky_httpserver.resources import SERVER_RESOURCES as SR
from bluesky_httpserver.utils import process_exception

# Try using a different name for the router
router2 = APIRouter(prefix="/some_prefix")

@router2.get("/testing_custom_router_2")
async def testing_custom_router_2(payload: dict = {}):
    return {"success": True, "msg": "Response from 'testing_custom_router_2'"}

@router2.post("/status_duplicate_post")
async def status_duplicate_post(payload: dict = {}):
    try:
        msg = await SR.RM.status(**payload)
    except Exception:
        process_exception()
    return msg
"""

_config_routers = """
server_configuration:
  custom_routers:
    - {0}
    - {1}
"""


# fmt: off
@pytest.mark.parametrize("option", ["ev", "cfg_file", "both"])
# fmt: on
def test_http_server_custom_routers_1(tmpdir, monkeypatch, re_manager, fastapi_server_fs, option):  # noqa: F811
    """
    Test if custom routers can be passed to the server using EV and config file and if settings in config file
    override the settings passed as EV (if both are used).
    """
    dir_mod_root = os.path.join(tmpdir, "mod_dir")
    dir_submod = os.path.join(dir_mod_root, "submod_dir")

    os.makedirs(dir_mod_root, exist_ok=True)
    os.makedirs(dir_submod, exist_ok=True)

    with open(os.path.join(dir_mod_root, "mod1.py"), "wt") as f:
        f.writelines(_mod1)
    with open(os.path.join(dir_submod, "mod2.py"), "wt") as f:
        f.writelines(_mod2)

    mod1_name, mod2_name = "mod1", "submod_dir.mod2"

    monkeypatch.setenv("PYTHONPATH", dir_mod_root)

    routers = [f"{mod1_name}.router", f"{mod2_name}.router2"]

    if option in ("cfg_file", "both"):
        config = _config_routers.format(routers[0], routers[1])
        setup_server_with_config_file(config_file_str=config, tmpdir=tmpdir, monkeypatch=monkeypatch)
        if option == "both":
            monkeypatch.setenv("QSERVER_HTTP_CUSTOM_ROUTERS", "non.existing:router")
    elif option == "ev":
        monkeypatch.setenv("QSERVER_HTTP_CUSTOM_ROUTERS", f"{routers[0]}:{routers[1]}")
    else:
        assert False, f"Unknown test option {option!r}"

    fastapi_server_fs()

    # Test router from mod1
    resp1 = request_to_json("get", "/testing_custom_router_1", request_prefix="")
    assert "success" in resp1, pprint.pformat(resp1)
    assert resp1["success"] is True
    assert resp1["msg"] == "Response from 'testing_custom_router_1'"

    # Test router from mod2
    resp2 = request_to_json("get", "/some_prefix/testing_custom_router_2", request_prefix="")
    assert "success" in resp2, pprint.pformat(resp1)
    assert resp2["success"] is True
    assert resp2["msg"] == "Response from 'testing_custom_router_2'"

    # Compare RE Manager status returned by standard and test API
    resp3 = request_to_json("get", "/status")
    assert "manager_state" in resp3, pprint.pformat(resp3)
    resp4 = request_to_json("post", "/some_prefix/status_duplicate_post", request_prefix="")
    assert resp3 == resp4
