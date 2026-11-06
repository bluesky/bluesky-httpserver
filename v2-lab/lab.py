"""Keys, a stand-in identity provider and access tokens for the v2 lab.

    python lab.py init     create state/: signing and TLS keys, the controller's settings, the socket directory,
                           and the settings of bluesky-httpserver v1 for compare.py (state/v1/)
    python lab.py issuer   serve the token signing keys at https://localhost:8443/jwks.json
    python lab.py token    print an access token; --sub names the caller, --role grants its rights

The issuer stands in for the facility identity provider: the controller verifies tokens as it would in production,
fetching the signing keys over HTTPS. Whoever can read state/signing-key.pem can mint any token, so this is for a
lab on one host only.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import fcntl
import hashlib
import http.server
import ipaddress
import json
import os
import secrets
import ssl
import time
from pathlib import Path
from typing import Any

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from jwt.algorithms import RSAAlgorithm

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
SIGNING_KEY = STATE / "signing-key.pem"
CA_CERT = STATE / "ca.pem"
TLS_CERT = STATE / "tls-cert.pem"
TLS_KEY = STATE / "tls-key.pem"
SETTINGS = STATE / "controller.toml"
SOCKETS = STATE / "run"

ISSUER_PORT = 8443
ISSUER = f"https://localhost:{ISSUER_PORT}"
JWKS_URI = f"{ISSUER}/jwks.json"
AUDIENCE = "bluesky-httpserver-lab"
CONTROLLER_URL = "http://127.0.0.1:8000"
ROLES = ("read", "edit", "run", "admin")

# bluesky-httpserver v1 in front of a second RE Manager in normal mode, for compare.py.
V1_URL = "http://127.0.0.1:8010"
V1_STATE = STATE / "v1"
V1_SOCKETS = V1_STATE / "run"
V1_SETTINGS = V1_STATE / "httpserver.yml"
# Lab users of v1 and their roles; each gets a random password in V1_SETTINGS.
V1_ROLES = {"alice": "user", "bob": "user", "viewer": "observer"}

# Written once; edit state/controller.toml to change the controller's settings.
_SETTINGS = """\
store_path = {store_path}
user_group = "primary"

[identity]
provider = "oidc"
issuer = {issuer}
audience = {audience}
jwks_uri = {jwks_uri}

[manager]
control_address = {control_address}
info_address = {info_address}

[server]
host = "127.0.0.1"
port = 8000
"""

# Written once, with the users' passwords and the token signing secret, so readable only by this user. JSON values
# are valid YAML flow values.
_V1_SETTINGS = """\
uvicorn:
  host: 127.0.0.1
  port: 8010
qserver_zmq_configuration:
  control_address: {control_address}
  info_address: {info_address}
database:
  uri: {database_uri}
authentication:
  secret_keys: [{secret_key}]
  providers:
    - provider: lab
      authenticator: bluesky_httpserver.authenticators:DictionaryAuthenticator
      args:
        users_to_passwords: {passwords}
api_access:
  policy: bluesky_httpserver.authorization:DictionaryAPIAccessControl
  args:
    users: {users}
resource_access:
  policy: bluesky_httpserver.authorization:DefaultResourceAccessControl
  args:
    default_group: primary
"""


def init() -> None:
    """Create whatever is missing in ``state/``; existing keys and settings are kept."""
    STATE.mkdir(exist_ok=True)
    # Every task runs init, so tasks started together would otherwise race to create the same keys.
    with open(STATE / "init.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        SOCKETS.mkdir(exist_ok=True)
        # Only this user may connect to RE Manager's sockets; mkdir's mode would be reduced by the umask.
        SOCKETS.chmod(0o700)
        if not SIGNING_KEY.exists():
            _write_private(SIGNING_KEY, _new_key())
        if not CA_CERT.exists():
            _write_tls()
        if not SETTINGS.exists():
            toml = {
                "store_path": STATE / "controller.sqlite",
                "issuer": ISSUER,
                "audience": AUDIENCE,
                "jwks_uri": JWKS_URI,
                "control_address": f"ipc://{SOCKETS}/control",
                "info_address": f"ipc://{SOCKETS}/info",
            }
            # A JSON string is a valid TOML basic string.
            SETTINGS.write_text(_SETTINGS.format(**{k: json.dumps(str(v)) for k, v in toml.items()}))
        V1_SOCKETS.mkdir(parents=True, exist_ok=True)
        V1_SOCKETS.chmod(0o700)
        if not V1_SETTINGS.exists():
            yaml = {
                "control_address": f"ipc://{V1_SOCKETS}/control",
                "info_address": f"ipc://{V1_SOCKETS}/info",
                "database_uri": f"sqlite:///{V1_STATE}/httpserver.sqlite",
                "secret_key": secrets.token_hex(32),
                "passwords": {user: secrets.token_urlsafe(16) for user in V1_ROLES},
                "users": {user: {"roles": role} for user, role in V1_ROLES.items()},
            }
            _write_secret(V1_SETTINGS, _V1_SETTINGS.format(**{k: json.dumps(v) for k, v in yaml.items()}).encode())


def mint(sub: str = "alice", role: str = "admin", ttl_seconds: int = 3600) -> str:
    """Return an access token for ``sub`` that grants ``role`` and expires ``ttl_seconds`` from now."""
    key = _signing_key()
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "scope": f"QueueServer.{role.capitalize()}",
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": _public_jwk(key)["kid"]})


def serve_issuer() -> None:
    """Serve the signing keys as a JWK Set over HTTPS until interrupted."""
    body = json.dumps({"keys": [_public_jwk(_signing_key())]}).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (the name http.server dispatches to)
            if self.path != "/jwks.json":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(TLS_CERT, TLS_KEY)
    with http.server.ThreadingHTTPServer(("127.0.0.1", ISSUER_PORT), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print(f"serving signing keys at {JWKS_URI}", flush=True)
        server.serve_forever()


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_private(path: Path, key: rsa.RSAPrivateKey) -> None:
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    _write_secret(path, pem)


def _write_secret(path: Path, content: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(content)


def _signing_key() -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(SIGNING_KEY.read_bytes(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    return key


def _public_jwk(key: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    # RFC 7638 thumbprint, so a new key gets a new kid.
    canonical = json.dumps({k: jwk[k] for k in ("e", "kty", "n")}, separators=(",", ":"), sort_keys=True)
    kid = base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def _write_tls() -> None:
    """Write a lab CA and the issuer's certificate for localhost signed by it; the CA key is discarded."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key, server_key = _new_key(), _new_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bluesky-httpserver v2 lab CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_private(TLS_KEY, server_key)
    TLS_CERT.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    CA_CERT.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create state/")
    commands.add_parser("issuer", help="serve the signing keys over HTTPS")
    token = commands.add_parser("token", help="print an access token")
    token.add_argument("--sub", default="alice", help="the caller (default: alice)")
    token.add_argument("--role", choices=ROLES, default="admin", help="rights granted (default: admin)")
    token.add_argument("--ttl", type=int, default=3600, help="seconds until it expires (default: 3600)")
    args = parser.parse_args()
    if args.command == "init":
        init()
    elif args.command == "issuer":
        serve_issuer()
    else:
        print(mint(args.sub, args.role, args.ttl))


if __name__ == "__main__":
    main()
