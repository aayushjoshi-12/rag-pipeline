"""Retrieval settings validation and per-request option precedence."""

from __future__ import annotations

from typing import Any

import pytest
from app.config import Settings, SettingsError
from app.services.retriever import RetrievalOptions
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        ({"retrieval_mode": "fuzzy"}, "RETRIEVAL_MODE.*semantic, keyword, hybrid"),
        ({"retrieval_keyword_method": "tfidf"}, "RETRIEVAL_KEYWORD_METHOD.*bm25, postgres_fts"),
        ({"retrieval_fusion": "borda"}, "RETRIEVAL_FUSION.*rrf, weighted"),
        ({"retrieval_rrf_k": 0}, "retrieval_rrf_k"),
        ({"retrieval_rrf_k": 1001}, "retrieval_rrf_k"),
        ({"retrieval_candidates": 0}, "retrieval_candidates"),
        ({"retrieval_candidates": 501}, "retrieval_candidates"),
        ({"retrieval_keyword_pool": 0}, "retrieval_keyword_pool"),
        ({"bm25_k1": -0.1}, "bm25_k1"),
        ({"bm25_k1": 10.5}, "bm25_k1"),
        ({"bm25_b": 1.1}, "bm25_b"),
        ({"bm25_b": -0.2}, "bm25_b"),
        ({"bm25_b": float("nan")}, "finite"),
        ({"retrieval_weight_semantic": -1}, "weight"),
        ({"retrieval_weight_semantic": 0, "retrieval_weight_keyword": 0}, "weight"),
        ({"retrieval_fts_config": "English; DROP TABLE chunks"}, "retrieval_fts_config"),
        ({"retrieval_fts_config": ""}, "retrieval_fts_config"),
    ],
)
def test_invalid_retrieval_settings_are_rejected(kw: dict[str, Any], msg: str) -> None:
    with pytest.raises((SettingsError, ValidationError), match=msg):
        Settings(**kw)


def test_defaults_keep_the_current_behaviour_and_boundaries_are_accepted() -> None:
    s = Settings()
    assert (s.retrieval_mode, s.retrieval_keyword_method, s.retrieval_fusion) == ("semantic", "bm25", "rrf")
    assert (s.retrieval_rrf_k, s.retrieval_candidates, s.bm25_k1, s.bm25_b) == (60, 50, 1.2, 0.75)
    assert (s.retrieval_weight_semantic, s.retrieval_weight_keyword) == (0.6, 0.4)
    Settings(retrieval_rrf_k=1, retrieval_candidates=500, bm25_k1=0, bm25_b=1, retrieval_weight_semantic=0,
             retrieval_weight_keyword=1, retrieval_fts_config="simple", retrieval_mode="hybrid",
             retrieval_fusion="weighted", retrieval_keyword_method="postgres_fts")


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    monkeypatch.setenv("RETRIEVAL_RRF_K", "10")
    monkeypatch.setenv("BM25_B", "0.5")
    s = Settings()
    assert (s.retrieval_mode, s.retrieval_rrf_k, s.bm25_b) == ("hybrid", 10, 0.5)


def test_request_overrides_win_over_settings_and_none_keeps_them() -> None:
    cfg = Settings(retrieval_mode="keyword", retrieval_fusion="weighted", retrieval_rrf_k=7, retrieval_candidates=9)
    base = RetrievalOptions.from_settings(cfg)
    assert (base.mode, base.fusion, base.rrf_k, base.candidates) == ("keyword", "weighted", 7, 9)
    over = RetrievalOptions.from_settings(cfg, mode="hybrid", fusion="rrf")
    assert (over.mode, over.fusion) == ("hybrid", "rrf")
    assert RetrievalOptions.from_settings(cfg, mode="semantic").mode == "semantic"


def test_options_reject_unknown_values() -> None:
    with pytest.raises(ValueError, match="mode"):
        RetrievalOptions(mode="nope")
    with pytest.raises(ValueError, match="fusion"):
        RetrievalOptions(fusion="nope")


def test_trace_meta_only_lists_the_knobs_that_apply() -> None:
    assert RetrievalOptions(mode="semantic").trace_meta() == {"mode": "semantic"}
    assert RetrievalOptions(mode="keyword", keyword_method="postgres_fts", candidates=7).trace_meta() == {
        "mode": "keyword", "keyword_method": "postgres_fts", "candidates": 7,
    }
    assert RetrievalOptions(mode="hybrid", fusion="weighted").trace_meta() == {
        "mode": "hybrid", "keyword_method": "bm25", "fusion": "weighted", "candidates": 50,
    }


async def test_every_option_reaches_the_keyword_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retriever hands the deployment/request options to the keyword search unchanged."""
    import uuid

    from app.services import keyword, retriever

    seen: dict[str, Any] = {}

    async def spy(session: Any, query: str, **kw: Any) -> list[Any]:
        seen.update(kw, query=query)
        return []

    monkeypatch.setattr(keyword, "keyword_search", spy)
    docs = [uuid.uuid4()]
    opts = RetrievalOptions(
        mode="keyword", keyword_method="postgres_fts", candidates=7, keyword_pool=33, bm25_k1=0.3, bm25_b=0.2, fts_config="simple"
    )
    await retriever.retrieve("q?", top_k=3, embedder=None, store=None, session=None, document_ids=docs, options=opts)  # type: ignore[arg-type]
    assert seen == {
        "query": "q?", "limit": 7, "method": "postgres_fts", "config": "simple", "pool": 33, "k1": 0.3, "b": 0.2,
        "document_ids": docs,
    }
    # top_k larger than the candidate pool raises the pool
    await retriever.retrieve("q?", top_k=20, embedder=None, store=None, session=None, options=opts)  # type: ignore[arg-type]
    assert seen["limit"] == 20 and seen["document_ids"] is None
