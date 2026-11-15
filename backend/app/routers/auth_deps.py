"""Authentication dependencies: resolve the caller and enforce permissions."""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.routers.deps import get_session, get_settings
from app.services.auth import accounts, apikeys, tokens
from app.services.auth.permissions import Permission

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(
    auto_error=False,
    description="A user access token from POST /auth/login, or an API key (`rag_<prefix>_<secret>`).",
)
_api_key_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ApiKeyAuth",
    auto_error=False,
    description="An API key (`rag_<prefix>_<secret>`) created by an admin under POST /api-keys. "
    "Authoritative when present: it is never combined with or replaced by the Authorization header.",
)
_ephemeral_key = secrets.token_urlsafe(48)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID | None
    email: str
    permissions: frozenset[str]
    anonymous: bool = False
    #: Set when the caller authenticated with an API key (``email`` is then ``key:<name>``).
    api_key_id: uuid.UUID | None = None


def signing_key(settings: Settings) -> str:
    """The JWT key. An empty key is tolerated only in dev (ephemeral, tokens die on restart)."""
    return settings.secret_key or _ephemeral_key


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


_INVALID_KEY = "invalid or expired API key"


async def _key_principal(session: AsyncSession, raw: str) -> Principal:
    """Authenticate an API key. Every failure is the same 401 (no oracle for which part was wrong)."""
    try:
        key = await apikeys.authenticate(session, raw)
    except apikeys.InvalidKeyError:
        raise _unauthorized(_INVALID_KEY) from None
    await apikeys.touch(session, key)  # throttled: at most one write per key per minute
    return Principal(
        key.owner_user_id, f"key:{key.name}", apikeys.permissions_of(key), api_key_id=key.id
    )


async def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_api_key: str | None = Depends(_api_key_header),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    if not settings.auth_enabled:
        return Principal(None, "anonymous", frozenset(p.value for p in Permission), anonymous=True)
    if x_api_key is not None:  # authoritative when present: no fallback to Authorization
        return await _key_principal(session, x_api_key)
    if credentials is None:
        raise _unauthorized("not authenticated")
    if apikeys.looks_like_key(credentials.credentials):
        return await _key_principal(session, credentials.credentials)
    try:
        user_id = tokens.verify(credentials.credentials, "access", secret=signing_key(settings))
    except tokens.TokenError:
        raise _unauthorized("invalid or expired token") from None
    user = await accounts.get_user(session, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("invalid or expired token")
    return Principal(user.id, user.email, accounts.effective_permissions(user))


def require(*needed: Permission) -> Callable[..., Awaitable[Principal]]:
    """Dependency: the caller must hold every listed permission (else 403)."""

    async def _check(principal: Principal = Depends(get_principal)) -> Principal:
        missing = [p.value for p in needed if p.value not in principal.permissions]
        if missing:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission: {', '.join(missing)}")
        return principal

    return _check


def require_any(*options: Permission) -> Callable[..., Awaitable[Principal]]:
    """Dependency: the caller must hold at least one of the listed permissions (else 403)."""

    async def _check(principal: Principal = Depends(get_principal)) -> Principal:
        if not any(p.value in principal.permissions for p in options):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"missing permission: {' or '.join(p.value for p in options)}"
            )
        return principal

    return _check
