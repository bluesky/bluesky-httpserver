import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bluesky_httpserver.v2.identity import (
    AuthenticationFailed,
    IdentityProviderUnavailable,
    OidcAuthenticator,
    OidcSettings,
    Principal,
    Role,
)

ISSUER = "https://idp.example/realms/facility"
AUDIENCE = "queueserver-api"
JWKS_URI = "https://idp.example/realms/facility/keys"
SETTINGS = OidcSettings(issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI)

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OID = "99999999-8888-7777-6666-555555555555"

ABSENT = object()
"""Claim override that removes the claim."""


@dataclass(frozen=True)
class FromNow:
    """Time claim resolved to the current Unix time plus ``seconds`` when the token is built.

    The verifier checks time claims against the system clock, so tokens are built relative to it; offsets
    stay well away from the 60 s leeway so the time a test takes cannot change its outcome.
    """

    seconds: int


class FakeMonotonic:
    """Monotonic clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64json(value: Any) -> str:
    return b64(json.dumps(value, separators=(",", ":")).encode())


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def generate(cls, kid: str) -> "SigningKey":
        return cls(kid, rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def jwk(self) -> dict[str, Any]:
        numbers = self.private_key.public_key().public_numbers()
        return {"kty": "RSA", "kid": self.kid, "n": b64_uint(numbers.n), "e": b64_uint(numbers.e)}

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def b64_uint(value: int) -> str:
    return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class FakeIdentityProvider:
    """Serves a JWK Set like a real identity provider, including members the verifier must ignore."""

    def __init__(self, url: str, keys: list[SigningKey]):
        self.url = url
        self.keys = list(keys)
        self.extra: list[dict[str, Any]] = []
        self.status = 200
        self.fetches = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == self.url
        self.fetches += 1
        await asyncio.sleep(0)  # let concurrent callers run while the fetch is in flight
        if self.status != 200:
            return httpx.Response(self.status)
        served = [
            {**key.jwk(), "use": "sig", "alg": "RS256", "x5t": key.kid, "issuer": "https://ignored.example"}
            for key in self.keys
        ]
        return httpx.Response(200, json={"keys": self.extra + served})


@pytest.fixture(scope="module")
def idp_key() -> SigningKey:
    return SigningKey.generate("k1")


@pytest.fixture(scope="module")
def rotated_key() -> SigningKey:
    return SigningKey.generate("k2")


@pytest.fixture(scope="module")
def attacker_key() -> SigningKey:
    return SigningKey.generate("k1")


@asynccontextmanager
async def make_authenticator(
    settings: OidcSettings, fake: FakeIdentityProvider, monotonic: FakeMonotonic
) -> AsyncGenerator[OidcAuthenticator, None]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        yield OidcAuthenticator(settings, http, monotonic=monotonic)


@pytest_asyncio.fixture
async def idp(idp_key):
    monotonic = FakeMonotonic()
    fake = FakeIdentityProvider(JWKS_URI, [idp_key])
    async with make_authenticator(SETTINGS, fake, monotonic) as authenticator:
        yield authenticator, fake, monotonic


def compact(header: dict[str, Any], claims: dict[str, Any], sign: Callable[[bytes], bytes]) -> str:
    """Build a compact JWS by hand, so adversarial headers bypass any encoder's checks."""
    signing_input = f"{b64json(header)}.{b64json(claims)}"
    return f"{signing_input}.{b64(sign(signing_input.encode()))}"


def resolved(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    merged = {**base, **overrides}
    return {
        name: now + value.seconds if isinstance(value, FromNow) else value
        for name, value in merged.items()
        if value is not ABSENT
    }


def claims(**overrides: Any) -> dict[str, Any]:
    base = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": FromNow(0),
        "nbf": FromNow(0),
        "exp": FromNow(3600),
        "sub": "alice-subject",
        "scope": "openid QueueServer.Edit",
    }
    return resolved(base, overrides)


def valid(key: SigningKey, **overrides: Any) -> str:
    return compact({"typ": "JWT", "alg": "RS256", "kid": key.kid}, claims(**overrides), key.sign)


async def refusal(authenticator: OidcAuthenticator, token: str) -> str:
    with pytest.raises(AuthenticationFailed) as exc_info:
        await authenticator.authenticate(token)
    return exc_info.value.reason


@pytest.mark.asyncio
async def test_accepts_valid_token(idp, idp_key):
    authenticator, fake, _ = idp
    expires_at = int(time.time()) + 3600
    token = valid(idp_key, exp=expires_at, scope="openid QueueServer.Read QueueServer.Run other")
    expected = Principal(id="alice-subject", role=Role.RUN, expires_at=expires_at)
    assert await authenticator.authenticate(token) == expected
    assert await authenticator.authenticate(token) == expected
    assert fake.fetches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scope, role", [(["QueueServer.Admin"], Role.ADMIN), (ABSENT, None)])
