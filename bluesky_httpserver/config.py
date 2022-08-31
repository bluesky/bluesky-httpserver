"""
This module handles server configuration.

See profiles.py for client configuration.
"""
import copy
from datetime import timedelta
import os
from functools import lru_cache
from pathlib import Path

import jsonschema

from .utils import import_object, parse, prepend_to_sys_path


@lru_cache(maxsize=1)
def schema():
    "Load the schema for service-side configuration."
    import yaml

    here = Path(__file__).parent.absolute()
    schema_path = os.path.join(here, "config_schemas", "service_configuration.yml")
    with open(schema_path, "r") as file:
        return yaml.safe_load(file)


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
    return {
        "authentication": auth_spec,
        "server_settings": server_settings,
    }


def merge(configs):
    merged = {}

    # These variables are used to produce error messages that point
    # to the relevant config file(s).
    authentication_config_source = None
    uvicorn_config_source = None
    metrics_config_source = None
    database_config_source = None
    allow_origins = []

    for filepath, config in configs.items():
        allow_origins.extend(config.get("allow_origins", []))
        if "authentication" in config:
            if "authentication" in merged:
                raise ConfigError(
                    "authentication can only be specified in one file. "
                    f"It was found in both {authentication_config_source} and "
                    f"{filepath}"
                )
            authentication_config_source = filepath
            merged["authentication"] = config["authentication"]
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
                jsonschema.validate(instance=config, schema=schema())
            except jsonschema.ValidationError as err:
                msg = err.args[0]
                raise ConfigError(f"ValidationError while parsing configuration file {filepath}: {msg}") from err
            parsed_configs[filepath] = config

    merged_config = merge(parsed_configs)
    return merged_config


class ConfigError(ValueError):
    pass
