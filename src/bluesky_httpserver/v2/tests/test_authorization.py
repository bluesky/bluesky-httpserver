import pytest

from bluesky_httpserver.v2.identity import (
    AuthenticationFailed,
    InsufficientRole,
    Principal,
    Role,
    authorize,
    bearer_token,
)


@pytest.mark.parametrize("authorization", ["Bearer abc", "bearer abc"])
def test_bearer_token(authorization):
    assert bearer_token(authorization) == "abc"


@pytest.mark.parametrize(
    "authorization, reason",
    [
        (None, "missing bearer token"),
        ("", "missing bearer token"),
        ("Basic abc", "malformed authorization header"),
        ("Bearer", "malformed authorization header"),
        ("Bearer a b", "malformed authorization header"),
    ],
)
def test_bearer_token_refused(authorization, reason):
    with pytest.raises(AuthenticationFailed) as exc_info:
        bearer_token(authorization)
    assert exc_info.value.reason == reason


@pytest.mark.parametrize(
    "held, required, allowed",
    [
        (Role.READ, Role.READ, True),
        (Role.READ, Role.EDIT, False),
        (Role.RUN, Role.EDIT, True),
        (Role.RUN, Role.ADMIN, False),
        (Role.ADMIN, Role.RUN, True),
        (None, Role.READ, False),
    ],
)
def test_authorize(held, required, allowed):
    principal = Principal(id="p", role=held, expires_at=0)
    if allowed:
        authorize(principal, required)
        return
    with pytest.raises(InsufficientRole) as exc_info:
        authorize(principal, required)
    assert exc_info.value.required is required
    assert exc_info.value.held is held
