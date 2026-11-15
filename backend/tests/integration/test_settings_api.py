"""Admin settings over HTTP: real ASGI app + real isolated Postgres + real encryption (ticket 43, ADR-0008)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.db.models import AppSetting, SettingsAudit
from app.routers import deps
from app.services import runtime_settings as rt
from app.services.auth.permissions import KEY_FORBIDDEN, Permission
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.fakes import api_client
from tests.integration.test_auth_api import PASSWORD, Env  # noqa: F401  (shared helper class)

ENC_KEY = "k" * 48
CANARY = "sk-CANARY-7f3a9c1e-do-not-leak"
BACKEND = Path(__file__).resolve().parents[2]
READ = Permission.SETTINGS_READ.value
MANAGE = Permission.SETTINGS_MANAGE.value
ALL = [p.value for p in Permission]


class Fake:
    """Replaces the Celery enqueue / task-status dependencies."""

    def __init__(self) -> None:
        self.queued: list[str] = []
        self.state = ("pending", None, None)
        self.fail = False

    def enqueue(self) -> Any:
        def _enqueue(kind: str) -> str:
            if self.fail:
                raise ConnectionError("broker down")
            self.queued.append(kind)
            return f"task-{len(self.queued)}"

        return _enqueue

    def status(self) -> Any:
        return lambda _task_id: self.state


@pytest.fixture
async def env(
    isolated_session: AsyncSession,
    isolated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> AsyncIterator[Env]:
    from app.services.auth import accounts

    rt.reset_for_tests()
    monkeypatch.setenv("SECRET_KEY", "s" * 48)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    if "no_enc_key" not in request.keywords:
        monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ENC_KEY)
    await accounts.seed(isolated_session)
    # the throwaway database is shared by the whole module: start every test from an empty UI layer
    await isolated_session.execute(text("DELETE FROM app_settings"))
    await isolated_session.execute(text("DELETE FROM settings_audit"))
    await isolated_session.commit()
    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    rt.configure(async_sessionmaker(engine, expire_on_commit=False))
    fake = Fake()

    try:
        async with api_client(
            isolated_session, enqueue_reindex=fake.enqueue, task_status=fake.status
        ) as c:
            e = Env(c, isolated_session)
            e.fake = fake  # type: ignore[attr-defined]
            e.url = isolated_db_url  # type: ignore[attr-defined]
            yield e
    finally:
        rt.reset_for_tests()
        await engine.dispose()


async def admin(env: Env) -> dict[str, str]:
    _, headers = await env.make_user(ALL)
    return headers


def by_key(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["key"]: s for s in body["settings"]}


async def patch(env: Env, headers: dict[str, str], **body: Any) -> httpx.Response:
    return await env.client.patch("/v1/settings", headers=headers, json=body)


# ── reading ──────────────────────────────────────────────────────────────────────────────────────


async def test_get_lists_every_setting_with_source_and_never_a_secret(env: Env) -> None:
    h = await admin(env)
    r = await env.client.get("/v1/settings", headers=h)
    assert r.status_code == 200
    body = r.json()
    keys = by_key(body)
    assert set(keys) == set(Settings.model_fields)
    top_k = keys["default_top_k"]
    assert (top_k["source"], top_k["apply"], top_k["locked"]) == ("default", "live", False)
    key = keys["openai_api_key"]
    assert key["secret"] is True and key["value"] is None and key["default"] is None
    assert key["is_set"] in (True, False)
    assert keys["database_url"]["locked"] is True and keys["database_url"]["value"] is None
    assert keys["database_url"]["locked_reason"]
    assert body["summary"]["secrets_enabled"] is True


async def test_locked_by_environment_and_writes_rejected(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHUNK_SIZE", "700")
    h = await admin(env)
    row = by_key((await env.client.get("/v1/settings", headers=h)).json())["chunk_size"]
    assert (row["source"], row["locked"], row["value"]) == ("env", True, 700)
    assert row["locked_reason"] == "set by environment variable CHUNK_SIZE"
    r = await patch(env, h, **{"set": {"chunk_size": 900}})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "setting_locked"
    assert (await env.session.execute(select(AppSetting))).scalars().all() == []
    r = await env.client.post("/v1/settings/chunk_size/reset", headers=h)
    assert r.status_code == 409


async def test_bootstrap_settings_cannot_be_written(env: Env) -> None:
    h = await admin(env)
    for key, value in [
        ("database_url", "postgresql://x"),
        ("auth_enabled", False),
        ("secret_key", "z" * 40),
    ]:
        r = await patch(env, h, **{"set": {key: value}})
        assert r.status_code == 409, key
        assert r.json()["detail"]["code"] == "setting_not_editable"
    assert (await patch(env, h, **{"set": {"nope": 1}})).status_code == 404


async def test_a_tampered_database_row_for_a_bootstrap_key_is_ignored(env: Env) -> None:
    env.session.add(
        AppSetting(key="database_url", value="postgresql://evil", is_secret=False, updated_by="x")
    )
    env.session.add(AppSetting(key="auth_enabled", value=False, is_secret=False, updated_by="x"))
    await env.session.commit()
    await rt.refresh(env.session, force=True)
    assert Settings().auth_enabled is True
    assert "evil" not in Settings().database_url


# ── writing: live apply, validation, atomicity, audit ────────────────────────────────────────────


async def test_live_change_is_seen_by_the_next_request_without_restart(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(
        "CACHE_ENABLED"
    )  # the suite pins it off; here the UI layer must be able to flip it
    h = await admin(env)
    before = (await env.client.get("/v1/capabilities", headers=h)).json()
    assert before["retrieval"]["default_top_k"] == 5
    r = await patch(
        env,
        h,
        **{"set": {"default_top_k": 9, "max_upload_size_bytes": 12345, "cache_enabled": True}},
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["changed"]) == [
        "cache_enabled",
        "default_top_k",
        "max_upload_size_bytes",
    ]
    after = (await env.client.get("/v1/capabilities", headers=h)).json()
    assert after["retrieval"]["default_top_k"] == 9
    assert after["uploads"]["max_bytes"] == 12345
    assert after["features"]["query_cache"] is True
    row = by_key((await env.client.get("/v1/settings", headers=h)).json())["default_top_k"]
    assert (row["source"], row["value"]) == ("ui", 9)
    assert row["updated_by"].endswith("@example.test")
    assert row["updated_at"] is not None


async def test_llm_model_change_applies_to_the_next_llm_built(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key-not-a-secret-test")
    h = await admin(env)
    llm = deps.get_llm_dep()
    assert llm is not None and llm.model != "acme/model-x"  # type: ignore[attr-defined]
    assert (
        await patch(env, h, **{"set": {"default_llm_model": "acme/model-x"}})
    ).status_code == 200
    llm = deps.get_llm_dep()
    assert llm is not None and llm.model == "acme/model-x"  # type: ignore[attr-defined]


async def test_cache_ttl_change_builds_a_cache_with_the_new_ttl(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.cache import get_cache

    monkeypatch.delenv("CACHE_ENABLED")
    h = await admin(env)
    old = get_cache(Settings())
    assert (await patch(env, h, **{"set": {"cache_answer_ttl_seconds": 77}})).status_code == 200
    new = get_cache(Settings())
    assert new is not old and "77" in json.dumps(new.describe())


async def test_validation_errors_are_per_key_and_all_or_nothing(env: Env) -> None:
    h = await admin(env)
    r = await patch(env, h, **{"set": {"default_top_k": 7, "retrieval_mode": "wat", "bm25_b": 4}})
    assert r.status_code == 422
    locs = {tuple(d["loc"]) for d in r.json()["detail"]}
    assert ("settings", "retrieval_mode") in locs
    assert (
        await env.session.execute(select(AppSetting))
    ).scalars().all() == []  # default_top_k NOT saved
    # cross-field rules of the real model are enforced against the merged result
    r = await patch(env, h, **{"set": {"chunk_overlap": 5000}})
    assert r.status_code == 422
    assert Settings().chunk_overlap == 100
    assert (await patch(env, h, **{"set": {}, "reset": []})).status_code == 422


async def test_dry_run_validates_and_reports_without_saving(env: Env) -> None:
    h = await admin(env)
    r = await patch(env, h, **{"set": {"default_top_k": 3}, "dry_run": True})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert (await env.session.execute(select(AppSetting))).scalars().all() == []
    assert Settings().default_top_k == 5


async def test_reset_returns_to_the_lower_layers_and_is_audited(env: Env) -> None:
    h = await admin(env)
    await patch(env, h, **{"set": {"default_top_k": 9}})
    r = await env.client.post("/v1/settings/default_top_k/reset", headers=h)
    assert r.status_code == 200 and r.json()["reset"] == ["default_top_k"]
    assert Settings().default_top_k == 5
    audit = (await env.client.get("/v1/settings/audit", headers=h)).json()
    assert [(a["action"], a["key"]) for a in audit][:2] == [
        ("reset", "default_top_k"),
        ("set", "default_top_k"),
    ]
    reset_row, set_row = audit[0], audit[1]
    assert (set_row["old_value"], set_row["new_value"]) == (5, 9)  # old = what was in force
    assert (reset_row["old_value"], reset_row["new_value"]) == (9, 5)
    assert set_row["actor"].endswith("@example.test")


async def test_unchanged_value_writes_no_audit(env: Env) -> None:
    h = await admin(env)
    await patch(env, h, **{"set": {"default_top_k": 9}})
    r = await patch(env, h, **{"set": {"default_top_k": 9}})
    assert r.json()["unchanged"] == ["default_top_k"] and r.json()["changed"] == []
    rows = (await env.session.execute(select(SettingsAudit))).scalars().all()
    assert len(rows) == 1


async def test_restart_required_is_flagged_and_pending(env: Env) -> None:
    rt.record_boot_values()
    h = await admin(env)
    r = await patch(env, h, **{"set": {"cors_allow_origins": "https://app.example.com"}})
    assert r.json()["restart_required"] == ["cors_allow_origins"]
    body = (await env.client.get("/v1/settings", headers=h)).json()
    assert body["summary"]["restart_pending"] == ["cors_allow_origins"]
    assert by_key(body)["cors_allow_origins"]["restart_pending"] is True


async def test_credentials_in_a_non_secret_url_are_rejected(env: Env) -> None:
    h = await admin(env)
    r = await patch(env, h, **{"set": {"ollama_base_url": "http://u:hunter2@host:1"}})
    assert r.status_code == 422
    assert "hunter2" not in r.text


# ── reindex flow ─────────────────────────────────────────────────────────────────────────────────


class _FakeStore:
    def describe(self) -> dict[str, Any]:
        return {"count": 0, "problems": ["embedding dimension: stored 3, configured 1024"]}


async def test_reindex_requires_confirmation_then_applies_and_queues(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await admin(env)
    fake = env.fake  # type: ignore[attr-defined]
    r = await patch(env, h, **{"set": {"embedding_model": "other-model"}})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "reindex_confirmation_required"
    assert Settings().embedding_model == "mxbai-embed-large" and fake.queued == []
    r = await patch(env, h, **{"set": {"embedding_model": "other-model"}, "confirm_reindex": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reindex_required"] == ["embedding_model"]
    assert body["reindex"] == {
        "queued": True,
        "kinds": ["reindex"],
        "task_ids": ["task-1"],
        "error": None,
    }
    assert fake.queued == ["reindex"] and Settings().embedding_model == "other-model"
    monkeypatch.setattr("app.services.vectorstore.get_vector_store", lambda *a, **k: _FakeStore())
    fake.state = ("running", None, None)
    st = (await env.client.get("/v1/settings/reindex", headers=h)).json()
    assert (st["state"], st["needed"], st["kind"], st["task_id"]) == (
        "running",
        True,
        "reindex",
        "task-1",
    )
    assert st["problems"]
    fake.state = ("done", {"documents": 1, "vectors": 3, "seconds": 0.1}, None)
    assert (await env.client.get("/v1/settings/reindex", headers=h)).json()["result"][
        "vectors"
    ] == 3
    actions = [a["action"] for a in (await env.client.get("/v1/settings/audit", headers=h)).json()]
    assert "reindex_queued" in actions


async def test_changing_a_reindex_key_back_to_its_effective_value_needs_no_reindex(
    env: Env,
) -> None:
    h = await admin(env)
    r = await patch(
        env, h, **{"set": {"embedding_model": "mxbai-embed-large"}}
    )  # same as the default
    assert r.status_code == 200 and r.json()["reindex_required"] == []


async def test_fts_config_change_queues_the_keyword_rebuild(env: Env) -> None:
    h = await admin(env)
    r = await patch(env, h, **{"set": {"retrieval_fts_config": "simple"}, "confirm_reindex": True})
    assert r.status_code == 200 and r.json()["reindex"]["kinds"] == ["rebuild_fts"]
    assert env.fake.queued == ["rebuild_fts"]  # type: ignore[attr-defined]


async def test_a_broker_outage_keeps_the_change_and_reports_it(env: Env) -> None:
    h = await admin(env)
    env.fake.fail = True  # type: ignore[attr-defined]
    r = await patch(env, h, **{"set": {"embedding_model": "m2"}, "confirm_reindex": True})
    assert r.status_code == 200
    assert r.json()["reindex"]["queued"] is False and "retry" in r.json()["reindex"]["error"]
    assert Settings().embedding_model == "m2"
    env.fake.fail = False  # type: ignore[attr-defined]
    assert (await env.client.post("/v1/settings/reindex", headers=h, json={})).status_code == 409
    ok = await env.client.post("/v1/settings/reindex", headers=h, json={"confirm_reindex": True})
    assert ok.status_code == 200 and ok.json()["queued"] is True


# ── secrets ──────────────────────────────────────────────────────────────────────────────────────


async def test_secret_is_write_only_encrypted_and_never_leaks(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    h = await admin(env)
    seen: list[str] = []
    r = await patch(env, h, **{"set": {"openai_api_key": CANARY}})
    seen.append(r.text)
    assert r.status_code == 200, r.text
    assert Settings().openai_api_key == CANARY  # live, in memory
    seen.append((await env.client.get("/v1/settings", headers=h)).text)
    seen.append((await env.client.get("/v1/settings/audit", headers=h)).text)
    seen.append((await env.client.get("/v1/capabilities", headers=h)).text)
    row = by_key(json.loads(seen[1]))["openai_api_key"]
    assert (row["is_set"], row["value"], row["source"], row["updated_by"] is not None) == (
        True,
        None,
        "ui",
        True,
    )
    # a rejected secret write must not echo the value either
    seen.append((await patch(env, h, **{"set": {"openai_api_key": "bad\nvalue" + CANARY}})).text)
    seen.append(
        (await patch(env, h, **{"set": {"openai_api_key": CANARY, "retrieval_mode": "x"}})).text
    )
    for text_ in seen:
        assert CANARY not in text_
    # at rest: ciphertext only; the audit row carries no value
    stored = (
        await env.session.execute(select(AppSetting).where(AppSetting.key == "openai_api_key"))
    ).scalar_one()
    assert stored.value is None and stored.is_secret and stored.secret_blob
    dump = json.dumps(
        [
            dict(r._mapping)
            for r in (await env.session.execute(text("select * from app_settings"))).all()
        ]
        + [
            dict(r._mapping)
            for r in (await env.session.execute(text("select * from settings_audit"))).all()
        ],
        default=str,
    )
    assert CANARY not in dump
    aud = (
        (
            await env.session.execute(
                select(SettingsAudit).where(SettingsAudit.key == "openai_api_key")
            )
        )
        .scalars()
        .all()
    )
    assert [(a.action, a.secret, a.old_value, a.new_value) for a in aud] == [
        ("set_secret", True, None, None)
    ]
    assert CANARY not in caplog.text
    # clear
    r = await env.client.post("/v1/settings/openai_api_key/reset", headers=h)
    assert r.status_code == 200 and CANARY not in r.text
    assert Settings().openai_api_key == ""
    assert (
        (
            await env.session.execute(
                select(SettingsAudit).where(SettingsAudit.action == "clear_secret")
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.no_enc_key
async def test_without_an_encryption_key_secrets_are_disabled_but_other_settings_work(
    env: Env,
) -> None:
    assert not os.environ.get("SETTINGS_ENCRYPTION_KEY")
    h = await admin(env)
    body = (await env.client.get("/v1/settings", headers=h)).json()
    assert body["summary"]["secrets_enabled"] is False
    row = by_key(body)["openai_api_key"]
    assert row["disabled_reason"] and "SETTINGS_ENCRYPTION_KEY" in row["disabled_reason"]
    r = await patch(env, h, **{"set": {"openai_api_key": CANARY}})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "secrets_disabled"
    assert CANARY not in r.text
    assert (await env.session.execute(select(AppSetting))).scalars().all() == []
    assert (await patch(env, h, **{"set": {"default_top_k": 4}})).status_code == 200


async def test_secret_stored_under_another_key_is_reported_not_applied(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await admin(env)
    await patch(env, h, **{"set": {"openai_api_key": CANARY}})
    monkeypatch.setenv(
        "SETTINGS_ENCRYPTION_KEY", "z" * 48
    )  # the operator changed the key without rotating
    await rt.refresh(env.session, force=True)
    assert Settings().openai_api_key == ""
    body = (await env.client.get("/v1/settings", headers=h)).json()
    assert body["summary"]["undecryptable"] == ["openai_api_key"]
    assert by_key(body)["openai_api_key"]["undecryptable"] is True
    # rotation: new key primary, old key previous -> readable again
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY_PREVIOUS", ENC_KEY)
    await rt.refresh(env.session, force=True)
    assert Settings().openai_api_key == CANARY


async def test_invalid_stored_overrides_are_not_installed(env: Env) -> None:
    h = await admin(env)
    await patch(env, h, **{"set": {"default_top_k": 9}})
    env.session.add(AppSetting(key="chunk_size", value=100, is_secret=False, updated_by="x"))
    env.session.add(AppSetting(key="chunk_overlap", value=500, is_secret=False, updated_by="x"))
    await env.session.commit()
    assert await rt.refresh(env.session, force=True) is False
    assert Settings().default_top_k == 9 and Settings().chunk_overlap == 100  # last good layer kept
    body = (await env.client.get("/v1/settings", headers=h)).json()
    assert body["summary"]["overrides_error"]


# ── another process ──────────────────────────────────────────────────────────────────────────────

_PROBE = """
import asyncio, json
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.config import Settings
from app.services import runtime_settings as rt
from app.config import Settings

