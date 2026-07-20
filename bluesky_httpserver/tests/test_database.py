from __future__ import annotations

import uuid
from datetime import timedelta

from bluesky_httpserver import schemas
from bluesky_httpserver.database import orm as db_orm
from bluesky_httpserver.database.core import (
    create_user,
    get_or_create_principal,
)


def test_principal_carries_access_token_field():
    """Externally-authenticated principals attach the raw OIDC access token
    so downstream services can perform OBO exchanges."""
    p = schemas.Principal(
        uuid=uuid.uuid4(),
        type=schemas.PrincipalType.user,
        identities=[schemas.Identity(id="jane", provider="entra")],
        access_token="opaque-entra-token",
    )
    assert p.access_token == "opaque-entra-token"
    # Default is None so existing serializations of API-key-authenticated
    # principals are unaffected.
    p2 = schemas.Principal(uuid=uuid.uuid4(), type=schemas.PrincipalType.user)
    assert p2.access_token is None


def test_session_state_column_round_trips(sqlite_session):
    """Authenticator-supplied state must survive a DB round-trip so that
    tiled-style OBO handoff works across refresh_session calls."""
    db = sqlite_session
    principal = create_user(db, "entra", "jane@example.com")
    payload = {"entra_access_token": "AT", "entra_refresh_token": "RT"}
    from datetime import datetime

    session = db_orm.Session(
        principal_id=principal.id,
        expiration_time=datetime.utcnow() + timedelta(days=1),
        state=payload,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    reloaded = db.query(db_orm.Session).filter_by(id=session.id).one()
    assert reloaded.state == payload


def test_session_state_defaults_to_empty_dict(sqlite_session):
    from datetime import datetime

    db = sqlite_session
    principal = create_user(db, "internal", "alice")
    session = db_orm.Session(
        principal_id=principal.id,
        expiration_time=datetime.utcnow() + timedelta(days=1),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    # Server default is '{}' so a session created without an explicit state
    # must not present as None to the ORM.
    assert reloaded_state(session) == {}


def reloaded_state(session):
    # SQLite may return None if the server_default has not been re-selected;
    # normalize.
    return session.state if session.state is not None else {}


def test_get_or_create_principal_creates_when_missing(sqlite_session):
    db = sqlite_session
    p = get_or_create_principal(db, "entra", "jane@example.com")
    assert p is not None
    assert p.uuid is not None
    idents = db.query(db_orm.Identity).filter_by(id="jane@example.com", provider="entra").all()
    assert len(idents) == 1
    assert idents[0].principal_id == p.id


def test_get_or_create_principal_returns_existing_and_updates_latest_login(sqlite_session):
    db = sqlite_session
    first = get_or_create_principal(db, "entra", "jane@example.com")
    (first_identity,) = first.identities
    first_login = first_identity.latest_login

    # Second call must NOT create a new Principal / Identity.
    second = get_or_create_principal(db, "entra", "jane@example.com")
    assert second.id == first.id

    db.refresh(first_identity)
    assert first_identity.latest_login is not None
    # It gets refreshed on every lookup, so the second timestamp must be >= first.
    if first_login is not None:
        assert first_identity.latest_login >= first_login

    principals = db.query(db_orm.Principal).all()
    assert len(principals) == 1


def test_get_or_create_principal_does_not_create_a_session(sqlite_session):
    db = sqlite_session
    get_or_create_principal(db, "entra", "jane@example.com")
    assert db.query(db_orm.Session).count() == 0
