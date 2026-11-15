"""API keys over HTTP: real ASGI app + real isolated Postgres, real hashing, no auth bypass."""

from __future__ import annotations

import hashlib
import itertools
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from app.db.models import ApiKey, Trace
from app.main import app
from app.routers import deps
from app.services.auth import accounts, apikeys
from app.services.auth.permissions import KEY_FORBIDDEN, Permission
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.fakes import api_client
from tests.integration.test_auth_api import ALL, FAKE_ID, Env, env  # noqa: F401  (imported fixture)

ERR_BODY = {"detail": "invalid or expired API key", "code": "unauthorized"}


def err_body(r: httpx.Response) -> dict[str, Any]:
    """The error body minus the per-request ``request_id`` (which legitimately differs every call)."""
    body = dict(r.json())
    assert body.pop("request_id") == r.headers["x-request-id"]
    return body


class Fx:
    """An admin plus helpers to mint roles and keys through the real HTTP API."""

    def __init__(self, env: Env, admin: dict[str, str], admin_email: str, db_url: str) -> None:  # noqa: F811
        self.env = env
        self.c = env.client
        self.admin = admin
        self.admin_email = admin_email
        self.db_url = db_url

    async def role(self, permissions: list[str]) -> dict[str, Any]:
        r = await self.c.post(
            "/roles",
            headers=self.admin,
            json={"name": f"svc-{uuid.uuid4().hex[:8]}", "permissions": permissions},
        )
        assert r.status_code == 201, r.text
        return r.json()  # type: ignore[no-any-return]

    async def key(
        self, permissions: list[str], *, name: str | None = None, days: int | None = None
    ) -> dict[str, Any]:
        role = await self.role(permissions)
        body: dict[str, Any] = {"name": name or f"k-{uuid.uuid4().hex[:6]}", "role_id": role["id"]}
        if days is not None:
            body["expires_in_days"] = days
        r = await self.c.post("/api-keys", headers=self.admin, json=body)
        assert r.status_code == 201, r.text
        return {**r.json(), "role_obj": role}

    async def other_session(self) -> AsyncSession:
        """A second, independent session: changes made through it are invisible to the request session's identity map."""
        engine = create_async_engine(self.db_url, poolclass=NullPool)
        return async_sessionmaker(engine, expire_on_commit=False)()


