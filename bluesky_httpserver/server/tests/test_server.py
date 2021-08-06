import pytest

from bluesky_httpserver.server.server import validate_payload_keys


# fmt: off
@pytest.mark.parametrize("payload, req_keys, opt_keys, success", [
    ({"options": "hello"}, ["options"], None, True,),
    ({"options": "hello"}, None, ["options"], True,),
    ({"options": "hello"}, ["options"], ["options"], True,),
    ({"options": "hello"}, ["options"], ["different"], True,),
    ({"different": "hello"}, ["options"], ["different"], False,),
    ({"different": "hello"}, ["options", "different"], None, False,),
    ({"different": "hello", "options": "no"}, ["options", "different"], None, True,),
    ({"different": "hello", "options": "no", "additional": "value"},
     ["options", "different", "additional"], None, True,),
])
# fmt: on
def test_validate_payload_keys(payload, req_keys, opt_keys, success):

    if success:
        validate_payload_keys(payload, required_keys=req_keys, optional_keys=opt_keys)
    else:
        with pytest.raises(ValueError):
            validate_payload_keys(payload, required_keys=req_keys, optional_keys=opt_keys)
