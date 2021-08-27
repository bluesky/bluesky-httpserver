import logging
from enum import Enum
import io
import pprint
import os
import importlib

from fastapi import FastAPI, HTTPException, File, UploadFile, Form
from typing import Optional

from bluesky_queueserver.manager.comms import ZMQCommSendAsync, validate_zmq_key
from bluesky_queueserver.manager.conversions import simplify_plan_descriptions, spreadsheet_to_plan_list

from .console_output import CollectPublishedConsoleOutput, ConsoleOutputEventStream, StreamingResponseFromClass

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Login and authentication are not implemented, but some API methods require
#   login data. So for now we set up fixed user name and group
_login_data = {"user": "John Doe", "user_group": "admin"}

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bluesky_queueserver").setLevel("DEBUG")

# Use FastAPI
app = FastAPI()
zmq_to_manager = None

custom_code_module = None
console_output_loader = None


@app.on_event("startup")
async def startup_event():
    global zmq_to_manager
    global custom_code_module
    global console_output_loader

    # Read private key from the environment variable, then check if the CLI parameter exists
    zmq_public_key = os.environ.get("QSERVER_ZMQ_PUBLIC_KEY", None)
    zmq_public_key = zmq_public_key if zmq_public_key else None  # Case of ""
    if zmq_public_key is not None:
        try:
            validate_zmq_key(zmq_public_key)
        except Exception as ex:
            raise ValueError("ZMQ public key is improperly formatted: %s", str(ex))

    # TODO: implement nicer exit with error reporting in case of failure
    zmq_server_address_control = os.getenv("QSERVER_ZMQ_ADDRESS_CONTROL", None)
    if zmq_server_address_control is None:
        # Support for deprecated environment variable QSERVER_ZMQ_ADDRESS.
        # TODO: remove in one of the future versions
        zmq_server_address_control = os.getenv("QSERVER_ZMQ_ADDRESS", None)
        if zmq_server_address_control is not None:
            logger.warning(
                "Environment variable QSERVER_ZMQ_ADDRESS is deprecated: use environment variable "
                "QSERVER_ZMQ_ADDRESS_CONTROL to pass address of 0MQ control socket to HTTP Server."
            )

    zmq_server_address_console = os.getenv("QSERVER_ZMQ_ADDRESS_CONSOLE", None)

    # ZMQCommSendAsync should be created from the event loop of FastAPI server.
    zmq_to_manager = ZMQCommSendAsync(
        raise_exceptions=False, server_public_key=zmq_public_key, zmq_server_address=zmq_server_address_control
    )

    console_output_loader = CollectPublishedConsoleOutput(zmq_addr=zmq_server_address_console)
    console_output_loader.start()

    # Import module with custom code
    module_name = os.getenv("QSERVER_CUSTOM_MODULE", None)

    if module_name:
        try:
            logger.info("Importing custom module '%s' ...", module_name)
            custom_code_module = importlib.import_module(module_name.replace("-", "_"))
            logger.info("Module '%s' was imported successfully.", module_name)
        except Exception as ex:
            custom_code_module = None
            logger.error("Failed to import custom instrument module '%s': %s", module_name, ex)

    # The following message is used in unit tests to detect when HTTP server is started.
    #   Unit tests need to be modified if this message is modified.
    logger.info("Bluesky HTTP Server started successfully")


@app.on_event("shutdown")
def shutdown_event():
    global zmq_to_manager
    global console_output_loader

    zmq_to_manager.close()
    console_output_loader.stop()


class REPauseOptions(str, Enum):
    deferred = "deferred"
    immediate = "immediate"


def validate_payload_keys(payload, *, required_keys=None, optional_keys=None):
    """
    Validate keys in the payload. Raise an exception if the request contains unsupported
    keys or if some of the required keys are missing.

    Parameters
    ----------
    payload: dict
        Payload received with the request.
    required_keys: list(str)
        List of the required payload keys. All the keys must be present in the request.
    optional_keys: list(str)
        List of optional keys.

    Raises
    ------
    ValueError
        payload contains unsupported keys or some of the required keys are missing.
    """

    # TODO: it would be better to use something similar to 'jsonschema' validator.
    #   Unfortunately 'jsonschema' provides terrible error reporting.
    #   Any suggestions?
    #   For now let's use primitive validaator that ensures that the dictionary
    #   has necessary and only allowed top level keys.

    required_keys = required_keys or []
    optional_keys = optional_keys or []

    payload_keys = list(payload.keys())
    r_keys = set(required_keys)
    a_keys = set(required_keys).union(set(optional_keys))
    extra_keys = set()

    for key in payload_keys:
        if key not in a_keys:
            extra_keys.add(key)
        else:
            r_keys -= {key}

    err_msg = ""
    if r_keys:
        err_msg += f"Some required keys are missing in the request: {r_keys}. "
    if extra_keys:
        err_msg += f"Request contains keys the are not supported: {extra_keys}."

    if err_msg:
        raise ValueError(err_msg)


