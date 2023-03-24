"""
This module handles server configuration.

See profiles.py for client configuration.
"""
import copy
import logging
import os
from datetime import timedelta
from pathlib import Path

import jsonschema

from .config_schemas.loading import ConfigError, load_schema_from_yml
from .utils import import_object, parse, prepend_to_sys_path

logger = logging.getLogger(__name__)


SERVICE_CONFIGURATION_FILE_NAME = "service_configuration.yml"


def construct_build_app_kwargs(
    config,
    *,
    source_filepath=None,
):
    """
    Given parsed configuration, construct arguments for build_app(...).
    """
    config = copy.deepcopy(config)  # Avoid mutating input.
    sys_path_additions = []
    if source_filepath:
        if os.path.isdir(source_filepath):
            directory = source_filepath
        else:
            directory = os.path.dirname(source_filepath)
        sys_path_additions.append(directory)
    with prepend_to_sys_path(*sys_path_additions):
        auth_spec = config.get("authentication", {}) or {}
        auth_aliases = {}  # TODO Enable entrypoint as alias for authenticator_class?
        providers = list(auth_spec.get("providers", []))
        provider_names = [p["provider"] for p in providers]
        if len(set(provider_names)) != len(provider_names):
            raise ValueError("The names given for 'provider' must be unique. " f"Found duplicates in {providers}")
        for i, authenticator in enumerate(providers):
            import_path = auth_aliases.get(authenticator["authenticator"], authenticator["authenticator"])
            authenticator_class = import_object(import_path, accept_live_object=True)
            authenticator = authenticator_class(**authenticator.get("args", {}))
            # Replace "package.module:Object" with live instance.
            auth_spec["providers"][i]["authenticator"] = authenticator
        # The following parameters are integers, which should be changed to 'timedelta'
        for k in ("access_token_max_age", "refresh_token_max_age", "session_max_age"):
            if k in auth_spec:
                auth_spec[k] = timedelta(seconds=auth_spec[k])

        api_access_spec = config.get("api_access", {}) or {}
        import_path = api_access_spec.get("policy", "bluesky_httpserver.authorization:BasicAPIAccessControl")
        api_access_manager_class = import_object(import_path, accept_live_object=True)
        api_access_manager = api_access_manager_class(**api_access_spec.get("args", {}))
        api_access_spec["manager_object"] = api_access_manager

        resource_access_spec = config.get("resource_access", {}) or {}
        import_path = resource_access_spec.get(
            "policy", "bluesky_httpserver.authorization:DefaultResourceAccessControl"
        )
        resource_access_manager_class = import_object(import_path, accept_live_object=True)
        resouce_access_manager = resource_access_manager_class(**resource_access_spec.get("args", {}))
        resource_access_spec["manager_object"] = resouce_access_manager

        server_settings = {}
        server_settings["allow_origins"] = config.get("allow_origins")
        server_settings["database"] = config.get("database", {})
        metrics = config.get("metrics", {})
        if metrics.get("prometheus", False):
            prometheus_multiproc_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR", None)
            if not prometheus_multiproc_dir:
                raise ValueError("prometheus enabled but PROMETHEUS_MULTIPROC_DIR env variable not set")
            elif not Path(prometheus_multiproc_dir).is_dir():
                raise ValueError(
                    "prometheus enabled but PROMETHEUS_MULTIPROC_DIR "
                    f"({prometheus_multiproc_dir}) is not a directory"
                )
            elif not os.access(prometheus_multiproc_dir, os.W_OK):
                raise ValueError(
                    "prometheus enabled but PROMETHEUS_MULTIPROC_DIR "
                    f"({prometheus_multiproc_dir}) is not writable"
                )
        server_settings["metrics"] = metrics
        server_settings["qserver_zmq_configuration"] = config.get("qserver_zmq_configuration", {})
        server_settings["server_configuration"] = config.get("server_configuration", {})
    return {
        "authentication": auth_spec,
        "api_access": api_access_spec,
        "resource_access": resource_access_spec,
        "server_settings": server_settings,
    }


