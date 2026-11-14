"""Vector settings: validation, per-store defaults, drift comparison, pure helpers (no services)."""

from __future__ import annotations

from typing import Any

import pytest
from app.config import Settings, SettingsError
from app.services.embeddings import configured_dimension
from app.services.pgvector_store import PgVectorStore, dsn_from_database_url
from app.services.vectorstore import (
    VectorSettings,
    _filter_clauses,
    compare_settings,
    resolve_vector_settings,
)
from pydantic import ValidationError


def S(**kw: Any) -> Settings:
    return Settings(**kw)


# ── validation ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        ({"vector_store": "pinecone"}, "Unknown vector store"),
        ({"vector_metric": "manhattan"}, "VECTOR_METRIC"),
        ({"vector_index": "flat"}, "VECTOR_INDEX"),
        ({"vector_index": "ivfflat"}, "only available with VECTOR_STORE=pgvector"),
        ({"vector_store": "chroma", "vector_index": "none"}, "only available with VECTOR_STORE=pgvector"),
        ({"hnsw_m": 1}, "hnsw_m"),
        ({"hnsw_m": 101}, "hnsw_m"),
        ({"hnsw_ef_construction": 3}, "hnsw_ef_construction"),
        ({"hnsw_m": 32, "hnsw_ef_construction": 40}, "at least 2 \\* hnsw_m"),
        ({"hnsw_ef_search": 0}, "hnsw_ef_search"),
        ({"ivfflat_lists": 0}, "ivfflat_lists"),
        ({"ivfflat_lists": 10, "ivfflat_probes": 11}, "must not exceed"),
        ({"embedding_dimension": 0}, "embedding_dimension"),
    ],
)
def test_invalid_vector_settings_fail_with_the_allowed_values(kw: dict[str, Any], msg: str) -> None:
    with pytest.raises((SettingsError, ValidationError), match=msg):
        S(**kw)


def test_error_lists_allowed_values() -> None:
    with pytest.raises((SettingsError, ValidationError), match="cosine, l2, ip"):
        S(vector_metric="x")
    with pytest.raises((SettingsError, ValidationError), match="hnsw, ivfflat, none"):
        S(vector_store="pgvector", vector_index="x")


def test_valid_pgvector_options_are_accepted() -> None:
    s = S(vector_store="pgvector", vector_index="ivfflat", vector_metric="ip", ivfflat_lists=50, ivfflat_probes=5)
    assert (s.vector_index, s.vector_metric) == ("ivfflat", "ip")


# ── defaults ──────────────────────────────────────────────────────────────────────────────
def test_default_chroma_settings_are_what_an_existing_collection_already_has() -> None:
    v = resolve_vector_settings(S(), "chroma")
    assert (v.store, v.metric, v.index) == ("chroma", "l2", "hnsw")
    # HNSW params are left to Chroma (not asserted against an existing collection)
    assert v.hnsw_m is None and v.hnsw_ef_construction is None and v.hnsw_ef_search is None


def test_default_pgvector_settings() -> None:
    v = resolve_vector_settings(S(vector_store="pgvector"))
    assert (v.metric, v.index) == ("cosine", "hnsw")
    assert (v.hnsw_m, v.hnsw_ef_construction, v.hnsw_ef_search) == (16, 64, 40)
    assert (v.ivfflat_lists, v.ivfflat_probes) == (100, 10)
    assert v.dimension == 1024  # mxbai-embed-large, no network call


def test_explicit_settings_override_store_defaults() -> None:
    v = resolve_vector_settings(S(vector_store="pgvector", vector_metric="l2", hnsw_m=8, hnsw_ef_construction=32))
    assert (v.metric, v.hnsw_m, v.hnsw_ef_construction) == ("l2", 8, 32)
    c = resolve_vector_settings(S(vector_metric="cosine", hnsw_ef_search=77), "chroma")
    assert (c.metric, c.hnsw_ef_search) == ("cosine", 77)


def test_dimension_comes_from_config_then_known_model_then_unknown() -> None:
    assert configured_dimension(S(embedding_dimension=768)) == 768
    assert configured_dimension(S(embedding_model="nomic-embed-text:latest")) == 768
    assert configured_dimension(S(embedding_model="some-new-model")) is None


# ── drift comparison ──────────────────────────────────────────────────────────────────────
def pg(**kw: Any) -> VectorSettings:
    return resolve_vector_settings(S(vector_store="pgvector", **kw))


def test_matching_settings_have_no_problems() -> None:
    stored = {"metric": "cosine", "index": "hnsw", "dimension": 1024, "hnsw_m": 16, "hnsw_ef_construction": 64}
    assert compare_settings(pg(), stored) == []


