"""
Unit tests for the device code flow authentication error-reporting fix.

These tests cover:
  - _complete_device_code_authorization(): that failure branches write
    pending_session.error to the database before returning HTML responses.
  - build_device_code_token_route(): that the polling endpoint surfaces
    error codes immediately instead of returning "authorization_pending"
    indefinitely after a failed browser-side authentication.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import bluesky_httpserver.authentication as _auth
from bluesky_httpserver.database import orm
from bluesky_httpserver.database.base import Base
from bluesky_httpserver.database.core import create_user
from bluesky_httpserver.schemas import DeviceCode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(*, query_string: bytes = b"", path: str = "/api/auth") -> Request:
    return Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("localhost", 8000),
            "path": path,
            "query_string": query_string,
            "root_path": "",
            "headers": [(b"host", b"localhost:8000")],
        }
    )


def _make_sessionmaker(engine):
    """Return a callable that produces a context-manager-compatible session,
    bound to the supplied SQLAlchemy engine — matches the interface expected
    by get_sessionmaker()."""
    SessionLocal = sessionmaker(bind=engine)

    class _ContextSession:
        def __enter__(self):
            self._db = SessionLocal()
            return self._db

        def __exit__(self, exc_type, exc, tb):
            self._db.close()
            return False

    class _Maker:
        def __call__(self):
            return _ContextSession()

    return _Maker()


def _make_pending_session(db, *, raw_device_code: bytes, user_code: str = "ABCD1234", error=None):
    """Insert a PendingSession row and return (row, raw_device_code)."""
    hashed = hashlib.sha256(raw_device_code).digest()
    ps = orm.PendingSession(
        hashed_device_code=hashed,
        user_code=user_code,
        expiration_time=datetime.utcnow() + timedelta(minutes=10),
        session_id=None,
        error=error,
    )
    db.add(ps)
    db.commit()
    db.refresh(ps)
    return ps


def _make_settings(db_engine=None):
    settings = MagicMock()
    settings.database_settings = MagicMock()
    settings.secret_keys = [secrets.token_hex(32)]
    settings.access_token_max_age = timedelta(minutes=15)
    settings.refresh_token_max_age = timedelta(days=7)
    settings.session_max_age = timedelta(days=7)
    return settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_engine():
    # StaticPool ensures all connections (including those from run_in_executor
    # threads) share the same in-memory SQLite database. check_same_thread=False
    # is required because _create_session_orm runs in a thread pool executor.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    SessionLocal = sessionmaker(bind=db_engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tests: _complete_device_code_authorization()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_auth_invalid_user_code_returns_401(db_session, db_engine, monkeypatch):
    """When the user code is not found, return 401 HTML — no DB row to update."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    request = _make_request(path="/api/auth/provider/entra/device_code", query_string=b"code=deadbeef")
    authenticator = MagicMock()
    api_access_manager = MagicMock()
    settings = _make_settings()

    response = await _auth._complete_device_code_authorization(
        request=request,
        authenticator=authenticator,
        provider="entra",
        code="deadbeef",
        user_code="ZZZZZZZZ",  # no matching row in DB
        settings=settings,
        api_access_manager=api_access_manager,
    )

    assert response.status_code == 401
    assert b"Invalid user code" in response.body
    # authenticator should never have been called
    authenticator.authenticate.assert_not_called()


