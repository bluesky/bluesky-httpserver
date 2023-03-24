import asyncio
import enum
import hashlib
import secrets
import uuid as uuid_module
import warnings
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.openapi.models import APIKey, APIKeyIn
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm, SecurityScopes
from fastapi.security.api_key import APIKeyBase, APIKeyCookie, APIKeyQuery
from fastapi.security.utils import get_authorization_scheme_param

# To hide third-party warning
# .../jose/backends/cryptography_backend.py:18: CryptographyDeprecationWarning:
#     int_from_bytes is deprecated, use int.from_bytes instead
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from jose import ExpiredSignatureError, JWTError, jwt

from pydantic import BaseModel, BaseSettings

from . import schemas
from .authorization._defaults import _DEFAULT_ANONYMOUS_PROVIDER_NAME
from .core import json_or_msgpack
from .database import orm
from .database.core import create_user, latest_principal_activity, lookup_valid_api_key, lookup_valid_session
from .settings import get_sessionmaker, get_settings
from .utils import (
    API_KEY_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    SpecialUsers,
    get_api_access_manager,
    get_authenticators,
    get_base_url,
    get_current_username,
)

ALGORITHM = "HS256"
UNIT_SECOND = timedelta(seconds=1)


def utcnow():
    "UTC now with second resolution"
    return datetime.utcnow().replace(microsecond=0)


class Mode(enum.Enum):
    password = "password"
    external = "external"


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None


class APIKeyAuthorizationHeader(APIKeyBase):
    """
    Expect a header like

    Authorization: Apikey SECRET

    where Apikey is case-insensitive.
    """

    def __init__(
        self,
        *,
        name: str,
        scheme_name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        self.model: APIKey = APIKey(**{"in": APIKeyIn.header}, name=name, description=description)
        self.scheme_name = scheme_name or self.__class__.__name__

    async def __call__(self, request: Request) -> Optional[str]:
        authorization: str = request.headers.get("Authorization")
        scheme, param = get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() == "bearer":
            return None
        if scheme.lower() != "apikey":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Authorization header must include the authorization type "
                    "followed by a space and then the secret, as in "
                    "'Bearer SECRET' or 'Apikey SECRET'. "
                ),
            )
        return param


# The tokenUrl below is patched at app startup when we know it.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="PLACEHOLDER", auto_error=False)
api_key_query = APIKeyQuery(name="api_key", auto_error=False)
api_key_header = APIKeyAuthorizationHeader(
    name="Authorization",
    description="Prefix value with 'Apikey ' as in, 'Apikey SECRET'",
)
api_key_cookie = APIKeyCookie(name=API_KEY_COOKIE_NAME, auto_error=False)


