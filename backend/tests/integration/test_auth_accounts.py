"""Account/role rules against a real, isolated Postgres (migrated by Alembic)."""

from __future__ import annotations

import uuid

import pytest
from app.services.auth import accounts
from app.services.auth.accounts import ConflictError, InvalidError
from app.services.auth.permissions import ADMIN_ROLE, USER_ROLE, Permission
from sqlalchemy.ext.asyncio import AsyncSession


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _role_id(session: AsyncSession, name: str) -> uuid.UUID:
    role = await accounts.get_role_by_name(session, name)
    assert role is not None
    return role.id


async def test_seed_creates_builtin_roles_and_bootstrap_admin_once(
    isolated_session: AsyncSession,
) -> None:
    created = await accounts.seed(
        isolated_session, admin_email="Root@Example.test", admin_password="s3cret-pass"
    )
    assert created is True
    admin = await accounts.get_user_by_email(isolated_session, "root@example.test")
    assert admin is not None
    assert accounts.effective_permissions(admin) == frozenset(p.value for p in Permission)
    # idempotent: a second seed (even with different creds) creates nothing
    assert await accounts.seed(isolated_session, admin_email="b@example.test", admin_password="another-pass") is False
    assert await accounts.get_user_by_email(isolated_session, "b@example.test") is None
    assert {r.name for r in await accounts.list_roles(isolated_session)} >= {"admin", "user", "viewer"}


async def test_seed_resyncs_admin_role_with_new_permissions(isolated_session: AsyncSession) -> None:
    await accounts.seed(isolated_session)
    admin = await accounts.get_role_by_name(isolated_session, ADMIN_ROLE)
    assert admin is not None
    admin.permissions = [p for p in admin.permissions if p.permission != Permission.CHAT_USE.value]
    await isolated_session.commit()
    await accounts.seed(isolated_session)
    refreshed = await accounts.get_role_by_name(isolated_session, ADMIN_ROLE)
    assert refreshed is not None
    await isolated_session.refresh(refreshed)
    assert Permission.CHAT_USE.value in {p.permission for p in refreshed.permissions}


async def test_create_user_normalises_email_and_rejects_duplicates_and_bad_input(
    isolated_session: AsyncSession,
) -> None:
    await accounts.seed(isolated_session)
    role = await _role_id(isolated_session, USER_ROLE)
    email = _email()
    user = await accounts.create_user(
        isolated_session, email=f"  {email.upper()} ", password="long-enough-pw", role_ids=[role]
    )
    assert user.email == email
    assert user.password_hash != "long-enough-pw"
    with pytest.raises(ConflictError):
        await accounts.create_user(isolated_session, email=email, password="long-enough-pw", role_ids=[])
    for bad in ("nope", "@x.test", "a b@x.test"):
        with pytest.raises(InvalidError):
            await accounts.create_user(isolated_session, email=bad, password="long-enough-pw", role_ids=[])
    with pytest.raises(InvalidError):
        await accounts.create_user(isolated_session, email=_email(), password="short", role_ids=[])
    with pytest.raises(InvalidError):
        await accounts.create_user(
            isolated_session, email=_email(), password="long-enough-pw", role_ids=[uuid.uuid4()]
        )


async def test_effective_permissions_union_and_inactive_user_has_none(
    isolated_session: AsyncSession,
) -> None:
    await accounts.seed(isolated_session)
    a = await accounts.create_role(
        isolated_session, name=f"a-{uuid.uuid4().hex[:6]}", description="", permissions=["documents:read"]
    )
    b = await accounts.create_role(
        isolated_session, name=f"b-{uuid.uuid4().hex[:6]}", description="", permissions=["chat:use", "documents:read"]
    )
    user = await accounts.create_user(
        isolated_session, email=_email(), password="long-enough-pw", role_ids=[a.id, b.id]
    )
    assert accounts.effective_permissions(user) == {"documents:read", "chat:use"}
    user = await accounts.update_user(isolated_session, user, is_active=False)
    assert accounts.effective_permissions(user) == frozenset()


async def test_last_active_admin_cannot_be_removed_deactivated_or_deleted(
    isolated_session: AsyncSession,
) -> None:
    await accounts.seed(isolated_session)
    admin_role = await _role_id(isolated_session, ADMIN_ROLE)
    user_role = await _role_id(isolated_session, USER_ROLE)
    # make this test independent of any admins other tests created in the same database
    for existing in await accounts.list_users(isolated_session):
        await isolated_session.delete(existing)
    await isolated_session.commit()

    only = await accounts.create_user(
        isolated_session, email=_email(), password="long-enough-pw", role_ids=[admin_role]
    )
    with pytest.raises(ConflictError):
        await accounts.update_user(isolated_session, only, is_active=False)
    with pytest.raises(ConflictError):
        await accounts.update_user(isolated_session, only, role_ids=[user_role])
    with pytest.raises(ConflictError):
        await accounts.delete_user(isolated_session, only)

    second = await accounts.create_user(
        isolated_session, email=_email(), password="long-enough-pw", role_ids=[admin_role]
    )
    # with two admins, demoting one is fine; the survivor is then protected again
    await accounts.update_user(isolated_session, only, role_ids=[user_role])
    with pytest.raises(ConflictError):
        await accounts.delete_user(isolated_session, second)


