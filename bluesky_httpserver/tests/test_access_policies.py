import asyncio
import copy
import pprint
import pytest
import requests
import time as ttime
import threading
from xprocess import ProcessStarter

from bluesky_httpserver.authorization import (
    BasicAPIAccessControl,
    DictionaryAPIAccessControl,
    ServerBasedAPIAccessControl,
    DefaultResourceAccessControl,
)
from bluesky_httpserver.config_schemas.loading import ConfigError

from bluesky_httpserver.authorization._defaults import (
    _DEFAULT_USERNAME_SINGLE_USER,
    _DEFAULT_ROLE_SINGLE_USER,
    _DEFAULT_SCOPES_SINGLE_USER,
    _DEFAULT_USERNAME_PUBLIC,
    _DEFAULT_ROLE_PUBLIC,
    _DEFAULT_SCOPES_PUBLIC,
    _DEFAULT_RESOURCE_ACCESS_GROUP,
    _DEFAULT_USER_INFO,
)


# ====================================================================================
#                                API ACCESS POLICIES


# fmt: off
@pytest.mark.parametrize("parameters, success", [
    ({}, True),
    ({"roles": None}, True),
    ({"roles": {}}, True),
    ({"roles": {"user": None}}, True),
    ({"roles": {"user": {}}}, True),
    ({"roles": {"user": {"scopes_set": None}}}, True),
    ({"roles": {"user": {"scopes_set": []}}}, True),
    ({"roles": {"user": {"scopes_set": ["read:status", "write:queue:edit"]}}}, True),
    ({"roles": {"user": {"scopes_set": "read:status"}}}, True),
    ({"roles": {"user": {"scopes_add": None}}}, True),
    ({"roles": {"user": {"scopes_add": []}}}, True),
    ({"roles": {"user": {"scopes_add": ["read:status", "write:queue:edit"]}}}, True),
    ({"roles": {"user": {"scopes_add": "read:status"}}}, True),
    ({"roles": {"user": {"scopes_remove": None}}}, True),
    ({"roles": {"user": {"scopes_remove": []}}}, True),
    ({"roles": {"user": {"scopes_remove": ["read:status", "write:queue:edit"]}}}, True),
    ({"roles": {"user": {"scopes_remove": "read:status"}}}, True),
    ({"roles": {"new_role": {"scopes_set": None}}}, True),
    ({"roles": {"new_role": {"scopes_set": []}}}, True),
    ({"roles": {"new_role": {"scopes_set": ["read:status", "write:queue:edit"]}}}, True),
    ({"roles": {"new_role": {"scopes_set": "read:status"}}}, True),
    ({"roles": {"user": {"scopes_set": None}, "new_role": {"scopes_set": None}}}, True),

    # Failing cases
    ({"roles": 10}, False),
    ({"roles": {"user": 10}}, False),
    ({"roles": {"user": {"scopes_set": 10}}}, False),
    ({"roles": {"user": {"scopes_set": [10, 20]}}}, False),
    ({"roles": {"user": {"non_existing": None}}}, False),
])
@pytest.mark.parametrize("authorization_class", [BasicAPIAccessControl, DictionaryAPIAccessControl])
# fmt: on
def test_BasicAPIAccessControl_01(authorization_class, parameters, success):
    """
    class BasicAPIAccessControl and DictionaryAPIAccessControl: test for validation of class parameters
    """
    if success:
        authorization_class(**parameters)
    else:
        with pytest.raises(ConfigError):
            authorization_class(**parameters)


# fmt: off
@pytest.mark.parametrize("name, role, scopes", [
    (_DEFAULT_USERNAME_SINGLE_USER, _DEFAULT_ROLE_SINGLE_USER, _DEFAULT_SCOPES_SINGLE_USER),
    (_DEFAULT_USERNAME_PUBLIC, _DEFAULT_ROLE_PUBLIC, _DEFAULT_SCOPES_PUBLIC),
])
# fmt: on
def test_BasicAPIAccessControl_02(name, role, scopes):
    """
    class BasicAPIAccessControl: basic test.
    """
    ac_manager = BasicAPIAccessControl()

    assert ac_manager.is_user_known(name) is True
    assert ac_manager.get_user_roles(name) == set([role])
    assert ac_manager.get_displayed_user_name(name) == name
    assert ac_manager.get_user_scopes(name) == set(scopes)

    expected_user_info = {
        "displayed_name": name,
        "roles": set([role]),
        "scopes": set(scopes),
    }
    assert ac_manager.get_user_info(name) == expected_user_info


