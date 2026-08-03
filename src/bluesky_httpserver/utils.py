import builtins
import collections
import contextlib
import copy
import enum
import importlib
import operator
import os
import sys
import time

from bluesky_queueserver_api.zmq.aio import REManagerAPI
from fastapi import HTTPException

from .authorization import _DEFAULT_USERNAME_PUBLIC, _DEFAULT_USERNAME_SINGLE_USER
from .authorization._defaults import _DEFAULT_ANONYMOUS_PROVIDER_NAME


def process_exception():
    try:
        raise
    except REManagerAPI.RequestTimeoutError as ex:
        raise HTTPException(status_code=408, detail=str(ex))
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex))


# The default user name and user group should never be sent to the manager unless there is a bug.
_default_login_data = {"user": "Default HTTP User", "user_group": "THE_GROUP_THAT_DOES_NOT_EXIST"}


def get_default_login_data():
    return copy.deepcopy(_default_login_data)


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


def import_object(colon_separated_string, accept_live_object=False):
    if not isinstance(colon_separated_string, str):
        # We have been handed the live object itself.
        # Nothing to import. Pass it through.
        return colon_separated_string
    MESSAGE = (
        "Expected string formatted like:\n\n"
        "    package_name.module_name:object_name\n\n"
        "Notice *dots* between modules and a "
        f"*colon* before the object name. Received:\n\n{colon_separated_string!r}"
    )
    import_path, _, obj_path = colon_separated_string.partition(":")
    for segment in import_path.split("."):
        if not segment.isidentifier():
            raise ValueError(MESSAGE)
    for attr in obj_path.split("."):
        if not attr.isidentifier():
            raise ValueError(MESSAGE)
    module = importlib.import_module(import_path)
    return operator.attrgetter(obj_path)(module)


def expand_environment_variables(config):
    """Expand environment variables in a nested config dictionary

    VENDORED FROM dask.config.

    This function will recursively search through any nested dictionaries
    and/or lists.

    Parameters
    ----------
    config : dict, iterable, or str
        Input object to search for environment variables

    Returns
    -------
    config : same type as input

    Examples
    --------
    >>> expand_environment_variables({'x': [1, 2, '$USER']})  # doctest: +SKIP
    {'x': [1, 2, 'my-username']}
    """
    if isinstance(config, collections.abc.Mapping):
        return {k: expand_environment_variables(v) for k, v in config.items()}
    elif isinstance(config, str):
        return os.path.expandvars(config)
    elif isinstance(config, (list, tuple, builtins.set)):
        return type(config)([expand_environment_variables(v) for v in config])
    else:
        return config


def parse(file):
    """
    Given a config file, parse it.

    This wraps YAML parsing and environment variable expansion.
    """
    import yaml

    content = yaml.safe_load(file.read())
    return expand_environment_variables(content)


@contextlib.contextmanager
def prepend_to_sys_path(*paths):
    "Temporarily prepend items to sys.path."

    for item in reversed(paths):
        # Ensure item is str (not pathlib.Path).
        sys.path.insert(0, str(item))
    try:
        yield
    finally:
        for item in paths:
            sys.path.pop(0)


def get_authenticators():
    raise NotImplementedError(
        "This should be overridden via dependency_overrides. See bluesky_httpserver.server.app.build_app()."
    )


def get_resource_access_manager():
    raise NotImplementedError(
        "This should be overridden via dependency_overrides. See bluesky_httpserver.server.app.build_app()."
    )


def get_api_access_manager():
    raise NotImplementedError(
        "This should be overridden via dependency_overrides. See bluesky_httpserver.server.app.build_app()."
    )


class SpecialUsers(str, enum.Enum):
    public = _DEFAULT_USERNAME_PUBLIC
    single_user = _DEFAULT_USERNAME_SINGLE_USER


def safe_json_dump(content):
    """
    Try to use native orjson path; fall back to going through Python list.
    """
    import orjson

    def default(content):
        # No need to import numpy if it hasn't been used already.
        numpy = sys.modules.get("numpy", None)
        if numpy is not None:
            if isinstance(content, numpy.ndarray):
                # If we make it here, OPT_NUMPY_SERIALIZE failed because we have hit some edge case.
                # Give up on the numpy fast-path and convert to Python list.
                # If the items in this list aren't serializable (e.g. bytes) we'll recurse on each item.
                return content.tolist()
            elif isinstance(content, (bytes, numpy.bytes_)):
                return content.decode("utf-8")
        raise TypeError

    # Not all numpy dtypes are supported by orjson.
    # Fall back to converting to a (possibly nested) Python list.
    return orjson.dumps(content, option=orjson.OPT_SERIALIZE_NUMPY, default=default)


API_KEY_COOKIE_NAME = "bluesky_httpserver_api_key"
API_KEY_QUERY_PARAMETER = "api_key"
CSRF_COOKIE_NAME = "bluesky_httpserver_csrf"


@contextlib.contextmanager
def record_timing(metrics, key):
    """
    Set timings[key] equal to the run time (in milliseconds) of the context body.
    """
    t0 = time.perf_counter()
    yield
    metrics[key]["dur"] += time.perf_counter() - t0  # Units: seconds


def get_root_url(request):
    """
    URL at which the app is being server, including API and UI
    """
    return f"{get_root_url_low_level(request.headers, request.scope)}"


def get_base_url(request):
    """
    Base URL for the API
    """
    return f"{get_root_url(request)}/api"


def get_root_url_low_level(request_headers, scope):
    # We want to get the scheme, host, and root_path (if any)
    # *as it appears to the client* for use in assembling links to
    # include in our responses.
    #
    # We need to consider:
    #
    # * FastAPI may be behind a load balancer, such that for a client request
    #   like "https://example.com/..." the Host header is set to something
    #   like "localhost:8000" and the request.url.scheme is "http".
    #   We consult X-Forwarded-* headers to get the original Host and scheme.
    #   Note that, although these are a de facto standard, they may not be
    #   set by default. With nginx, for example, they need to be configured.
    #
    # * The client may be connecting through SSH port-forwarding. (This
    #   is a niche use case but one that we nonetheless care about.)
    #   The Host or X-Forwarded-Host header may include a non-default port.
    #   The HTTP spec specifies that the Host header may include a port
    #   to specify a non-default port.
    #   https://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.23
    host = request_headers.get("x-forwarded-host", request_headers["host"])
    scheme = request_headers.get("x-forwarded-proto", scope["scheme"])
    root_path = scope.get("root_path", "")
    if root_path.endswith("/"):
        root_path = root_path[:-1]
    return f"{scheme}://{host}{root_path}"


def modules_available(*module_names):
    for module_name in module_names:
        if not importlib.util.find_spec(module_name):
            break
    else:
        # All modules were found.
        return True
    return False


def get_current_username(*, principal, settings, api_access_manager):
    """
    Pick 'username' from identities in 'principal', which may contain multiple
    identities. The username is picked if it is the name of one of the 'special'
    users (single user or public) or related to currently active provider.
    This function should never raise exceptions unless there is a bug.

    Returns
    -------
    list(str)
        List of user names from all valid providers.
    """
    pnames = set(settings.authentication_provider_names) | set([_DEFAULT_ANONYMOUS_PROVIDER_NAME])
    ids = {_.id for _ in principal.identities if (_.provider in pnames) and api_access_manager.is_user_known(_.id)}
    ids = list(ids)
    if not ids:
        raise RuntimeError(
            "'username' is required to complete the operation, but needed to complete the operation",
        )
    return ids