@pytest.mark.asyncio
async def test_complete_auth_oidc_failure_sets_access_denied(db_session, db_engine, monkeypatch):
    """When authenticator.authenticate() returns falsy, error='access_denied'
    must be written to the PendingSession row and a 401 HTML response returned."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    raw_device_code = secrets.token_bytes(32)
    ps = _make_pending_session(db_session, raw_device_code=raw_device_code, user_code="AAAA1111")

    request = _make_request(path="/api/auth/provider/entra/device_code", query_string=b"code=deadbeef")

    authenticator = MagicMock()
    authenticator.authenticate = AsyncMock(return_value=None)  # OIDC fails
    api_access_manager = MagicMock()
    settings = _make_settings()

    response = await _auth._complete_device_code_authorization(
        request=request,
        authenticator=authenticator,
        provider="entra",
        code="deadbeef",
        user_code="AAAA1111",
        settings=settings,
        api_access_manager=api_access_manager,
    )

    assert response.status_code == 401
    assert b"Authentication Failed" in response.body

    # Verify the DB row was updated
    db_session.refresh(ps)
    assert ps.error == "access_denied"
    assert ps.session_id is None  # no session was created


@pytest.mark.asyncio
async def test_complete_auth_unauthorized_user_sets_unauthorized_user(db_session, db_engine, monkeypatch):
    """When is_user_known() returns False, error='unauthorized_user' must be
    written to the PendingSession row and a 403 HTML response returned."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    raw_device_code = secrets.token_bytes(32)
    ps = _make_pending_session(db_session, raw_device_code=raw_device_code, user_code="BBBB2222")

    request = _make_request(path="/api/auth/provider/entra/device_code", query_string=b"code=deadbeef")

    user_session_state = MagicMock()
    user_session_state.user_name = "jane@example.com"
    user_session_state.state = {}

    authenticator = MagicMock()
    authenticator.authenticate = AsyncMock(return_value=user_session_state)

    api_access_manager = MagicMock()
    api_access_manager.is_user_known.return_value = False  # user not permitted

    settings = _make_settings()

    response = await _auth._complete_device_code_authorization(
        request=request,
        authenticator=authenticator,
        provider="entra",
        code="deadbeef",
        user_code="BBBB2222",
        settings=settings,
        api_access_manager=api_access_manager,
    )

    assert response.status_code == 403
    assert b"jane@example.com" in response.body

    db_session.refresh(ps)
    assert ps.error == "unauthorized_user"
    assert ps.session_id is None