# fmt: off
@pytest.mark.parametrize("params, existing_scopes, missing_scopes", [
    # Note: scope name in some tests is capitalized on purpose
    ({"roles": {_DEFAULT_ROLE_SINGLE_USER: {"scopes_set": ["read:testing", "write:QUEUE:EDIT"]}}},
     ["read:testing", "write:queue:edit"], ["read:status"]),
    ({"roles": {_DEFAULT_ROLE_SINGLE_USER: {"scopes_add": ["write:teSTing"]}}},
     ["write:testing", "write:queue:edit"], []),
    ({"roles": {_DEFAULT_ROLE_SINGLE_USER: {"scopes_add": "write:teSTing"}}},
     ["write:testing", "write:queue:edit"], []),
    ({"roles": {_DEFAULT_ROLE_SINGLE_USER: {"scopes_remove": ["read:STatus"]}}},
     ["write:queue:edit"], ["read:status"]),
    ({"roles": {_DEFAULT_ROLE_SINGLE_USER: {"scopes_remove": "read:STatus"}}},
     ["write:queue:edit"], ["read:status"]),
])
# fmt: on
def test_BasicAPIAccessControl_03(params, existing_scopes, missing_scopes):
    """
    class BasicAPIAccessControl: modify scopes with parameters.
    """
    ac_manager = BasicAPIAccessControl(**params)
    scopes = ac_manager.get_user_scopes(_DEFAULT_USERNAME_SINGLE_USER)
    for scope in existing_scopes:
        assert scope in scopes
    for scope in missing_scopes:
        assert scope not in scopes


def test_BasicAPIAccessControl_04():
    """
    class BasicAPIAccessControl: authorization for non-existing user.
    """
    ac_manager = BasicAPIAccessControl()

    name = "nonexisting_user"
    assert ac_manager.is_user_known(name) is False
    assert ac_manager.get_user_roles(name) == set()
    assert ac_manager.get_displayed_user_name(name) == name
    assert ac_manager.get_user_scopes(name) == set()

    expected_user_info = {
        "displayed_name": name,
        "roles": set(),
        "scopes": set(),
    }
    assert ac_manager.get_user_info(name) == expected_user_info


# fmt: off
@pytest.mark.parametrize("parameters, success", [
    ({}, True),
    ({"users": None}, True),
    ({"users": None, "roles": None}, True),
    ({"users": {}}, True),
    ({"users": {"user1": None}}, True),
    ({"users": {"user1": {}}}, True),
    ({"users": {"user1": {}, "user2": {}}}, True),
    ({"users": {"user1": {"roles": None}}}, True),
    ({"users": {"user1": {"roles": []}}}, True),
    ({"users": {"user1": {"roles": ["admin", "user"]}}}, True),
    ({"users": {"user1": {"roles": "admin"}}}, True),
    ({"users": {"user1": {"displayed_name": None}}}, True),
    ({"users": {"user1": {"displayed_name": "Doe, John"}}}, True),
    ({"users": {"user1": {"mail": None}}}, True),
    ({"users": {"user1": {"mail": "jdoe25@gmail.com"}}}, True),

    # Failing cases
    ({"users": 10}, False),
    ({"users": {"user1": {"unknown": None}}}, False),
    ({"users": {"user1": {"roles": 10}}}, False),
    ({"users": {"user1": {"displayed_name": 10}}}, False),
    ({"users": {"user1": {"mail": 10}}}, False),
])
# fmt: on
def test_DictionaryAPIAccessControl_01(parameters, success):
    """
    class DictionaryAPIAccessControl: test for validation of class parameters
    """
    if success:
        DictionaryAPIAccessControl(**parameters)
    else:
        with pytest.raises(ConfigError):
            DictionaryAPIAccessControl(**parameters)


