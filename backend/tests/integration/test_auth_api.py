"""RBAC over HTTP: real ASGI app + real isolated Postgres, real tokens, no auth bypass."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.main import app
from app.routers import deps
from app.services.auth import accounts
from app.services.auth.permissions import Permission
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct-horse-battery"
SECRET = "s" * 48
ALL = [p.value for p in Permission]
FAKE_ID = "00000000-0000-0000-0000-000000000001"

#: (method, path, permission that guards it). Bodies are intentionally empty/invalid: the guard
#: must answer 401/403 before validation or the handler run, and with the right permission the
#: request must get *past* the guard (any status other than 401/403).
GUARDED: list[tuple[str, str, Permission]] = [
    ("GET", "/documents", Permission.DOCUMENTS_READ),
    ("GET", f"/documents/{FAKE_ID}", Permission.DOCUMENTS_READ),
    ("GET", f"/documents/{FAKE_ID}/chunks", Permission.DOCUMENTS_READ),
    ("POST", "/documents", Permission.DOCUMENTS_WRITE),
    ("POST", "/documents/from-url", Permission.DOCUMENTS_WRITE),
    ("PUT", f"/documents/{FAKE_ID}", Permission.DOCUMENTS_WRITE),
    ("DELETE", f"/documents/{FAKE_ID}", Permission.DOCUMENTS_DELETE),
    ("GET", f"/jobs/{FAKE_ID}", Permission.JOBS_READ),
    ("POST", "/chat", Permission.CHAT_USE),
    ("POST", "/chat/stream", Permission.CHAT_USE),
    ("POST", "/search", Permission.SEARCH_USE),
    ("GET", "/traces", Permission.TRACES_READ),
    ("GET", f"/traces/{FAKE_ID}", Permission.TRACES_READ),
    ("GET", "/usage", Permission.TRACES_READ),
    ("GET", "/usage/insights", Permission.TRACES_READ),
    ("GET", "/usage/credits", Permission.TRACES_READ),
    ("GET", "/users", Permission.USERS_MANAGE),
    ("POST", "/users", Permission.USERS_MANAGE),
    ("PATCH", f"/users/{FAKE_ID}", Permission.USERS_MANAGE),
    ("DELETE", f"/users/{FAKE_ID}", Permission.USERS_MANAGE),
    ("GET", "/roles", Permission.ROLES_MANAGE),
    ("POST", "/roles", Permission.ROLES_MANAGE),
    ("PATCH", f"/roles/{FAKE_ID}", Permission.ROLES_MANAGE),
    ("DELETE", f"/roles/{FAKE_ID}", Permission.ROLES_MANAGE),
    ("GET", "/permissions", Permission.ROLES_MANAGE),
    ("GET", "/api-keys", Permission.APIKEYS_MANAGE),
    ("POST", "/api-keys", Permission.APIKEYS_MANAGE),
    ("POST", f"/api-keys/{FAKE_ID}/revoke", Permission.APIKEYS_MANAGE),
]


class Env:
    def __init__(self, client: httpx.AsyncClient, session: AsyncSession) -> None:
        self.client = client
        self.session = session

    async def make_user(self, permissions: list[str], *, active: bool = True) -> tuple[str, dict[str, str]]:
        """Create a user holding exactly ``permissions`` via a fresh role; return (email, headers)."""
        role = await accounts.create_role(
            self.session, name=f"r-{uuid.uuid4().hex[:8]}", description="", permissions=permissions
        )
        email = f"{uuid.uuid4().hex[:10]}@example.test"
        user = await accounts.create_user(
            self.session, email=email, password=PASSWORD, role_ids=[role.id], is_active=True
        )
        headers = await self.login(email)
        if not active:
            await accounts.update_user(self.session, user, is_active=False)
        return email, headers

    async def login(self, email: str, password: str = PASSWORD) -> dict[str, str]:
        r = await self.client.post("/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def env(
    isolated_session: AsyncSession, tmp_path: Path
) -> AsyncIterator[Env]:
    await accounts.seed(isolated_session)
    cfg = Settings(secret_key=SECRET, auth_enabled=True, data_dir=tmp_path)

    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield isolated_session

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: cfg
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield Env(c, isolated_session)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("method", "path", "perm"), GUARDED, ids=lambda v: str(v))
async def test_permission_matrix(env: Env, method: str, path: str, perm: Permission) -> None:
    c = env.client
    # no credentials / garbage credentials -> 401
    assert (await c.request(method, path)).status_code == 401
    bad = await c.request(method, path, headers={"Authorization": "Bearer not.a.token"})
    assert bad.status_code == 401
    # everything EXCEPT the guarding permission -> 403
    _, without = await env.make_user([p for p in ALL if p != perm.value])
    r = await c.request(method, path, headers=without)
    assert r.status_code == 403, (r.status_code, r.text)
    assert perm.value in r.json()["detail"]
    # exactly the guarding permission -> gets past the guard
    _, only = await env.make_user([perm.value])
    r = await c.request(method, path, headers=only)
    assert r.status_code not in (401, 403), (r.status_code, r.text)


async def test_health_is_public(env: Env) -> None:
    resp = await env.client.get("/health")
    assert resp.status_code != 401
    assert isinstance(resp.json()["auth_enabled"], bool)  # the dashboard reads this to decide on the login form


async def test_refresh_token_cannot_authenticate_requests(env: Env) -> None:
    email, _ = await env.make_user(["documents:read"])
    r = await env.client.post("/auth/login", json={"email": email, "password": PASSWORD})
    refresh = r.json()["refresh_token"]
    assert (await env.client.get("/documents", headers={"Authorization": f"Bearer {refresh}"})).status_code == 401


async def test_login_failures_are_indistinguishable(env: Env) -> None:
    email, _ = await env.make_user(["documents:read"])
    wrong_pw = await env.client.post("/auth/login", json={"email": email, "password": "wrong-password"})
    unknown = await env.client.post("/auth/login", json={"email": "ghost@example.test", "password": PASSWORD})
    assert wrong_pw.status_code == unknown.status_code == 401
    assert {**wrong_pw.json(), "request_id": ""} == {**unknown.json(), "request_id": ""}  # ids differ per call
    assert "password" not in wrong_pw.text.lower().replace("invalid credentials", "")


async def test_inactive_user_cannot_log_in(env: Env) -> None:
    email, _ = await env.make_user(["documents:read"], active=False)
    r = await env.client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 401


async def test_refresh_flow(env: Env) -> None:
    email, _ = await env.make_user(["documents:read"])
    pair = (await env.client.post("/auth/login", json={"email": email, "password": PASSWORD})).json()
    new = await env.client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert new.status_code == 200
    ok = await env.client.get("/documents", headers={"Authorization": f"Bearer {new.json()['access_token']}"})
    assert ok.status_code == 200
    # an access token is not a refresh token; garbage is rejected
    assert (await env.client.post("/auth/refresh", json={"refresh_token": pair["access_token"]})).status_code == 401
    assert (await env.client.post("/auth/refresh", json={"refresh_token": "junk"})).status_code == 401


async def test_deactivation_blocks_existing_tokens_immediately(env: Env) -> None:
    email, headers = await env.make_user(["documents:read"])
    assert (await env.client.get("/documents", headers=headers)).status_code == 200
    user = await accounts.get_user_by_email(env.session, email)
    assert user is not None
    await accounts.update_user(env.session, user, is_active=False)
    assert (await env.client.get("/documents", headers=headers)).status_code == 401
    pair = await env.client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert pair.status_code == 401


async def test_role_edit_changes_access_for_tokens_already_issued(env: Env) -> None:
    email, headers = await env.make_user(["documents:read"])
    assert (await env.client.get("/documents", headers=headers)).status_code == 200
    assert (await env.client.post("/chat", headers=headers, json={})).status_code == 403
    user = await accounts.get_user_by_email(env.session, email)
    assert user is not None
    role = user.roles[0]
    await accounts.update_role(env.session, role, permissions=["chat:use"])
    assert (await env.client.get("/documents", headers=headers)).status_code == 403
    assert (await env.client.post("/chat", headers=headers, json={})).status_code != 403


async def test_me_reports_identity_and_permissions(env: Env) -> None:
    email, headers = await env.make_user(["documents:read", "chat:use"])
    r = await env.client.get("/auth/me", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == email
    assert body["permissions"] == ["chat:use", "documents:read"]
    assert body["auth_enabled"] is True
    assert (await env.client.get("/auth/me")).status_code == 401


async def test_admin_manages_roles_and_users_end_to_end(env: Env) -> None:
    _, admin = await env.make_user(ALL)
    c = env.client

    perms = (await c.get("/permissions", headers=admin)).json()
    assert {p["name"] for p in perms} == set(ALL)

    created = await c.post(
        "/roles", headers=admin, json={"name": f"ops-{uuid.uuid4().hex[:5]}", "permissions": ["jobs:read"]}
    )
    assert created.status_code == 201
    role_id = created.json()["id"]
    assert created.json()["permissions"] == ["jobs:read"]
    assert (await c.post("/roles", headers=admin, json={"name": created.json()["name"]})).status_code == 409
    assert (await c.post("/roles", headers=admin, json={"name": "x", "permissions": ["nope"]})).status_code == 422

    email = f"{uuid.uuid4().hex[:8]}@example.test"
    u = await c.post("/users", headers=admin, json={"email": email, "password": PASSWORD, "role_ids": [role_id]})
    assert u.status_code == 201
    assert u.json()["permissions"] == ["jobs:read"]
    assert "password" not in u.text
    assert (await c.post("/users", headers=admin, json={"email": email, "password": PASSWORD})).status_code == 409
    assert (await c.post("/users", headers=admin, json={"email": "x@y.test", "password": "short"})).status_code == 422

    new_headers = await env.login(email)
    assert (await c.get(f"/jobs/{FAKE_ID}", headers=new_headers)).status_code == 404  # allowed, just not found
    assert (await c.get("/documents", headers=new_headers)).status_code == 403
    assert (await c.get("/users", headers=new_headers)).status_code == 403

    # editing a role over HTTP, keeping one permission and adding another (regression: was a 500)
    edited = await c.patch(f"/roles/{role_id}", headers=admin, json={"permissions": ["jobs:read", "chat:use"]})
    assert edited.status_code == 200, edited.text
    assert edited.json()["permissions"] == ["chat:use", "jobs:read"]
    assert (await c.patch(f"/roles/{role_id}", headers=admin, json={"permissions": ["nope"]})).status_code == 422
    assert (await c.patch(f"/roles/{role_id}", headers=admin, json={"permissions": ["jobs:read"]})).status_code == 200
    assert (await c.delete(f"/roles/{role_id}", headers=admin)).status_code == 409  # still assigned
    assert (await c.patch(f"/users/{u.json()['id']}", headers=admin, json={"role_ids": []})).status_code == 200
    assert (await c.delete(f"/roles/{role_id}", headers=admin)).status_code == 204
    assert (await c.delete(f"/users/{u.json()['id']}", headers=admin)).status_code == 204
    assert (await c.patch("/users/not-a-uuid", headers=admin, json={})).status_code == 404


async def test_admin_cannot_delete_self_or_last_admin_via_api(env: Env) -> None:
    c = env.client
    for u in await accounts.list_users(env.session):
        await env.session.delete(u)
    await env.session.commit()
    admin_role = await accounts.get_role_by_name(env.session, "admin")
    assert admin_role is not None
    email = f"{uuid.uuid4().hex[:8]}@example.test"
    me = await accounts.create_user(env.session, email=email, password=PASSWORD, role_ids=[admin_role.id])
    headers = await env.login(email)
    assert (await c.delete(f"/users/{me.id}", headers=headers)).status_code == 409
    assert (await c.patch(f"/users/{me.id}", headers=headers, json={"is_active": False})).status_code == 409


async def test_auth_disabled_allows_everything(isolated_session: AsyncSession, tmp_path: Path) -> None:
    cfg = Settings(auth_enabled=False, data_dir=tmp_path)

    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield isolated_session

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: cfg
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/documents")).status_code == 200
            me: dict[str, Any] = (await c.get("/auth/me")).json()
            assert me["auth_enabled"] is False
            assert set(me["permissions"]) == set(ALL)
    finally:
        app.dependency_overrides.clear()


async def test_traces_and_usage_endpoints(env: Env) -> None:
    from app.services import traces

    _, reader = await env.make_user(["traces:read"])
    for i in range(3):
        await traces.record(env.session, kind="chat", status="ok" if i else "error",
                            duration_ms=100 * (i + 1), prompt_tokens=10, completion_tokens=2,
                            error="boom" if not i else None)
    c = env.client
    page = (await c.get("/traces?kind=chat&limit=2", headers=reader)).json()
    assert page["total"] >= 3 and len(page["items"]) == 2
    only_err = (await c.get("/traces?status=error&kind=chat", headers=reader)).json()
    assert only_err["total"] >= 1 and all(t["status"] == "error" for t in only_err["items"])
    one = await c.get(f"/traces/{page['items'][0]['id']}", headers=reader)
    assert one.status_code == 200 and one.json()["id"] == page["items"][0]["id"]
    assert (await c.get(f"/traces/{FAKE_ID}", headers=reader)).status_code == 404
    assert (await c.get("/traces?kind=bogus", headers=reader)).status_code == 422
    assert (await c.get("/traces?status=bogus", headers=reader)).status_code == 422
    assert (await c.get("/traces?limit=0", headers=reader)).status_code == 422
    assert (await c.get("/traces?limit=500", headers=reader)).status_code == 422
    usage = (await c.get("/usage?days=3", headers=reader)).json()
    assert len(usage) == 3 and usage[-1]["requests"] >= 3 and usage[-1]["errors"] >= 1
    assert usage[-1]["prompt_tokens"] >= 30
    assert (await c.get("/usage?days=0", headers=reader)).status_code == 422
    assert (await c.get("/usage?days=91", headers=reader)).status_code == 422
    assert (await c.get("/usage?kind=bogus", headers=reader)).status_code == 422
