import copy
from fastapi import HTTPException

from bluesky_queueserver_api.zmq.aio import REManagerAPI


def process_exception():
    try:
        raise
    except REManagerAPI.RequestTimeoutError as ex:
        raise HTTPException(status_code=408, detail=str(ex))
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex))


# Login and authentication are not implemented, but some API methods require
#   login data. So for now we set up fixed user name and group
_login_data = {"user": "Default HTTP User", "user_group": "admin"}


def get_login_data():
    return copy.deepcopy(_login_data)


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