# fmt: off
@pytest.mark.parametrize("params, existing_scopes, missing_scopes", [
    # Note: scope name in some tests is capitalized on purpose
    ({"roles": {_DEFAULT_ROLE_SINGLE_USER: {"scopes_set": ["read:testing", "write:QUEUE:EDIT"]}}},
     ["read:testing", "write:queue:edit"], ["read:status"]),
    ({"roles": {_DEFAULT_ROLE_SINGLE_USER: {"scopes_add": ["write:teSTing"]}}},
     ["write:testing", "write:queue:edit"], []),
    ({"roles": {_DEFAULT_ROLE_SINGLE_USER: {"scopes_remove": ["read:STatus"]}}},
     ["write:queue:edit"], ["read:status"]),
])
# fmt: on
def test_DictionaryAPIAccessControl_02(params, existing_scopes, missing_scopes):
    """
    class BasicAPIAccessControl: modify scopes with parameters.
    """
    name, displayed_name, mail, roles = "jdoe", "Doe, John", "jdoe25@gmail.com", ["admin", "observer"]
    users = {"jdoe": {"displayed_name": displayed_name, "mail": mail, "roles": roles}}

    ac_manager = DictionaryAPIAccessControl(users=users)

    assert ac_manager.is_user_known(name) is True
    assert ac_manager.get_user_roles(name) == set(roles)
    assert ac_manager.get_displayed_user_name(name) == f'{name} "{displayed_name} <{mail}>"'
    scopes = ac_manager.get_user_scopes(name)
    assert "read:status" in scopes
    assert "write:permissions" not in scopes


@pytest.fixture
def access_api_server(xprocess):
    server_module = "bluesky_httpserver.tests.access_api_server.api_server"
    server_address = "localhost"
    server_port = 60001

    class Starter(ProcessStarter):
        pattern = "Access API Server started successfully"
        args = f"uvicorn --host={server_address} --port {server_port} {server_module}:app".split()

    xprocess.ensure("access_api_server", Starter)

    yield

    xprocess.getinfo("access_api_server").terminate()


_user_access_info_1 = {
    "bob": {
        "roles": ["admin", "expert"],
        "mail": "bob@mail.com",
        "displayed_name": "Doe, Bob",
    },
    "alice": {"roles": ["advanced"], "mail": "alice@mail.com"},
    "tom": {"roles": ["user"], "displayed_name": "Doe, Tom"},
    "cara": {"roles": ["observer"]},
}


def user_access_info_to_groups(user_access_info):
    groups = {}
    for username, user_info in user_access_info.items():
        for role in user_info["roles"]:
            groups.setdefault(role, {})
            groups[role].setdefault(username, {})
            if "displayed_name" in user_info:
                groups[role][username].update({"displayed_name": user_info["displayed_name"]})
            if "mail" in user_info:
                groups[role][username].update({"mail": user_info["mail"]})
    return groups


# fmt: off
@pytest.mark.parametrize("n_requests", [1, 2, 3])
# fmt: on
def test_ServerBasedAPIAccessControl_01(access_api_server, n_requests):
    """
    ServerBasedAPIAccessControl: basic test
    """
    groups = user_access_info_to_groups(_user_access_info_1)
    requests.post("http://localhost:60001/test/set_info", json=groups)

    ac_manager = ServerBasedAPIAccessControl(
        server="localhost", port=60001, update_period=2, http_timeout=1, instrument="TST", endstation="default"
    )

    # Read user info from the API server (once)
    async def read_info():
        for _ in range(n_requests):
            await ac_manager.request_authentication_info()

    asyncio.run(read_info())

    # Verify loaded user info
    expected_info = copy.deepcopy(_user_access_info_1)
    expected_info.update(_DEFAULT_USER_INFO)
    assert ac_manager._user_info == expected_info

    # Recognizing users
    assert ac_manager.is_user_known("bob")
    assert not ac_manager.is_user_known("unknown_user")

    # Checking roles
    for username, user_info in _user_access_info_1.items():
        assert ac_manager.get_user_roles(username) == set(user_info["roles"])

    # Checking name formatting
    assert ac_manager.get_displayed_user_name("bob") == 'bob "Doe, Bob <bob@mail.com>"'

    # Brief check of scopes
    assert "admin:apikeys" in ac_manager.get_user_scopes("bob")
    assert "admin:apikeys" not in ac_manager.get_user_scopes("alice")


