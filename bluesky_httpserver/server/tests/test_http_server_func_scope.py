import os
import pandas as pd
import pytest

from bluesky_queueserver.manager.tests.plan_lists import plan_list_sample, create_excel_file_from_plan_list

from bluesky_queueserver.manager.tests.common import (  # noqa F401
    re_manager,
    re_manager_pc_copy,
    re_manager_cmd,
    copy_default_profile_collection,
    append_code_to_last_startup_file,
    set_qserver_zmq_public_key,
    set_qserver_zmq_address,
)

from bluesky_httpserver.server.tests.conftest import (  # noqa F401
    SERVER_ADDRESS,
    SERVER_PORT,
    add_plans_to_queue,
    fastapi_server_fs,
    request_to_json,
    wait_for_environment_to_be_created,
    wait_for_queue_execution_to_complete,
    wait_for_manager_state_idle,
)

from bluesky_queueserver.manager.comms import generate_new_zmq_key_pair


# Plans used in most of the tests: '_plan1' and '_plan2' are quickly executed '_plan3' runs for 5 seconds.
_plan1 = {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}
_plan2 = {"name": "scan", "args": [["det1", "det2"], "motor", -1, 1, 10], "item_type": "plan"}
_plan3 = {"name": "count", "args": [["det1", "det2"]], "kwargs": {"num": 5, "delay": 1}, "item_type": "plan"}


def _create_test_excel_file1(tmp_path, *, plan_params, col_names):
    """
    Create test spreadsheet file in temporary directory. Return full path to the spreadsheet
    and the expected list of plans with parameters.
    """
    # Create sample Excel file
    ss_fln = "spreadsheet.xlsx"
    ss_path = os.path.join(tmp_path, ss_fln)

    # Expected plans
    plans_expected = []
    for p in plan_params:
        plans_expected.append(
            {
                "name": p[0],
                "args": [["det1", "det2"]],
                "kwargs": {k: v for k, v in zip(col_names[1:], p[1:])},
                "item_type": "plan",
            }
        )

    def create_excel(ss_path):
        df = pd.DataFrame(plan_params)
        df = df.set_axis(col_names, axis=1)
        df.to_excel(ss_path, engine="openpyxl")
        return df

    def verify_excel(ss_path, df):
        df_read = pd.read_excel(ss_path, index_col=0, engine="openpyxl")
        assert df_read.equals(df), str(df_read)

    df = create_excel(ss_path)
    verify_excel(ss_path, df)

    return ss_path, plans_expected


def test_http_server_queue_upload_spreasheet_1(re_manager, fastapi_server_fs, tmp_path, monkeypatch):  # noqa F811
    """
    Test for ``/queue/upload/spreadsheet`` API: generate .xlsx file, upload it to the server, verify
    the contents of the queue, run the queue and verify that the required number of plans were successfully
    completed.
    """
    monkeypatch.setenv(
        "QSERVER_CUSTOM_MODULE",
        "bluesky_queueserver.manager.tests.spreadsheet_custom_functions",
        prepend=False,
    )
    fastapi_server_fs()

    plan_params = [["count", 5, 1], ["count", 6, 0.5]]
    col_names = ["name", "num", "delay"]
    ss_path, plans_expected = _create_test_excel_file1(tmp_path, plan_params=plan_params, col_names=col_names)

    # Send the Excel file to the server
    files = {"spreadsheet": open(ss_path, "rb")}
    resp1 = request_to_json("post", "/queue/upload/spreadsheet", files=files)
    assert resp1["success"] is True, str(resp1)
    items1 = resp1["items"]
    results1 = resp1["results"]
    assert len(items1) == len(plans_expected), str(items1)
    for p, p_exp in zip(items1, plans_expected):
        for k, v in p_exp.items():
            assert k in p
            assert v == p[k]

    assert len(results1) == len(plans_expected), str(results1)
    assert all([_["success"] is True for _ in results1]), str(results1)
    assert all([_["msg"] == "" for _ in results1]), str(results1)

    # Verify that the queue contains correct plans
    resp2 = request_to_json("get", "/queue/get")
    assert resp2["success"] is True
    assert resp2["running_item"] == {}
    queue = resp2["items"]
    assert len(queue) == len(plans_expected), str(queue)
    for p, p_exp in zip(queue, plans_expected):
        for k, v in p_exp.items():
            assert k in p
            assert v == p[k]

    resp3 = request_to_json("post", "/environment/open")
    assert resp3["success"] is True
    assert wait_for_environment_to_be_created(10)

    resp4 = request_to_json("post", "/queue/start")
    assert resp4["success"] is True
    assert wait_for_queue_execution_to_complete(60)

    resp5 = request_to_json("get", "/status")
    assert resp5["items_in_queue"] == 0
    assert resp5["items_in_history"] == len(plans_expected)

    resp6 = request_to_json("post", "/environment/close")
    assert resp6 == {"success": True, "msg": ""}
    assert wait_for_manager_state_idle(10)