@pytest.mark.asyncio
async def test_complete_auth_success_sets_session_id(db_session, db_engine, monkeypatch):
    """Happy path: session_id is set on success and the HTML response confirms it."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    raw_device_code = secrets.token_bytes(32)
    ps = _make_pending_session(db_session, raw_device_code=raw_device_code, user_code="CCCC3333")

    request = _make_request(path="/api/auth/provider/entra/device_code", query_string=b"code=deadbeef")

    user_session_state = MagicMock()
    user_session_state.user_name = "alice@example.com"
    user_session_state.state = {}

    authenticator = MagicMock()
    authenticator.authenticate = AsyncMock(return_value=user_session_state)

    api_access_manager = MagicMock()
    api_access_manager.is_user_known.return_value = True

    settings = _make_settings()
    settings.session_max_age = timedelta(days=7)

    response = await _auth._complete_device_code_authorization(
        request=request,
        authenticator=authenticator,
        provider="entra",
        code="deadbeef",
        user_code="CCCC3333",
        settings=settings,
        api_access_manager=api_access_manager,
    )

    assert response.status_code == 200
    assert b"Success" in response.body

    db_session.refresh(ps)
    assert ps.session_id is not None
    assert ps.error is None


# ---------------------------------------------------------------------------
# Tests: build_device_code_token_route() — the polling endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_route_invalid_hex_raises_401(db_session, db_engine, monkeypatch):
    """A device_code that is not valid hex must raise HTTP 401 immediately."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    token_fn = _auth.build_device_code_token_route(MagicMock(), "entra")
    request = _make_request()
    settings = _make_settings()

    with pytest.raises(HTTPException) as exc_info:
        await token_fn(
            request=request,
            body=DeviceCode(device_code="not-valid-hex!!"),
            settings=settings,
            api_access_manager=MagicMock(),
        )
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_token_route_unknown_device_code_raises_404(db_session, db_engine, monkeypatch):
    """An unknown (or expired) device_code must raise HTTP 404."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    token_fn = _auth.build_device_code_token_route(MagicMock(), "entra")
    request = _make_request()
    settings = _make_settings()

    with pytest.raises(HTTPException) as exc_info:
        await token_fn(
            request=request,
            body=DeviceCode(device_code=secrets.token_hex(32)),  # valid hex, no matching row
            settings=settings,
            api_access_manager=MagicMock(),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_token_route_pending_no_error_returns_authorization_pending(db_session, db_engine, monkeypatch):
    """While session_id is None and error is None, return authorization_pending."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    raw_device_code = secrets.token_bytes(32)
    _make_pending_session(db_session, raw_device_code=raw_device_code, user_code="DDDD4444")

    token_fn = _auth.build_device_code_token_route(MagicMock(), "entra")
    request = _make_request()
    settings = _make_settings()

    with pytest.raises(HTTPException) as exc_info:
        await token_fn(
            request=request,
            body=DeviceCode(device_code=raw_device_code.hex()),
            settings=settings,
            api_access_manager=MagicMock(),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {"error": "authorization_pending"}


@pytest.mark.asyncio
async def test_token_route_access_denied_surfaces_error_immediately(db_session, db_engine, monkeypatch):
    """When error='access_denied' is set in the DB, the polling endpoint must
    return that error immediately rather than 'authorization_pending'."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    raw_device_code = secrets.token_bytes(32)
    _make_pending_session(db_session, raw_device_code=raw_device_code, user_code="EEEE5555", error="access_denied")

    token_fn = _auth.build_device_code_token_route(MagicMock(), "entra")
    request = _make_request()
    settings = _make_settings()

    with pytest.raises(HTTPException) as exc_info:
        await token_fn(
            request=request,
            body=DeviceCode(device_code=raw_device_code.hex()),
            settings=settings,
            api_access_manager=MagicMock(),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {"error": "access_denied"}


@pytest.mark.asyncio
async def test_token_route_unauthorized_user_surfaces_error_immediately(db_session, db_engine, monkeypatch):
    """When error='unauthorized_user' is set in the DB, the polling endpoint
    must surface that error immediately."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    raw_device_code = secrets.token_bytes(32)
    _make_pending_session(
        db_session, raw_device_code=raw_device_code, user_code="FFFF6666", error="unauthorized_user"
    )

    token_fn = _auth.build_device_code_token_route(MagicMock(), "entra")
    request = _make_request()
    settings = _make_settings()

    with pytest.raises(HTTPException) as exc_info:
        await token_fn(
            request=request,
            body=DeviceCode(device_code=raw_device_code.hex()),
            settings=settings,
            api_access_manager=MagicMock(),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {"error": "unauthorized_user"}


@pytest.mark.asyncio
async def test_token_route_success_returns_tokens(db_session, db_engine, monkeypatch):
    """Happy path: when session_id is linked, the endpoint returns access and
    refresh tokens and deletes the pending session."""
    monkeypatch.setattr(_auth, "get_sessionmaker", lambda _: _make_sessionmaker(db_engine))

    # Create a principal, identity, and session in the test DB
    principal = create_user(db_session, "entra", "alice@example.com")
    session_orm = orm.Session(
        principal_id=principal.id,
        expiration_time=datetime.utcnow() + timedelta(days=7),
        state={},
    )
    db_session.add(session_orm)
    db_session.commit()
    db_session.refresh(session_orm)

    raw_device_code = secrets.token_bytes(32)
    ps = _make_pending_session(db_session, raw_device_code=raw_device_code, user_code="GGGG7777")
    ps.session_id = session_orm.id
    db_session.add(ps)
    db_session.commit()

    api_access_manager = MagicMock()
    api_access_manager.is_user_known.return_value = True
    api_access_manager.get_user_scopes.return_value = {"read:status"}

    token_fn = _auth.build_device_code_token_route(MagicMock(), "entra")
    request = _make_request()
    settings = _make_settings()

    result = await token_fn(
        request=request,
        body=DeviceCode(device_code=raw_device_code.hex()),
        settings=settings,
        api_access_manager=api_access_manager,
    )

    assert "access_token" in result
    assert "refresh_token" in result
    assert result["token_type"] == "bearer"

    # The pending session should have been deleted (one-time use)
    remaining = (
        db_session.query(orm.PendingSession)
        .filter_by(hashed_device_code=hashlib.sha256(raw_device_code).digest())
        .first()
    )
    assert remaining is None
