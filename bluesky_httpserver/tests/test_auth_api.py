import pprint
import time as ttime

from bluesky_queueserver.manager.tests.common import re_manager, re_manager_cmd  # noqa F401

from bluesky_httpserver.authorization._defaults import _DEFAULT_ROLES

from .conftest import fastapi_server_fs  # noqa: F401
from .conftest import request_to_json, setup_server_with_config_file

config_toy_test = """
authentication:
    allow_anonymous_access: True
    providers:
        - provider: toy
          authenticator: bluesky_httpserver.authenticators:DictionaryAuthenticator
          args:
              users_to_passwords:
                  bob: bob_password
                  alice: alice_password
                  cara: cara_password
                  tom: tom_password
api_access:
  policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
  args:
    users:
      bob:
        roles:
          - admin
          - expert
      alice:
        roles: advanced
      tom:
        roles: user
"""


def test_api_auth_post_apikey_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/apikey`` (POST): basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in resp1
    token = resp1["access_token"]

    resp2 = request_to_json("get", "/auth/scopes", token=token)
    assert "scopes" in resp2, pprint.pformat(resp2)
    user_scopes = set(resp2["scopes"])

    # TEST1-1: generate API key using access token: 'inherit' scope
    resp3 = request_to_json(
        "post", "/auth/apikey", json={"expires_in": 900, "note": "API key for testing"}, token=token
    )
    assert "secret" in resp3, pprint.pformat(resp3)
    assert "note" in resp3, pprint.pformat(resp3)
    assert resp3["note"] == "API key for testing"
    assert resp3["scopes"] == ["inherit"]
    api_key1 = resp3["secret"]

    resp3a = request_to_json("get", "/auth/scopes", api_key=api_key1)
    assert "scopes" in resp3a, pprint.pformat(resp3a)
    assert set(resp3a["scopes"]) == user_scopes

    # TEST1-2: generate API key using the existing API key: 'inherit' scope
    resp4 = request_to_json(
        "post", "/auth/apikey", json={"expires_in": 900, "note": "API key - 2"}, api_key=api_key1
    )
    assert "secret" in resp4, pprint.pformat(resp4)
    assert "note" in resp4, pprint.pformat(resp4)
    assert resp4["note"] == "API key - 2"
    assert resp4["scopes"] == ["inherit"]
    api_key2 = resp4["secret"]

    resp4a = request_to_json("get", "/auth/scopes", api_key=api_key2)
    assert "scopes" in resp4a, pprint.pformat(resp4a)
    assert set(resp4a["scopes"]) == user_scopes

    # TEST1-3: generate API key using the existing API key: fixed scope
    scopes3 = ["read:status", "user:apikeys"]
    resp5 = request_to_json("post", "/auth/apikey", json={"expires_in": 900, "scopes": scopes3}, api_key=api_key2)
    assert "secret" in resp5, pprint.pformat(resp5)
    assert "note" in resp5, pprint.pformat(resp5)
    assert resp5["note"] is None
    assert set(resp5["scopes"]) == set(scopes3)
    api_key3 = resp5["secret"]

    resp5a = request_to_json("get", "/auth/scopes", api_key=api_key3)
    assert "scopes" in resp5a, pprint.pformat(resp5a)
    assert set(resp5a["scopes"]) == set(scopes3)

    # TEST2-1: generate API key using token: fixed scope
    scopes4 = ["read:status", "user:apikeys"]
    resp6 = request_to_json("post", "/auth/apikey", json={"expires_in": 900, "scopes": scopes4}, token=token)
    assert "secret" in resp6, pprint.pformat(resp6)
    assert "note" in resp6, pprint.pformat(resp6)
    assert resp6["note"] is None
    assert set(resp6["scopes"]) == set(scopes4)
    api_key4 = resp6["secret"]

    resp6a = request_to_json("get", "/auth/scopes", api_key=api_key4)
    assert "scopes" in resp6a, pprint.pformat(resp6a)
    assert set(resp6a["scopes"]) == set(scopes4)

    # TEST2-2: generate API key using API key: using scopes that are not allowed
    scopes5 = ["read:status", "user:apikeys", "read:queue"]
    resp7 = request_to_json("post", "/auth/apikey", json={"expires_in": 900, "scopes": scopes5}, api_key=api_key4)
    assert "detail" in resp7, pprint.pformat(resp7)
    assert "must be a subset of the allowed principal's scopes" in resp7["detail"]

    # TEST2-3: generate API key using API key: fixed scope
    scopes6 = ["read:status"]
    resp8 = request_to_json("post", "/auth/apikey", json={"expires_in": 900, "scopes": scopes6}, api_key=api_key4)
    assert "secret" in resp8, pprint.pformat(resp8)
    assert "note" in resp8, pprint.pformat(resp8)
    assert resp8["note"] is None
    assert set(resp8["scopes"]) == set(scopes6)
    api_key5 = resp8["secret"]

    resp8a = request_to_json("get", "/auth/scopes", api_key=api_key5)
    assert "scopes" in resp8a, pprint.pformat(resp8a)
    assert set(resp8a["scopes"]) == set(scopes6)