def create_access_token(data, secret_key, expires_delta):
    to_encode = data.copy()
    expire = utcnow() + expires_delta
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(to_encode, secret_key, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(session_id, secret_key, expires_delta):
    expire = utcnow() + expires_delta
    to_encode = {
        "type": "refresh",
        "sid": session_id,
        "exp": expire,
    }
    encoded_jwt = jwt.encode(to_encode, secret_key, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token, secret_keys):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    # The first key in settings.secret_keys is used for *encoding*.
    # All keys are tried for *decoding* until one works or they all
    # fail. They supports key rotation.
    for secret_key in secret_keys:
        try:
            payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
            break
        except ExpiredSignatureError:
            # Do not let this be caught below with the other JWTError types.
            raise
        except JWTError:
            # Try the next key in the key rotation.
            continue
    else:
        raise credentials_exception
    return payload


async def get_api_key(
    api_key_query: str = Security(api_key_query),
    api_key_header: str = Security(api_key_header),
    api_key_cookie: str = Security(api_key_cookie),
):
    for api_key in [api_key_query, api_key_header, api_key_cookie]:
        if api_key is not None:
            return api_key
    return None


def get_current_principal(
    request: Request,
    security_scopes: SecurityScopes,
    access_token: str = Depends(oauth2_scheme),
    api_key: str = Depends(get_api_key),
    settings: BaseSettings = Depends(get_settings),
    authenticators=Depends(get_authenticators),
    api_access_manager=Depends(get_api_access_manager),
):
    """
    Get current Principal from:
    - API key in 'api_key' query parameter
    - API key in header 'Authorization: Apikey ...'
    - API key in cookie 'tiled_api_key'
    - OAuth2 JWT access token in header 'Authorization: Bearer ...'

    Fall back to SpecialUsers.public, if anonymous access is allowed
    If this server is configured with a "single-user API key", then
    the Principal will be SpecialUsers.admin always.
    """
    if security_scopes.scopes:
        authenticate_value = f'Bearer scope="{security_scopes.scope_str}"'
    else:
        authenticate_value = "Bearer"
    headers_for_401 = {
        "WWW-Authenticate": authenticate_value,
        "X-Tiled-Root": get_base_url(request),
    }

    # 'api_key_scopes'  is a set of allowed scopes for API key if authorized with API key.
    #   otherwise it is None. The original set of API key scopes is used for generating new
    #   API keys.
    roles, scopes, api_key_scopes = {}, {}, None

    if api_key is not None:
        if authenticators:
            # Tiled is in a multi-user configuration with authentication providers.
            with get_sessionmaker(settings.database_settings)() as db:
                # We store the hashed value of the API key secret.
                # By comparing hashes we protect against timing attacks.
                # By storing only the hash of the (high-entropy) secret
                # we reduce the value of that an attacker can extracted from a
                # stolen database backup.
                try:
                    secret = bytes.fromhex(api_key)
                except Exception:
                    # Not valid hex, therefore not a valid API key
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid API key",
                        headers=headers_for_401,
                    )
                api_key_orm = lookup_valid_api_key(db, secret)
                if api_key_orm is not None:
                    principal = schemas.Principal.from_orm(api_key_orm.principal)
                    ids = get_current_username(
                        principal=principal, settings=settings, api_access_manager=api_access_manager
                    )
                    scope_sets = [api_access_manager.get_user_scopes(_) for _ in ids]
                    principal_scopes = set.union(*scope_sets) if scope_sets else set()

                    roles_sets = [api_access_manager.get_user_roles(_) for _ in ids]
                    roles = set.union(*roles_sets) if roles_sets else set()

                    # principal_scopes = set().union(*[role.scopes for role in principal.roles])

                    # This intersection addresses the case where the Principal has
                    # lost a scope that they had when this key was created.
                    api_key_scopes = set(api_key_orm.scopes)
                    scopes = api_key_scopes.intersection(principal_scopes | {"inherit"})
                    if "inherit" in scopes:
                        # The scope "inherit" is a metascope that confers all the
                        # scopes for the Principal associated with this API,
                        # resolved at access time.
                        scopes.update(principal_scopes)
                        scopes.discard("inherit")
                    api_key_orm.latest_activity = utcnow()
                    db.commit()
                else:
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid API key",
                        headers=headers_for_401,
                    )
        else:
            # HTTP Server is in a "single user" mode with only one API key.
            if secrets.compare_digest(api_key, settings.single_user_api_key):
                username = SpecialUsers.single_user.value
                scopes = api_access_manager.get_user_scopes(username)
                roles = api_access_manager.get_user_roles(username)

                principal = schemas.Principal(
                    uuid=uuid_module.uuid4(),  # Generate unique UUID each time - it is not expected to be used
                    type="user",
                    identities=[schemas.Identity(id=username, provider=_DEFAULT_ANONYMOUS_PROVIDER_NAME)],
                )

            else:
                raise HTTPException(status_code=401, detail="Invalid API key", headers=headers_for_401)
        # If we made it to this point, we have a valid API key.
        # If the API key was given in query param, move to cookie.
        # This is convenient for browser-based access.
        if ("api_key" in request.query_params) and (request.cookies.get(API_KEY_COOKIE_NAME) != api_key):
            request.state.cookies_to_set.append({"key": API_KEY_COOKIE_NAME, "value": api_key})
    elif access_token is not None:
        try:
            payload = decode_token(access_token, settings.secret_keys)
        except ExpiredSignatureError:
            raise HTTPException(
                status_code=401,
                detail="Access token has expired. Refresh token.",
                headers=headers_for_401,
            )
        principal = schemas.Principal(
            uuid=uuid_module.UUID(hex=payload["sub"]),
            type=payload["sub_typ"],
            identities=[
                schemas.Identity(id=identity["id"], provider=identity["idp"]) for identity in payload["ids"]
            ],
        )

        # scopes = payload["scp"]

        # Combine scopes for all identities (it is expected to be only one identity).
        ids = [_["id"] for _ in payload["ids"] if _["idp"] in settings.authentication_provider_names]
        scopes = set.union(*[api_access_manager.get_user_scopes(_) for _ in ids])

        roles_sets = [api_access_manager.get_user_roles(_) for _ in ids]
        roles = set.union(*roles_sets) if roles_sets else set()

    else:
        # No form of authentication is present.
        username = SpecialUsers.public.value
        # This is a 'dummy' principal used to pass data within the server. Not saved to the databased.
        principal = schemas.Principal(
            uuid=uuid_module.uuid4(),  # Generate unique UUID each time - it is not expected to be used
            type="user",
            identities=[schemas.Identity(id=username, provider=_DEFAULT_ANONYMOUS_PROVIDER_NAME)],
        )

        # Is anonymous public access permitted?
        if settings.allow_anonymous_access:
            # Any user who can see the server can make unauthenticated requests.
            # This is a sentinel that has special meaning to the authorization
            # code (the access control policies).
            scopes = api_access_manager.get_user_scopes(username)
            roles = api_access_manager.get_user_roles(username)

        else:
            # In this mode, there may still be entries that are visible to all,
            # but users have to authenticate as *someone* to see anything.
            # They can still access the /  and /docs routes.
            scopes = {}
            roles = {}

    # Scope enforcement happens here.
    # https://fastapi.tiangolo.com/advanced/security/oauth2-scopes/
    if not set(security_scopes.scopes).issubset(scopes):
        # Include a link to the root page which provides a list of
        # authenticators. The use case here is:
        # 1. User is emailed a link like https://example.com/subpath/node/metadata/a/b/c
        # 2. Tiled Client tries to connect to that and gets 401.
        # 3. Client can use this header to find its way to
        #    https://examples.com/subpath/ and obtain a list of
        #    authentication providers and endpoints.
        raise HTTPException(
            status_code=401,
            detail=(
                "Not enough permissions. "
                f"Requires scopes {security_scopes.scopes}. "
                f"Request had scopes {list(scopes)}"
            ),
            headers=headers_for_401,
        )

    roles_list, scopes_list = list(roles), list(scopes)
    roles_list.sort()
    scopes_list.sort()
    if api_key_scopes is not None:
        api_key_scopes_list = list(api_key_scopes)
        api_key_scopes_list.sort()
    else:
        api_key_scopes_list = api_key_scopes
    principal.roles, principal.scopes, principal.api_key_scopes = roles_list, scopes_list, api_key_scopes_list
    return principal


