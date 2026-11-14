"""Semantic / keyword / hybrid retrieval on real Postgres + REAL Chroma and pgvector (both parametrised).

Text is synthetic. A deterministic concept embedder (below) makes the semantic side predictable without
a model: words from one concept group share a vector dimension, so a paraphrase that shares NO word with
the target still lands on it, while an exact identifier maps to no concept at all (the failure mode that
motivates keyword search).
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.db import ops
from app.db.models import Trace
from app.main import app
from app.routers import deps
from app.services.llm import Completion
from app.services.retriever import ChunkResult, RetrievalOptions, retrieve
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.vector_helpers import real_store

GROUPS = [
    {"equipment", "depreciation", "schedules", "leased", "vehicles"},
    {"wait", "waiting", "years", "discharge", "period", "hardship", "interval", "cooling", "long", "lapse"},
    {"office", "parking", "badge"},
    {"insurance", "deductibles", "warehouse", "fires"},
    {"cafeteria", "vegetarian", "noon"},
]
DIM = len(GROUPS) + 1


class ConceptEmbedder:
    """Vector = per-group word counts + a constant 0.01 (so no vector is zero)."""

    def embed_query(self, text: str) -> list[float]:
        words = re.findall(r"[a-z]+", text.lower())
        return [float(sum(w in g for w in words)) for g in GROUPS] + [0.01]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


class FakeLLM:
    model = "fake"

    def complete(self, system: str, user: str) -> Completion:
        return Completion("ok [1]", 1, 1)


POLICY = [
    "Section Z9-4.2-07 covers equipment depreciation schedules for leased vehicles.",
    "A borrower must wait four years after discharge before applying again; the waiting period is shorter with documented hardship.",
    "Notes on office parking and badge renewal.",
    "Section Z9-4.2-08 covers insurance deductibles for warehouse fires.",
    "Miscellaneous notes.",
]
HANDBOOK = [
    "Cafeteria opens at noon with vegetarian options.",
    "Z9-4.2-07 appendix: equipment depreciation, leased vehicles and schedules reviewed.",
]
ID_QUERY = "What does section Z9-4.2-07 cover?"
PARAPHRASE = "How long is the cooling-off interval for a client?"  # shares no word with the target chunk


def ck(r: ChunkResult) -> tuple[str, int]:
    return (r.filename, r.chunk_index)


@pytest.fixture(params=["chroma", "pgvector"])
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
async def corpus(kind: str, isolated_db_url: str, db_session: AsyncSession) -> AsyncIterator[dict[str, Any]]:
    """policy.pdf, handbook.pdf and policy-copy.pdf (a duplicate upload of policy.pdf), in Postgres + the store."""
    emb = ConceptEmbedder()
    ids: dict[str, uuid.UUID] = {}
    with real_store(kind, isolated_db_url, dimension=DIM) as store:
        for name, texts in (("policy.pdf", POLICY), ("handbook.pdf", HANDBOOK), ("policy-copy.pdf", POLICY)):
            doc = await ops.create_document(filename=name, content_type="application/pdf", session=db_session)
            ids[name] = doc.id
            await ops.create_chunks(
                document_id=doc.id,
                chunks=[{"index": i, "text": t, "metadata": {}} for i, t in enumerate(texts)],
                session=db_session,
            )
            store.add_chunks(
                str(doc.id),
                [{"text": t, "metadata": {"index": i}} for i, t in enumerate(texts)],
                emb.embed_documents(texts),
            )
        try:
            yield {"store": store, "ids": ids, "emb": emb, "session": db_session}
        finally:
            for d in ids.values():
                await ops.delete_document(d, session=db_session)


async def run(c: dict[str, Any], q: str, *, top_k: int = 5, docs: list[uuid.UUID] | None = None, **opt: Any) -> list[ChunkResult]:
    return await retrieve(
        q, top_k=top_k, embedder=c["emb"], store=c["store"], session=c["session"], document_ids=docs,
        options=RetrievalOptions(**opt),
    )


# ── the two failure modes the ticket is about ────────────────────────────────────────────
async def test_exact_id_is_missed_by_semantic_but_found_by_keyword_and_hybrid(corpus: dict[str, Any]) -> None:
    sem = await run(corpus, ID_QUERY, top_k=4, mode="semantic")
    assert ("policy.pdf", 0) not in map(ck, sem) and ("handbook.pdf", 1) not in map(ck, sem)

    bm = await run(corpus, ID_QUERY, top_k=2, mode="keyword", keyword_method="bm25")
    assert {r.text for r in bm} == {POLICY[0], HANDBOOK[1]}  # both chunks that contain the exact ID
    fts = await run(corpus, ID_QUERY, top_k=2, mode="keyword", keyword_method="postgres_fts")
    assert fts[0].text == POLICY[0]  # plain ts_rank_cd is the weaker ranker: it gets the best chunk first only
    assert {r.score_kind for r in bm} == {"bm25"} and {r.score_kind for r in fts} == {"ts_rank_cd"}

    for fusion in ("rrf", "weighted"):
        hyb = await run(corpus, ID_QUERY, top_k=2, mode="hybrid", fusion=fusion, weight_semantic=0.2, weight_keyword=0.8)
        assert hyb[0].text == POLICY[0], fusion  # in both lists and contains the exact ID
        assert fusion != "rrf" or HANDBOOK[1] in {r.text for r in await run(corpus, ID_QUERY, top_k=4, mode="hybrid")}


async def test_paraphrase_is_missed_by_keyword_but_found_by_semantic_and_hybrid(corpus: dict[str, Any]) -> None:
    assert await run(corpus, PARAPHRASE, mode="keyword") == []
    sem = await run(corpus, PARAPHRASE, top_k=1, mode="semantic")
    assert ck(sem[0]) == ("policy.pdf", 1) or ck(sem[0]) == ("policy-copy.pdf", 1)
    for fusion in ("rrf", "weighted"):
        hyb = await run(corpus, PARAPHRASE, top_k=1, mode="hybrid", fusion=fusion)
        assert ck(hyb[0])[1] == 1 and hyb[0].text == POLICY[1]
        assert hyb[0].semantic_rank == 1 and hyb[0].keyword_rank is None


async def test_hybrid_ranks_a_chunk_found_by_both_retrievers_first(corpus: dict[str, Any]) -> None:
    hyb = await run(corpus, ID_QUERY, top_k=3, mode="hybrid")
    first = hyb[0]
    assert first.text in (POLICY[0], HANDBOOK[1]) and first.keyword_rank in (1, 2) and first.semantic_rank is not None
    assert first.score_kind == "rrf"
    assert [r.score for r in hyb] == sorted((r.score for r in hyb), reverse=True)  # fused: higher = better, best first


async def test_weighted_fusion_honours_the_weights(corpus: dict[str, Any]) -> None:
    kw_heavy = await run(corpus, ID_QUERY, top_k=1, mode="hybrid", fusion="weighted", weight_semantic=0.1, weight_keyword=0.9)
    sem_heavy = await run(corpus, ID_QUERY, top_k=1, mode="hybrid", fusion="weighted", weight_semantic=0.9, weight_keyword=0.1)
    assert kw_heavy[0].text in (POLICY[0], HANDBOOK[1])  # the ID chunk (keyword evidence)
    assert sem_heavy[0].text == POLICY[4]  # the empty-vocabulary chunk the embedder likes best
    assert kw_heavy[0].score_kind == "weighted" and 0.0 <= kw_heavy[0].score <= 1.0
    # weights are normalised: (1, 9) behaves exactly like (0.1, 0.9)
    same = await run(corpus, ID_QUERY, top_k=1, mode="hybrid", fusion="weighted", weight_semantic=1, weight_keyword=9)
    assert same[0].score == pytest.approx(kw_heavy[0].score)


async def test_rrf_k_is_used(corpus: dict[str, Any]) -> None:
    a = await run(corpus, PARAPHRASE, top_k=1, mode="hybrid", rrf_k=60)
    b = await run(corpus, PARAPHRASE, top_k=1, mode="hybrid", rrf_k=5)
    assert a[0].score == pytest.approx(1 / 61) and b[0].score == pytest.approx(1 / 6)  # semantic rank 1 only


# ── document filters in every mode ───────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["semantic", "keyword", "hybrid"])
async def test_document_filter_single_and_in_in_every_mode(corpus: dict[str, Any], mode: str) -> None:
    ids = corpus["ids"]
    only_b = await run(corpus, ID_QUERY, docs=[ids["handbook.pdf"]], mode=mode)
    assert only_b and {r.filename for r in only_b} == {"handbook.pdf"}
    a_and_b = await run(corpus, ID_QUERY, docs=[ids["policy.pdf"], ids["handbook.pdf"]], mode=mode, dedupe=False)
    assert a_and_b and {r.filename for r in a_and_b} <= {"policy.pdf", "handbook.pdf"}
    assert await run(corpus, ID_QUERY, docs=[uuid.uuid4()], mode=mode) == []
    if mode != "semantic":
        only_a = await run(corpus, ID_QUERY, docs=[ids["policy.pdf"]], mode=mode, top_k=1)
        assert ck(only_a[0]) == ("policy.pdf", 0)


# ── de-duplication across duplicate documents ────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["semantic", "keyword", "hybrid"])
async def test_identical_text_from_duplicate_documents_takes_one_slot(corpus: dict[str, Any], mode: str) -> None:
    q = ID_QUERY if mode != "semantic" else PARAPHRASE
    res = await run(corpus, q, top_k=3, mode=mode)
    texts = [r.text for r in res]
    assert len(texts) == len(set(texts)) and len(res) == 3
    raw = await run(corpus, q, top_k=10, mode=mode, dedupe=False)
    assert len({r.text for r in raw}) < len(raw)  # without dedupe the copy fills a slot


async def test_duplicate_in_the_semantic_and_keyword_lists_is_fused_into_one_item(corpus: dict[str, Any]) -> None:
    res = await run(corpus, ID_QUERY, top_k=10, mode="hybrid")
    id_chunks = [r for r in res if r.text == POLICY[0]]
    assert len(id_chunks) == 1
    assert id_chunks[0].semantic_rank is not None and id_chunks[0].keyword_rank is not None


# ── documents deleted mid-flight ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["semantic", "keyword", "hybrid"])
async def test_documents_deleted_from_postgres_but_still_in_the_store_are_dropped(corpus: dict[str, Any], mode: str) -> None:
    ids = corpus["ids"]
    await ops.delete_document(ids["policy.pdf"], session=corpus["session"])
    await ops.delete_document(ids["policy-copy.pdf"], session=corpus["session"])  # vectors stay in the store
    res = await run(corpus, ID_QUERY, top_k=10, mode=mode)
    assert res and {r.filename for r in res} == {"handbook.pdf"}


async def test_hybrid_with_a_noisy_query_falls_back_to_semantic(corpus: dict[str, Any]) -> None:
    res = await run(corpus, "???", top_k=3, mode="hybrid")
    assert res and all(r.keyword_rank is None and r.semantic_rank is not None for r in res)
    assert await run(corpus, "???", mode="keyword") == []


async def test_default_options_are_plain_semantic_search(corpus: dict[str, Any]) -> None:
    res = await retrieve(
        PARAPHRASE, top_k=2, embedder=corpus["emb"], store=corpus["store"], session=corpus["session"]
    )
    assert res[0].score_kind == "distance" and res[0].text == POLICY[1]
    assert res[0].score == pytest.approx(0.0, abs=0.01) and res[0].score <= res[1].score  # distance: lower = closer


async def test_distance_scores_are_real_cosine_distances(corpus: dict[str, Any]) -> None:
    emb = corpus["emb"]
    q, d = emb.embed_query(PARAPHRASE), emb.embed_query(POLICY[1])
    cos = sum(x * y for x, y in zip(q, d, strict=True)) / (math.hypot(*q) * math.hypot(*d))
    res = await run(corpus, PARAPHRASE, top_k=1, mode="semantic")
    assert res[0].score == pytest.approx(1 - cos, abs=1e-4)


# ── the /chat API: overrides, validation, trace metadata ─────────────────────────────────
@pytest.fixture
async def chat(corpus: dict[str, Any], db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: Settings(
        default_top_k=3, auth_enabled=False, retrieval_neighbors=0, trace_capture_content=False
    )
    app.dependency_overrides[deps.get_embedder_dep] = lambda: corpus["emb"]
    app.dependency_overrides[deps.get_store] = lambda: corpus["store"]
    app.dependency_overrides[deps.get_llm_dep] = lambda: FakeLLM()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


async def last_trace_meta(session: AsyncSession) -> dict[str, Any]:
    row = (await session.execute(select(Trace).order_by(Trace.created_at.desc()).limit(1))).scalars().one()
    await session.refresh(row)
    return dict(row.meta)


async def test_chat_defaults_to_semantic_and_records_it_in_the_trace(chat: httpx.AsyncClient, db_session: AsyncSession) -> None:
    r = await chat.post("/chat", json={"question": PARAPHRASE})
    assert r.status_code == 200, r.text
    src = r.json()["sources"][0]
    assert src["score_kind"] == "distance" and src["semantic_rank"] is None
    assert (await last_trace_meta(db_session))["mode"] == "semantic"


async def test_chat_mode_override_keyword(chat: httpx.AsyncClient, db_session: AsyncSession) -> None:
    r = await chat.post("/chat", json={"question": ID_QUERY, "mode": "keyword", "top_k": 2})
    assert r.status_code == 200, r.text
    sources = r.json()["sources"]
    assert {s["score_kind"] for s in sources} == {"bm25"} and sources[0]["text"] in (POLICY[0], HANDBOOK[1])
    meta = await last_trace_meta(db_session)
    assert meta["mode"] == "keyword" and meta["keyword_method"] == "bm25" and "fusion" not in meta


async def test_chat_hybrid_with_fusion_override_exposes_ranks_and_trace(chat: httpx.AsyncClient, db_session: AsyncSession) -> None:
    r = await chat.post("/chat", json={"question": ID_QUERY, "mode": "hybrid", "fusion": "weighted", "top_k": 2})
    assert r.status_code == 200, r.text
    sources = r.json()["sources"]
    assert {s["score_kind"] for s in sources} == {"weighted"}
    assert any(s["keyword_rank"] is not None and s["semantic_rank"] is not None for s in sources)
    meta = await last_trace_meta(db_session)
    assert (meta["mode"], meta["fusion"], meta["keyword_method"]) == ("hybrid", "weighted", "bm25")
    r = await chat.post("/chat", json={"question": ID_QUERY, "mode": "hybrid", "top_k": 2})
    assert r.json()["sources"][0]["score_kind"] == "rrf"  # no override -> the deployment default (rrf)


@pytest.mark.parametrize("payload", [{"mode": "fuzzy"}, {"fusion": "borda"}, {"mode": 3}, {"mode": ""}, {"fusion": "RRF"}])
async def test_chat_rejects_invalid_overrides_with_422(chat: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    r = await chat.post("/chat", json={"question": "q", **payload})
    assert r.status_code == 422


async def test_chat_fusion_is_validated_but_ignored_outside_hybrid(chat: httpx.AsyncClient) -> None:
    r = await chat.post("/chat", json={"question": ID_QUERY, "mode": "keyword", "fusion": "weighted"})
    assert r.status_code == 200 and {s["score_kind"] for s in r.json()["sources"]} == {"bm25"}


async def test_chat_keyword_mode_with_a_mismatched_index_is_503(chat: httpx.AsyncClient, db_session: AsyncSession) -> None:
    from app.services import keyword as kw

    try:
        await kw.rebuild_fts(db_session, "simple")
        r = await chat.post("/chat", json={"question": ID_QUERY, "mode": "keyword"})
        assert r.status_code == 503 and "rebuild-fts" in r.json()["detail"]
        # semantic mode does not touch the keyword index and keeps working
        assert (await chat.post("/chat", json={"question": ID_QUERY, "mode": "semantic"})).status_code == 200
    finally:
        await kw.rebuild_fts(db_session, "english")