@pytest.fixture
async def fx(env: Env, isolated_db_url: str) -> Fx:  # noqa: F811
    email, admin = await env.make_user(ALL)
    return Fx(env, admin, email, isolated_db_url)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def xkey(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


# ── lifecycle ────────────────────────────────────────────────────────────────


async def test_create_list_revoke_lifecycle(fx: Fx) -> None:
    created = await fx.key(["search:use"], name="billing-service", days=30)
    assert created["key"].startswith("rag_") and created["key"].count("_") >= 2
    assert created["prefix"] == created["key"][4:12]
    assert created["status"] == "active" and created["warnings"] == []
    assert created["role"] == created["role_obj"]["name"] and created["owner_email"] == fx.admin_email
    assert created["expires_at"] is not None and created["last_used_at"] is None and created["revoked_at"] is None

    listed = (await fx.c.get("/api-keys", headers=fx.admin)).json()
    mine = next(k for k in listed if k["id"] == created["id"])
    assert mine["name"] == "billing-service" and mine["prefix"] == created["prefix"]
    assert "key" not in mine and "secret_hash" not in mine
    assert created["key"] not in (await fx.c.get("/api-keys", headers=fx.admin)).text

    first = (await fx.c.post(f"/api-keys/{created['id']}/revoke", headers=fx.admin))
    assert first.status_code == 200 and first.json()["status"] == "revoked"
    again = (await fx.c.post(f"/api-keys/{created['id']}/revoke", headers=fx.admin)).json()
    assert again["revoked_at"] == first.json()["revoked_at"]  # idempotent, first time kept
    assert (await fx.c.post(f"/api-keys/{FAKE_ID}/revoke", headers=fx.admin)).status_code == 404
    assert (await fx.c.post("/api-keys/not-a-uuid/revoke", headers=fx.admin)).status_code == 422


async def test_secret_is_never_persisted_or_returned_after_creation(fx: Fx) -> None:
    created = await fx.key(["search:use"])
    full = created["key"]
    secret = full[len("rag_") + apikeys.PREFIX_LEN + 1 :]
    row = (await fx.env.session.execute(select(ApiKey).where(ApiKey.id == uuid.UUID(created["id"])))).scalar_one()
    assert row.secret_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert row.secret_hash != secret
    # no column of the row (nor any text column of the whole table) holds the secret or the full key
    raw = (await fx.env.session.execute(text("select row_to_json(k)::text from api_keys k"))).scalars().all()
    assert raw and all(secret not in r and full not in r for r in raw)
    # nothing the API returns later contains it either
    for resp in (
        await fx.c.get("/api-keys", headers=fx.admin),
        await fx.c.post(f"/api-keys/{created['id']}/revoke", headers=fx.admin),
        await fx.c.get("/auth/me", headers=xkey(full)),
        await fx.c.get("/roles", headers=fx.admin),
    ):
        assert secret not in resp.text and full not in resp.text


async def test_create_validation_and_role_rules(fx: Fx) -> None:
    role = await fx.role(["search:use"])
    before = len((await fx.c.get("/api-keys", headers=fx.admin)).json())
    post = lambda body: fx.c.post("/api-keys", headers=fx.admin, json=body)  # noqa: E731
    assert (await post({"name": "x", "role_id": role["id"], "expires_in_days": 0})).status_code == 422
    assert (await post({"name": "x", "role_id": role["id"], "expires_in_days": 4000})).status_code == 422
    assert (await post({"name": "", "role_id": role["id"]})).status_code == 422
    assert (await post({"name": "x", "role_id": "nope"})).status_code == 422
    assert (await post({"name": "x", "role_id": FAKE_ID})).status_code == 404
    # nothing was created by the failures
    assert len((await fx.c.get("/api-keys", headers=fx.admin)).json()) == before


async def test_cannot_bind_a_role_with_permissions_the_creator_lacks(fx: Fx) -> None:
    _, limited = await fx.env.make_user(["apikeys:manage", "search:use"])
    bigger = await fx.role(["search:use", "documents:delete"])
    r = await fx.c.post("/api-keys", headers=limited, json={"name": "esc", "role_id": bigger["id"]})
    assert r.status_code == 409 and "documents:delete" in r.json()["detail"]
    ok = await fx.role(["search:use"])
    assert (await fx.c.post("/api-keys", headers=limited, json={"name": "ok", "role_id": ok["id"]})).status_code == 201


async def test_admin_role_is_allowed_but_warned_and_still_cannot_manage(fx: Fx) -> None:
    admin_role = await accounts.get_role_by_name(fx.env.session, "admin")
    assert admin_role is not None
    r = await fx.c.post("/api-keys", headers=fx.admin, json={"name": "root-ish", "role_id": str(admin_role.id)})
    assert r.status_code == 201 and r.json()["warnings"]
    key = r.json()["key"]
    assert (await fx.c.get("/documents", headers=xkey(key))).status_code == 200
    me = (await fx.c.get("/auth/me", headers=xkey(key))).json()
    assert set(me["permissions"]) == set(ALL) - KEY_FORBIDDEN
    for method, path in (("GET", "/users"), ("GET", "/roles"), ("GET", "/api-keys"), ("POST", "/api-keys")):
        assert (await fx.c.request(method, path, headers=xkey(key))).status_code == 403, path


async def test_auth_disabled_cannot_create_keys(tmp_path: Any, isolated_session: AsyncSession) -> None:
    from app.config import Settings

    async with api_client(
        isolated_session, settings=lambda: Settings(auth_enabled=False, data_dir=tmp_path)
    ) as c:
        assert (await c.get("/api-keys")).status_code == 200  # anonymous holds every permission
        r = await c.post("/api-keys", json={"name": "x", "role_id": FAKE_ID})
        assert r.status_code == 409 and "AUTH_ENABLED" in r.json()["detail"]


# ── permission matrix for a retrieval-only key ───────────────────────────────

MATRIX: list[tuple[str, str, Permission | None]] = [
    ("POST", "/search", Permission.SEARCH_USE),  # the one thing the key may do
    ("GET", "/documents", None),
    ("DELETE", f"/documents/{FAKE_ID}", None),
    ("POST", "/documents", None),
    ("POST", "/chat", None),
    ("GET", "/traces", None),
    ("GET", "/users", None),
    ("POST", "/users", None),
    ("GET", "/roles", None),
    ("GET", "/permissions", None),
    ("GET", "/api-keys", None),
    ("POST", "/api-keys", None),
    ("POST", f"/api-keys/{FAKE_ID}/revoke", None),
]


@pytest.mark.parametrize("style", [bearer, xkey], ids=["bearer", "x-api-key"])
@pytest.mark.parametrize(("method", "path", "allowed"), MATRIX, ids=lambda v: str(v))
async def test_retrieval_only_key_permission_matrix(
    fx: Fx, style: Any, method: str, path: str, allowed: Permission | None
) -> None:
    key = (await fx.key(["search:use"]))["key"]
    # no credentials and a garbage key are 401 whatever the route
    assert (await fx.c.request(method, path)).status_code == 401
    assert (await fx.c.request(method, path, headers=style("rag_00000000_" + "x" * 43))).status_code == 401
    r = await fx.c.request(method, path, headers=style(key), json={})
    if allowed is None:
        assert r.status_code == 403, (path, r.status_code, r.text)
    else:
        assert r.status_code not in (401, 403), (path, r.status_code, r.text)


# ── 401 shapes: no oracle ────────────────────────────────────────────────────


async def test_every_key_failure_is_the_same_401(fx: Fx) -> None:
    good = await fx.key(["search:use"])
    full = good["key"]
    prefix = good["prefix"]
    # revoked
    revoked = await fx.key(["search:use"])
    await fx.c.post(f"/api-keys/{revoked['id']}/revoke", headers=fx.admin)
    # expired (shift expiry into the past directly in the DB)
    expired = await fx.key(["search:use"], days=1)
    await fx.env.session.execute(
        text("update api_keys set expires_at = now() - interval '1 second' where id = :i"), {"i": expired["id"]}
    )
    await fx.env.session.commit()
    # owner deactivated
    _, owner_headers = await fx.env.make_user(["apikeys:manage", "search:use"])
    owner_email = (await fx.c.get("/auth/me", headers=owner_headers)).json()["email"]
    role = await fx.role(["search:use"])
    owned = (await fx.c.post("/api-keys", headers=owner_headers, json={"name": "o", "role_id": role["id"]})).json()
    owner = await accounts.get_user_by_email(fx.env.session, owner_email)
    assert owner is not None
    assert (await fx.c.get("/documents", headers=xkey(owned["key"]))).status_code == 403  # works (no documents perm)
    await accounts.update_user(fx.env.session, owner, is_active=False)

    bad = {
        "malformed": "rag_notakey",
        "empty-secret": f"rag_{prefix}_",
        "unknown-prefix": "rag_zzzzzzzz_" + full.split("_", 2)[2],
        "wrong-secret": f"rag_{prefix}_" + "A" * 43,
        "secret-of-other-key": f"rag_{prefix}_" + revoked["key"].split("_", 2)[2],
        "revoked": revoked["key"],
        "expired": expired["key"],
        "owner-inactive": owned["key"],
        "not-rag-xapikey": "something-else",
    }
    seen = []
    for name, value in bad.items():
        for hdr in (xkey(value), bearer(value) if value.startswith("rag_") else xkey(value)):
            r = await fx.c.get("/documents", headers=hdr)
            assert r.status_code == 401, (name, r.status_code, r.text)
            assert err_body(r) == ERR_BODY, name
            assert r.headers["www-authenticate"] == "Bearer"
            seen.append((r.status_code, str(err_body(r)), r.headers["www-authenticate"]))
    assert len(set(seen)) == 1  # byte-identical: no oracle
    # and the good key still works (the failures changed nothing)
    assert (await fx.c.post("/search", headers=xkey(full), json={})).status_code not in (401, 403)


async def test_hash_compare_is_constant_time_primitive_and_always_runs(
    fx: Fx, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = await fx.key(["search:use"])
    calls: list[tuple[str, str]] = []
    real = apikeys.hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(apikeys.hmac, "compare_digest", spy)
    session = fx.env.session
    key = await apikeys.authenticate(session, good["key"])
    assert key.name == good["name"] and len(calls) == 1
    secret = good["key"].split("_", 2)[2]
    assert calls[0] == (hashlib.sha256(secret.encode()).hexdigest(),) * 2
    for raw in (f"rag_{good['prefix']}_wrong", "rag_zzzzzzzz_wrong", "rag_bad"):  # wrong / unknown / malformed
        calls.clear()
        with pytest.raises(apikeys.InvalidKeyError):
            await apikeys.authenticate(session, raw)
        assert len(calls) == 1, raw  # a comparison happens even when there is nothing real to compare


# ── immediate effects (changes made through a DIFFERENT session) ─────────────


async def test_role_edit_applies_to_an_already_used_key_immediately(fx: Fx) -> None:
    created = await fx.key(["documents:read", "search:use"])
    key = created["key"]
    assert (await fx.c.get("/documents", headers=xkey(key))).status_code == 200
    # Pin the key + role + permissions in the request session's identity map (it is weak-referenced, so
    # without a strong reference nothing would ever be stale); only a forced re-read sees the edit below.
    pinned = await apikeys.authenticate(fx.env.session, key)
    assert {p.permission for p in pinned.role.permissions} == {"documents:read", "search:use"}
    other = await fx.other_session()
    role = await accounts.get_role(other, uuid.UUID(created["role_obj"]["id"]))
    assert role is not None
    await accounts.update_role(other, role, permissions=["search:use"])
    assert (await fx.c.get("/documents", headers=xkey(key))).status_code == 403
    assert (await fx.c.post("/search", headers=xkey(key), json={})).status_code not in (401, 403)
    await accounts.update_role(other, role, permissions=["documents:read"])
    assert (await fx.c.get("/documents", headers=xkey(key))).status_code == 200
    assert (await fx.c.post("/search", headers=xkey(key), json={})).status_code == 403
    assert pinned.role is not None  # keep the pin alive to the end
    await other.close()


async def test_revocation_applies_immediately_even_from_another_session(fx: Fx) -> None:
    created = await fx.key(["documents:read"])
    assert (await fx.c.get("/documents", headers=xkey(created["key"]))).status_code == 200
    other = await fx.other_session()
    row = await apikeys.get(other, uuid.UUID(created["id"]))
    assert row is not None
    await apikeys.revoke(other, row)
    await other.close()
    r = await fx.c.get("/documents", headers=xkey(created["key"]))
    assert r.status_code == 401 and err_body(r) == ERR_BODY


async def test_expiry_applies_immediately(fx: Fx) -> None:
    created = await fx.key(["documents:read"], days=1)
    assert (await fx.c.get("/documents", headers=xkey(created["key"]))).status_code == 200
    other = await fx.other_session()
    await other.execute(
        text("update api_keys set expires_at = now() - interval '1 minute' where id = :i"), {"i": created["id"]}
    )
    await other.commit()
    await other.close()
    assert (await fx.c.get("/documents", headers=xkey(created["key"]))).status_code == 401
    listed = (await fx.c.get("/api-keys", headers=fx.admin)).json()
    assert next(k for k in listed if k["id"] == created["id"])["status"] == "expired"


async def test_owner_deactivation_applies_immediately_and_is_reversible(fx: Fx) -> None:
    _, owner_headers = await fx.env.make_user(["apikeys:manage", "documents:read"])
    owner_email = (await fx.c.get("/auth/me", headers=owner_headers)).json()["email"]
    role = await fx.role(["documents:read"])
    key = (await fx.c.post("/api-keys", headers=owner_headers, json={"name": "o", "role_id": role["id"]})).json()["key"]
    assert (await fx.c.get("/documents", headers=xkey(key))).status_code == 200
    other = await fx.other_session()
    owner = await accounts.get_user_by_email(other, owner_email)
    assert owner is not None
    await accounts.update_user(other, owner, is_active=False)
    assert (await fx.c.get("/documents", headers=xkey(key))).status_code == 401
    await accounts.update_user(other, owner, is_active=True)
    assert (await fx.c.get("/documents", headers=xkey(key))).status_code == 200
    await accounts.delete_user(other, owner)  # keys die with their owner
    await other.close()
    assert (await fx.c.get("/documents", headers=xkey(key))).status_code == 401


async def test_role_with_live_keys_cannot_be_deleted_until_revoked(fx: Fx) -> None:
    created = await fx.key(["search:use"])
    rid = created["role_obj"]["id"]
    r = await fx.c.delete(f"/roles/{rid}", headers=fx.admin)
    assert r.status_code == 409 and "API key" in r.json()["detail"]
    await fx.c.post(f"/api-keys/{created['id']}/revoke", headers=fx.admin)
    assert (await fx.c.delete(f"/roles/{rid}", headers=fx.admin)).status_code == 204


# ── header handling ──────────────────────────────────────────────────────────


async def test_header_precedence_and_cross_use(fx: Fx) -> None:
    docs = await fx.key(["documents:read"])
    search = await fx.key(["search:use"])
    jwt = fx.admin  # a valid user JWT with every permission
    c = fx.c
    # key in Bearer and in X-API-Key both work
    assert (await c.get("/documents", headers=bearer(docs["key"]))).status_code == 200
    assert (await c.get("/documents", headers=xkey(docs["key"]))).status_code == 200
    # a valid key in X-API-Key beats a junk Authorization header
    assert (await c.get("/documents", headers={**xkey(docs["key"]), "Authorization": "Bearer junk"})).status_code == 200
    # X-API-Key is authoritative: it wins over a valid JWT in either direction (no fallback)
    assert (await c.get("/documents", headers={**xkey(search["key"]), **jwt})).status_code == 403
    assert (await c.get("/documents", headers={**xkey("rag_00000000_" + "x" * 43), **jwt})).status_code == 401
    # a JWT in X-API-Key is NOT accepted; a JWT in Authorization still is
    token = jwt["Authorization"].split(" ", 1)[1]
    r = await c.get("/documents", headers=xkey(token))
    assert r.status_code == 401 and err_body(r) == ERR_BODY
    assert (await c.get("/documents", headers=jwt)).status_code == 200
    # two keys: X-API-Key's permissions apply, Authorization is ignored
    both = {**xkey(search["key"]), **bearer(docs["key"])}
    assert (await c.get("/documents", headers=both)).status_code == 403
    # a non-key Bearer is still treated as a JWT (and fails as one)
    r = await c.get("/documents", headers=bearer("not.a.token"))
    assert r.status_code == 401 and r.json()["detail"] == "invalid or expired token"
    # an API key is not a refresh token
    assert (await c.post("/auth/refresh", json={"refresh_token": docs["key"]})).status_code == 401


# ── audit: last_used throttle, traces, logs ──────────────────────────────────


async def test_last_used_is_written_at_most_once_per_minute(fx: Fx) -> None:
    created = await fx.key(["documents:read"])
    session = fx.env.session
    t0 = apikeys.utcnow()
    key = await apikeys.authenticate(session, created["key"], now=t0)
    assert key.last_used_at is None
    assert await apikeys.touch(session, key, now=t0) is True
    assert (await _last_used(session, created["id"])) == t0
    assert await apikeys.touch(session, key, now=t0 + timedelta(seconds=30)) is False
    assert await apikeys.touch(session, key, now=t0 + timedelta(seconds=59)) is False
    assert (await _last_used(session, created["id"])) == t0  # unchanged inside the window
    assert await apikeys.touch(session, key, now=t0 + timedelta(seconds=61)) is True
    assert (await _last_used(session, created["id"])) == t0 + timedelta(seconds=61)


async def test_http_requests_do_not_write_last_used_on_every_call(fx: Fx) -> None:
    created = await fx.key(["documents:read"])
    assert (await fx.c.get("/documents", headers=xkey(created["key"]))).status_code == 200
    first = await _last_used(fx.env.session, created["id"])
    assert first is not None  # the first use is recorded
    for _ in range(3):
        assert (await fx.c.get("/documents", headers=xkey(created["key"]))).status_code == 200
    assert await _last_used(fx.env.session, created["id"]) == first  # still inside the minute: no new write
    listed = (await fx.c.get("/api-keys", headers=fx.admin)).json()
    assert next(k for k in listed if k["id"] == created["id"])["last_used_at"] is not None


async def _last_used(session: AsyncSession, key_id: str) -> Any:
    return (
        await session.execute(text("select last_used_at from api_keys where id = :i"), {"i": key_id})
    ).scalar_one()


class _BoomEmbedder:
    dim = 1024

    def embed(self, *a: Any, **k: Any) -> Any:
        raise HTTPException(503, "no embedder in this test")

    embed_query = embed
    embed_documents = embed


async def test_trace_shows_key_name_and_no_secret_leaks_anywhere(
    fx: Fx, caplog: pytest.LogCaptureFixture
) -> None:
    created = await fx.key(["search:use"], name="reporting-bot")
    full = created["key"]
    secret = full.split("_", 2)[2]
    app.dependency_overrides[deps.get_embedder_dep] = lambda: _BoomEmbedder()
    app.dependency_overrides[deps.get_store] = lambda: object()
    try:
        with caplog.at_level(logging.DEBUG):
            r = await fx.c.post("/search", headers=xkey(full), json={"query": "q"})
            await fx.c.get("/documents", headers=bearer(full))
            await fx.c.get("/documents", headers=xkey(f"rag_{created['prefix']}_{'z' * 43}"))
    finally:
        app.dependency_overrides.pop(deps.get_embedder_dep, None)
        app.dependency_overrides.pop(deps.get_store, None)
    assert r.status_code in (500, 503, 422, 200)
    traces = (await fx.env.session.execute(select(Trace).where(Trace.kind == "search"))).scalars().all()
    assert any(t.user_email == "key:reporting-bot" for t in traces)
    for t in traces:
        assert secret not in str(t.__dict__) and full not in str(t.__dict__)
    assert secret not in caplog.text and full not in caplog.text


async def test_prefix_collision_is_retried(fx: Fx, monkeypatch: pytest.MonkeyPatch) -> None:
    real = apikeys.generate
    first = real()
    seq = itertools.chain([first, (first[0], "other-secret-1")], iter(lambda: real(), None))
    monkeypatch.setattr(apikeys, "generate", lambda: next(seq))
    a = await fx.key(["search:use"])
    b = await fx.key(["search:use"])  # draws the colliding prefix first, must retry
    assert a["prefix"] == first[0] and b["prefix"] != a["prefix"]
    assert (await fx.c.post("/search", headers=xkey(a["key"]), json={})).status_code not in (401, 403)
    assert (await fx.c.post("/search", headers=xkey(b["key"]), json={})).status_code not in (401, 403)


async def test_openapi_documents_api_key_security(fx: Fx) -> None:
    spec = (await fx.c.get("/openapi.json")).json()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["ApiKeyAuth"]["type"] == "apiKey" and schemes["ApiKeyAuth"]["name"] == "X-API-Key"
    assert schemes["ApiKeyAuth"]["in"] == "header"
    assert any(s.get("type") == "http" and s.get("scheme") == "bearer" for s in schemes.values())
    for path, methods in (("/v1/api-keys", {"get", "post"}), ("/v1/api-keys/{key_id}/revoke", {"post"})):
        assert methods <= set(spec["paths"][path])
    assert "ApiKeyAuth" in str(spec["paths"]["/v1/search"]["post"]["security"])
    out = spec["components"]["schemas"]["ApiKeyOut"]["properties"]
    assert "key" not in out and "secret_hash" not in out
    assert "key" in spec["components"]["schemas"]["ApiKeyCreated"]["properties"]
