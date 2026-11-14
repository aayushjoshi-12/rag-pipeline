"""Authentication dependencies: resolve the caller and enforce permissions."""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.routers.deps import get_session, get_settings
from app.services.auth import accounts, tokens
from app.services.auth.permissions import Permission

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
_ephemeral_key = secrets.token_urlsafe(48)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID | None
    email: str
    permissions: frozenset[str]
    anonymous: bool = False


def signing_key(settings: Settings) -> str:
    """The JWT key. An empty key is tolerated only in dev (ephemeral, tokens die on restart)."""
    return settings.secret_key or _ephemeral_key


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    if not settings.auth_enabled:
        return Principal(None, "anonymous", frozenset(p.value for p in Permission), anonymous=True)
    if credentials is None:
        raise _unauthorized("not authenticated")
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
