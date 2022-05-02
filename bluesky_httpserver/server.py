import importlib
import logging
import re
import os
import pprint

from fastapi import FastAPI

from bluesky_queueserver.manager.comms import validate_zmq_key
from bluesky_queueserver_api.zmq.aio import REManagerAPI

from .console_output import CollectPublishedConsoleOutput
from .resources import SERVER_RESOURCES as SR
from .utils import get_login_data

from .routers import general

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bluesky_queueserver").setLevel("DEBUG")

# Use FastAPI
app = FastAPI()


def add_router(app, *, module_and_router_name):
    """
    Include a router specified by module and router name represented as a string.

    Parameters
    ----------
    app: FastAPI
        Instantiated ``FastAPI`` object.
    module_and_router_name: str
        Name of the module and router object represented as a string, e.g. ``'some.module.router'``,
        where ``some.module`` is the module name and ``router`` is the name of the router object
        in the module.

    Raises
    ------
    ImportError
        Failed to include router, most likely because the module could not be imported or the router
        is not found.
    """
    try:
        components = module_and_router_name.split(".")
        if len(components) < 2:
            raise ValueError(
                f"Module name or router name is not found in {module_and_router_name!r}: "
                "expected format '<module-name>.<router-name>'"
            )
        module_name = ".".join(components[:-1])
        router_name = components[-1]
        mod = importlib.import_module(module_name)
        router = getattr(mod, router_name)
        app.include_router(router)
    except Exception as ex:
        raise ImportError(f"Failed to import router {module_and_router_name!r}: {ex}") from ex


@app.on_event("startup")
async def startup_event():
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
    module_names_str = os.getenv("QSERVER_CUSTOM_MODULES", None)
    if (module_names_str is None) and (os.getenv("QSERVER_CUSTOM_MODULE", None) is not None):
        logger.warning(
            "Environment variable QSERVER_CUSTOM_MODULE is deprecated and will be removed. "
            "Use the environment variable QSERVER_CUSTOM_MODULES, which accepts a string with "
            "comma or colon-separated module names."
        )
    module_names_str = module_names_str or os.getenv("QSERVER_CUSTOM_MODULE", None)

    if module_names_str:
        module_names = re.split(":|,", module_names_str)
        logger.info("Custom modules to import (env. variable): %s", pprint.pformat(module_names))

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
    else:
        SR.set_custom_code_modules([])

    # Include standard routers
    app.include_router(general.router)

    # Include custom routers
    router_names_str = os.getenv("QSERVER_HTTP_CUSTOM_ROUTERS", None)
    if router_names_str:
        router_names = re.split(":|,", router_names_str)
        logger.info("Custom routers to include (env. variable): %s", pprint.pformat(router_names))
        routers_already_included = set()
        for rn in router_names:
            if rn and (rn not in routers_already_included):
                logger.info("Including custom router '%s' ...", rn)
                routers_already_included.add(rn)
                add_router(app, module_and_router_name=rn)
        logger.info("All custom routers are included successfully.")

    # The following message is used in unit tests to detect when HTTP server is started.
    #   Unit tests need to be modified if this message is modified.
    logger.info("Bluesky HTTP Server started successfully")


@app.on_event("shutdown")
async def shutdown_event():
    await SR.RM.close()
    await SR.console_output_loader.stop()