def test_http_server_queue_upload_spreasheet_2(re_manager, fastapi_server_fs, tmp_path, monkeypatch):  # noqa F811
    """
    Test for ``/queue/upload/spreadsheet`` API. Test that ``data_type`` parameter is passed correctly.
    The test function raises exception if ``data_type=='unsupported'``, which causes the request to
    return error message. Verify that correct error message is returned.
    """
    monkeypatch.setenv(
        "QSERVER_CUSTOM_MODULE",
        "bluesky_queueserver.manager.tests.spreadsheet_custom_functions",
        prepend=False,
    )
    fastapi_server_fs()

    plan_params = [["count", 5, 1], ["count", 6, 0.5]]
    col_names = ["name", "num", "delay"]
    ss_path, plans_expected = _create_test_excel_file1(tmp_path, plan_params=plan_params, col_names=col_names)

    # Send the Excel file to the server
    files = {"spreadsheet": open(ss_path, "rb")}
    data = {"data_type": "unsupported"}
    resp1 = request_to_json("post", "/queue/upload/spreadsheet", files=files, data=data)
    assert resp1["success"] is False, str(resp1)
    assert resp1["msg"] == "Unsupported data type: 'unsupported'"
    assert resp1["items"] == []
    assert resp1["results"] == []


def test_http_server_queue_upload_spreasheet_3(re_manager, fastapi_server_fs, tmp_path, monkeypatch):  # noqa F811
    """
    Test for ``/queue/upload/spreadsheet`` API. Pass file of unsupported type (file types are found based
    on file extension) and check the returned error message.
    """
    monkeypatch.setenv(
        "QSERVER_CUSTOM_MODULE",
        "bluesky_queueserver.manager.tests.spreadsheet_custom_functions",
        prepend=False,
    )
    fastapi_server_fs()

    plan_params = [["count", 5, 1], ["count", 6, 0.5]]
    col_names = ["name", "num", "delay"]
    ss_path, plans_expected = _create_test_excel_file1(tmp_path, plan_params=plan_params, col_names=col_names)

    # Rename .xlsx file to .txt file. This should cause processing error, since only .xlsx files are supported.
    new_ext = ".txt"
    new_path = os.path.splitext(ss_path)[0] + new_ext
    os.rename(ss_path, new_path)

    # Send the Excel file to the server
    files = {"spreadsheet": open(new_path, "rb")}
    resp1 = request_to_json("post", "/queue/upload/spreadsheet", files=files)
    assert resp1["success"] is False, str(resp1)
    assert resp1["msg"] == f"Unsupported file (extension '{new_ext}')"


@pytest.mark.parametrize("use_custom", [False, True])
def test_http_server_queue_upload_spreasheet_4(
    re_manager, fastapi_server_fs, tmp_path, monkeypatch, use_custom  # noqa F811
):
    """
    Test for ``/queue/upload/spreadsheet`` API. Pass the spreadsheet to the default processing function
    either directly (use_custom=False) or first pass it to the custom processing function which
    rejects the spreadsheet by returning ``None``. If custom processing function returns ``None``, then
    the spreadsheet is passed to the default function.

    NOTE: currently the default processing function is not implemented and the request returns error message.
    The test will have to be modified, when the function is implemented.
    """
    if use_custom:
        monkeypatch.setenv(
            "QSERVER_CUSTOM_MODULE",
            "bluesky_queueserver.manager.tests.spreadsheet_custom_functions",
            prepend=False,
        )
    fastapi_server_fs()

    ss_path = create_excel_file_from_plan_list(tmp_path, plan_list=plan_list_sample)
    plans_expected = [_ for _ in plan_list_sample if isinstance(_["name"], str)]

    # Send the Excel file to the server
    params = {"files": {"spreadsheet": open(ss_path, "rb")}}
    if use_custom:
        params["data"] = {"data_type": "process_with_default_function"}
    resp1 = request_to_json("post", "/queue/upload/spreadsheet", **params)
    assert resp1["success"] is True, str(resp1)
    assert "items" in resp1, str(resp1)
    assert "results" in resp1, str(resp1)
    assert len(resp1["results"]) == len(plans_expected), str(resp1)

    # Verify that the queue contains correct plans
    resp2 = request_to_json("get", "/queue/get")
    assert resp2["success"] is True
    assert resp2["running_item"] == {}
    queue = resp2["items"]
    assert len(queue) == len(plans_expected), str(queue)
    for p, p_exp in zip(queue, plans_expected):
        for k, v in p_exp.items():
            assert k in p
            assert v == p[k]

    resp3 = request_to_json("post", "/environment/open")
    assert resp3["success"] is True
    assert wait_for_environment_to_be_created(10)

    resp4 = request_to_json("post", "/queue/start")
    assert resp4["success"] is True
    assert wait_for_queue_execution_to_complete(60)

    resp5 = request_to_json("get", "/status")
    assert resp5["items_in_queue"] == 0
    assert resp5["items_in_history"] == len(plans_expected)

    resp6 = request_to_json("post", "/environment/close")
    assert resp6 == {"success": True, "msg": ""}
    assert wait_for_manager_state_idle(10)