def create_session(settings, identity_provider, id, scopes):
    with get_sessionmaker(settings.database_settings)() as db:
        # Have we seen this Identity before?
        identity = (
            db.query(orm.Identity)
            .filter(orm.Identity.id == id)
            .filter(orm.Identity.provider == identity_provider)
            .first()
        )
        now = utcnow()
        if identity is None:
            # We have not. Make a new Principal and link this new Identity to it.
            # TODO Confirm that the user intends to create a new Principal here.
            # Give them the opportunity to link an existing Principal instead.
            principal = create_user(db, identity_provider, id)
            (new_identity,) = principal.identities
            new_identity.latest_login = now
        else:
            identity.latest_login = now
            principal = identity.principal

        session = orm.Session(
            principal_id=principal.id,
            expiration_time=utcnow() + settings.session_max_age,
        )
        db.add(session)
        db.commit()
        db.refresh(session)  # Refresh to sync back the auto-generated session.uuid.
        # Provide enough information in the access token to reconstruct Principal
        # and its Identities sufficient for access policy enforcement without a
        # database hit.
        data = {
            "sub": principal.uuid.hex,
            "sub_typ": principal.type.value,
            "scp": list(scopes),
            "ids": [{"id": identity.id, "idp": identity.provider} for identity in principal.identities],
        }
        access_token = create_access_token(
            data=data,
            expires_delta=settings.access_token_max_age,
            secret_key=settings.secret_keys[0],  # Use the *first* secret key to encode.
        )
        refresh_token = create_refresh_token(
            session_id=session.uuid.hex,
            expires_delta=settings.refresh_token_max_age,
            secret_key=settings.secret_keys[0],  # Use the *first* secret key to encode.
        )
        return {
            "access_token": access_token,
            "expires_in": settings.access_token_max_age / UNIT_SECOND,
            "refresh_token": refresh_token,
            "refresh_token_expires_in": settings.refresh_token_max_age / UNIT_SECOND,
            "token_type": "bearer",
        }


