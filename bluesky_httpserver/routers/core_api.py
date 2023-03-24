import asyncio
import io
import logging
import pprint
from typing import Optional

from bluesky_queueserver.manager.conversions import simplify_plan_descriptions, spreadsheet_to_plan_list
from fastapi import APIRouter, Depends, File, Form, Request, Security, UploadFile
from pydantic import BaseSettings

from ..authentication import get_current_principal
from ..console_output import ConsoleOutputEventStream, StreamingResponseFromClass
from ..resources import SERVER_RESOURCES as SR
from ..settings import get_settings
from ..utils import (
    get_api_access_manager,
    get_current_username,
    get_resource_access_manager,
    process_exception,
    validate_payload_keys,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/")
@router.get("/ping")
async def ping_handler(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:status"])):
    """
    May be called to get some response from the server. Currently returns status of RE Manager.
    """
    try:
        msg = await SR.RM.ping(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/status")
async def status_handler(
    request: Request,
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:status"]),
):
    """
    Returns status of RE Manager.
    """
    request.state.endpoint = "status"
    # logger.info(f"payload = {payload} principal={principal}")
    try:
        msg = await SR.RM.status(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/mode/set")
async def queue_mode_set_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:control"]),
):
    """
    Set queue mode.
    """
    try:
        msg = await SR.RM.queue_mode_set(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/queue/get")
async def queue_get_handler(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:queue"])):
    """
    Returns the contents of the current queue.
    """
    try:
        msg = await SR.RM.queue_get(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/clear")
async def queue_clear_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:edit"])
):
    """
    Clear the plan queue.
    """
    try:
        msg = await SR.RM.queue_clear(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/start")
async def queue_start_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Start execution of the loaded queue. Additional runs can be added to the queue while
    it is executed. If the queue is empty, then nothing will happen.
    """
    try:
        msg = await SR.RM.queue_start(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/stop")
async def queue_stop(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Activate the sequence of stopping the queue. The currently running plan will be completed,
    but the next plan will not be started. The request will be rejected if no plans are currently
    running
    """
    try:
        msg = await SR.RM.queue_stop(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/stop/cancel")
async def queue_stop_cancel(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Cancel pending request to stop the queue while the current plan is still running.
    It may be useful if the `/queue/stop` request was issued by mistake or the operator
    changed his mind. Since `/queue/stop` takes effect only after the currently running
    plan is completed, user may have time to cancel the request and continue execution of
    the queue. The command always succeeds, but it has no effect if no queue stop
    requests are pending.
    """
    try:
        msg = await SR.RM.queue_stop_cancel(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/item/add")
async def queue_item_add_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Adds new plan to the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "item" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_add", params=payload)
        else:
            msg = await SR.RM.item_add(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/item/execute")
async def queue_item_execute_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:execute"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Immediately execute an item
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "item" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_execute", params=payload)
        else:
            msg = await SR.RM.item_execute(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/item/add/batch")
async def queue_item_add_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Adds new plan to the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "items" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_add_batch", params=payload)
        else:
            msg = await SR.RM.item_add_batch(**payload)
    except Exception:
        process_exception()

    return msg


@router.post("/queue/upload/spreadsheet")
async def queue_upload_spreadsheet(
    spreadsheet: UploadFile = File(...),
    data_type: Optional[str] = Form(None),
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
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

        # Select custom module from the list of loaded modules
        custom_module = None
        for module in SR.custom_code_modules:
            if "spreadsheet_to_plan_list" in module.__dict__:
                custom_module = module
                break

        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)

        if custom_module:
            logger.info("Processing spreadsheet using function from external module ...")
            # Try applying  the custom processing function. Some additional useful data is passed to
            #   the function. Unnecessary parameters can be ignored.
            item_list = custom_module.spreadsheet_to_plan_list(
                spreadsheet_file=f, file_name=f_name, data_type=data_type, user=username
            )
            # The function is expected to return None if it rejects the file (based on 'data_type').
            #   Then try to apply the default processing function.
            processed = item_list is not None

        if not processed:
            # Apply default spreadsheet processing function.
            logger.info("Processing spreadsheet using default function ...")
            item_list = spreadsheet_to_plan_list(
                spreadsheet_file=f, file_name=f_name, data_type=data_type, user=username
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
    except Exception as ex:
        msg = {"success": False, "msg": str(ex), "items": [], "results": []}
    else:
        try:
            params = {"user": displayed_name, "user_group": user_group}
            params["items"] = item_list
            msg = await SR.RM.item_add_batch(**params)
        except Exception:
            process_exception()
    return msg


@router.post("/queue/item/update")
async def queue_item_update_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Update existing plan in the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        msg = await SR.RM.item_update(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/item/remove")
async def queue_item_remove_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Remove plan from the queue
    """
    try:
        msg = await SR.RM.item_remove(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/item/remove/batch")
async def queue_item_remove_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Remove a batch of plans from the queue
    """
    try:
        if "uids" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_remove_batch", params=payload)
        else:
            msg = await SR.RM.item_remove_batch(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/item/move")
async def queue_item_move_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Move plan in the queue
    """
    try:
        msg = await SR.RM.item_move(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/queue/item/move/batch")
async def queue_item_move_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Move a batch of plans in the queue
    """
    try:
        msg = await SR.RM.item_move_batch(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/queue/item/get")
async def queue_item_get_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["read:queue"])
):
    """
    Get a plan from the queue
    """
    try:
        msg = await SR.RM.item_get(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/history/get")
async def history_get_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["read:history"])
):
    """
    Returns the plan history (list of dicts).
    """
    try:
        msg = await SR.RM.history_get(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/history/clear")
async def history_clear_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:history:edit"])
):
    """
    Clear plan history.
    """
    try:
        msg = await SR.RM.history_clear(**payload)
    except Exception:
        process_exception()

    return msg


@router.post("/environment/open")
async def environment_open_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:manager:control"])
):
    """
    Creates RE environment: creates RE Worker process, starts and configures Run Engine.
    """
    try:
        msg = await SR.RM.environment_open(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/environment/close")
async def environment_close_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:manager:control"])
):
    """
    Orderly closes of RE environment. The command returns success only if no plan is running,
    i.e. RE Manager is in the idle state. The command is rejected if a plan is running.
    """
    try:
        msg = await SR.RM.environment_close(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/environment/destroy")
async def environment_destroy_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:manager:control"])
):
    """
    Destroys RE environment by killing RE Worker process. This is a last resort command which
    should be made available only to expert level users.
    """
    try:
        msg = await SR.RM.environment_destroy(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/re/pause")
async def re_pause_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["write:plan:control"]),
):
    """
    Pause Run Engine.
    """
    try:
        msg = await SR.RM.re_pause(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/re/resume")
async def re_resume_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:plan:control"])
):
    """
    Run Engine: resume execution of a paused plan
    """
    try:
        msg = await SR.RM.re_resume(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/re/stop")
async def re_stop_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:plan:control"])
):
    """
    Run Engine: stop execution of a paused plan
    """
    try:
        msg = await SR.RM.re_stop(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/re/abort")
async def re_abort_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:plan:control"])
):
    """
    Run Engine: abort execution of a paused plan
    """
    try:
        msg = await SR.RM.re_abort(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/re/halt")
async def re_halt_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:plan:control"])
):
    """
    Run Engine: halt execution of a paused plan
    """
    try:
        msg = await SR.RM.re_halt(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/re/runs")
async def re_runs_handler(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the list of active, open or closed runs (runs that were opened
    during execution of the currently running plan and combines the subsets of 'open' and
    'closed' runs.) The parameter ``options`` is used to select the category of runs
    (``'active'``, ``'open'`` or ``'closed'``). By default the API returns the active runs.
    """
    try:
        msg = await SR.RM.re_runs(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/re/runs/active")
async def re_runs_active_handler(principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the list of active runs (runs that were opened during execution of
    the currently running plan and combines the subsets of 'open' and 'closed' runs.)
    """
    try:
        params = {"option": "active"}
        msg = await SR.RM.re_runs(**params)
    except Exception:
        process_exception()
    return msg


@router.get("/re/runs/open")
async def re_runs_open_handler(principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the subset of active runs that includes runs that were open, but not yet closed.
    """
    try:
        params = {"option": "open"}
        msg = await SR.RM.re_runs(**params)
    except Exception:
        process_exception()
    return msg


@router.get("/re/runs/closed")
async def re_runs_closed_handler(principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the subset of active runs that includes runs that were already closed.
    """
    try:
        params = {"option": "closed"}
        msg = await SR.RM.re_runs(**params)
    except Exception:
        process_exception()
    return msg


@router.get("/plans/allowed")
async def plans_allowed_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:resources"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Returns the lists of allowed plans. If boolean optional parameter ``reduced``
    is ``True``(default value is ``False`), then simplify plan descriptions before
    calling the API.
    """

    try:
        validate_payload_keys(payload, optional_keys=["reduced"])

        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        user_group = resource_access_manager.get_resource_group(username)

        if "reduced" in payload:
            reduced = payload["reduced"]
            del payload["reduced"]
        else:
            reduced = False
        payload.update({"user_group": user_group})

        msg = await SR.RM.plans_allowed(**payload)
        if reduced and ("plans_allowed" in msg):
            msg["plans_allowed"] = simplify_plan_descriptions(msg["plans_allowed"])
    except Exception:
        process_exception()
    return msg


@router.get("/devices/allowed")
async def devices_allowed_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:resources"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Returns the lists of allowed devices.
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        user_group = resource_access_manager.get_resource_group(username)

        payload.update({"user_group": user_group})

        msg = await SR.RM.devices_allowed(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/plans/existing")
async def plans_existing_handler(
    payload: dict = {},
):
    """
    Returns the lists of existing plans. If boolean optional parameter ``reduced``
    is ``True``(default value is ``False`), then simplify plan descriptions before
    calling the API.
    """
    try:
        validate_payload_keys(payload, optional_keys=["reduced"])

        if "reduced" in payload:
            reduced = payload["reduced"]
            del payload["reduced"]
        else:
            reduced = False

        msg = await SR.RM.plans_existing(**payload)
        if reduced and ("plans_existing" in msg):
            msg["plans_existing"] = simplify_plan_descriptions(msg["plans_existing"])
    except Exception:
        process_exception()

    return msg


@router.get("/devices/existing")
async def devices_existing_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:resources"]),
):
    """
    Returns the lists of existing devices.
    """
    try:
        msg = await SR.RM.devices_existing(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/permissions/reload")
async def permissions_reload_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["write:config"]),
):
    """
    Reloads the list of allowed plans and devices and user group permission from the default location
    or location set using command line parameters of RE Manager. Use this request to reload the data
    if the respective files were changed on disk.
    """
    try:
        msg = await SR.RM.permissions_reload(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/permissions/get")
async def permissions_get_handler(principal=Security(get_current_principal, scopes=["read:config"])):
    """
    Download the dictionary of user group permissions.
    """
    try:
        msg = await SR.RM.permissions_get()
    except Exception:
        process_exception()
    return msg


@router.post("/permissions/set")
async def permissions_set_handler(
    payload: dict, principal=Security(get_current_principal, scopes=["write:permissions", "write:permissions"])
):
    """
    Upload the dictionary of user group permissions (parameter: ``user_group_permissions``).
    """
    try:
        if "user_group_permissions" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="permissions_set", params=payload)
        else:
            msg = await SR.RM.permissions_set(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/function/execute")
async def function_execute_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:execute"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Execute function defined in startup scripts in RE Worker environment.
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "item" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="function_execute", params=payload)
        else:
            msg = await SR.RM.function_execute(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/script/upload")
async def script_upload_handler(
    payload: dict, principal=Security(get_current_principal, scopes=["write:scripts"])
):
    """
    Upload and execute script in RE Worker environment.
    """
    try:
        if "script" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="script_upload", params=payload)
        else:
            msg = await SR.RM.script_upload(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/task/status")
async def task_status(payload: dict, principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Return status of one or more running tasks.
    """
    try:
        if "task_uid" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="task_status", params=payload)
        else:
            msg = await SR.RM.task_status(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/task/result")
async def task_result(payload: dict, principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Return result of execution of a running or completed task.
    """
    try:
        if "task_uid" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="task_result", params=payload)
        else:
            msg = await SR.RM.task_result(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/lock")
async def lock_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:lock"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
):
    """
    Lock RE Manager.
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        payload.update({"user": displayed_name})

        msg = await SR.RM.lock(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/unlock")
async def unlock_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:lock"]),
):
    """
    Unlock RE Manager.
    """
    try:
        msg = await SR.RM.unlock(**payload)
    except Exception:
        process_exception()
    return msg


@router.get("/lock/info")
async def lock_info_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["read:lock"]),
):
    """
    Unlock RE Manager.
    """
    try:
        msg = await SR.RM.lock_info(**payload)
    except Exception:
        process_exception()
    return msg


@router.post("/manager/stop")
async def manager_stop_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:manager:stop"])
):
    """
    Stops of RE Manager. RE Manager will not be restarted after it is stoped.
    """
    try:
        msg = await SR.RM.send_request(method="manager_stop", params=payload)
    except Exception:
        process_exception()
    return msg


@router.post("/test/manager/kill")
async def test_manager_kill_handler(principal=Security(get_current_principal, scopes=["write:testing"])):
    """
    The command stops event loop of RE Manager process. Used for testing of RE Manager
    stability and handling of communication timeouts.
    """
    try:
        msg = await SR.RM.send_request(method="manager_kill")
    except Exception:
        process_exception()
    return msg


@router.get("/test/server/sleep")
async def test_server_sleep_handler(
    payload: dict, principal=Security(get_current_principal, scopes=["read:testing"])
):
    """
    The API is intended for testing how the client applications and API libraries handle timeouts.
    The handler waits for the requested number of seconds and then returns the message indicating success.
    The API call is safe, since it does not block the event loop or calls to RE Manager
    """
    try:
        if "time" not in payload:
            raise IndexError(f"The required parameter 'time' is missing in the API call: {payload}")
        sleep_time = payload["time"]
        await asyncio.sleep(sleep_time)
        msg = {"success": True, "msg": ""}
    except Exception:
        process_exception()
    return msg


@router.get("/stream_console_output")
def stream_console_output(principal=Security(get_current_principal, scopes=["read:console"])):
    queues_set = SR.console_output_loader.queues_set
    stm = ConsoleOutputEventStream(queues_set=queues_set)
    sr = StreamingResponseFromClass(stm, media_type="text/plain")
    return sr


@router.get("/console_output")
async def console_output(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:console"])):
    try:
        n_lines = payload.get("nlines", 200)
        text = await SR.console_output_loader.get_text_buffer(n_lines)
    except Exception:
        process_exception()

    # Add 'success' and 'msg' so that the API is compatible with other QServer API.
    return {"success": True, "msg": "", "text": text}


@router.get("/console_output/uid")
def console_output_uid(principal=Security(get_current_principal, scopes=["read:console"])):
    """
    UID of the text buffer. Use with ``console_output`` API.
    """
    try:
        uid = SR.console_output_loader.text_buffer_uid
    except Exception:
        process_exception()
    return {"success": True, "msg": "", "console_output_uid": uid}


@router.get("/console_output_update")
def console_output_update(payload: dict, principal=Security(get_current_principal, scopes=["read:console"])):
    """
    Download the list of new messages that were accumulated at the server. The API
    accepts a required parameter ``last_msg_uid`` with UID of the last downloaded message.
    If the UID is not found in the buffer, an empty message list and valid UID is
    returned. If UID is ``"ALL"``, then all accumulated messages in the buffer is
    returned. If UID is found in the buffer, then the list of new messages is returned.

    At the client: initialize the system by sending request with ``last_msg_uid`` set
    to random string or ``"ALL"``. In each request use ``last_msg_uid`` returned by the previous
    request to download new messages.
    """
    try:
        validate_payload_keys(payload, required_keys=["last_msg_uid"])

        response = SR.console_output_loader.get_new_msgs(last_msg_uid=payload["last_msg_uid"])
        # Add 'success' and 'msg' so that the API is compatible with other QServer API.
        response.update({"success": True, "msg": ""})
    except Exception:
        process_exception()

    return response
