"""Password hashing, login tokens and credential encryption.

Credentials for saved connections are encrypted at rest with a Fernet master key that
comes from the environment (CONDUIT_SECRET_KEY). The API never returns them.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

# ---- passwords (scrypt, stdlib) -------------------------------------------------

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---- login tokens ---------------------------------------------------------------

def _jwt_secret() -> str:
    s = get_settings()
    if s.jwt_secret:
        return s.jwt_secret
    if not s.secret_key:
        raise RuntimeError("CONDUIT_SECRET_KEY (or CONDUIT_JWT_SECRET) must be set")
    return hashlib.sha256(b"conduit-jwt:" + s.secret_key.encode()).hexdigest()


def create_token(user_id: str, role: str, version: int = 0) -> str:
    """`role` is informational only (authorization always reads the users table); `ver` must equal the user's
    token_version, so bumping it ends every session the user has."""
    now = int(time.time())
    payload = {"sub": user_id, "role": role, "ver": version, "iat": now, "exp": now + get_settings().jwt_ttl_minutes * 60}
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ---- credential encryption ------------------------------------------------------

class SecretKeyError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise SecretKeyError("CONDUIT_SECRET_KEY is not set; cannot encrypt or decrypt credentials")
    try:
        return Fernet(key.encode())
    except Exception as e:  # bad length / encoding
        raise SecretKeyError("CONDUIT_SECRET_KEY is not a valid Fernet key") from e


def encrypt_secrets(secrets: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(secrets).encode()).decode()


def decrypt_secrets(token: str) -> dict[str, Any]:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as e:
        raise SecretKeyError("Stored credentials cannot be decrypted with the current CONDUIT_SECRET_KEY") from e
