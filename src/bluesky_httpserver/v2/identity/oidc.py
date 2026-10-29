"""Verification of JWT access tokens from one configured OIDC identity provider."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

from .principal import AuthenticationFailed, IdentityProviderUnavailable, Principal, Role

logger = logging.getLogger(__name__)

MAX_TOKEN_LENGTH = 16_384
MAX_PRINCIPAL_LENGTH = 255  # OIDC Core caps sub at 255 ASCII characters
KEY_REFRESH_SECONDS = 86_400  # check signing keys daily
MIN_KEY_FETCH_INTERVAL_SECONDS = 60  # bounds refetches triggered by unknown kids
KEY_FETCH_TIMEOUT_SECONDS = 10.0
MAX_LEEWAY_SECONDS = 300
_ALGORITHM = "RS256"
_ROLES_BY_CLAIM_VALUE = {role.claim_value: role for role in Role}


@dataclass(frozen=True)
class OidcSettings:
    """Where access tokens come from and how to read them.

    Attributes
    ----------
    issuer : str
        Exact ``iss`` of accepted tokens.
    audience : str
        Value that must appear in ``aud``; identifies this API, not a client app.
    jwks_uri : str
        HTTPS URL of the issuer's signing keys (JWK Set).
    principal_claim : str
        Claim whose string value identifies the caller.
    roles_claim : str
        Claim holding the caller's rights, as a space-delimited string or a list of strings.
    leeway_seconds : int
        Clock skew tolerated on ``exp``, ``nbf`` and ``iat``, in seconds.
    """

    issuer: str
    audience: str
    jwks_uri: str
    principal_claim: str = "sub"
    roles_claim: str = "scope"
    leeway_seconds: int = 60

    def __post_init__(self) -> None:
        _require_text("issuer", self.issuer)
        _require_text("audience", self.audience)
        _require_text("principal_claim", self.principal_claim)
        _require_text("roles_claim", self.roles_claim)
        url = urlsplit(self.jwks_uri)
        if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None:
            raise ValueError(f"jwks_uri must be an https URL without credentials, not {self.jwks_uri!r}")
        if not 0 <= self.leeway_seconds <= MAX_LEEWAY_SECONDS:
            raise ValueError(
                f"leeway_seconds must be between 0 and {MAX_LEEWAY_SECONDS}, not {self.leeway_seconds}"
            )

    @classmethod
    def entra(cls, *, tenant_id: str, client_id: str, leeway_seconds: int = 60) -> OidcSettings:
        """Settings for v2.0 access tokens of a single-tenant Microsoft Entra ID API registration.

        The caller is identified by ``oid``, the object ID of the user or service principal. Entra's ``sub``
        is pairwise per client app: the same person would be a different principal in each client app, and
        audit ids could not be looked up in Entra or Microsoft Graph. Rights come from ``roles`` because app
        roles are the only rights claim present in both user tokens and app-only (client-credentials) tokens.
        The API registration must set ``requestedAccessTokenVersion: 2``; v1.0 tokens have issuer
        ``https://sts.windows.net/{tenant}/`` and are rejected. The API registration must not also sign users
        in: Entra puts app roles in the ID tokens of an app that signs users in, and such an ID token has the
        API's client ID as ``aud``, so it would pass as an access token. Users sign in through a separate
        client registration.

        Parameters
        ----------
        tenant_id : str
            Directory (tenant) ID, a GUID.
        client_id : str
            Application (client) ID of the API registration, a GUID; the ``aud`` of its v2.0 access tokens.
        leeway_seconds : int
            Clock skew tolerated on ``exp``, ``nbf`` and ``iat``, in seconds.
        """
        tenant = _canonical_guid("tenant_id", tenant_id)
        client = _canonical_guid("client_id", client_id)
        return cls(
            issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
            audience=client,
            jwks_uri=f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
            principal_claim="oid",
            roles_claim="roles",
            leeway_seconds=leeway_seconds,
        )


class OidcAuthenticator:
    """Verifies access tokens against the signing keys of one identity provider.

    Accepts RS256 only. Keys come only from ``settings.jwks_uri``, never from the token or a redirect, and the
    token must name its key with ``kid``. Keys are cached and refreshed daily; an unknown ``kid`` triggers a
    refetch at most once per ``MIN_KEY_FETCH_INTERVAL_SECONDS``. ``exp``, ``nbf`` and ``iat`` are checked
    against the system clock.

    Parameters
    ----------
    settings : OidcSettings
        Which tokens to accept and how to read the caller's identity and rights.
    http : httpx.AsyncClient
        Client used to fetch signing keys; owned by the caller. Redirects are never followed for the fetch,
        whatever the client's own setting.
    monotonic : callable
        Monotonic seconds, for the age of cached keys and the fetch rate limit.
    """

    def __init__(
        self,
        settings: OidcSettings,
        http: httpx.AsyncClient,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http
        self._monotonic = monotonic
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._last_fetch_failed = False
        self._lock = asyncio.Lock()
        self._jwt = jwt.PyJWT({"require": ["exp", "iss", "aud"]})

    async def authenticate(self, token: str) -> Principal:
        """Verify ``token`` and return the caller it identifies.

        Raises
        ------
        AuthenticationFailed
            The token is not a valid access token for this API.
        IdentityProviderUnavailable
            The token could not be checked because signing keys could not be fetched.
        """
        if len(token) > MAX_TOKEN_LENGTH:
            raise AuthenticationFailed("token too large")
        try:
            # Unverified: the header only selects the key; the signature is checked over the raw segments.
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise AuthenticationFailed("malformed token")
        if header.get("alg") != _ALGORITHM:
            raise AuthenticationFailed("unsupported token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationFailed("malformed token")
        key = await self._signing_key(kid)
        try:
            claims = self._jwt.decode(
                token,
                key,
                algorithms=[_ALGORITHM],
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                leeway=self._settings.leeway_seconds,
            )
        except jwt.InvalidSignatureError:
            raise AuthenticationFailed("invalid token signature")
        except jwt.DecodeError:  # payload or a time claim that cannot be decoded
            raise AuthenticationFailed("malformed token")
        except jwt.InvalidTokenError:
            raise AuthenticationFailed("invalid token claims")
        principal_id = claims.get(self._settings.principal_claim)
        if (
            not isinstance(principal_id, str)
            or not principal_id.strip()
            or len(principal_id) > MAX_PRINCIPAL_LENGTH
        ):
            raise AuthenticationFailed("missing or malformed principal claim")
        role = _highest_role(claims.get(self._settings.roles_claim))
        return Principal(id=principal_id, role=role, expires_at=int(claims["exp"]))

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        key = self._keys.get(kid)
        if key is not None and not self._stale():
            return key
        async with self._lock:
            key = self._keys.get(kid)
            if (key is None or self._stale()) and self._may_fetch():
                await self._refresh()
            key = self._keys.get(kid)
        if key is not None:
            return key
        if self._last_fetch_failed:
            raise IdentityProviderUnavailable()
        raise AuthenticationFailed("unknown signing key")

    def _stale(self) -> bool:
        return self._fetched_at is None or self._monotonic() - self._fetched_at >= KEY_REFRESH_SECONDS

    def _may_fetch(self) -> bool:
        return (
            self._attempted_at is None or self._monotonic() - self._attempted_at >= MIN_KEY_FETCH_INTERVAL_SECONDS
        )

    async def _refresh(self) -> None:
        # Record the attempt only once the fetch has finished: a cancelled fetch must not count, or the next
        # caller would be rate-limited with no keys and wrongly told the key is unknown.
        started = self._monotonic()
        try:
            keys = await self._fetch_keys()
        except _KeyFetchError as error:
            self._attempted_at = started
            self._last_fetch_failed = True
            logger.warning("could not fetch signing keys from %s: %s", self._settings.jwks_uri, error)
            return
        self._keys = keys
        self._fetched_at = self._attempted_at = started
        self._last_fetch_failed = False

    async def _fetch_keys(self) -> dict[str, jwt.PyJWK]:
        try:
            response = await self._http.get(
                self._settings.jwks_uri, timeout=KEY_FETCH_TIMEOUT_SECONDS, follow_redirects=False
            )
        except httpx.HTTPError as error:
            # str() of an httpx timeout is empty, so the error type is what tells the log what happened.
            detail = str(error)
            raise _KeyFetchError(f"{type(error).__name__}: {detail}" if detail else type(error).__name__)
        if response.status_code != 200:
            raise _KeyFetchError(f"HTTP {response.status_code}")
        try:
            document = response.json()
        except ValueError:
            raise _KeyFetchError("invalid JSON")
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise _KeyFetchError("no keys array")
        keys: dict[str, jwt.PyJWK] = {}
        for entry in document["keys"]:
            if not (
                isinstance(entry, dict)
                and entry.get("kty") == "RSA"
                and entry.get("use", "sig") == "sig"
                and entry.get("alg", _ALGORITHM) == _ALGORITHM
            ):
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                logger.warning("skipping signing key without kid")
                continue
            if kid in keys:
                logger.warning("skipping duplicate signing key %s", kid)
                continue
            try:
                # Only the public members: IdP extras such as x5c or issuer never reach PyJWT.
                public_jwk = {"kty": "RSA", "n": entry["n"], "e": entry["e"], "kid": kid}
                keys[kid] = jwt.PyJWK(public_jwk, algorithm=_ALGORITHM)
            except (jwt.PyJWTError, KeyError):
                logger.warning("skipping unusable signing key %s", kid)
        if not keys:
            raise _KeyFetchError("no usable RSA signing keys")
        return keys


class _KeyFetchError(Exception):
    """The signing keys could not be fetched or contained no usable key."""


def _highest_role(raw: Any) -> Role | None:
    """Highest role granted by a rights claim; unrecognized values grant nothing."""
    if raw is None:
        values: list[str] = []
    elif isinstance(raw, str):
        values = raw.split()
    elif isinstance(raw, list) and all(isinstance(value, str) for value in raw):
        values = raw
    else:
        raise AuthenticationFailed("malformed roles claim")
    return max((_ROLES_BY_CLAIM_VALUE[value] for value in values if value in _ROLES_BY_CLAIM_VALUE), default=None)


def _require_text(name: str, value: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty string without surrounding whitespace, not {value!r}")


def _canonical_guid(name: str, value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError(f"{name} must be a GUID, not {value!r}")
