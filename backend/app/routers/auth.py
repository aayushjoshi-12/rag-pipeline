from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Role, User
from app.routers.auth_deps import Principal, get_principal, require, signing_key
from app.routers.deps import get_session, get_settings
from app.schema.auth import (
    LoginRequest,
    MeOut,
    PermissionOut,
    RefreshRequest,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    TokenPair,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services.auth import accounts, tokens
from app.services.auth.accounts import AccountError, ConflictError, NotFoundError
from app.services.auth.passwords import DUMMY_HASH, verify_password
from app.services.auth.permissions import DESCRIPTIONS, Permission

router = APIRouter()


def _http(exc: AccountError) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _role_out(role: Role) -> RoleOut:
    return RoleOut(
        id=role.id,
        name=role.name,
        description=role.description,
        is_builtin=role.is_builtin,
        permissions=sorted(p.permission for p in role.permissions),
    )


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        roles=sorted(r.name for r in user.roles),
        permissions=sorted(accounts.effective_permissions(user)),
    )


def _pair(user: User, settings: Settings) -> TokenPair:
    key = signing_key(settings)
    access_ttl = timedelta(minutes=settings.access_token_minutes)
    return TokenPair(
        access_token=tokens.issue(user.id, "access", secret=key, lifetime=access_ttl),
        refresh_token=tokens.issue(
            user.id, "refresh", secret=key, lifetime=timedelta(days=settings.refresh_token_days)
        ),
        expires_in=int(access_ttl.total_seconds()),
    )


# ── session ──────────────────────────────────────────────────────────────────


@router.post("/auth/login", response_model=TokenPair)
async def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    user = await accounts.get_user_by_email(session, body.email)
    # Always run one scrypt verification so response time does not reveal whether the email exists.
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if user is None or not ok or not user.is_active:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid credentials", headers={"WWW-Authenticate": "Bearer"}
        )
    return _pair(user, settings)


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    try:
        user_id = tokens.verify(body.refresh_token, "refresh", secret=signing_key(settings))
    except tokens.TokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from None
    user = await accounts.get_user(session, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    return _pair(user, settings)


@router.get("/auth/me", response_model=MeOut)
async def me(
    principal: Principal = Depends(get_principal), settings: Settings = Depends(get_settings)
) -> MeOut:
    return MeOut(
        id=principal.user_id,
        email=principal.email,
        permissions=sorted(principal.permissions),
        auth_enabled=settings.auth_enabled,
    )


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(
    _: Principal = Depends(require(Permission.ROLES_MANAGE)),
) -> list[PermissionOut]:
    return [PermissionOut(name=p.value, description=DESCRIPTIONS[p]) for p in Permission]


# ── users ────────────────────────────────────────────────────────────────────


@router.get("/users", response_model=list[UserOut])
async def list_users(
    _: Principal = Depends(require(Permission.USERS_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> list[UserOut]:
    return [_user_out(u) for u in await accounts.list_users(session)]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    _: Principal = Depends(require(Permission.USERS_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    try:
        user = await accounts.create_user(
            session,
            email=body.email,
            password=body.password,
            role_ids=body.role_ids,
            is_active=body.is_active,
        )
    except AccountError as exc:
        raise _http(exc) from exc
    return _user_out(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    body: UserUpdate,
    _: Principal = Depends(require(Permission.USERS_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await _user_or_404(session, user_id)
    try:
        user = await accounts.update_user(
            session,
            user,
            password=body.password,
            is_active=body.is_active,
            role_ids=body.role_ids,
        )
    except AccountError as exc:
        raise _http(exc) from exc
    return _user_out(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    principal: Principal = Depends(require(Permission.USERS_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> None:
    user = await _user_or_404(session, user_id)
    if principal.user_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "you cannot delete your own account")
    try:
        await accounts.delete_user(session, user)
    except AccountError as exc:
        raise _http(exc) from exc


async def _user_or_404(session: AsyncSession, raw_id: str) -> User:
    import uuid

    try:
        uid = uuid.UUID(raw_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found") from None
    user = await accounts.get_user(session, uid)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return user


# ── roles ────────────────────────────────────────────────────────────────────


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    _: Principal = Depends(require(Permission.ROLES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> list[RoleOut]:
    return [_role_out(r) for r in await accounts.list_roles(session)]


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreate,
    _: Principal = Depends(require(Permission.ROLES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    try:
        role = await accounts.create_role(
            session, name=body.name, description=body.description, permissions=body.permissions
        )
    except AccountError as exc:
        raise _http(exc) from exc
    return _role_out(role)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: str,
    body: RoleUpdate,
    _: Principal = Depends(require(Permission.ROLES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    role = await _role_or_404(session, role_id)
    try:
        role = await accounts.update_role(
            session,
            role,
            name=body.name,
            description=body.description,
            permissions=body.permissions,
        )
    except AccountError as exc:
        raise _http(exc) from exc
    return _role_out(role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    _: Principal = Depends(require(Permission.ROLES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> None:
    role = await _role_or_404(session, role_id)
    try:
        await accounts.delete_role(session, role)
    except AccountError as exc:
        raise _http(exc) from exc


async def _role_or_404(session: AsyncSession, raw_id: str) -> Role:
    import uuid

    try:
        rid = uuid.UUID(raw_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "role not found") from None
    role = await accounts.get_role(session, rid)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "role not found")
    return role