@pytest.mark.parametrize(
    ("stored", "needle"),
    [
        ({"metric": "l2", "index": "hnsw"}, "VECTOR_METRIC: stored l2, configured cosine"),
        ({"metric": "cosine", "index": "ivfflat"}, "VECTOR_INDEX: stored ivfflat, configured hnsw"),
        ({"index": "none"}, "VECTOR_INDEX: stored none, configured hnsw"),
        ({"dimension": 768}, "embedding dimension: stored 768, configured 1024"),
        ({"index": "hnsw", "hnsw_m": 8}, "HNSW_M: stored 8, configured 16"),
        ({"index": "hnsw", "hnsw_ef_construction": 200}, "HNSW_EF_CONSTRUCTION"),
    ],
)
def test_each_kind_of_drift_is_named(stored: dict[str, Any], needle: str) -> None:
    assert any(needle in p for p in compare_settings(pg(), stored)), compare_settings(pg(), stored)


def test_unknown_stored_values_and_irrelevant_params_are_not_mismatches() -> None:
    # an un-indexed table has no stored metric: changing the metric is a query-time choice
    assert compare_settings(pg(vector_metric="l2", vector_index="none"), {"metric": None, "index": "none", "dimension": 1024}) == []
    # ivfflat configured: HNSW params of a stored hnsw index are not compared (the index type already is)
    p = compare_settings(pg(vector_index="ivfflat"), {"index": "hnsw", "hnsw_m": 99})
    assert p == ["VECTOR_INDEX: stored hnsw, configured ivfflat"]
    assert compare_settings(pg(ivfflat_lists=10, vector_index="ivfflat"), {"index": "ivfflat", "ivfflat_lists": 20}) == [
        "IVFFLAT_LISTS: stored 20, configured 10"
    ]


# ── pure helpers ──────────────────────────────────────────────────────────────────────────
def test_filter_clauses_normalise_equality_and_in() -> None:
    assert _filter_clauses({"doc_id": "a", "k": {"$in": [1, 2]}}) == [("doc_id", ["a"]), ("k", [1, 2])]
    with pytest.raises(ValueError, match="Unsupported filter"):
        _filter_clauses({"k": {"$gt": 1}})
    with pytest.raises(ValueError, match="Unsupported filter"):
        _filter_clauses({"k": {"$in": "abc"}})


def test_parse_indexdef_reads_type_metric_and_params() -> None:
    hnsw = PgVectorStore._parse_indexdef(
        "CREATE INDEX t_idx ON public.t USING hnsw (embedding vector_l2_ops) WITH (m='8', ef_construction='32')"
    )
    assert hnsw == {"index": "hnsw", "metric": "l2", "hnsw_m": 8, "hnsw_ef_construction": 32}
    ivf = PgVectorStore._parse_indexdef(
        "CREATE INDEX t_idx ON public.t USING ivfflat (embedding vector_ip_ops) WITH (lists='20')"
    )
    assert ivf == {"index": "ivfflat", "metric": "ip", "ivfflat_lists": 20}


def test_dsn_drops_the_async_driver() -> None:
    assert dsn_from_database_url("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"


def test_table_name_is_validated() -> None:
    vs = VectorSettings(store="pgvector", metric="cosine", index="none", dimension=3)
    with pytest.raises(ValueError, match="invalid table name"):
        PgVectorStore("postgresql://x", vs, table='x"; DROP TABLE y; --')


def test_where_clause_uses_bound_parameters_not_string_interpolation() -> None:
    vs = VectorSettings(store="pgvector", metric="cosine", index="none", dimension=3)
    store = PgVectorStore("postgresql://x", vs)
    clause, params = store._where({"doc_id": {"$in": ["a'; DROP TABLE x;--", "b"]}, "tag": "t"})
    text = clause.as_string(None) if hasattr(clause, "as_string") else str(clause)
    assert "DROP" not in text and "%s" in text
    assert params[0] == ["a'; DROP TABLE x;--", "b"] and len(params) == 2


def test_search_plan_sets_session_knobs_only_for_the_configured_index() -> None:
    def sets(**kw: Any) -> list[str]:
        vs = resolve_vector_settings(S(vector_store="pgvector", **kw))
        out, _stmt, _p = PgVectorStore("postgresql://x", vs)._search_plan([0.0, 1.0], 5, None)
        return [s.as_string(None) for s in out]

    assert sets(hnsw_ef_search=64) == ["SET LOCAL hnsw.ef_search = 64"]
    out, _s, _p = PgVectorStore(
        "postgresql://x", resolve_vector_settings(S(vector_store="pgvector", hnsw_ef_search=10))
    )._search_plan([0.0, 1.0], 100, None)
    assert [s.as_string(None) for s in out] == ["SET LOCAL hnsw.ef_search = 100"]  # never below top_k
    assert sets(vector_index="ivfflat", ivfflat_probes=7) == ["SET LOCAL ivfflat.probes = 7"]
    assert sets(vector_index="none") == []