@app.get("/")
@app.get("/ping")
async def ping_handler():
    """
    May be called to get some response from the server. Currently returns status of RE Manager.
    """
    msg = await zmq_to_manager.send_message(method="ping")
    return msg


@app.get("/status")
async def status_handler():
    """
    Returns status of RE Manager.
    """
    msg = await zmq_to_manager.send_message(method="status")
    return msg


@app.post("/queue/mode/set")
async def queue_mode_set_handler(payload: dict):
    """
    Clear the plan queue.
    """
    params = payload
    msg = await zmq_to_manager.send_message(method="queue_mode_set", params=params)
    return msg


@app.get("/queue/get")
async def queue_get_handler():
    """
    Returns the contents of the current queue.
    """
    msg = await zmq_to_manager.send_message(method="queue_get")
    return msg


@app.post("/queue/clear")
async def queue_clear_handler():
    """
    Clear the plan queue.
    """
    msg = await zmq_to_manager.send_message(method="queue_clear")
    return msg


@app.post("/queue/start")
async def queue_start_handler():
    """
    Start execution of the loaded queue. Additional runs can be added to the queue while
    it is executed. If the queue is empty, then nothing will happen.
    """
    msg = await zmq_to_manager.send_message(method="queue_start")
    return msg


@app.post("/queue/stop")
async def queue_stop():
    """
    Activate the sequence of stopping the queue. The currently running plan will be completed,
    but the next plan will not be started. The request will be rejected if no plans are currently
    running
    """
    msg = await zmq_to_manager.send_message(method="queue_stop")
    return msg


@app.post("/queue/stop/cancel")
async def queue_stop_cancel():
    """
    Cancel pending request to stop the queue while the current plan is still running.
    It may be useful if the `/queue/stop` request was issued by mistake or the operator
    changed his mind. Since `/queue/stop` takes effect only after the currently running
    plan is completed, user may have time to cancel the request and continue execution of
    the queue. The command always succeeds, but it has no effect if no queue stop
    requests are pending.
    """
    msg = await zmq_to_manager.send_message(method="queue_stop_cancel")
    return msg


@app.post("/queue/item/add")
async def queue_item_add_handler(payload: dict):
    """
    Adds new plan to the queue
    """
    # TODO: validate inputs!
    params = payload
    params["user"] = _login_data["user"]
    params["user_group"] = _login_data["user_group"]
    msg = await zmq_to_manager.send_message(method="queue_item_add", params=params)
    return msg


@app.post("/queue/item/execute")
async def queue_item_execute_handler(payload: dict):
    """
    Immediately execute an item
    """
    # TODO: validate inputs!
    params = payload
    params["user"] = _login_data["user"]
    params["user_group"] = _login_data["user_group"]
    msg = await zmq_to_manager.send_message(method="queue_item_execute", params=params)
    return msg


@app.post("/queue/item/add/batch")
async def queue_item_add_batch_handler(payload: dict):
    """
    Adds new plan to the queue
    """
    # TODO: validate inputs!
    params = payload
    params["user"] = _login_data["user"]
    params["user_group"] = _login_data["user_group"]
    msg = await zmq_to_manager.send_message(method="queue_item_add_batch", params=params)
    return msg