def test_api_auth_get_apikey_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/apikey`` (GET): basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in pprint.pformat(resp1)
    token = resp1["access_token"]

    resp3 = request_to_json(
        "post", "/auth/apikey", json={"expires_in": 900, "note": "API key for testing"}, token=token
    )
    assert "secret" in resp3, pprint.pformat(resp3)
    assert "note" in resp3, pprint.pformat(resp3)
    assert resp3["note"] == "API key for testing"
    assert resp3["scopes"] == ["inherit"]
    api_key = resp3["secret"]

    resp4 = request_to_json("get", "/auth/apikey", api_key=api_key)
    assert "expiration_time" in resp4
    assert "first_eight" in resp4
    assert "latest_activity" in resp4
    assert "note" in resp4
    assert resp4["first_eight"] == api_key[:8]
    assert resp4["note"] == "API key for testing"


def test_api_auth_delete_apikey_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/apikey`` (DELETE): basic tests.

    Test if the case when the API key used for authentication is successfully deleted.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in pprint.pformat(resp1)
    token = resp1["access_token"]

    resp3 = request_to_json(
        "post", "/auth/apikey", json={"expires_in": 900, "note": "API key for testing"}, token=token
    )
    assert "secret" in resp3, pprint.pformat(resp3)
    assert "note" in resp3, pprint.pformat(resp3)
    assert resp3["note"] == "API key for testing"
    assert resp3["scopes"] == ["inherit"]
    api_key = resp3["secret"]

    resp4 = request_to_json("get", "/auth/apikey", api_key=api_key)
    assert resp4["first_eight"] == api_key[:8]
    assert resp4["note"] == "API key for testing"

    resp5 = request_to_json("delete", "/auth/apikey", params={"first_eight": api_key[:8]}, api_key=api_key)
    assert "success" in resp5
    assert resp5["success"] is True

    # The API is already revoked. The request fails.
    resp6 = request_to_json("delete", "/auth/apikey", params={"first_eight": api_key[:8]}, api_key=api_key)
    assert "detail" in resp6, pprint.pformat(resp6)
    assert "Invalid API key" in resp6["detail"]


def test_api_auth_scopes_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/scopes``: basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    user_roles = {"admin", "expert"}
    user_scopes = set()
    for role in user_roles:
        user_scopes = user_scopes | set(_DEFAULT_ROLES[role])

    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in resp1
    token = resp1["access_token"]

    resp2 = request_to_json("get", "/auth/scopes", token=token)
    assert "roles" in resp2, pprint.pformat(resp2)
    assert "scopes" in resp2, pprint.pformat(resp2)
    assert set(resp2["roles"]) == user_roles
    assert set(resp2["scopes"]) == user_scopes


def test_api_auth_session_refresh_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/session/refresh``: basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in resp1
    assert "refresh_token" in resp1
    token1 = resp1["access_token"]
    refresh_token = resp1["refresh_token"]

    # Wait for more than 1 second to generate different token (otherwise the token
    #   is likely going to be the same)
    ttime.sleep(1.5)

    resp2 = request_to_json("post", "/auth/session/refresh", json={"refresh_token": refresh_token})
    assert "access_token" in resp2, pprint.pformat(resp2)
    assert "refresh_token" in resp2, pprint.pformat(resp2)
    assert resp2["access_token"] != token1


def test_api_auth_whoami_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/whoami``: basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in resp1
    assert "refresh_token" in resp1
    token = resp1["access_token"]

    # It is assumed that there are only 1 session running (test is using a fresh instance of the database)
    resp2 = request_to_json("get", "/auth/whoami", token=token)
    assert resp2["identities"][0]["id"] == "bob"
    assert len(resp2["sessions"]) == 1

    resp3 = request_to_json("post", "/auth/apikey", json={"expires_in": 900}, token=token)
    assert "secret" in resp3, pprint.pformat(resp3)
    api_key = resp3["secret"]

    resp4 = request_to_json("get", "/auth/whoami", api_key=api_key)
    assert resp4["identities"][0]["id"] == "bob"
    assert len(resp4["sessions"]) == 1