def merge(configs):
    merged = {}

    # These variables are used to produce error messages that point
    # to the relevant config file(s).
    qserver_zmq_config_source = None
    server_config_source = None
    authentication_config_source = None
    uvicorn_config_source = None
    metrics_config_source = None
    database_config_source = None
    api_access_config_source = None
    resource_access_config_source = None
    allow_origins = []

    for filepath, config in configs.items():
        allow_origins.extend(config.get("allow_origins", []))
        if "qserver_zmq_configuration" in config:
            if "qserver_zmq_configuration" in merged:
                raise ConfigError(
                    "'qserver_zmq_configuration' can only be specified in one file. "
                    f"It was found in both {qserver_zmq_config_source} and "
                    f"{filepath}"
                )
            qserver_zmq_config_source = filepath
            merged["qserver_zmq_configuration"] = config["qserver_zmq_configuration"]
        if "server_configuration" in config:
            if "server_configuration" in merged:
                raise ConfigError(
                    "'server_configuration' can only be specified in one file. "
                    f"It was found in both {server_config_source} and "
                    f"{filepath}"
                )
            server_config_source = filepath
            merged["server_configuration"] = config["server_configuration"]
        if "authentication" in config:
            if "authentication" in merged:
                raise ConfigError(
                    "authentication can only be specified in one file. "
                    f"It was found in both {authentication_config_source} and "
                    f"{filepath}"
                )
            authentication_config_source = filepath
            merged["authentication"] = config["authentication"]
        if "api_access" in config:
            if "api_access" in merged:
                raise ConfigError(
                    "api access can only be specified in one file. "
                    f"It was found in both {api_access_config_source} and "
                    f"{filepath}"
                )
            api_access_config_source = filepath
            merged["api_access"] = config["api_access"]
        if "resource_access" in config:
            if "resource_access" in merged:
                raise ConfigError(
                    "resource access can only be specified in one file. "
                    f"It was found in both {resource_access_config_source} and "
                    f"{filepath}"
                )
            resource_access_config_source = filepath
            merged["resource_access"] = config["resource_access"]
        if "uvicorn" in config:
            if "uvicorn" in merged:
                raise ConfigError(
                    "uvicorn can only be specified in one file. "
                    f"It was found in both {uvicorn_config_source} and "
                    f"{filepath}"
                )
            uvicorn_config_source = filepath
            merged["uvicorn"] = config["uvicorn"]
        if "metrics" in config:
            if "metrics" in merged:
                raise ConfigError(
                    "metrics can only be specified in one file. "
                    f"It was found in both {metrics_config_source} and "
                    f"{filepath}"
                )
            metrics_config_source = filepath
            merged["metrics"] = config["metrics"]
        if "database" in config:
            if "database" in merged:
                raise ConfigError(
                    "database configuration can only be specified in one file. "
                    f"It was found in both {database_config_source} and "
                    f"{filepath}"
                )
            database_config_source = filepath
            merged["database"] = config["database"]
    merged["allow_origins"] = allow_origins
    return merged


def parse_configs(config_path):
    """
    Parse configuration file or directory of configuration files.

    If a directory is given it is expected to contain only valid
    configuration files, except for the following which are ignored:

    * Hidden files or directories (starting with .)
    * Python scripts (ending in .py)
    * The __pycache__ directory
    """
    if isinstance(config_path, str):
        config_path = Path(config_path)
    if config_path.is_file():
        filepaths = [config_path]
    elif config_path.is_dir():
        filepaths = list(config_path.iterdir())
    elif not config_path.exists():
        raise ValueError(f"The config path {config_path!s} doesn't exist.")
    else:
        assert False, "It should be impossible to reach this line."

    parsed_configs = {}
    # The sorting here is just to make the order of the results deterministic.
    # There is *not* any sorting-based precedence applied.
    for filepath in sorted(filepaths):
        # Ignore hidden files and .py files.
        if filepath.parts[-1].startswith(".") or filepath.suffix == ".py" or filepath.parts[-1] == "__pycache__":
            continue
        with open(filepath) as file:
            config = parse(file)
            try:
                jsonschema.validate(instance=config, schema=load_schema_from_yml(SERVICE_CONFIGURATION_FILE_NAME))
            except jsonschema.ValidationError as err:
                msg = err.args[0]
                raise ConfigError(f"ValidationError while parsing configuration file {filepath}: {msg}") from err
            parsed_configs[filepath] = config

    merged_config = merge(parsed_configs)
    return merged_config