@app.post("/queue/upload/spreadsheet")
async def queue_upload_spreadsheet(spreadsheet: UploadFile = File(...), data_type: Optional[str] = Form(None)):

    """
    The endpoint receives uploaded spreadsheet, converts it to the list of plans and adds
    the plans to the queue.

    Parameters
    ----------
    spreadsheet : File
        uploaded excel file
    data_type : str
        user defined spreadsheet type, which determines which processing function is used to
        process the spreadsheet.

    Returns
    -------
    success : boolean
        Indicates if the spreadsheet was successfully converted to a sequence of plans.
        ``True`` value does not indicate that the plans were accepted by the RE Manager and
        successfully added to the queue.
    msg : str
        Error message in case of failure to process the spreadsheet
    item_list : list(dict)
        The list of parameter dictionaries returned by RE Manager in response to requests
        to add each plan in the list. Check ``success`` parameter in each dictionary to
        see if the plan was accepted and ``msg`` parameter for an error message in case
        the plan was rejected. The list may be empty if the spreadsheet contains no items
        or processing of the spreadsheet failed.
    """
    try:
        # Create fully functional file object. The file object returned by FastAPI is not fully functional.
        f = io.BytesIO(spreadsheet.file.read())
        # File name is also passed to the processing function (may be useful in user created
        #   processing code, since processing may differ based on extension or file name)
        f_name = spreadsheet.filename
        logger.info(f"Spreadsheet file '{f_name}' was uploaded")

        # Determine which processing function should be used
        item_list = []
        processed = False
        if custom_code_module and ("spreadsheet_to_plan_list" in custom_code_module.__dict__):
            logger.info("Processing spreadsheet using function from external module ...")
            # Try applying  the custom processing function. Some additional useful data is passed to
            #   the function. Unnecessary parameters can be ignored.
            item_list = custom_code_module.spreadsheet_to_plan_list(
                spreadsheet_file=f, file_name=f_name, data_type=data_type, user=_login_data["user"]
            )
            # The function is expected to return None if it rejects the file (based on 'data_type').
            #   Then try to apply the default processing function.
            processed = item_list is not None

        if not processed:
            # Apply default spreadsheet processing function.
            logger.info("Processing spreadsheet using default function ...")
            item_list = spreadsheet_to_plan_list(
                spreadsheet_file=f, file_name=f_name, data_type=data_type, user=_login_data["user"]
            )

        if item_list is None:
            raise RuntimeError("Failed to process the spreadsheet: unsupported data type or format")

        # Since 'item_list' may be returned by user defined functions, verify the type of the list.
        if not isinstance(item_list, (tuple, list)):
            raise ValueError(
                f"Spreadsheet processing function returned value of '{type(item_list)}' "
                f"type instead of 'list' or 'tuple'"
            )

        # Ensure, that 'item_list' is sent as a list
        item_list = list(item_list)

        # Set item type for all items that don't have item type already set (item list may contain
        #   instructions, but it is responsibility of the user to set item types correctly.
        #   By default an item is considered a plan.
        for item in item_list:
            if "item_type" not in item:
                item["item_type"] = "plan"

        logger.debug("The following plans were created: %s", pprint.pformat(item_list))

        params = dict()
        params["user"] = _login_data["user"]
        params["user_group"] = _login_data["user_group"]
        params["items"] = item_list
        msg = await zmq_to_manager.send_message(method="queue_item_add_batch", params=params)

    except Exception as ex:
        msg = {"success": False, "msg": str(ex), "items": [], "results": []}

    return msg


@app.post("/queue/item/update")
async def queue_item_update_handler(payload: dict):
    """
    Update existing plan in the queue
    """
    # TODO: validate inputs! Also: payload["replace"] parameter may be used to change what metadata
    #   is added to the plan (or whether metadata is changed at all)
    params = payload
    params["user"] = _login_data["user"]
    params["user_group"] = _login_data["user_group"]
    msg = await zmq_to_manager.send_message(method="queue_item_update", params=params)
    return msg


@app.post("/queue/item/remove")
async def queue_item_remove_handler(payload: dict):
    """
    Remove plan from the queue
    """
    msg = await zmq_to_manager.send_message(method="queue_item_remove", params=payload)
    return msg


@app.post("/queue/item/remove/batch")
async def queue_item_remove_batch_handler(payload: dict):
    """
    Remove a batch of plans from the queue
    """
    msg = await zmq_to_manager.send_message(method="queue_item_remove_batch", params=payload)
    return msg


@app.post("/queue/item/move")
async def queue_item_move_handler(payload: dict):
    """
    Move plan in the queue
    """
    msg = await zmq_to_manager.send_message(method="queue_item_move", params=payload)
    return msg


@app.post("/queue/item/move/batch")
async def queue_item_move_batch_handler(payload: dict):
    """
    Move a batch of plans in the queue
    """
    msg = await zmq_to_manager.send_message(method="queue_item_move_batch", params=payload)
    return msg


@app.post("/queue/item/get")
async def queue_item_get_handler(payload: dict):
    """
    Get a plan from the queue
    """
    msg = await zmq_to_manager.send_message(method="queue_item_get", params=payload)
    return msg


@app.get("/history/get")
async def history_get_handler():
    """
    Returns the plan history (list of dicts).
    """
    msg = await zmq_to_manager.send_message(method="history_get")
    return msg


@app.post("/history/clear")
async def history_clear_handler():
    """
    Clear plan history.
    """
    msg = await zmq_to_manager.send_message(method="history_clear")
    return msg


@app.post("/environment/open")
async def environment_open_handler():
    """
    Creates RE environment: creates RE Worker process, starts and configures Run Engine.
    """
    msg = await zmq_to_manager.send_message(method="environment_open")
    return msg