def test_ServerBasedAPIAccessControl_02(access_api_server):
    """
    ServerBasedAPIAccessControl: periodic updates
    """
    groups = user_access_info_to_groups(_user_access_info_1)
    requests.post("http://localhost:60001/test/set_info", json=groups)

    ac_manager = ServerBasedAPIAccessControl(
        server="localhost", port=60001, update_period=2, http_timeout=1, instrument="TST", endstation="default"
    )

    stop_loop = False

    def func():
        # Read user info from the API server (once)
        async def read_info():
            task = asyncio.create_task(ac_manager.background_update_authentication_info())
            while True:
                await asyncio.sleep(0.1)
                if stop_loop:
                    break
            task.cancel()

        asyncio.run(read_info())

    th = threading.Thread(target=func)
    th.start()

    ttime.sleep(1)

    assert ac_manager.is_user_known("tom"), pprint.pformat(ac_manager._user_info)
    assert ac_manager.get_user_roles("bob") == {"admin", "expert"}

    groups2 = copy.deepcopy(groups)
    groups2["user"].pop("tom")
    groups2["admin"].pop("bob")
    requests.post("http://localhost:60001/test/set_info", json=groups2)

    ttime.sleep(3)

    assert not ac_manager.is_user_known("tom"), pprint.pformat(ac_manager._user_info)
    assert ac_manager.get_user_roles("bob") == {"expert"}

    stop_loop = True
    th.join()


# fmt: off
@pytest.mark.parametrize("ac_params, delay", [
    # Wrong port (fails to connect)
    ({"port": 60002, "instrument": "TST", "endstation": "default"}, 0),
    # Wrong instrument
    ({"port": 60001, "instrument": "NEX", "endstation": "default"}, 0),
    # Wrong endstation
    ({"port": 60001, "instrument": "TST", "endstation": "nonexisting"}, 0),
    # Long response delay (request timeout)
    ({"port": 60001, "instrument": "TST", "endstation": "default"}, 10),
])
# fmt: on
def test_ServerBasedAPIAccessControl_03(access_api_server, ac_params, delay):
    """
    ServerBasedAPIAccessControl: expiration of user access data
    """
    groups = user_access_info_to_groups(_user_access_info_1)
    requests.post("http://localhost:60001/test/set_info", json=groups)
    if delay:
        requests.post("http://localhost:60001/test/set_delay", json={"delay": delay})

    ac_manager = ServerBasedAPIAccessControl(
        server="localhost",
        update_period=2,
        expiration_period=3,
        http_timeout=1,
        **ac_params,
    )

    # Set user info (artificially)
    ac_manager._user_info.update(_user_access_info_1)

    stop_loop = False

    def func():
        # Read user info from the API server (once)
        async def read_info():
            task = asyncio.create_task(ac_manager.background_update_authentication_info())
            while True:
                await asyncio.sleep(0.1)
                if stop_loop:
                    break
            task.cancel()

        asyncio.run(read_info())

    th = threading.Thread(target=func)
    th.start()

    for username in _user_access_info_1:
        assert ac_manager.is_user_known(username)

    ttime.sleep(5)

    for username in _user_access_info_1:
        assert not ac_manager.is_user_known(username)

    stop_loop = True
    th.join()


# ====================================================================================
#                            RESOURCE ACCESS POLICIES


# fmt: off
@pytest.mark.parametrize("params, group, success", [
    ({}, _DEFAULT_RESOURCE_ACCESS_GROUP, True),
    ({"default_group": None}, _DEFAULT_RESOURCE_ACCESS_GROUP, True),
    ({"default_group": "custom_group_name"}, "custom_group_name", True),
    ({"default_group": 10}, "", False),
])
# fmt: on
def test_DefaultResourceAccessControl_01(params, group, success):
    """
    DefaultResourceAccessControl: basic tests.
    """
    if success:
        manager = DefaultResourceAccessControl(**params)
        assert manager.get_resource_group("arbitrary_user_name") == group
    else:
        with pytest.raises(ConfigError):
            DefaultResourceAccessControl(**params)