def test_http_server_queue_upload_spreasheet_5(re_manager, fastapi_server_fs, tmp_path, monkeypatch):  # noqa F811
    """
    Test for ``/queue/upload/spreadsheet``. Test the case when one of the plans is not accepted by
    RE Manager. The API is expected to return ``success==False``, error message. Items in ``result``
    will contain ``success`` status and error message for each plan.
    """
    monkeypatch.setenv(
        "QSERVER_CUSTOM_MODULE",
        "bluesky_queueserver.manager.tests.spreadsheet_custom_functions",
        prepend=False,
    )
    fastapi_server_fs()

    plan_params = [["count", 5, 1], ["nonexisting_plan", 4, 0.7], ["count", 6, 0.5]]
    col_names = ["name", "num", "delay"]
    ss_path, plans_expected = _create_test_excel_file1(tmp_path, plan_params=plan_params, col_names=col_names)

    # Send the Excel file to the server
    files = {"spreadsheet": open(ss_path, "rb")}
    resp1 = request_to_json("post", "/queue/upload/spreadsheet", files=files)
    assert resp1["success"] is False, str(resp1)
    assert resp1["msg"] == "Failed to add all items: validation of 1 out of 3 submitted items failed"

    items, results = resp1["items"], resp1["results"]
    assert len(items) == len(plans_expected), str(items)
    assert len(results) == len(plans_expected), str(items)
    for n, p_exp in enumerate(plans_expected):
        p, r = items[n], results[n]
        if p_exp["name"] == "nonexisting_plan":
            assert r["success"] is False
            assert "not in the list of allowed plans" in r["msg"], r["msg"]
        else:
            assert r["success"] is True
            assert r["msg"] == ""
        for k, v in p_exp.items():
            assert k in p
            assert v == p[k]

    # No plans are expected to be added to the queue
    resp2 = request_to_json("get", "/status")
    assert resp2["items_in_queue"] == 0
    assert resp2["items_in_history"] == 0


# fmt: off
@pytest.mark.parametrize("test_mode", ["none", "ev"])
# fmt: on
def test_http_server_secure_1(monkeypatch, re_manager_cmd, fastapi_server_fs, test_mode):  # noqa: F811
    """
    Test operation of HTTP server with enabled encryption. Security of HTTP server can be enabled
    only by setting the environment variable to the value of the public key.
    """
    public_key, private_key = generate_new_zmq_key_pair()

    if test_mode == "none":
        # No encryption
        pass
    elif test_mode == "ev":
        # Set server private key using environment variable
        monkeypatch.setenv("QSERVER_ZMQ_PRIVATE_KEY", private_key)  # RE Manager
        monkeypatch.setenv("QSERVER_ZMQ_PUBLIC_KEY", public_key)  # HTTP server
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


def test_http_server_set_zmq_address_1(monkeypatch, re_manager_cmd, fastapi_server_fs):  # noqa: F811
    """
    Test if ZMQ address of RE Manager is passed to the HTTP server using 'QSERVER_ZMQ_ADDRESS_CONTROL'
    environment variable. Start RE Manager and HTTP server with ZMQ address for control communication
    channel different from default address, add and execute a plan.
    """

    # Change ZMQ address to use port 60616 instead of the default port 60615.
    zmq_server_address = "tcp://localhost:60616"
    monkeypatch.setenv("QSERVER_ZMQ_ADDRESS_CONTROL", zmq_server_address)  # RE Manager
    fastapi_server_fs()

    set_qserver_zmq_address(monkeypatch, zmq_server_address=zmq_server_address)
    re_manager_cmd(["--zmq-addr", "tcp://*:60616"])

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