async def test_rights_claim_forms(idp, idp_key, scope, role):
    authenticator, _, _ = idp
    assert (await authenticator.authenticate(valid(idp_key, scope=scope))).role is role


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"exp": FromNow(-30)}, {"nbf": FromNow(30)}, {"iat": FromNow(30)}])
async def test_clock_skew_within_leeway_is_accepted(idp, idp_key, overrides):
    authenticator, _, _ = idp
    assert (await authenticator.authenticate(valid(idp_key, **overrides))).id == "alice-subject"


def alg_none(idp_key: SigningKey) -> str:
    return compact({"typ": "JWT", "alg": "none", "kid": "k1"}, claims(), lambda data: b"")


def hs256_keyed_with_public_key(idp_key: SigningKey) -> str:
    secret = idp_key.private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return compact(
        {"typ": "JWT", "alg": "HS256", "kid": "k1"},
        claims(),
        lambda data: hmac.new(secret, data, hashlib.sha256).digest(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [alg_none, hs256_keyed_with_public_key])
async def test_unsupported_algorithm_is_refused_before_fetching_keys(idp, idp_key, build):
    authenticator, fake, _ = idp
    assert await refusal(authenticator, build(idp_key)) == "unsupported token algorithm"
    assert fake.fetches == 0


def embedded_attacker_jwk(idp_key: SigningKey, attacker_key: SigningKey) -> str:
    header = {"alg": "RS256", "kid": "k1", "jwk": attacker_key.jwk()}
    return compact(header, claims(), attacker_key.sign)


def escalated_payload(idp_key: SigningKey, attacker_key: SigningKey) -> str:
    header, _, signature = valid(idp_key).split(".")
    return f"{header}.{b64json(claims(scope='QueueServer.Admin'))}.{signature}"


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [embedded_attacker_jwk, escalated_payload])
async def test_forged_signature_is_refused(idp, idp_key, attacker_key, build):
    authenticator, _, _ = idp
    assert await refusal(authenticator, build(idp_key, attacker_key)) == "invalid token signature"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"iss": ["https://attacker.example", ISSUER]}, "invalid token claims"),
        ({"iss": "https://idp.example/realms/other"}, "invalid token claims"),
        ({"aud": "other-api"}, "invalid token claims"),
        ({"exp": FromNow(-120)}, "invalid token claims"),
        ({"nbf": FromNow(120)}, "invalid token claims"),
        ({"iat": FromNow(120)}, "invalid token claims"),
        ({"exp": ABSENT}, "invalid token claims"),
        ({"sub": 42}, "invalid token claims"),
        ({"sub": ABSENT}, "missing or malformed principal claim"),
        ({"sub": ""}, "missing or malformed principal claim"),
        ({"sub": "   "}, "missing or malformed principal claim"),
        ({"sub": "x" * 256}, "missing or malformed principal claim"),
        ({"scope": 42}, "malformed roles claim"),
        ({"scope": ["QueueServer.Read", 1]}, "malformed roles claim"),
    ],
)
async def test_signed_token_with_bad_claims_is_refused(idp, idp_key, overrides, reason):
    authenticator, _, _ = idp
    assert await refusal(authenticator, valid(idp_key, **overrides)) == reason


def five_segments(idp_key: SigningKey) -> str:
    return ".".join([b64json({"alg": "RSA-OAEP", "enc": "A256GCM", "kid": "k1"}), "a", "b", "c", "d"])


def header_not_json(idp_key: SigningKey) -> str:
    return f"{b64(b'not json')}.{b64json(claims())}.{b64(b'signature')}"


def header_without_kid(idp_key: SigningKey) -> str:
    return compact({"typ": "JWT", "alg": "RS256"}, claims(), idp_key.sign)


def deeply_nested_header(idp_key: SigningKey) -> str:
    return f"{b64(b'[' * 5000 + b']' * 5000)}.{b64json(claims())}.{b64(b'signature')}"


def too_large(idp_key: SigningKey) -> str:
    return "a" * 16_385


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build, reason",
    [
        (five_segments, "malformed token"),
        (header_not_json, "malformed token"),
        (header_without_kid, "malformed token"),
        (deeply_nested_header, "malformed token"),
        (too_large, "token too large"),
    ],
)
async def test_malformed_token_is_refused(idp, idp_key, build, reason):
    authenticator, _, _ = idp
    assert await refusal(authenticator, build(idp_key)) == reason


@pytest.mark.asyncio
async def test_unknown_kid_refetch_is_rate_limited(idp, idp_key, rotated_key):
    authenticator, fake, monotonic = idp
    await authenticator.authenticate(valid(idp_key))
    assert await refusal(authenticator, valid(rotated_key)) == "unknown signing key"
    assert fake.fetches == 1
    monotonic.advance(60)
    assert await refusal(authenticator, valid(rotated_key)) == "unknown signing key"
    assert fake.fetches == 2


@pytest.mark.asyncio
async def test_rotated_key_is_fetched(idp, idp_key, rotated_key):
    authenticator, fake, monotonic = idp
    await authenticator.authenticate(valid(idp_key))
    fake.keys.append(rotated_key)
    monotonic.advance(60)
    assert (await authenticator.authenticate(valid(rotated_key))).id == "alice-subject"
    assert fake.fetches == 2


