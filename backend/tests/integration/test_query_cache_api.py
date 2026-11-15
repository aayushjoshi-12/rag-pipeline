"""Query cache through the real API, real Postgres + real Chroma/pgvector + REAL Redis (db 13). No real LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import redis as redis_lib
from app.config import Settings
from app.db import ops
from app.main import app
from app.routers import deps
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeLLM, api_client
from tests.integration.test_retrieval_modes import ID_QUERY, PARAPHRASE, corpus, kind  # noqa: F401

URL = "redis://localhost:6379/13"
MODES = ("semantic", "keyword", "hybrid")


def _redis_up() -> bool:
    try:
        return bool(redis_lib.Redis.from_url(URL, socket_connect_timeout=0.5).ping())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_up(), reason="no Redis on localhost:6379")


@pytest.fixture
async def ctx(
    corpus: dict[str, Any],  # noqa: F811 (fixture imported above)
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[dict[str, Any]]:
    # the invalidation hooks (ops.*, ingest) read the process-wide settings: point them at the same Redis db
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("CACHE_REDIS_URL", URL)
    monkeypatch.setattr("app.services.cache._process_settings", None)
    redis_lib.Redis.from_url(URL).flushdb()
    llm = FakeLLM("answer {n} [1]", usage=(3, 2), cost=None)
    settings = Settings(
        default_top_k=3,
        auth_enabled=False,
        retrieval_neighbors=0,
        trace_capture_content=False,
        cache_enabled=True,
        cache_redis_url=URL,
        cache_timeout_ms=2000,  # a loaded CI/dev machine must not trip the 100 ms production deadline
    )

    async with api_client(
        db_session,
        settings=lambda: settings,
        embedder_dep=lambda: corpus["emb"],
        store=lambda: corpus["store"],
        llm_dep=lambda: llm,
    ) as c:
        try:
            yield {"c": c, "llm": llm, "corpus": corpus, "settings": settings}
        finally:
            redis_lib.Redis.from_url(URL).flushdb()


def strip(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in ("took_ms", "cache")}


@pytest.mark.parametrize("mode", MODES)
async def test_cached_search_equals_uncached_and_reports_hit(
    ctx: dict[str, Any], mode: str
) -> None:
    c = ctx["c"]
    req = {"query": ID_QUERY, "mode": mode, "top_k": 4, "neighbors": 1}
    first = await c.post("/search", json=req)
    second = await c.post("/search", json=req)
    off = await c.post("/search", json={**req, "cache": False})
    assert (first.headers["x-cache"], second.headers["x-cache"], off.headers["x-cache"]) == (
        "miss",
        "hit",
        "bypass",
    )
    assert strip(first.json()) == strip(second.json()) == strip(off.json())
    assert second.json()["cache"] == "hit"


async def test_whitespace_variants_share_an_entry_and_cache_control_bypasses(
    ctx: dict[str, Any],
) -> None:
    c = ctx["c"]
    await c.post("/search", json={"query": ID_QUERY})
    r = await c.post("/search", json={"query": f"  {ID_QUERY}  "})
    assert r.headers["x-cache"] == "hit"
    r = await c.post("/search", json={"query": ID_QUERY}, headers={"Cache-Control": "no-cache"})
    assert r.headers["x-cache"] == "bypass"


async def test_embedding_layer_serves_a_different_search_over_the_same_query(
    ctx: dict[str, Any],
) -> None:
    c = ctx["c"]
    await c.post("/search", json={"query": PARAPHRASE, "top_k": 2})
    r = await c.post("/search", json={"query": PARAPHRASE, "top_k": 3})  # new L2 key, same L1 key
    assert r.headers["x-cache"] == "miss"
    assert "embedding=hit" in r.headers["x-cache-layers"]


async def test_chat_answer_cached_and_llm_called_once(ctx: dict[str, Any]) -> None:
    c, llm = ctx["c"], ctx["llm"]
    req = {"question": ID_QUERY, "mode": "hybrid", "top_k": 3, "neighbors": 0}
    a = await c.post("/chat", json=req)
    b = await c.post("/chat", json=req)
    assert (a.headers["x-cache"], b.headers["x-cache"]) == ("miss", "hit")
    assert a.json()["answer"] == b.json()["answer"] and llm.calls == 1
    d = await c.post("/chat", json={**req, "cache": False})
    assert d.headers["x-cache"] == "bypass" and llm.calls == 2


async def test_answer_is_never_reused_for_a_different_context(ctx: dict[str, Any]) -> None:
    c, llm = ctx["c"], ctx["llm"]
    base = {"question": ID_QUERY, "mode": "hybrid", "neighbors": 0}
    await c.post("/chat", json={**base, "top_k": 1})
    r = await c.post(
        "/chat", json={**base, "top_k": 3}
    )  # same question, different retrieved context
    assert r.headers["x-cache"] == "miss" and llm.calls == 2


@pytest.mark.parametrize("mode", MODES)
async def test_deleting_a_document_invalidates_cached_results_immediately(
    ctx: dict[str, Any], mode: str
) -> None:
    c, corp = ctx["c"], ctx["corpus"]
    req = {"query": ID_QUERY, "mode": mode, "top_k": 10}
    warm = (await c.post("/search", json=req)).json()
    hb = str(corp["ids"]["handbook.pdf"])
    assert hb in {x["document_id"] for x in warm["results"]}
    assert (await c.post("/search", json=req)).headers["x-cache"] == "hit"
    corp["store"].delete_doc(hb)
    assert await ops.delete_document(corp["ids"]["handbook.pdf"], session=corp["session"])
    after = await c.post("/search", json=req)
    assert after.headers["x-cache"] == "miss"  # the version moved: no stale entry is reachable
    assert hb not in {x["document_id"] for x in after.json()["results"]}


async def test_fail_open_when_redis_is_down(ctx: dict[str, Any]) -> None:
    app.dependency_overrides[deps.get_settings] = lambda: Settings(
        default_top_k=3,
        auth_enabled=False,
        retrieval_neighbors=0,
        cache_enabled=True,
        cache_redis_url="redis://localhost:1/12",
        cache_timeout_ms=50,
    )
    c = ctx["c"]
    r = await c.post("/search", json={"query": ID_QUERY})
    assert r.status_code == 200 and r.json()["results"]
    assert r.headers["x-cache"] == "miss"
    chat = await c.post("/chat", json={"question": ID_QUERY, "top_k": 2})
    assert chat.status_code == 200


async def test_cache_disabled_reports_off(ctx: dict[str, Any]) -> None:
    app.dependency_overrides[deps.get_settings] = lambda: Settings(
        default_top_k=3,
        auth_enabled=False,
        retrieval_neighbors=0,
        cache_redis_url=URL,
        cache_enabled=False,
    )
    r = await ctx["c"].post("/search", json={"query": ID_QUERY})
    assert r.headers["x-cache"] == "off"


async def test_stats_and_purge_endpoints(ctx: dict[str, Any]) -> None:
    c = ctx["c"]
    await c.post("/search", json={"query": ID_QUERY})
    await c.post("/search", json={"query": ID_QUERY})
    stats = (await c.get("/cache/stats")).json()
    assert stats["redis"]["reachable"] and stats["redis"]["keys"]["retrieval"] >= 1
    assert stats["process"]["layers"]["retrieval"]["hit"] >= 1
    assert stats["traces"]["search"]["hits"] >= 1
    purged = (await c.post("/cache/purge")).json()
    assert purged["purged_keys"] >= 1 and purged["index_version_bumped"]
    assert (await c.post("/search", json={"query": ID_QUERY})).headers["x-cache"] == "miss"


async def test_retrieval_bypass_is_counted_in_cache_stats(ctx: dict[str, Any]) -> None:
    c = ctx["c"]
    before = (await c.get("/cache/stats")).json()["process"]["layers"]["retrieval"]["bypass"]
    await c.post("/search", json={"query": ID_QUERY, "cache": False})
    after = (await c.get("/cache/stats")).json()["process"]["layers"]["retrieval"]["bypass"]
    assert after == before + 1
