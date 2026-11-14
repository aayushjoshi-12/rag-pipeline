"""Password hashing with stdlib scrypt (no extra dependency)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_N, _R, _P = 2**14, 8, 1
_MIN_LENGTH = 8


class WeakPasswordError(ValueError):
    pass


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024)


def validate_password(password: str) -> None:
    if len(password) < _MIN_LENGTH:
        raise WeakPasswordError(f"password must be at least {_MIN_LENGTH} characters")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(_derive(password, salt, _N, _R, _P))}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check; malformed hashes verify as False rather than raising."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        actual = _derive(password, base64.b64decode(salt_b64), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


#: Verified against when the email is unknown, so timing does not reveal which emails exist.
DUMMY_HASH = hash_password("not-a-real-password")
