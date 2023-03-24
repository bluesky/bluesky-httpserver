import argparse
import logging
import os
import pprint
import sys
from pathlib import Path

import bluesky_httpserver

from .app import build_app
from .config import construct_build_app_kwargs, parse_configs
from .settings import get_settings
from .utils import get_authenticators

logger = logging.getLogger(__name__)

qserver_version = bluesky_httpserver.__version__

default_http_server_host = "localhost"
default_http_server_port = 60610


def start_server():
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("bluesky_httpserver").setLevel("INFO")

    def formatter(prog):
        # Set maximum width such that printed help mostly fits in the RTD theme code block (documentation).
        return argparse.RawDescriptionHelpFormatter(prog, max_help_position=20, width=90)

    parser = argparse.ArgumentParser(
        description="Start Bluesky HTTP Server.\n" f"bluesky-httpserver version {qserver_version}.\n",
        formatter_class=formatter,
    )

    parser.add_argument(
        "--host",
        dest="http_server_host",
        action="store",
        default=None,
        help="HTTP server host name, e.g. '127.0.0.1' or 'localhost' " f"(default: {default_http_server_host!r}).",
    )

    parser.add_argument(
        "--port",
        dest="http_server_port",
        action="store",
        default=None,
        help="HTTP server port, e.g. '127.0.0.1' or 'localhost' " f"(default: {default_http_server_port!r}).",
    )

    parser.add_argument(
        "--public",
        dest="public",
        action="store_true",
        default=False,
        help="Explicitly allows public access to the server and disables authorization/authentication.",
    )

    parser.add_argument(
        "--config_path",
        dest="config_path",
        action="store",
        default=None,
        help="Path to configuration file or directory with configuration files. The path overrides "
        "the path defined in QSERVER_HTTP_SERVER_CONFIG environment variable. If the parameter and "
        "the environemnt variable is not specified, then no configuration file is loaded.",
    )

    args = parser.parse_args()

    public = args.public
    config_path = args.config_path

    http_server_host = args.http_server_host
    http_server_port = args.http_server_port
    http_server_port = int(http_server_port) if http_server_port else http_server_port

    logger.info("Preparing to start Bluesky HTTP Server ...")

    config_path = config_path or os.getenv("QSERVER_HTTP_SERVER_CONFIG", None)
    try:
        parsed_config = parse_configs(config_path) if config_path else {}
    except Exception as ex:
        logger.error(ex)
        raise

    # Let --public flag override settings in config.
    if public:
        if "authentication" not in parsed_config:
            parsed_config["authentication"] = {}
        parsed_config["authentication"]["allow_anonymous_access"] = True

    # Extract config for uvicorn.
    uvicorn_kwargs = parsed_config.pop("uvicorn", {})
    # 'host' and 'port' from CLI parameters overrides the parameters from config.
    uvicorn_kwargs["host"] = http_server_host or uvicorn_kwargs.get("host", default_http_server_host)
    uvicorn_kwargs["port"] = http_server_port or uvicorn_kwargs.get("port", default_http_server_port)

    # This config was already validated when it was parsed. Do not re-validate.
    kwargs = construct_build_app_kwargs(parsed_config, source_filepath=config_path)
    if config_path:
        logger.info(f"Using configuration from {Path(config_path).absolute()}")
    else:
        logger.info("No configuration file was specified. Using CLI parameters and environment variables.")

    web_app = build_app(**kwargs)
    print_admin_api_key_if_generated(web_app, host=uvicorn_kwargs["host"], port=uvicorn_kwargs["port"])

    logger.info("Starting Bluesky HTTP Server at {http_server_host}:{http_server_port} ...")

    import uvicorn

    uvicorn.run(web_app, **uvicorn_kwargs)


def print_admin_api_key_if_generated(web_app, host, port):
    # host = host or "127.0.0.1"
    # port = port or 8000
    settings = web_app.dependency_overrides.get(get_settings, get_settings)()

    logger.info("APP settings: %s", pprint.pformat(dict(settings)))
    authenticators = web_app.dependency_overrides.get(get_authenticators, get_authenticators)()
    if settings.allow_anonymous_access:
        print(
            "The server is running in 'public' mode, permitting open, anonymous access\n"
            "for reading. Any data that is not specifically controlled with an access\n"
            "policy will be visible to anyone who can connect to this server.\n",
            file=sys.stderr,
        )
    if (not authenticators) and settings.single_user_api_key_generated:
        print(
            "Navigate a web browser to:\n\n"
            f"http://{host}:{port}?api_key={settings.single_user_api_key}\n\n"
            "or connect an HTTP client to:\n\n"
            f"http://{host}:{port}/api?api_key={settings.single_user_api_key}\n",
            file=sys.stderr,
        )


def app_factory():
    """
    Return an ASGI app instance.

    Use a configuration file at the path specified by the environment variable
    QSERVER_HTTP_SERVER_CONFIG. If the env. variable is not set, then do not load
    configuration.

    This is intended to be used for horizontal deployment (using gunicorn, for
    example) where only a module and instance or factory can be specified.
    """
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("bluesky_httpserver").setLevel("INFO")

    config_path = os.getenv("QSERVER_HTTP_SERVER_CONFIG", None)

    from .config import construct_build_app_kwargs, parse_configs

    try:
        parsed_config = parse_configs(config_path) if config_path else {}
    except Exception as ex:
        logger.error(ex)
        raise

    # This config was already validated when it was parsed. Do not re-validate.
    kwargs = construct_build_app_kwargs(parsed_config, source_filepath=config_path)
    if config_path:
        logger.info(f"Using configuration from {Path(config_path).absolute()}")
    else:
        logger.info("No configuration file was specified. Using environment variables.")

    web_app = build_app(**kwargs)
    uvicorn_config = parsed_config.get("uvicorn", {})
    print_admin_api_key_if_generated(web_app, host=uvicorn_config.get("host"), port=uvicorn_config.get("port"))

    return web_app


def __getattr__(name):
    """
    This supports tiled.server.app.app by creating app on demand.
    """
    if name == "app":
        try:
            return app_factory()
        except Exception as err:
            raise Exception("Failed to create app.") from err
    raise AttributeError(name)
