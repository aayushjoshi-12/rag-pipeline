"""JWT access/refresh tokens (HS256 via PyJWT)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

TokenType = Literal["access", "refresh"]
_ALGORITHM = "HS256"


class TokenError(Exception):
    """The token is missing, malformed, expired, wrongly signed or of the wrong type."""


def issue(
    user_id: uuid.UUID,
    token_type: TokenType,
    *,
    secret: str,
    lifetime: timedelta,
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "typ": token_type,
        "iat": issued,
        "exp": issued + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def verify(token: str, expected: TokenType, *, secret: str) -> uuid.UUID:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "typ", "exp", "iat"]},
        )
        if claims["typ"] != expected:
            raise TokenError(f"expected a {expected} token")
        return uuid.UUID(str(claims["sub"]))
    except (jwt.PyJWTError, ValueError) as exc:
        raise TokenError(str(exc)) from exc
