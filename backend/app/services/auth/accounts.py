"""User/role management rules (no HTTP knowledge).

Raises ``AccountError`` subclasses; routers map them to status codes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey, Role, RolePermission, User, UserRole
from app.services.auth.passwords import hash_password
from app.services.auth.permissions import (
    ADMIN_ROLE,
    BUILTIN_ROLES,
    Permission,
    is_valid_permission,
)


class AccountError(Exception):
    pass


class NotFoundError(AccountError):
    pass


class ConflictError(AccountError):
    pass


class InvalidError(AccountError):
    pass


def effective_permissions(user: User) -> frozenset[str]:
    if not user.is_active:
        return frozenset()
    return frozenset(p.permission for role in user.roles for p in role.permissions)


def _norm_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise InvalidError("invalid email address")
    return email


# ── lookups ──────────────────────────────────────────────────────────────────


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.strip().lower()))
    return result.scalar_one_or_none()


async def list_users(session: AsyncSession) -> list[User]:
    return list((await session.execute(select(User).order_by(User.email))).scalars())


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return await session.get(Role, role_id)


async def get_role_by_name(session: AsyncSession, name: str) -> Role | None:
    return (await session.execute(select(Role).where(Role.name == name))).scalar_one_or_none()


async def list_roles(session: AsyncSession) -> list[Role]:
    return list((await session.execute(select(Role).order_by(Role.name))).scalars())


async def _roles_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> list[Role]:
    wanted = set(ids)
    if not wanted:
        return []
    roles = list((await session.execute(select(Role).where(Role.id.in_(wanted)))).scalars())
    if len(roles) != len(wanted):
        raise InvalidError("unknown role id")
    return roles


async def _active_admin_count(session: AsyncSession, *, excluding: uuid.UUID | None = None) -> int:
    stmt = (
        select(func.count(func.distinct(User.id)))
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.name == ADMIN_ROLE, User.is_active.is_(True))
    )
    if excluding is not None:
        stmt = stmt.where(User.id != excluding)
    return int((await session.execute(stmt)).scalar_one())


# ── users ────────────────────────────────────────────────────────────────────


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    role_ids: Iterable[uuid.UUID],
    is_active: bool = True,
) -> User:
    email = _norm_email(email)
    if await get_user_by_email(session, email):
        raise ConflictError("a user with that email already exists")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise InvalidError(str(exc)) from exc
    user = User(
        email=email,
        password_hash=password_hash,
        is_active=is_active,
        roles=await _roles_by_ids(session, role_ids),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def update_user(
    session: AsyncSession,
    user: User,
    *,
    password: str | None = None,
    is_active: bool | None = None,
    role_ids: Iterable[uuid.UUID] | None = None,
) -> User:
    new_roles = await _roles_by_ids(session, role_ids) if role_ids is not None else None
    stays_admin = any(r.name == ADMIN_ROLE for r in (new_roles if new_roles is not None else user.roles))
    stays_active = user.is_active if is_active is None else is_active
    was_admin = any(r.name == ADMIN_ROLE for r in user.roles) and user.is_active
    if (
        was_admin
        and not (stays_admin and stays_active)
        and await _active_admin_count(session, excluding=user.id) == 0
    ):
        raise ConflictError("cannot remove or deactivate the last active admin")
    if password is not None:
        try:
            user.password_hash = hash_password(password)
        except ValueError as exc:
            raise InvalidError(str(exc)) from exc
    if is_active is not None:
        user.is_active = is_active
    if new_roles is not None:
        user.roles = new_roles
    await session.commit()
    await session.refresh(user)
    return user


async def delete_user(session: AsyncSession, user: User) -> None:
    if (
        any(r.name == ADMIN_ROLE for r in user.roles)
        and user.is_active
        and await _active_admin_count(session, excluding=user.id) == 0
    ):
        raise ConflictError("cannot delete the last active admin")
    await session.delete(user)
    await session.commit()


# ── roles ────────────────────────────────────────────────────────────────────


def _validated_permissions(permissions: Iterable[str]) -> set[str]:
    perms = set(permissions)
    unknown = sorted(p for p in perms if not is_valid_permission(p))
    if unknown:
        raise InvalidError(f"unknown permission(s): {', '.join(unknown)}")
    return perms


def _clean_role_name(name: str) -> str:
    name = name.strip()
    if not (1 <= len(name) <= 64):
        raise InvalidError("role name must be 1-64 characters")
    return name


async def create_role(
    session: AsyncSession, *, name: str, description: str, permissions: Iterable[str]
) -> Role:
    name = _clean_role_name(name)
    perms = _validated_permissions(permissions)
    if await get_role_by_name(session, name):
        raise ConflictError("a role with that name already exists")
    role = Role(
        name=name,
        description=description.strip(),
        is_builtin=False,
        permissions=[RolePermission(permission=p) for p in sorted(perms)],
    )
    session.add(role)
    await session.commit()
    await session.refresh(role)
    return role


async def update_role(
    session: AsyncSession,
    role: Role,
    *,
    name: str | None = None,
    description: str | None = None,
    permissions: Iterable[str] | None = None,
) -> Role:
    if role.is_builtin and (name is not None and name != role.name):
        raise ConflictError("built-in roles cannot be renamed")
    if role.name == ADMIN_ROLE and permissions is not None:
        raise ConflictError("the admin role always has every permission")
    if name is not None and name != role.name:
        name = _clean_role_name(name)
        if await get_role_by_name(session, name):
            raise ConflictError("a role with that name already exists")
        role.name = name
    if description is not None:
        role.description = description.strip()
    if permissions is not None:
        wanted = _validated_permissions(permissions)
        # Diff rather than replace: re-inserting a retained permission before the old row is
        # deleted violates the (role_id, permission) unique constraint.
        role.permissions = [p for p in role.permissions if p.permission in wanted]
        have = {p.permission for p in role.permissions}
        role.permissions.extend(RolePermission(permission=p) for p in sorted(wanted - have))
    await session.commit()
    await session.refresh(role)
    return role


async def delete_role(session: AsyncSession, role: Role) -> None:
    if role.is_builtin:
        raise ConflictError("built-in roles cannot be deleted")
    in_use = (
        await session.execute(select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id))
    ).scalar_one()
    if in_use:
        raise ConflictError(f"role is assigned to {in_use} user(s)")
    live_keys = (
        await session.execute(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.role_id == role.id, ApiKey.revoked_at.is_(None))
        )
    ).scalar_one()
    if live_keys:
        raise ConflictError(f"role is used by {live_keys} API key(s); revoke them first")
    # Revoked keys keep a RESTRICT foreign key to the role; they are dead, so they go with it.
    await session.execute(delete(ApiKey).where(ApiKey.role_id == role.id))
    await session.delete(role)
    await session.commit()


# ── bootstrap ────────────────────────────────────────────────────────────────


async def seed(session: AsyncSession, *, admin_email: str = "", admin_password: str = "") -> bool:
    """Create/sync built-in roles and, if there are no users, the first admin.

    Returns True when a bootstrap admin was created. Idempotent.
    """
    for name, (description, perms) in BUILTIN_ROLES.items():
        role = await get_role_by_name(session, name)
        if role is None:
            session.add(
                Role(
                    name=name,
                    description=description,
                    is_builtin=True,
                    permissions=[RolePermission(permission=p.value) for p in sorted(perms)],
                )
            )
        elif name == ADMIN_ROLE:
            have = {p.permission for p in role.permissions}
            for p in Permission:
                if p.value not in have:
                    role.permissions.append(RolePermission(permission=p.value))
    await session.commit()

    created = False
    if admin_email and admin_password:
        any_user = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        if not any_user:
            admin_role = await get_role_by_name(session, ADMIN_ROLE)
            assert admin_role is not None
            await create_user(
                session, email=admin_email, password=admin_password, role_ids=[admin_role.id]
            )
            created = True
    return created
