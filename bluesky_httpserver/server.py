import logging
import re
import os
import importlib

from fastapi import FastAPI

from bluesky_queueserver.manager.comms import validate_zmq_key
from bluesky_queueserver_api.zmq.aio import REManagerAPI

from .console_output import CollectPublishedConsoleOutput
from .resources import SERVER_RESOURCES as SR
from .utils import get_login_data

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bluesky_queueserver").setLevel("DEBUG")

# Use FastAPI
app = FastAPI()


@app.on_event("startup")
async def startup_event():
    global zmq_to_manager

    # Read private key from the environment variable, then check if the CLI parameter exists
    zmq_public_key = os.environ.get("QSERVER_ZMQ_PUBLIC_KEY", None)
    zmq_public_key = zmq_public_key if zmq_public_key else None  # Case of ""
    if zmq_public_key is not None:
        try:
            validate_zmq_key(zmq_public_key)
        except Exception as ex:
            raise ValueError("ZMQ public key is improperly formatted: %s", str(ex))

    # TODO: implement nicer exit with error reporting in case of failure
    zmq_control_addr = os.getenv("QSERVER_ZMQ_CONTROL_ADDRESS", None)
    if zmq_control_addr is None:
        zmq_control_addr = os.getenv("QSERVER_ZMQ_ADDRESS_CONTROL", None)
        if zmq_control_addr is not None:
            logger.warning(
                "Environment variable QSERVER_ZMQ_ADDRESS_CONTROL is deprecated: use environment variable "
                "QSERVER_ZMQ_CONTROL_ADDRESS to pass address of 0MQ control socket to HTTP Server."
            )
    if zmq_control_addr is None:
        # Support for deprecated environment variable QSERVER_ZMQ_ADDRESS.
        # TODO: remove in one of the future versions
        zmq_control_addr = os.getenv("QSERVER_ZMQ_ADDRESS", None)
        if zmq_control_addr is not None:
            logger.warning(
                "Environment variable QSERVER_ZMQ_ADDRESS is deprecated: use environment variable "
                "QSERVER_ZMQ_CONTROL_ADDRESS to pass address of 0MQ control socket to HTTP Server."
            )

    zmq_info_addr = os.getenv("QSERVER_ZMQ_INFO_ADDRESS", None)
    if zmq_info_addr is None:
        # Support for deprecated environment variable QSERVER_ZMQ_ADDRESS.
        # TODO: remove in one of the future versions
        zmq_info_addr = os.getenv("QSERVER_ZMQ_ADDRESS_CONSOLE", None)
        if zmq_info_addr is not None:
            logger.warning(
                "Environment variable QSERVER_ZMQ_ADDRESS_CONSOLE is deprecated: use environment variable "
                "QSERVER_ZMQ_INFO_ADDRESS to pass address of 0MQ information socket to HTTP Server."
            )

    logger.info(
        f"Connecting to RE Manager: \nControl 0MQ socket address: {zmq_control_addr}\n"
        f"Information 0MQ socket address: {zmq_info_addr}"
    )

    RM = REManagerAPI(
        zmq_control_addr=zmq_control_addr,
        zmq_info_addr=zmq_info_addr,
        zmq_public_key=zmq_public_key,
        request_fail_exceptions=False,
        status_expiration_period=0.4,  # Make it smaller than default
        console_monitor_max_lines=2000,
    )
    SR.set_RM(RM)

    login_data = get_login_data()
    SR.RM._user = login_data["user"]
    SR.RM._user_group = login_data["user_group"]

    SR.set_console_output_loader(CollectPublishedConsoleOutput(rm_ref=RM))
    SR.console_output_loader.start()

    # Import module with custom code
    module_names = os.getenv("QSERVER_CUSTOM_MODULES", None)
    if (module_names is None) and (os.getenv("QSERVER_CUSTOM_MODULE", None) is not None):
        logger.warning(
            "Environment variable QSERVER_CUSTOM_MODULE is deprecated and will be removed. "
            "Use the environment variable QSERVER_CUSTOM_MODULES, which accepts a string with "
            "comma or colon-separated module names."
        )
    module_names = module_names or os.getenv("QSERVER_CUSTOM_MODULE", None)
    if isinstance(module_names, str):
        module_names = re.split(":|,", module_names)
    else:
        logger.info("The value of environment variable QSERVER_CUSTOM_MODULES is not a string")
        module_names = []

    logger.info(f"The following custom modules will be imported: {module_names}")

    # Import all listed custom modules
    custom_code_modules = []
    for name in module_names:
        try:
            logger.info("Importing custom module '%s' ...", name)
            custom_code_modules.append(importlib.import_module(name.replace("-", "_")))
            logger.info("Module '%s' was imported successfully.", name)
        except Exception as ex:
            logger.error("Failed to import custom instrument module '%s': %s", name, ex)
    SR.set_custom_code_modules(custom_code_modules)

    from .routers import general

    app.include_router(general.router)

    # import module_code  ##
    # app.include_router(module_code.router)  ##

    # The following message is used in unit tests to detect when HTTP server is started.
    #   Unit tests need to be modified if this message is modified.
    logger.info("Bluesky HTTP Server started successfully")


@app.on_event("shutdown")
async def shutdown_event():
    await SR.RM.close()
    await SR.console_output_loader.stop()