async def main():
    e = create_async_engine(Settings().database_url, poolclass=NullPool)
    async with async_sessionmaker(e, expire_on_commit=False)() as s:
        await rt.refresh(s)
    c = Settings()
    print(json.dumps({"top_k": c.default_top_k, "key_ok": c.openai_api_key == %r}))
    await e.dispose()
asyncio.run(main())
"""


def _other_process(url: str, env_extra: dict[str, str] | None = None) -> dict[str, Any]:
    environ = {
        **os.environ,
        "DATABASE_URL": url,
        "RAG_ENV_FILE": os.devnull,
        "SETTINGS_ENCRYPTION_KEY": ENC_KEY,
        **(env_extra or {}),
    }
    out = subprocess.run(
        [sys.executable, "-c", _PROBE % CANARY],
        cwd=BACKEND,
        env=environ,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


async def test_another_process_picks_up_changes_including_secrets(env: Env) -> None:
    url = env.url  # type: ignore[attr-defined]
    assert _other_process(url) == {"top_k": 5, "key_ok": False}
    h = await admin(env)
    await patch(env, h, **{"set": {"default_top_k": 11, "openai_api_key": CANARY}})
    assert _other_process(url) == {
        "top_k": 11,
        "key_ok": True,
    }  # a fresh worker/replica, no restart of this one
    # and an environment variable in THAT process still beats the stored value
    assert _other_process(url, {"DEFAULT_TOP_K": "2"})["top_k"] == 2


async def test_a_running_replica_refreshes_after_the_interval(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await admin(env)
    monkeypatch.setenv("SETTINGS_REFRESH_SECONDS", "0.5")
    await rt.ensure_fresh(force=True)
    # another replica writes straight to the database
    env.session.add(
        AppSetting(key="default_top_k", value=13, is_secret=False, updated_by="other-replica")
    )
    await env.session.commit()
    assert Settings().default_top_k == 5  # not yet read here
    rt._last_check = 0.0  # the interval has elapsed
    assert (await env.client.get("/v1/capabilities", headers=h)).json()["retrieval"][
        "default_top_k"
    ] == 13


# ── permissions ──────────────────────────────────────────────────────────────────────────────────


GUARDED: list[tuple[str, str, set[str], dict[str, Any] | None]] = [
    ("GET", "/v1/settings", {READ, MANAGE}, None),
    ("GET", "/v1/settings/reindex", {READ, MANAGE}, None),
    ("PATCH", "/v1/settings", {MANAGE}, {"set": {"default_top_k": 6}}),
    ("POST", "/v1/settings/default_top_k/reset", {MANAGE}, None),
    ("POST", "/v1/settings/reindex", {MANAGE}, {"confirm_reindex": True}),
    ("POST", "/v1/settings/test/storage", {MANAGE}, None),
    ("GET", "/v1/settings/audit", {MANAGE}, None),
]


@pytest.mark.parametrize(("method", "path", "allowed", "body"), GUARDED, ids=lambda v: str(v)[:40])
async def test_permission_matrix(
    env: Env, method: str, path: str, allowed: set[str], body: Any
) -> None:
    c = env.client
    assert (await c.request(method, path)).status_code == 401
    others = [p for p in ALL if p not in allowed]
    _, without = await env.make_user(others)
    assert (await c.request(method, path, headers=without, json=body)).status_code == 403
    for perm in allowed:  # each permission that is enough on its own
        _, only = await env.make_user([perm])
        r = await c.request(method, path, headers=only, json=body)
        assert r.status_code not in (401, 403), (perm, r.status_code, r.text)


async def test_read_permission_cannot_see_audit_or_write(env: Env) -> None:
    _, reader = await env.make_user([READ])
    assert (await env.client.get("/v1/settings", headers=reader)).status_code == 200
    assert (await patch(env, reader, **{"set": {"default_top_k": 2}})).status_code == 403
    assert (await env.client.get("/v1/settings/audit", headers=reader)).status_code == 403


async def test_api_keys_can_never_manage_settings(env: Env) -> None:
    assert MANAGE in KEY_FORBIDDEN and READ not in KEY_FORBIDDEN
    h = await admin(env)
    role = (
        await env.client.post(
            "/roles",
            headers=h,
            json={"name": f"svc-{uuid.uuid4().hex[:6]}", "permissions": [READ, MANAGE]},
        )
    ).json()
    key = (
        await env.client.post("/api-keys", headers=h, json={"name": "k", "role_id": role["id"]})
    ).json()["key"]
    kh = {"X-API-Key": key}
    assert (await env.client.get("/v1/settings", headers=kh)).status_code == 200
    assert (await patch(env, kh, **{"set": {"default_top_k": 2}})).status_code == 403
    assert (await env.client.get("/v1/settings/audit", headers=kh)).status_code == 403


# ── connection tests ─────────────────────────────────────────────────────────────────────────────


async def test_connection_test_storage_local_and_no_secret_in_output(env: Env) -> None:
    h = await admin(env)
    r = await env.client.post("/v1/settings/test/storage", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["provider"] == "local"


async def test_connection_test_llm_without_key_says_what_is_missing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    h = await admin(env)
    r = await env.client.post("/v1/settings/test/llm", headers=h)
    assert r.json()["ok"] is False and "no API key" in r.json()["detail"]
    miss = (await env.client.get("/v1/settings", headers=h)).json()["summary"]["missing"]
    assert any(m["area"] == "llm" for m in miss)


async def test_connection_test_llm_uses_the_saved_ui_key_and_never_returns_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import respx
    from httpx import Response

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    h = await admin(env)
    # key/models check goes to the configured endpoint with the stored key as bearer
    saved = await patch(
        env,
        h,
        **{
            "set": {
                "openrouter_api_key": CANARY,
                "openrouter_base_url": "https://or.example.test/api/v1",
            }
        },
    )
    assert saved.status_code == 200, saved.text
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://or.example.test/api/v1/key").mock(
            return_value=Response(200, json={"data": {}})
        )
        r = await env.client.post("/v1/settings/test/llm", headers=h)
        assert route.calls.last.request.headers["authorization"] == f"Bearer {CANARY}"
    assert r.json()["ok"] is True and CANARY not in r.text
    with respx.mock() as mock:
        mock.get("https://or.example.test/api/v1/key").mock(
            return_value=Response(401, text=f"bad {CANARY}")
        )
        r = await env.client.post("/v1/settings/test/llm", headers=h)
    assert r.json()["ok"] is False and "rejected" in r.json()["detail"] and CANARY not in r.text
