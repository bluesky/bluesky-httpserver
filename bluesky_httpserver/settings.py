import collections
import os
import secrets
from datetime import timedelta
from functools import lru_cache
from typing import Any, List, Optional

import pydantic
from packaging import version

if version.parse(pydantic.__version__) < version.parse("2.0.0"):
    from pydantic import BaseSettings
else:
    from pydantic_settings import BaseSettings

DatabaseSettings = collections.namedtuple("DatabaseSettings", "uri pool_size pool_pre_ping")


class Settings(BaseSettings):
    tree: Any = None
    allow_anonymous_access: bool = bool(int(os.getenv("QSERVER_HTTP_SERVER_ALLOW_ANONYMOUS_ACCESS", False)))
    allow_origins: List[str] = [
        item for item in os.getenv("QSERVER_HTTP_SERVER_ALLOW_ORIGINS", "").split() if item
    ]
    authentication_provider_names: List[str] = []  # The list of authentication provider names
    authenticator: Any = None
    # These 'single user' settings are only applicable if authenticator is None.
    single_user_api_key: str = os.getenv("QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY", secrets.token_hex(32))
    single_user_api_key_generated: bool = not ("QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY" in os.environ)
    # The QSERVER_HTTP_SERVER_SERVER_SECRET_KEYS may be a single key or a ;-separated list of
    # keys to support key rotation. The first key will be used for encryption. Each
    # key will be tried in turn for decryption.
    secret_keys: List[str] = os.getenv("QSERVER_HTTP_SERVER_SERVER_SECRET_KEYS", secrets.token_hex(32)).split(";")
    access_token_max_age: timedelta = timedelta(
        seconds=int(os.getenv("QSERVER_HTTP_SERVER_ACCESS_TOKEN_MAX_AGE", 15 * 60))  # 15 minutes
    )
    refresh_token_max_age: timedelta = timedelta(
        seconds=int(os.getenv("QSERVER_HTTP_SERVER_REFRESH_TOKEN_MAX_AGE", 7 * 24 * 60 * 60))  # 7 days
    )
    session_max_age: Optional[timedelta] = timedelta(
        seconds=int(os.getenv("QSERVER_HTTP_SERVER_SESSION_MAX_AGE", 365 * 24 * 60 * 60))  # 365 days
    )
    # Put a fairly low limit on the maximum size of one chunk, keeping in mind
    # that data should generally be chunked. When we implement async responses,
    # we can raise this global limit.
    response_bytesize_limit: int = int(
        os.getenv("QSERVER_HTTP_SERVER_RESPONSE_BYTESIZE_LIMIT", 300_000_000)
    )  # 300 MB
    database_uri: Optional[str] = os.getenv("QSERVER_HTTP_SERVER_DATABASE_URI")
    database_pool_size: Optional[int] = int(os.getenv("QSERVER_HTTP_SERVER_DATABASE_POOL_SIZE", 5))
    database_pool_pre_ping: Optional[bool] = bool(int(os.getenv("QSERVER_HTTP_SERVER_DATABASE_POOL_PRE_PING", 1)))

    @property
    def database_settings(self):
        # The point of this alias is to return a hashable argument for get_sessionmaker.
        return DatabaseSettings(
            uri=self.database_uri,
            pool_size=self.database_pool_size,
            pool_pre_ping=self.database_pool_pre_ping,
        )


@lru_cache()
def get_settings():
    return Settings()


@lru_cache(1)
def get_sessionmaker(database_settings):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker

    connect_args = {}
    kwargs = {}  # extra kwargs passed to create_engine
    kwargs["pool_size"] = database_settings.pool_size
    kwargs["pool_pre_ping"] = database_settings.pool_pre_ping
    if database_settings.uri.startswith("sqlite"):
        from sqlalchemy.pool import QueuePool

        kwargs["poolclass"] = QueuePool
        connect_args.update({"check_same_thread": False})
    engine = create_engine(database_settings.uri, connect_args=connect_args, **kwargs)
    sm = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    if database_settings.uri.startswith("sqlite"):
        # Scope to a session per thread.
        return scoped_session(sm)
    return sm
