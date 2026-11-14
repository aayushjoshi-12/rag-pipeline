"""POST /search against real Postgres + REAL Chroma and pgvector (both parametrised); no LLM anywhere.

Reuses the synthetic corpus of test_retrieval_modes (policy.pdf, handbook.pdf, policy-copy.pdf = duplicate).
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.db.models import Trace
from app.main import app
from app.routers import deps
from app.services import keyword as kw
from app.services.auth import accounts
from app.services.auth.permissions import BUILTIN_ROLES, Permission
from app.services.retriever import RetrievalOptions, retrieve
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# fixtures + data of the retrieval-mode suite (pytest discovers imported fixtures by name)
from tests.integration.test_retrieval_modes import (  # noqa: F401  (imported fixtures)
    HANDBOOK,
    ID_QUERY,
    PARAPHRASE,
    POLICY,
    corpus,
    kind,
)

MODES = ("semantic", "keyword", "hybrid")
BACKEND = Path(__file__).resolve().parents[2]


class ExplodingLLM:
    """Any use of the LLM during /search is a bug."""

    model = "boom"

    def complete(self, system: str, user: str) -> Any:
        raise AssertionError("the LLM must not be called by /search")


class ChatLLM:
    model = "fake"

    def complete(self, system: str, user: str) -> Any:
        from app.services.llm import Completion

        return Completion("ok [1]", 1, 1)


@pytest.fixture
async def api(corpus: dict[str, Any], db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    llm_calls: list[str] = []

    def llm_dep() -> ExplodingLLM:
        llm_calls.append("resolved")
        return ExplodingLLM()

    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: Settings(
        default_top_k=3, auth_enabled=False, retrieval_neighbors=0, trace_capture_content=False
    )
    app.dependency_overrides[deps.get_embedder_dep] = lambda: corpus["emb"]
    app.dependency_overrides[deps.get_store] = lambda: corpus["store"]
    app.dependency_overrides[deps.get_llm_dep] = llm_dep
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c
    finally:
        app.dependency_overrides.clear()
    assert llm_calls == [], "/search resolved the LLM dependency"


def keys(results: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(r["filename"], r["chunk_index"]) for r in results]


# ── ranking == the retriever's / /chat's ranking ─────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("query", [ID_QUERY, PARAPHRASE])
async def test_search_equals_the_retriever_and_chat_order(
    api: httpx.AsyncClient, corpus: dict[str, Any], mode: str, query: str  # noqa: F811
) -> None:
    r = await api.post("/search", json={"query": query, "mode": mode, "top_k": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    direct = await retrieve(
        query, top_k=4, embedder=corpus["emb"], store=corpus["store"], session=corpus["session"],
        options=RetrievalOptions(mode=mode),
    )
    assert keys(body["results"]) == [(d.filename, d.chunk_index) for d in direct]
    assert [x["score"] for x in body["results"]] == pytest.approx([d.score for d in direct])
    assert [x["rank"] for x in body["results"]] == list(range(1, len(direct) + 1))
    assert body["returned"] == len(direct) and body["mode"] == mode and body["query"] == query
    assert body["fusion"] == ("rrf" if mode == "hybrid" else None)
    assert body["took_ms"] >= 0 and body["top_k"] == 4 and body["neighbors"] == 0
    # /chat uses the same path: same passages in the same order
    app.dependency_overrides[deps.get_llm_dep] = lambda: ChatLLM()
    chat = await api.post("/chat", json={"question": query, "mode": mode, "top_k": 4, "neighbors": 0})
    assert chat.status_code == 200, chat.text
    assert keys(chat.json()["sources"]) == keys(body["results"])
    assert [s["rank"] for s in chat.json()["sources"]] == [x["rank"] for x in body["results"]]


@pytest.mark.parametrize(("mode", "score_kind"), [("semantic", "distance"), ("keyword", "bm25"), ("hybrid", "rrf")])
async def test_each_mode_reports_its_score_kind_and_ranks(api: httpx.AsyncClient, mode: str, score_kind: str) -> None:
    body = (await api.post("/search", json={"query": ID_QUERY, "mode": mode, "top_k": 3})).json()
    assert {x["score_kind"] for x in body["results"]} == {score_kind}
    scores = [x["score"] for x in body["results"]]
    assert scores == sorted(scores, reverse=(mode != "semantic"))  # best first
    if mode == "hybrid":
        assert any(x["semantic_rank"] and x["keyword_rank"] for x in body["results"])
    else:
        assert all(x["semantic_rank"] is None and x["keyword_rank"] is None for x in body["results"])


async def test_default_mode_comes_from_settings_and_fusion_override(api: httpx.AsyncClient) -> None:
    body = (await api.post("/search", json={"query": ID_QUERY})).json()
    assert body["mode"] == "semantic" and body["top_k"] == 3 and body["returned"] <= 3
    body = (await api.post("/search", json={"query": ID_QUERY, "mode": "hybrid", "fusion": "weighted"})).json()
    assert (body["fusion"], {x["score_kind"] for x in body["results"]}) == ("weighted", {"weighted"})
    body = (await api.post("/search", json={"query": ID_QUERY, "mode": "keyword", "fusion": "weighted"})).json()
    assert body["fusion"] is None and {x["score_kind"] for x in body["results"]} == {"bm25"}


# ── filters, include_text ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", MODES)
async def test_document_filter_restricts_every_mode(api: httpx.AsyncClient, corpus: dict[str, Any], mode: str) -> None:  # noqa: F811
    hb = str(corpus["ids"]["handbook.pdf"])
    body = (await api.post("/search", json={"query": ID_QUERY, "mode": mode, "document_ids": [hb]})).json()
    assert body["results"] and {x["document_id"] for x in body["results"]} == {hb}
    both = [hb, str(corpus["ids"]["policy.pdf"])]
    body = (await api.post("/search", json={"query": ID_QUERY, "mode": mode, "document_ids": both, "top_k": 10})).json()
    assert {x["document_id"] for x in body["results"]} <= set(both)
    none = (await api.post("/search", json={"query": ID_QUERY, "mode": mode, "document_ids": [str(uuid.uuid4())]})).json()
    assert none["results"] == [] and none["returned"] == 0


async def test_include_text_false_returns_ids_and_scores_only(api: httpx.AsyncClient) -> None:
    full = (await api.post("/search", json={"query": ID_QUERY, "mode": "hybrid", "neighbors": 1})).json()
    lean = (await api.post("/search", json={"query": ID_QUERY, "mode": "hybrid", "neighbors": 1, "include_text": False})).json()
    assert all(x["text"] for x in full["results"])
    assert all(x["text"] is None for x in lean["results"])
    assert all(c["text"] is None for x in lean["results"] for c in x["context"])
    strip = lambda b: [{k: v for k, v in x.items() if k not in ("text", "context")} for x in b["results"]]  # noqa: E731
    assert strip(lean) == strip(full)  # same ranking and scores
    assert [[c["chunk_index"] for c in x["context"]] for x in lean["results"]] == [
        [c["chunk_index"] for c in x["context"]] for x in full["results"]
    ]


async def test_duplicate_documents_take_one_slot(api: httpx.AsyncClient) -> None:
    body = (await api.post("/search", json={"query": ID_QUERY, "mode": "semantic", "top_k": 10})).json()
    texts = [" ".join(x["text"].split()).casefold() for x in body["results"]]
    assert len(texts) == len(set(texts))


# ── neighbours ───────────────────────────────────────────────────────────────────────────────────────
async def test_neighbours_are_grouped_under_their_hit_unscored_in_document_order(
    api: httpx.AsyncClient, corpus: dict[str, Any]  # noqa: F811
) -> None:
    pol = str(corpus["ids"]["policy.pdf"])
    body = (await api.post("/search", json={"query": ID_QUERY, "mode": "keyword", "top_k": 1, "neighbors": 1, "document_ids": [pol]})).json()
    (hit,) = body["results"]
    assert hit["chunk_index"] == 0 and hit["text"] == POLICY[0]
    assert [c["chunk_index"] for c in hit["context"]] == [1] and hit["context"][0]["text"] == POLICY[1]
    assert set(hit["context"][0]) == {"chunk_index", "text"}  # no score / rank on context
    mid = (await api.post("/search", json={"query": "insurance deductibles warehouse fires", "mode": "keyword", "top_k": 1, "neighbors": 2, "document_ids": [pol]})).json()["results"][0]
    assert mid["chunk_index"] == 3
    assert [c["chunk_index"] for c in mid["context"]] == [1, 2, 4]  # nearest 2 each side, document order
    assert [c["text"] for c in mid["context"]] == [POLICY[1], POLICY[2], POLICY[4]]


async def test_results_never_contain_context_chunks_and_default_is_no_context(api: httpx.AsyncClient) -> None:
    body = (await api.post("/search", json={"query": ID_QUERY, "top_k": 5, "neighbors": 2})).json()
    assert all(x["is_hit"] is True for x in body["results"])
    assert [x["rank"] for x in body["results"]] == list(range(1, len(body["results"]) + 1))
    plain = (await api.post("/search", json={"query": ID_QUERY, "top_k": 5})).json()
    assert all(x["context"] == [] for x in plain["results"]) and plain["neighbors"] == 0
    # neighbour expansion never changes the hits themselves
    assert keys(body["results"]) == keys(plain["results"])


async def test_chat_context_chunks_are_unscored_and_point_at_their_hit(
    api: httpx.AsyncClient, corpus: dict[str, Any]  # noqa: F811
) -> None:
    app.dependency_overrides[deps.get_llm_dep] = lambda: ChatLLM()
    pol = str(corpus["ids"]["policy.pdf"])
    r = await api.post("/chat", json={"question": "insurance deductibles warehouse fires", "mode": "keyword", "top_k": 1, "neighbors": 1, "document_ids": [pol]})
    src = r.json()["sources"]
    assert [(s["chunk_index"], s["is_hit"]) for s in src] == [(2, False), (3, True), (4, False)]
    hit = next(s for s in src if s["is_hit"])
    assert hit["rank"] == 1 and hit["score"] is not None and hit["context_for"] is None
    for s in src:
        if not s["is_hit"]:
            assert s["score"] is None and s["rank"] is None
            assert s["semantic_rank"] is None and s["keyword_rank"] is None
            assert s["context_for"] == {"document_id": pol, "chunk_index": 3}


async def test_chat_hybrid_context_chunk_does_not_borrow_the_hits_ranks(
    api: httpx.AsyncClient, corpus: dict[str, Any]  # noqa: F811
) -> None:
    app.dependency_overrides[deps.get_llm_dep] = lambda: ChatLLM()
    r = await api.post("/chat", json={"question": ID_QUERY, "mode": "hybrid", "top_k": 2, "neighbors": 1})
    src = r.json()["sources"]
    hits = [s for s in src if s["is_hit"]]
    assert [h["rank"] for h in hits] == [1, 2] and hits[0]["score"] >= hits[1]["score"]  # hits stay best first
    assert any(not s["is_hit"] for s in src)
    assert all(s["score"] is None and s["semantic_rank"] is None and s["keyword_rank"] is None for s in src if not s["is_hit"])


# ── validation (422 table) ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "x" * 4001},
        {"query": 5},
        {"query": "q", "top_k": 0},
        {"query": "q", "top_k": 51},
        {"query": "q", "top_k": "five"},
        {"query": "q", "mode": "fuzzy"},
        {"query": "q", "mode": "SEMANTIC"},
        {"query": "q", "fusion": "borda"},
        {"query": "q", "neighbors": -1},
        {"query": "q", "neighbors": 6},
        {"query": "q", "document_ids": ["nope"]},
        {"query": "q", "document_ids": "abc"},
        {"query": "q", "include_text": "maybe"},
    ],
)
async def test_validation_errors_are_422_and_traceless(api: httpx.AsyncClient, db_session: AsyncSession, payload: dict[str, Any]) -> None:
    before = len(await _search_traces(db_session))
    assert (await api.post("/search", json=payload)).status_code == 422
    assert len(await _search_traces(db_session)) == before


@pytest.mark.parametrize("payload", [{"query": "q", "top_k": 1}, {"query": "q", "top_k": 50}, {"query": "x" * 4000, "neighbors": 5}, {"query": "q", "neighbors": 0}])
async def test_boundary_values_are_accepted(api: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    assert (await api.post("/search", json=payload)).status_code == 200


# ── errors, trace ───────────────────────────────────────────────────────────────────────────────────
async def _search_traces(session: AsyncSession) -> list[Trace]:
    rows = (await session.execute(select(Trace).where(Trace.kind == "search").order_by(Trace.created_at.desc()))).scalars().all()
    for r in rows:
        await session.refresh(r)
    return list(rows)


async def test_a_search_records_a_trace_with_meta_and_no_question_by_default(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    before = len(await _search_traces(db_session))
    r = await api.post("/search", json={"query": ID_QUERY, "mode": "hybrid", "fusion": "weighted", "top_k": 2, "neighbors": 1})
    assert r.status_code == 200
    rows = await _search_traces(db_session)
    assert len(rows) == before + 1
    t = rows[0]
    assert (t.kind, t.status, t.hits, t.model, t.prompt_tokens, t.completion_tokens) == ("search", "ok", 2, None, None, None)
    assert t.question is None and t.error is None and t.user_email is None
    assert t.meta == {
        "top_k": 2, "neighbors": 1, "document_filter": 0, "include_text": True,
        "mode": "hybrid", "keyword_method": "bm25", "fusion": "weighted", "candidates": 50,
    }


async def test_trace_stores_the_query_only_with_content_capture(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    app.dependency_overrides[deps.get_settings] = lambda: Settings(default_top_k=3, auth_enabled=False, trace_capture_content=True)
    await api.post("/search", json={"query": "secret question text"})
    assert (await _search_traces(db_session))[0].question == "secret question text"


async def test_backend_failure_is_503_and_traced_as_error(api: httpx.AsyncClient, db_session: AsyncSession, corpus: dict[str, Any]) -> None:  # noqa: F811
    class Boom:
        def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise ConnectionError("store down")

    app.dependency_overrides[deps.get_store] = lambda: Boom()
    r = await api.post("/search", json={"query": "q"})
    assert r.status_code == 503 and r.json()["detail"] == "Retrieval backend unavailable."
    t = (await _search_traces(db_session))[0]
    assert t.status == "error" and "unavailable" in (t.error or "") and t.hits is None


async def test_keyword_index_mismatch_is_503_with_the_fix(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise kw.KeywordIndexMismatchError("chunk FTS index does not match; run rebuild-fts")

    original = kw.keyword_search
    kw.keyword_search = boom  # type: ignore[assignment]  # simulate the mismatch the real check raises
    try:
        r = await api.post("/search", json={"query": "q", "mode": "keyword"})
    finally:
        kw.keyword_search = original
    assert r.status_code == 503 and "rebuild-fts" in r.json()["detail"]
    # identical to /chat's behaviour for the same failure
    kw.keyword_search = boom  # type: ignore[assignment]
    try:
        app.dependency_overrides[deps.get_llm_dep] = lambda: ChatLLM()
        c = await api.post("/chat", json={"question": "q", "mode": "keyword"})
    finally:
        kw.keyword_search = original
    assert c.status_code == 503 and c.json()["detail"] == r.json()["detail"]


async def test_works_when_no_llm_is_configured(api: httpx.AsyncClient) -> None:
    app.dependency_overrides[deps.get_llm_dep] = lambda: None
    assert (await api.post("/search", json={"query": ID_QUERY})).status_code == 200


# ── permission: search:use ───────────────────────────────────────────────────────────────────────────
def test_builtin_roles_user_and_viewer_hold_search_use_and_admin_all() -> None:
    assert Permission.SEARCH_USE in BUILTIN_ROLES["user"][1]
    assert Permission.SEARCH_USE in BUILTIN_ROLES["viewer"][1]
    assert BUILTIN_ROLES["admin"][1] == frozenset(Permission)


async def test_search_permission_over_http(isolated_session: AsyncSession, tmp_path: Path) -> None:
    from tests.integration.test_auth_api import PASSWORD, SECRET

    await accounts.seed(isolated_session)
    cfg = Settings(secret_key=SECRET, auth_enabled=True, data_dir=tmp_path, default_top_k=3)

    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield isolated_session

    class EmptyStore:
        def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return []

    class Emb:
        def embed_query(self, t: str) -> list[float]:
            return [1.0]

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: cfg
    app.dependency_overrides[deps.get_store] = lambda: EmptyStore()
    app.dependency_overrides[deps.get_embedder_dep] = lambda: Emb()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            async def login(perms: list[str]) -> dict[str, str]:
                role = await accounts.create_role(isolated_session, name=f"r-{uuid.uuid4().hex[:8]}", description="", permissions=perms)
                email = f"{uuid.uuid4().hex[:10]}@example.test"
                await accounts.create_user(isolated_session, email=email, password=PASSWORD, role_ids=[role.id])
                tok = (await c.post("/auth/login", json={"email": email, "password": PASSWORD})).json()["access_token"]
                return {"Authorization": f"Bearer {tok}"}

            body = {"query": "q"}
            assert (await c.post("/search", json=body)).status_code == 401
            assert (await c.post("/search", json=body, headers={"Authorization": "Bearer junk"})).status_code == 401
            others = [p.value for p in Permission if p is not Permission.SEARCH_USE]
            r = await c.post("/search", json=body, headers=await login(others))  # chat:use alone is not enough
            assert r.status_code == 403 and "search:use" in r.json()["detail"]
            assert (await c.post("/search", json=body, headers=await login(["search:use"]))).status_code == 200
            # the seeded built-in roles
            for role_name in ("admin", "user", "viewer"):
                role = await accounts.get_role_by_name(isolated_session, role_name)
                assert role is not None
                email = f"{role_name}-{uuid.uuid4().hex[:6]}@example.test"
                await accounts.create_user(isolated_session, email=email, password=PASSWORD, role_ids=[role.id])
                tok = (await c.post("/auth/login", json={"email": email, "password": PASSWORD})).json()["access_token"]
                assert (await c.post("/search", json=body, headers={"Authorization": f"Bearer {tok}"})).status_code == 200, role_name
    finally:
        app.dependency_overrides.clear()


async def test_migration_0007_grants_search_use_to_existing_builtin_roles_only(
    isolated_db_url: str, isolated_session: AsyncSession
) -> None:
    """Simulate an install from before this ticket: built-in roles without search:use."""
    import os

    await accounts.seed(isolated_session)
    custom = await accounts.create_role(isolated_session, name="user2", description="", permissions=["chat:use"])
    await isolated_session.execute(text("DELETE FROM role_permissions WHERE permission = 'search:use'"))
    await isolated_session.commit()
    env = {**os.environ, "DATABASE_URL": isolated_db_url, "RAG_ENV_FILE": os.devnull}

    def alembic(*args: str) -> None:
        res = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    alembic("downgrade", "0006")  # one step back: just before the search:use migration
    alembic("upgrade", "head")
    alembic("upgrade", "head")  # idempotent

    async def perms(name: str) -> set[str]:
        role = await accounts.get_role_by_name(isolated_session, name)
        assert role is not None
        await isolated_session.refresh(role)
        return {p.permission for p in role.permissions}

    assert "search:use" in await perms("user") and "search:use" in await perms("viewer")
    assert "search:use" not in await perms("admin")  # admin is re-synced by seed(), not by the migration
    assert "search:use" not in await perms(custom.name)  # custom roles are never touched
    await accounts.seed(isolated_session)
    assert "search:use" in await perms("admin")
    # an admin who removes it from a built-in role keeps it removed across restarts (seed never re-adds)
    user = await accounts.get_role_by_name(isolated_session, "user")
    assert user is not None
    await accounts.update_role(isolated_session, user, permissions=[p for p in await perms("user") if p != "search:use"])
    await accounts.seed(isolated_session)
    assert "search:use" not in await perms("user")


async def test_traces_and_usage_filters_accept_search(api: httpx.AsyncClient) -> None:
    await api.post("/search", json={"query": ID_QUERY})
    page = (await api.get("/traces", params={"kind": "search"})).json()
    assert page["total"] >= 1 and {t["kind"] for t in page["items"]} == {"search"}
    assert (await api.get("/usage", params={"kind": "search"})).status_code == 200
    assert (await api.get("/traces", params={"kind": "nope"})).status_code == 422