def build_auth_code_route(authenticator, provider):
    "Build an auth_code route function for this Authenticator."

    async def auth_code(
        request: Request,
        settings: BaseSettings = Depends(get_settings),
        api_access_manager=Depends(get_api_access_manager),
    ):
        request.state.endpoint = "auth"
        username = await authenticator.authenticate(request)

        if username and api_access_manager.is_user_known(username):
            scopes = api_access_manager.get_user_scopes(username)
        else:
            raise HTTPException(status_code=401, detail="Authentication failure")

        tokens = await asyncio.get_running_loop().run_in_executor(
            None, create_session, settings, provider, username, scopes
        )
        # Show only the refresh_token, which is what the user should
        # paste into a terminal-based client.
        # In the future for web apps we may want this to be optional,
        # controlled by a query parameter (if it's possible to inject that).
        return tokens["refresh_token"]

    return auth_code


def build_handle_credentials_route(authenticator, provider):
    "Register a handle_credentials route function for this Authenticator."

    async def handle_credentials(
        request: Request,
        form_data: OAuth2PasswordRequestForm = Depends(),
        settings: BaseSettings = Depends(get_settings),
        api_access_manager=Depends(get_api_access_manager),
    ):
        request.state.endpoint = "auth"
        username = await authenticator.authenticate(username=form_data.username, password=form_data.password)

        err_msg = None
        if not username:
            err_msg = "Incorrect username or password"
        elif not api_access_manager.is_user_known(username):
            err_msg = "User is not authorized to access the server"
        else:
            scopes = api_access_manager.get_user_scopes(username)

        if err_msg:
            raise HTTPException(
                status_code=401,
                detail=err_msg,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await asyncio.get_running_loop().run_in_executor(
            None, create_session, settings, provider, username, scopes
        )

    return handle_credentials


def generate_apikey(db, principal, apikey_params, request, allowed_scopes, source_api_key_scopes):
    # Use API key scopes if API key is generated based on existing API key, otherwise used allowed scopes
    if (source_api_key_scopes is not None) and ("inherit" not in source_api_key_scopes):
        scopes_allowed_set = source_api_key_scopes
    else:
        scopes_allowed_set = allowed_scopes

    if "inherit" in scopes_allowed_set:
        scopes_allowed_set = allowed_scopes

    if (apikey_params.scopes is None) or ("inherit" in apikey_params.scopes):
        if (source_api_key_scopes is None) or ("inherit" in source_api_key_scopes):
            scopes = ["inherit"]
        else:
            scopes = source_api_key_scopes
    else:
        scopes = apikey_params.scopes

    # principal_scopes = set().union(*[role.scopes for role in principal.roles])
    if not set(scopes).issubset(scopes_allowed_set | {"inherit"}):
        scopes_list = list(scopes)
        scopes_list.sort()
        scopes_allowed_list = list(scopes_allowed_set)
        scopes_allowed_list.sort()
        raise HTTPException(
            400,
            (
                f"Requested scopes {scopes_list} must be a subset of the "
                f"allowed principal's scopes {scopes_allowed_list}."
            ),
        )
    if apikey_params.expires_in is not None:
        expiration_time = utcnow() + timedelta(seconds=apikey_params.expires_in)
    else:
        expiration_time = None
    # The standard 32 byes of entropy,
    # plus 4 more for extra safety since we store the first eight HEX chars.
    secret = secrets.token_bytes(4 + 32)
    hashed_secret = hashlib.sha256(secret).digest()
    new_key = orm.APIKey(
        principal_id=principal.id,
        expiration_time=expiration_time,
        note=apikey_params.note,
        scopes=list(scopes),
        first_eight=secret.hex()[:8],
        hashed_secret=hashed_secret,
    )
    db.add(new_key)
    db.commit()
    db.refresh(new_key)
    return json_or_msgpack(
        request,
        schemas.APIKeyWithSecret.from_orm(new_key, secret=secret.hex()).dict(),
    )


base_authentication_router = APIRouter()


@base_authentication_router.get(
    "/principal",
    response_model=schemas.Principal,
)
def principal_list(
    request: Request,
    settings: BaseSettings = Depends(get_settings),
    principal=Security(get_current_principal, scopes=["admin:read:principals"]),
):
    "List Principals (users and services)."
    # TODO Pagination
    request.state.endpoint = "auth"
    with get_sessionmaker(settings.database_settings)() as db:
        principal_orms = db.query(orm.Principal).all()

        principals = [
            schemas.Principal.from_orm(principal_orm, latest_principal_activity(db, principal_orm)).dict()
            for principal_orm in principal_orms
        ]

        return json_or_msgpack(request, principals)


@base_authentication_router.get(
    "/principal/{uuid}",
    response_model=schemas.Principal,
)
def principal(
    request: Request,
    uuid: uuid_module.UUID,
    settings: BaseSettings = Depends(get_settings),
    principal=Security(get_current_principal, scopes=["admin:read:principals"]),
):
    "Get information about one Principal (user or service)."
    request.state.endpoint = "auth"
    with get_sessionmaker(settings.database_settings)() as db:
        principal_orm = db.query(orm.Principal).filter(orm.Principal.uuid == uuid).first()
        return json_or_msgpack(
            request,
            schemas.Principal.from_orm(principal_orm, latest_principal_activity(db, principal_orm)).dict(),
        )


@base_authentication_router.post(
    "/principal/{uuid}/apikey",
    response_model=schemas.APIKeyWithSecret,
)
def apikey_for_principal(
    request: Request,
    uuid: uuid_module.UUID,
    apikey_params: schemas.APIKeyRequestParams,
    principal=Security(get_current_principal, scopes=["admin:apikeys"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
):
    "Generate an API key for a Principal."
    request.state.endpoint = "auth"
    with get_sessionmaker(settings.database_settings)() as db:
        principal = db.query(orm.Principal).filter(orm.Principal.uuid == uuid).first()
        if principal is None:
            raise HTTPException(404, f"Principal {uuid} does not exist or insufficient permissions.")

        ids = {_.id for _ in principal.identities}
        scope_sets = [api_access_manager.get_user_scopes(_) for _ in ids]
        principal_scopes = set.union(*scope_sets) if scope_sets else set()
        source_api_key_scopes = None

        return generate_apikey(db, principal, apikey_params, request, principal_scopes, source_api_key_scopes)


@base_authentication_router.post("/session/refresh", response_model=schemas.AccessAndRefreshTokens)
def refresh_session(
    request: Request,
    refresh_token: schemas.RefreshToken,
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
):
    "Obtain a new access token and refresh token."
    request.state.endpoint = "auth"
    with get_sessionmaker(settings.database_settings)() as db:
        new_tokens = slide_session(refresh_token.refresh_token, settings, db, api_access_manager)
        return new_tokens


@base_authentication_router.delete("/session/revoke/{session_id}")
def revoke_session(
    session_id: str,  # from path parameter
    request: Request,
    principal: schemas.Principal = Security(get_current_principal, scopes=[]),
    settings: BaseSettings = Depends(get_settings),
):
    "Mark a Session as revoked so it cannot be refreshed again."
    request.state.endpoint = "auth"
    with get_sessionmaker(settings.database_settings)() as db:
        # Find this session in the database.
        session = lookup_valid_session(db, session_id)
        if session is None:
            raise HTTPException(404, detail=f"No session {session_id}")
        if principal.uuid != session.principal.uuid:
            # TODO Add a scope for doing this for other users.
            raise HTTPException(
                404,
                detail="Sessions does not exist or requester has insufficient permissions",
            )
        session.revoked = True
        db.commit()
        # return Response(status_code=204)
        return JSONResponse(status_code=200, content={"success": True, "msg": ""})


def slide_session(refresh_token, settings, db, api_access_manager):
    try:
        payload = decode_token(refresh_token, settings.secret_keys)
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session has expired. Please re-authenticate.")
    # Find this session in the database.
    session = lookup_valid_session(db, payload["sid"])
    now = utcnow()
    # This token is *signed* so we know that the information came from us.
    # If the Session is forgotten or revoked or expired, do not allow refresh.
    if (session is None) or session.revoked or (session.expiration_time < now):
        # Do not leak (to a potential attacker) whether this has been *revoked*
        # specifically. Give the same error as if it had expired.
        raise HTTPException(status_code=401, detail="Session has expired. Please re-authenticate.")
    # Update Session info.
    session.time_last_refreshed = now
    # This increments in a way that avoids a race condition.
    session.refresh_count = orm.Session.refresh_count + 1
    # Provide enough information in the access token to reconstruct Principal
    # and its Identities sufficient for access policy enforcement without a
    # database hit.
    principal = schemas.Principal.from_orm(session.principal)

    ids = get_current_username(principal=principal, settings=settings, api_access_manager=api_access_manager)
    if not ids:
        raise HTTPException(
            status_code=401,
            detail="Permissions for the user are revoked. Please contact the administrator.",
        )
    scopes = set.union(*[api_access_manager.get_user_scopes(_) for _ in ids])

    data = {
        "sub": principal.uuid.hex,
        "sub_typ": principal.type.value,
        "scp": list(scopes),
        "ids": [{"id": identity.id, "idp": identity.provider} for identity in principal.identities],
    }
    access_token = create_access_token(
        data=data,
        expires_delta=settings.access_token_max_age,
        secret_key=settings.secret_keys[0],  # Use the *first* secret key to encode.
    )
    new_refresh_token = create_refresh_token(
        session_id=payload["sid"],
        expires_delta=settings.refresh_token_max_age,
        secret_key=settings.secret_keys[0],  # Use the *first* secret key to encode.
    )
    return {
        "access_token": access_token,
        "expires_in": settings.access_token_max_age / UNIT_SECOND,
        "refresh_token": new_refresh_token,
        "refresh_token_expires_in": settings.refresh_token_max_age / UNIT_SECOND,
        "token_type": "bearer",
    }


@base_authentication_router.post(
    "/apikey",
    response_model=schemas.APIKeyWithSecret,
)
def new_apikey(
    request: Request,
    apikey_params: schemas.APIKeyRequestParams,
    principal=Security(get_current_principal, scopes=["user:apikeys"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
):
    """
    Generate an API for the currently-authenticated user or service."""
    # TODO Permit filtering the fields of the response.
    request.state.endpoint = "auth"
    if principal is None:
        return None

    # ids = get_current_username(principal=principal, settings=settings, api_access_manager=api_access_manager)
    # scope_sets = [api_access_manager.get_user_scopes(_) for _ in ids]
    # principal_scopes = set.union(*scope_sets) if scope_sets else set()

    allowed_scopes = set(principal.scopes)
    source_api_key_scopes = set(principal.api_key_scopes) if (principal.api_key_scopes is not None) else None

    with get_sessionmaker(settings.database_settings)() as db:
        # The principal from get_current_principal tells us everything that the
        # access_token carries around, but the database knows more than that.
        principal_orm = db.query(orm.Principal).filter(orm.Principal.uuid == principal.uuid).first()
        apikey = generate_apikey(db, principal_orm, apikey_params, request, allowed_scopes, source_api_key_scopes)
        return apikey


@base_authentication_router.get("/apikey", response_model=schemas.APIKey)
def current_apikey_info(
    request: Request,
    api_key: str = Depends(get_api_key),
    settings: BaseSettings = Depends(get_settings),
):
    """
    Give info about the API key used to authentication the current request.

    This provides a way to look up the API uuid, given the API secret.
    """
    # TODO Permit filtering the fields of the response.
    request.state.endpoint = "auth"
    if api_key is None:
        raise HTTPException(status_code=401, detail="No API key was provided with this request.")
    try:
        secret = bytes.fromhex(api_key)
    except Exception:
        # Not valid hex, therefore not a valid API key
        raise HTTPException(status_code=401, detail="Invalid API key")
    with get_sessionmaker(settings.database_settings)() as db:
        api_key_orm = lookup_valid_api_key(db, secret)
        if api_key_orm is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return json_or_msgpack(request, schemas.APIKey.from_orm(api_key_orm).dict())


@base_authentication_router.delete("/apikey")
def revoke_apikey(
    request: Request,
    first_eight: str,
    principal=Security(get_current_principal, scopes=["user:apikeys"]),
    settings: BaseSettings = Depends(get_settings),
):
    """
    Revoke an API belonging to the currently-authenticated user or service."""
    # TODO Permit filtering the fields of the response.
    request.state.endpoint = "auth"
    if principal is None:
        return None
    with get_sessionmaker(settings.database_settings)() as db:
        api_key_orm = db.query(orm.APIKey).filter(orm.APIKey.first_eight == first_eight[:8]).first()
        if (api_key_orm is None) or (api_key_orm.principal.uuid != principal.uuid):
            raise HTTPException(
                404,
                f"The currently-authenticated {principal.type} has no such API key.",
            )
        db.delete(api_key_orm)
        db.commit()
        # return Response(status_code=204)
        return JSONResponse(status_code=200, content={"success": True, "msg": ""})


@base_authentication_router.get(
    "/whoami",
    response_model=schemas.Principal,
)
def whoami(
    request: Request,
    principal=Security(get_current_principal, scopes=[]),
    settings: BaseSettings = Depends(get_settings),
):
    # TODO Permit filtering the fields of the response.
    request.state.endpoint = "auth"
    if principal is SpecialUsers.public:
        return json_or_msgpack(request, None)
    # The principal from get_current_principal tells us everything that the
    # access_token carries around, but the database knows more than that.
    with get_sessionmaker(settings.database_settings)() as db:
        principal_orm = db.query(orm.Principal).filter(orm.Principal.uuid == principal.uuid).first()
        return json_or_msgpack(
            request,
            schemas.Principal.from_orm(principal_orm, latest_principal_activity(db, principal_orm)).dict(),
        )


@base_authentication_router.get(
    "/scopes",
    response_model=schemas.Principal,
)
def scopes(
    request: Request,
    principal=Security(get_current_principal, scopes=[]),
):
    roles, scopes = principal.roles, principal.scopes
    return json_or_msgpack(request, schemas.AllowedScopes(roles=roles, scopes=scopes).dict())


@base_authentication_router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    principal=Security(get_current_principal, scopes=[]),
):
    request.state.endpoint = "auth"
    response.delete_cookie(API_KEY_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE_NAME)
    return {}