async def test_role_rules(isolated_session: AsyncSession) -> None:
    await accounts.seed(isolated_session)
    name = f"auditor-{uuid.uuid4().hex[:6]}"
    role = await accounts.create_role(
        isolated_session, name=name, description=" reads ", permissions=["documents:read", "documents:read"]
    )
    assert role.description == "reads"
    assert sorted(p.permission for p in role.permissions) == ["documents:read"]
    with pytest.raises(ConflictError):
        await accounts.create_role(isolated_session, name=name, description="", permissions=[])
    with pytest.raises(InvalidError, match="unknown permission"):
        await accounts.create_role(
            isolated_session, name=_email(), description="", permissions=["documents:read", "root:all"]
        )
    with pytest.raises(InvalidError):
        await accounts.create_role(isolated_session, name="  ", description="", permissions=[])

    role = await accounts.update_role(
        isolated_session, role, permissions=["chat:use"], description="chatters"
    )
    assert [p.permission for p in role.permissions] == ["chat:use"]

    builtin = await accounts.get_role_by_name(isolated_session, USER_ROLE)
    admin = await accounts.get_role_by_name(isolated_session, ADMIN_ROLE)
    assert builtin is not None and admin is not None
    with pytest.raises(ConflictError):
        await accounts.update_role(isolated_session, builtin, name="renamed")
    with pytest.raises(ConflictError):
        await accounts.update_role(isolated_session, admin, permissions=["chat:use"])
    with pytest.raises(ConflictError):
        await accounts.delete_role(isolated_session, builtin)

    user = await accounts.create_user(
        isolated_session, email=_email(), password="long-enough-pw", role_ids=[role.id]
    )
    with pytest.raises(ConflictError, match="assigned"):
        await accounts.delete_role(isolated_session, role)
    await accounts.update_user(isolated_session, user, role_ids=[])
    await accounts.delete_role(isolated_session, role)
    assert await accounts.get_role(isolated_session, role.id) is None


async def test_manage_create_admin_creates_then_resets_an_existing_user(
    isolated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery CLI against a real DB: creates an admin, then re-grants/reset on re-run."""
    import app.manage as manage
    from app.services.auth.passwords import verify_password
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    monkeypatch.setattr(manage, "async_session", async_sessionmaker(engine, expire_on_commit=False))
    email = _email()
    try:
        assert await manage.create_admin(email, "first-password-1") == "created"
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            user = await accounts.get_user_by_email(s, email)
            assert user is not None
            assert {r.name for r in user.roles} == {"admin"}
            # demote + deactivate, as if someone locked themselves out
            await accounts.create_user(s, email=_email(), password="other-admin-pw", role_ids=[user.roles[0].id])
            await accounts.update_user(s, user, is_active=False, role_ids=[])
        assert await manage.create_admin(email, "second-password-2") == "updated"
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            user = await accounts.get_user_by_email(s, email)
            assert user is not None
            assert user.is_active and {r.name for r in user.roles} == {"admin"}
            assert verify_password("second-password-2", user.password_hash)
            assert not verify_password("first-password-1", user.password_hash)
        with pytest.raises(InvalidError):
            await manage.create_admin(email, "short")
    finally:
        await engine.dispose()


async def test_update_role_keeps_retained_permissions_adds_new_and_drops_removed(
    isolated_session: AsyncSession,
) -> None:
    """Regression: replacing a permission list that overlaps the old one hit a unique violation."""
    await accounts.seed(isolated_session)
    role = await accounts.create_role(
        isolated_session,
        name=f"overlap-{uuid.uuid4().hex[:6]}",
        description="",
        permissions=["documents:read", "jobs:read", "chat:use"],
    )
    role = await accounts.update_role(
        isolated_session, role, permissions=["chat:use", "documents:write"]  # keeps one, adds one, drops two
    )
    assert sorted(p.permission for p in role.permissions) == ["chat:use", "documents:write"]
    # same set again (no-op) and an empty set must also work
    role = await accounts.update_role(isolated_session, role, permissions=["documents:write", "chat:use"])
    assert sorted(p.permission for p in role.permissions) == ["chat:use", "documents:write"]
    role = await accounts.update_role(isolated_session, role, permissions=[])
    assert role.permissions == []