@pytest.mark.asyncio
async def test_keys_are_refreshed_daily(idp, idp_key):
    authenticator, fake, monotonic = idp
    await authenticator.authenticate(valid(idp_key))
    monotonic.advance(86_400)
    await authenticator.authenticate(valid(idp_key))
    assert fake.fetches == 2


@pytest.mark.asyncio
async def test_unavailable_identity_provider(idp, idp_key):
    authenticator, fake, monotonic = idp
    fake.status = 503
    for _ in range(2):
        with pytest.raises(IdentityProviderUnavailable):
            await authenticator.authenticate(valid(idp_key))
    assert fake.fetches == 1
    fake.status = 200
    monotonic.advance(60)
    assert (await authenticator.authenticate(valid(idp_key))).id == "alice-subject"


@pytest.mark.asyncio
async def test_stale_keys_survive_failed_refresh(idp, idp_key, rotated_key):
    authenticator, fake, monotonic = idp
    await authenticator.authenticate(valid(idp_key))
    fake.status = 503
    monotonic.advance(86_400)
    assert (await authenticator.authenticate(valid(idp_key))).id == "alice-subject"
    assert fake.fetches == 2
    with pytest.raises(IdentityProviderUnavailable):
        await authenticator.authenticate(valid(rotated_key))
    assert fake.fetches == 2


@pytest.mark.asyncio
async def test_redirected_key_fetch_is_a_failure_even_if_the_client_follows_redirects(idp_key):
    # A redirect-following client must still never take keys from the redirect target.
    requested = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == JWKS_URI:
            return httpx.Response(302, headers={"location": "https://elsewhere.example/keys"})
        return httpx.Response(200, json={"keys": [{**idp_key.jwk(), "use": "sig"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        authenticator = OidcAuthenticator(SETTINGS, http, monotonic=FakeMonotonic())
        with pytest.raises(IdentityProviderUnavailable):
            await authenticator.authenticate(valid(idp_key))
    assert requested == [JWKS_URI]


@pytest.mark.asyncio
async def test_unusable_keys_are_skipped(idp, idp_key, attacker_key):
    authenticator, fake, _ = idp
    fake.extra = [
        {"kty": "RSA", "use": "sig", "kid": "bad", "n": "!!", "e": "AQAB"},
        {"kty": "EC", "use": "sig", "kid": "ec", "crv": "P-256", "x": "AA", "y": "AA"},
        {**attacker_key.jwk(), "use": "sig", "alg": "RS512"},
    ]
    assert (await authenticator.authenticate(valid(idp_key))).id == "alice-subject"


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_fetch(idp, idp_key):
    authenticator, fake, _ = idp
    token = valid(idp_key)
    principals = await asyncio.gather(*(authenticator.authenticate(token) for _ in range(10)))
    assert {principal.id for principal in principals} == {"alice-subject"}
    assert fake.fetches == 1


@pytest.mark.asyncio
async def test_entra_preset(idp_key):
    settings = OidcSettings.entra(tenant_id=TENANT.upper(), client_id="{" + CLIENT + "}")
    assert settings.issuer == f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert settings.audience == CLIENT
    assert settings.jwks_uri == f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
    assert settings.principal_claim == "oid"
    assert settings.roles_claim == "roles"

    expires_at = int(time.time()) + 3600
    entra_claims = {
        "iss": settings.issuer,
        "aud": CLIENT,
        "iat": FromNow(0),
        "nbf": FromNow(0),
        "exp": expires_at,
        "oid": OID,
        "sub": "pairwise-subject",
        "tid": TENANT,
        "ver": "2.0",
        "scp": "access_as_user",
        "roles": ["QueueServer.Read", "QueueServer.Run", "Other.Role"],
    }

    def token(**overrides: Any) -> str:
        header = {"typ": "JWT", "alg": "RS256", "x5t": "k1", "kid": "k1"}
        return compact(header, resolved(entra_claims, overrides), idp_key.sign)

    fake = FakeIdentityProvider(settings.jwks_uri, [idp_key])
    async with make_authenticator(settings, fake, FakeMonotonic()) as authenticator:
        assert await authenticator.authenticate(token()) == Principal(id=OID, role=Role.RUN, expires_at=expires_at)
        assert await refusal(authenticator, token(oid=ABSENT)) == "missing or malformed principal claim"
        v1_issuer = f"https://sts.windows.net/{TENANT}/"
        assert await refusal(authenticator, token(iss=v1_issuer)) == "invalid token claims"


@pytest.mark.parametrize(
    "changes",
    [
        {"jwks_uri": "http://idp.example/keys"},
        {"jwks_uri": "https://user:pw@idp.example/keys"},
        {"issuer": ""},
        {"audience": " queueserver-api"},
        {"principal_claim": ""},
        {"leeway_seconds": 301},
    ],
)
def test_settings_refused(changes):
    with pytest.raises(ValueError):
        dataclasses.replace(SETTINGS, **changes)


def test_entra_ids_must_be_guids():
    with pytest.raises(ValueError):
        OidcSettings.entra(tenant_id="contoso.onmicrosoft.com", client_id=CLIENT)
