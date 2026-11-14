"""Keyword search on real Postgres: tokenisation of IDs/numbers, true BM25 values, noisy/odd queries,
filters, config drift. All text is synthetic. The module's isolated database is shared by its tests, so
every test deletes the documents it created (corpus statistics must not leak between tests)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.db import ops
from app.services import keyword as kw
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def make_doc(db_session: AsyncSession) -> AsyncIterator[Any]:
    created: list[uuid.UUID] = []

    async def make(name: str, texts: list[str]) -> uuid.UUID:
        doc = await ops.create_document(filename=name, content_type="text/plain", session=db_session)
        created.append(doc.id)
        await ops.create_chunks(
            document_id=doc.id,
            chunks=[{"index": i, "text": t, "metadata": {}} for i, t in enumerate(texts)],
            session=db_session,
        )
        return doc.id

    yield make
    for d in created:
        await ops.delete_document(d, session=db_session)


async def search(session: AsyncSession, q: str, **kw_args: Any) -> list[kw.KeywordHit]:
    kw_args.setdefault("limit", 10)
    return await kw.keyword_search(session, q, **kw_args)


def where(hits: list[kw.KeywordHit]) -> list[tuple[str, int]]:
    return [(h.filename, h.index) for h in hits]


# ── tokenisation: IDs and numbers must stay searchable ───────────────────────────────────
@pytest.mark.parametrize("config", ["simple", "english"])
async def test_section_ids_and_numbers_survive_tokenisation(db_session: AsyncSession, config: str) -> None:
    lex = await kw.query_lexemes(db_session, "What does section B3-5.3-05 cover? 12 months, $1,500.00 on 2025-11-05", config)
    # Postgres splits the ID into 'b3', '-5.3', '-05' (sign attached to the number part); all three are kept
    assert {"b3", "-5.3", "-05"} <= set(lex)
    assert {"12", "500.00", "2025"} <= set(lex)
    assert "months" in lex or "month" in lex


async def test_english_drops_stopwords_and_stems_simple_does_neither(db_session: AsyncSession) -> None:
    eng = await kw.query_lexemes(db_session, "What does the waiting period cover", "english")
    sim = await kw.query_lexemes(db_session, "What does the waiting period cover", "simple")
    assert "what" not in eng and "the" not in eng and "wait" in eng
    assert {"what", "the", "waiting"} <= set(sim)


async def test_an_id_query_finds_exactly_the_chunk_with_that_id(db_session: AsyncSession, make_doc: Any) -> None:
    await make_doc("rules.txt", [
        "Section B3-5.3-04 covers inquiries.",
        "Section B3-5.3-05 covers authorized user tradelines and their treatment.",
        "Section B3-4.2-05 covers collections.",
        "Unrelated text about parking.",
    ])
    for method in ("bm25", "postgres_fts"):
        hits = await search(db_session, "What does section B3-5.3-05 cover?", method=method)
        assert where(hits)[0] == ("rules.txt", 1), method
    # the partial-ID decoys match only some tokens and rank below the exact chunk
    assert {h.index for h in hits} >= {0, 1, 2}


# ── BM25: exact values from SQL-computed corpus statistics ───────────────────────────────
@pytest.fixture
async def bm25_corpus(make_doc: Any) -> None:
    # token lengths 2, 6, 1 -> N=3, avgdl=3 ; 'alpha' df=2, 'gamma' df=2 -> idf = ln(1.6) = 0.4700036
    await make_doc("bm25.txt", ["alpha beta", "alpha alpha gamma gamma gamma delta", "gamma"])


async def test_bm25_scores_match_hand_computation(db_session: AsyncSession, bm25_corpus: None) -> None:
    hits = await search(db_session, "alpha", k1=1.2, b=0.75)
    scores = {h.index: h.score for h in hits}
    # c0: tf=1, dl=2 -> 2.2 / (1 + 1.2 (0.25 + 0.75*2/3)) = 1.157895 * idf = 0.544215
    # c1: tf=2, dl=6 -> 4.4 / (2 + 1.2 (0.25 + 0.75*2)) = 1.073171 * idf = 0.504394 (length norm beats tf)
    assert scores[0] == pytest.approx(0.544215, abs=1e-5)
    assert scores[1] == pytest.approx(0.504394, abs=1e-5)
    assert [h.index for h in hits] == [0, 1]


async def test_bm25_b_and_k1_are_applied(db_session: AsyncSession, bm25_corpus: None) -> None:
    no_len = await search(db_session, "alpha", k1=1.2, b=0.0)
    s = {h.index: h.score for h in no_len}
    assert s[0] == pytest.approx(0.4700036, abs=1e-5)  # 2.2/2.2 * idf
    assert s[1] == pytest.approx(0.646255, abs=1e-5)  # 4.4/3.2 * idf
    assert [h.index for h in no_len] == [1, 0]  # without length normalisation the higher tf wins
    flat = {h.index: h.score for h in await search(db_session, "alpha", k1=0.0, b=0.75)}
    assert flat[0] == pytest.approx(0.4700036, abs=1e-5) and flat[1] == pytest.approx(0.4700036, abs=1e-5)


async def test_bm25_sums_over_query_terms(db_session: AsyncSession, bm25_corpus: None) -> None:
    hits = await search(db_session, "alpha gamma")
    s = {h.index: h.score for h in hits}
    # c1 = 0.504394 (alpha) + 0.608240 (gamma tf=3: 6.6/5.1 * idf) ; c2 = 1.375 * idf ; c0 = alpha only
    assert s[1] == pytest.approx(1.112634, abs=1e-5)
    assert s[2] == pytest.approx(0.646255, abs=1e-5)
    assert s[0] == pytest.approx(0.544215, abs=1e-5)
    assert [h.index for h in hits] == [1, 2, 0]


async def test_corpus_statistics_cover_all_chunks_not_only_the_filtered_documents(
    db_session: AsyncSession, bm25_corpus: None, make_doc: Any
) -> None:
    other = await make_doc("other.txt", ["alpha one two three four"])
    only = await search(db_session, "alpha", document_ids=[other])
    both = await search(db_session, "alpha")
    # N=4, avgdl=(2+6+1+5)/4=3.5, df(alpha)=3: the score of other.txt does not depend on the filter
    mine = next(h.score for h in both if h.filename == "other.txt")
    assert only[0].score == pytest.approx(mine)


async def test_postgres_fts_method_returns_ts_rank_cd(db_session: AsyncSession, bm25_corpus: None) -> None:
    hits = await search(db_session, "alpha gamma", method="postgres_fts")
    assert hits and all(h.score > 0 for h in hits)
    assert hits[0].index == 1  # the chunk with both terms ranks first by cover density too


async def test_pool_limits_candidates_before_rescoring(db_session: AsyncSession, bm25_corpus: None) -> None:
    # 'gamma': BM25 prefers c2 (short, tf=1: 1.375 idf) over c1 (tf=3: 1.294 idf), but ts_rank_cd ranks c1 first
    # (0.154 vs 0.144). With a candidate pool of 1 only the ts_rank_cd winner is re-scored: the documented approximation.
    assert [h.index for h in await search(db_session, "gamma", limit=1, pool=1)] == [1]
    assert [h.index for h in await search(db_session, "gamma", limit=1, pool=10)] == [2]
    assert [h.index for h in await search(db_session, "gamma", limit=3, pool=1)] == [2, 1]  # pool is raised to limit


# ── odd queries ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("q", ["", "   ", "???", "!!! @@@ ###", "the of and to a", "\n\t"])
async def test_empty_or_noisy_queries_return_nothing_without_error(
    db_session: AsyncSession, bm25_corpus: None, q: str
) -> None:
    for method in ("bm25", "postgres_fts"):
        assert await search(db_session, q, method=method) == []


async def test_tsquery_syntax_in_the_question_is_just_text(db_session: AsyncSession, bm25_corpus: None) -> None:
    q = "alpha & (beta | !gamma):* ' \\ ) ( <-> 'quoted' O'Brien"
    hits = await search(db_session, q)
    assert hits and hits[0].index in (0, 1)


async def test_terms_absent_from_the_corpus_match_nothing(db_session: AsyncSession, bm25_corpus: None) -> None:
    assert await search(db_session, "zzzxqj plugh") == []
    assert [h.index for h in await search(db_session, "zzzxqj alpha")] == [0, 1]


async def test_very_long_queries_are_capped_not_failed(db_session: AsyncSession, bm25_corpus: None) -> None:
    long_q = " ".join(f"w{i}x" for i in range(3000)) + " alpha"  # >> MAX_QUERY_CHARS: the tail is ignored
    assert await search(db_session, long_q) == []
    head = "alpha " + " ".join(f"w{i}x" for i in range(3000))
    assert [h.index for h in await search(db_session, head)] == [0, 1]


async def test_term_cap_keeps_the_rarest_terms(
    db_session: AsyncSession, make_doc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_doc("cap.txt", ["rarething plain", "plain filler", "plain more", "plain stuff"])
    monkeypatch.setattr(kw, "MAX_QUERY_TERMS", 1)
    hits = await search(db_session, "plain rarething")  # df(plain)=4 > df(rarething)=1: only 'rarething' kept
    assert [h.index for h in hits] == [0]
    monkeypatch.setattr(kw, "MAX_QUERY_TERMS", 64)
    assert len(await search(db_session, "plain rarething")) == 4


# ── filters / deletion ───────────────────────────────────────────────────────────────────
async def test_document_filters_single_and_in(db_session: AsyncSession, make_doc: Any) -> None:
    a = await make_doc("a.txt", ["shared marker alpha"])
    b = await make_doc("b.txt", ["shared marker beta"])
    c = await make_doc("c.txt", ["shared marker gamma"])
    assert {h.filename for h in await search(db_session, "marker")} == {"a.txt", "b.txt", "c.txt"}
    assert {h.filename for h in await search(db_session, "marker", document_ids=[b])} == {"b.txt"}
    assert {h.filename for h in await search(db_session, "marker", document_ids=[a, c])} == {"a.txt", "c.txt"}
    assert await search(db_session, "marker", document_ids=[uuid.uuid4()]) == []


async def test_deleted_documents_are_never_returned(db_session: AsyncSession, make_doc: Any) -> None:
    a = await make_doc("gone.txt", ["unique zebra marker"])
    await make_doc("kept.txt", ["unique zebra marker too"])
    await ops.delete_document(a, session=db_session)
    assert {h.filename for h in await search(db_session, "zebra")} == {"kept.txt"}


# ── configuration drift ──────────────────────────────────────────────────────────────────
async def test_fts_config_drift_is_detected_and_rebuild_fixes_it(db_session: AsyncSession, make_doc: Any) -> None:
    await make_doc("drift.txt", ["Waiting periods apply to the borrowers"])
    assert await kw.stored_fts_config(db_session) == "english"
    await kw.ensure_fts(db_session, "english")
    with pytest.raises(kw.KeywordIndexMismatchError, match="rebuild-fts"):
        await kw.ensure_fts(db_session, "simple")
    with pytest.raises(kw.KeywordIndexMismatchError):
        await search(db_session, "waiting", config="simple")
    try:
        await kw.rebuild_fts(db_session, "simple")
        assert await kw.stored_fts_config(db_session) == "simple"
        await kw.ensure_fts(db_session, "simple")
        # 'simple' does not stem: 'waiting' matches, 'wait' does not
        assert await search(db_session, "waiting", config="simple")
        assert await search(db_session, "wait", config="simple") == []
        idx = (await db_session.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_chunks_fts'"))).scalar_one()
        assert "gin" in idx.lower()
    finally:
        await kw.rebuild_fts(db_session, "english")
    assert await search(db_session, "wait", config="english")


async def test_rebuild_rejects_bad_config_names(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await kw.rebuild_fts(db_session, "english; drop table chunks")
    with pytest.raises(ValueError, match="no text-search configuration"):
        await kw.rebuild_fts(db_session, "klingon")


async def test_fts_columns_are_generated_and_fts_len_counts_tokens(db_session: AsyncSession, make_doc: Any) -> None:
    await make_doc("len.txt", ["alpha beta alpha the of", ""])
    rows = (await db_session.execute(text("SELECT index, fts_len FROM chunks ORDER BY index"))).all()
    assert [r.fts_len for r in rows] == [3, 0]  # stopwords not counted; empty chunk = 0
    plan = (await db_session.execute(text("EXPLAIN SELECT 1 FROM chunks WHERE fts @@ to_tsquery('english','alpha')"))).all()
    assert plan  # the GIN index itself is asserted via pg_indexes in the drift test