def test_api_auth_session_revoke_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/session/revoke``: basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in resp1
    assert "refresh_token" in resp1
    token = resp1["access_token"]
    refresh_token = resp1["refresh_token"]

    # Make sure that the token can be refreshed
    resp2 = request_to_json("post", "/auth/session/refresh", json={"refresh_token": refresh_token})
    assert "access_token" in resp2, pprint.pformat(resp2)
    assert "refresh_token" in resp2, pprint.pformat(resp2)

    resp3 = request_to_json("get", "/auth/whoami", token=token)
    assert resp3["identities"][0]["id"] == "bob"
    assert len(resp3["sessions"]) == 1
    session_uuid = resp3["sessions"][0]["uuid"]

    resp4 = request_to_json("delete", f"/auth/session/revoke/{session_uuid}", token=token)
    assert "success" in resp4
    assert resp4["success"] is True

    resp5 = request_to_json("post", "/auth/session/refresh", json={"refresh_token": refresh_token})
    assert "detail" in resp5
    assert "Session has expired. Please re-authenticate" in resp5["detail"]


def test_api_auth_logout_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/logout``: basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    resp1 = request_to_json("post", "/auth/logout", api_key=None)
    assert resp1 == {}


def test_api_admin_auth_principal_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/principal``, ``/auth/principal/<principal-UUID>``: basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    # Login with admin access
    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in resp1
    assert "refresh_token" in resp1
    token = resp1["access_token"]

    # Another user logs in
    resp2 = request_to_json("post", "/auth/provider/toy/token", login=("alice", "alice_password"))
    assert "access_token" in resp2
    token_user = resp2["access_token"]

    # Get a list of all principals
    resp3 = request_to_json("get", "/auth/principal", token=token)
    assert len(resp3) == 2
    principals = {_["identities"][0]["id"]: _["uuid"] for _ in resp3}
    assert set(principals.keys()) == {"bob", "alice"}

    # Request information on a user
    resp4 = request_to_json("get", f"/auth/principal/{principals['alice']}", token=token)
    assert resp4["identities"][0]["id"] == "alice"

    # Attempt to do the same without admin permissions
    resp5 = request_to_json("get", "/auth/principal", token=token_user)
    assert "detail" in resp5
    assert "Not enough permissions" in resp5["detail"]

    resp6 = request_to_json("get", f"/auth/principal/{principals['bob']}", token=token_user)
    assert "detail" in resp6
    assert "Not enough permissions" in resp6["detail"]


def test_api_admin_auth_principal_apikey_01(
    tmpdir,
    monkeypatch,
    re_manager,  # noqa: F811
    fastapi_server_fs,  # noqa: F811
):
    """
    ``/auth/principal/<principal-UUID>/apikey``: basic tests.
    """

    setup_server_with_config_file(config_file_str=config_toy_test, tmpdir=tmpdir, monkeypatch=monkeypatch)
    fastapi_server_fs()

    # Login with admin access
    resp1 = request_to_json("post", "/auth/provider/toy/token", login=("bob", "bob_password"))
    assert "access_token" in resp1
    assert "refresh_token" in resp1
    token = resp1["access_token"]

    # Another user logs in (to create a session and a principal)
    resp2 = request_to_json("post", "/auth/provider/toy/token", login=("alice", "alice_password"))
    assert "access_token" in resp2

    # Get a list of all principals
    resp3 = request_to_json("get", "/auth/principal", token=token)
    assert len(resp3) == 2
    principals = {_["identities"][0]["id"]: _["uuid"] for _ in resp3}
    assert set(principals.keys()) == {"bob", "alice"}

    # Get an API key for the user ('inherit' scope)
    resp4 = request_to_json(
        "post", f"/auth/principal/{principals['alice']}/apikey", json={"expires_in": 900}, token=token
    )
    assert "secret" in resp4
    api_key1 = resp4["secret"]
    resp4a = request_to_json("get", "/auth/whoami", api_key=api_key1)
    assert resp4a["identities"][0]["id"] == "alice"
    assert len(resp4a["sessions"]) == 1
    resp4b = request_to_json("get", "/auth/scopes", api_key=api_key1)
    assert "scopes" in resp4b
    assert set(resp4b["scopes"]) == _DEFAULT_ROLES["advanced"]

    # Get an API key for the user (fixed scope)
    resp5 = request_to_json(
        "post",
        f"/auth/principal/{principals['alice']}/apikey",
        json={"expires_in": 900, "scopes": ["read:status", "read:console"]},
        token=token,
    )
    assert "secret" in resp5
    api_key2 = resp5["secret"]
    resp5b = request_to_json("get", "/auth/scopes", api_key=api_key2)
    assert "scopes" in resp5b
    assert set(resp5b["scopes"]) == {"read:status", "read:console"}

    # Get an API key for the user (fixed scopes outside the scopes of the user)
    resp6 = request_to_json(
        "post",
        f"/auth/principal/{principals['alice']}/apikey",
        json={"expires_in": 900, "scopes": ["admin:apikeys", "read:status", "read:console"]},
        token=token,
    )
    assert "detail" in resp6
    assert "must be a subset of the allowed principal's scopes" in resp6["detail"]
