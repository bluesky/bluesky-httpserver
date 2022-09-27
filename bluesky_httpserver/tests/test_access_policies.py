import pytest

from bluesky_httpserver.authorization import (
    BasicAPIAccessControl,
    DictionaryAPIAccessControl,
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