@app.post("/environment/close")
async def environment_close_handler():
    """
    Orderly closes of RE environment. The command returns success only if no plan is running,
    i.e. RE Manager is in the idle state. The command is rejected if a plan is running.
    """
    msg = await zmq_to_manager.send_message(method="environment_close")
    return msg


@app.post("/environment/destroy")
async def environment_destroy_handler():
    """
    Destroys RE environment by killing RE Worker process. This is a last resort command which
    should be made available only to expert level users.
    """
    msg = await zmq_to_manager.send_message(method="environment_destroy")
    return msg


@app.post("/re/pause")
async def re_pause_handler(payload: dict):
    """
    Pause Run Engine.
    """
    try:
        validate_payload_keys(payload, required_keys=["option"])
        if not hasattr(REPauseOptions, payload["option"]):
            raise ValueError(
                f'The specified option "{payload["option"]}" is not allowed.\n'
                f"Allowed options: {list(REPauseOptions.__members__.keys())}"
            )
    except Exception as ex:
        raise HTTPException(status_code=444, detail=str(ex))

    msg = await zmq_to_manager.send_message(method="re_pause", params=payload)
    return msg


@app.post("/re/resume")
async def re_resume_handler():
    """
    Run Engine: resume execution of a paused plan
    """
    msg = await zmq_to_manager.send_message(method="re_resume")
    return msg


@app.post("/re/stop")
async def re_stop_handler():
    """
    Run Engine: stop execution of a paused plan
    """
    msg = await zmq_to_manager.send_message(method="re_stop")
    return msg


@app.post("/re/abort")
async def re_abort_handler():
    """
    Run Engine: abort execution of a paused plan
    """
    msg = await zmq_to_manager.send_message(method="re_abort")
    return msg


@app.post("/re/halt")
async def re_halt_handler():
    """
    Run Engine: halt execution of a paused plan
    """
    msg = await zmq_to_manager.send_message(method="re_halt")
    return msg


@app.get("/re/runs/active")
async def re_runs_active_handler():
    """
    Run Engine: download the list of active runs (runs that were opened during execution of
    the currently running plan and combines the subsets of 'open' and 'closed' runs.)
    """
    params = {"option": "active"}
    msg = await zmq_to_manager.send_message(method="re_runs", params=params)
    return msg


@app.get("/re/runs/open")
async def re_runs_open_handler():
    """
    Run Engine: download the subset of active runs that includes runs that were open, but not yet closed.
    """
    params = {"option": "open"}
    msg = await zmq_to_manager.send_message(method="re_runs", params=params)
    return msg


@app.get("/re/runs/closed")
async def re_runs_closed_handler():
    """
    Run Engine: download the subset of active runs that includes runs that were already closed.
    """
    params = {"option": "closed"}
    msg = await zmq_to_manager.send_message(method="re_runs", params=params)
    return msg


@app.get("/plans/allowed")
async def plans_allowed_handler():
    """
    Returns the lists of allowed plans.
    """
    params = {"user_group": _login_data["user_group"]}
    msg = await zmq_to_manager.send_message(method="plans_allowed", params=params)
    if "plans_allowed" in msg:
        msg["plans_allowed"] = simplify_plan_descriptions(msg["plans_allowed"])
    return msg


@app.get("/devices/allowed")
async def devices_allowed_handler():
    """
    Returns the lists of allowed devices.
    """
    params = {"user_group": _login_data["user_group"]}
    msg = await zmq_to_manager.send_message(method="devices_allowed", params=params)
    return msg


@app.post("/permissions/reload")
async def permissions_reload_handler():
    """
    Reloads the list of allowed plans and devices and user group permission from the default location
    or location set using command line parameters of RE Manager. Use this request to reload the data
    if the respective files were changed on disk.
    """
    msg = await zmq_to_manager.send_message(method="permissions_reload")
    return msg


@app.post("/manager/stop")
async def manager_stop_handler(payload: dict):
    """
    Stops of RE Manager. RE Manager will not be restarted after it is stoped.
    """
    msg = await zmq_to_manager.send_message(method="manager_stop", params=payload)
    return msg


@app.post("/test/manager/kill")
async def test_manager_kill_handler():
    """
    The command stops event loop of RE Manager process. Used for testing of RE Manager
    stability and handling of communication timeouts.
    """
    msg = await zmq_to_manager.send_message(method="manager_kill")
    return msg


@app.get("/stream_console_output")
def stream_console_output():
    queues_set = console_output_loader.queues_set
    stm = ConsoleOutputEventStream(queues_set=queues_set)
    sr = StreamingResponseFromClass(stm, media_type="text/plain")
    return sr
