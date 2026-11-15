"""Configured-vs-stored drift on real services: Chroma collection configuration and the pgvector
catalogs are read back and compared; a mismatch must name the reindex path, never fail silently."""

from __future__ import annotations

import uuid
from typing import Any

import chromadb
import pytest
from app.config import Settings
from app.services.pgvector_store import PgVectorStore, dsn_from_database_url
from app.services.vectorstore import (
    ChromaStore,
    VectorStoreMismatchError,
    get_vector_store,
    resolve_vector_settings,
)
from tests.vector_helpers import real_store, settings_for

KINDS = ["chroma", "pgvector"]


def reopen(store: Any, kind: str, url: str, **kw: Any) -> Any:
    """The same collection/table opened with different configured settings."""
    vs = settings_for(kind, **kw)
    if kind == "chroma":
        return ChromaStore(collection_name=store._collection.name, vector_settings=vs)
    return PgVectorStore(dsn_from_database_url(url), vs, table=store._table)


@pytest.mark.parametrize("kind", KINDS)
def test_changed_metric_is_a_mismatch_that_names_the_reindex_path(kind: str, isolated_db_url: str) -> None:
    with real_store(kind, isolated_db_url, metric="cosine") as s:
        s.add_chunks("d", [{"text": "x"}], [[1.0, 0.0, 0.0]])
        other = reopen(s, kind, isolated_db_url, metric="l2")
        with pytest.raises(VectorStoreMismatchError) as exc:
            other.verify()
        msg = str(exc.value)
        assert "VECTOR_METRIC: stored cosine, configured l2" in msg
        assert "python -m app.manage reindex" in msg and "reindex_vectors" in msg
        assert other.describe()["problems"] == exc.value.problems


@pytest.mark.parametrize("kind", KINDS)
def test_changed_dimension_is_a_mismatch(kind: str, isolated_db_url: str) -> None:
    with real_store(kind, isolated_db_url, dimension=3) as s:
        s.add_chunks("d", [{"text": "x"}], [[1.0, 0.0, 0.0]])
        with pytest.raises(VectorStoreMismatchError, match=r"embedding dimension: stored 3, configured 5"):
            reopen(s, kind, isolated_db_url, dimension=5).verify()


@pytest.mark.parametrize("kind", KINDS)
def test_changed_hnsw_build_params_are_a_mismatch(kind: str, isolated_db_url: str) -> None:
    with real_store(kind, isolated_db_url, hnsw_m=8, hnsw_ef_construction=32) as s:
        with pytest.raises(VectorStoreMismatchError, match="HNSW_M: stored 8, configured 12"):
            reopen(s, kind, isolated_db_url, hnsw_m=12, hnsw_ef_construction=32).verify()
        with pytest.raises(VectorStoreMismatchError, match="HNSW_EF_CONSTRUCTION: stored 32, configured 64"):
            reopen(s, kind, isolated_db_url, hnsw_m=8, hnsw_ef_construction=64).verify()


def test_pgvector_changed_index_type_is_a_mismatch_in_both_directions(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="hnsw") as s:
        for index in ("ivfflat", "none"):
            with pytest.raises(VectorStoreMismatchError, match=f"VECTOR_INDEX: stored hnsw, configured {index}"):
                reopen(s, "pgvector", isolated_db_url, index=index).verify()


def test_pgvector_metric_without_an_index_is_a_query_time_choice_not_a_mismatch(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="none", metric="cosine") as s:
        s.add_chunks("d", [{"text": "a"}, {"text": "b"}], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        l2 = reopen(s, "pgvector", isolated_db_url, index="none", metric="l2")
        l2.verify()  # nothing stored depends on the metric
        assert l2.similarity_search([1.0, 0, 0], 1)[0]["text"] == "a"


def test_reset_with_new_settings_is_the_fix(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, metric="cosine") as s:
        s.add_chunks("d", [{"text": "x"}], [[1.0, 0.0, 0.0]])
        fixed = reopen(s, "pgvector", isolated_db_url, metric="l2")
        with pytest.raises(VectorStoreMismatchError):
            fixed.verify()
        fixed.reset()
        fixed.verify()
        assert fixed.describe()["stored"]["metric"] == "l2" and fixed.describe()["count"] == 0


# ── Chroma specifics: which options the server really honours ─────────────────────────────
def test_chroma_applies_space_m_and_ef_construction_at_creation_and_reads_them_back(isolated_db_url: str) -> None:
    with real_store("chroma", isolated_db_url, metric="ip", hnsw_m=8, hnsw_ef_construction=50, hnsw_ef_search=30) as s:
        hnsw = s._client.get_collection(name=s._collection.name).configuration["hnsw"]  # type: ignore[attr-defined]
        assert (hnsw["space"], hnsw["max_neighbors"], hnsw["ef_construction"], hnsw["ef_search"]) == ("ip", 8, 50, 30)


def test_chroma_ef_search_is_query_time_and_reapplied_on_verify(isolated_db_url: str) -> None:
    with real_store("chroma", isolated_db_url, hnsw_ef_search=30) as s:
        again = reopen(s, "chroma", isolated_db_url, hnsw_ef_search=77)
        again.verify()  # no mismatch: ef_search does not need a reindex
        cfg = s._client.get_collection(name=s._collection.name).configuration["hnsw"]  # type: ignore[attr-defined]
        assert cfg["ef_search"] == 77


def test_chroma_ignores_creation_options_on_an_existing_collection_hence_verify(isolated_db_url: str) -> None:
    """Documents WHY the mismatch check exists: Chroma silently keeps the original space."""
    with real_store("chroma", isolated_db_url, metric="cosine") as s:
        again = reopen(s, "chroma", isolated_db_url, metric="l2")
        raw = again._client.get_collection(name=s._collection.name).configuration["hnsw"]  # type: ignore[attr-defined]
        assert raw["space"] == "cosine"  # the l2 request was ignored by the server
        with pytest.raises(VectorStoreMismatchError):
            again.verify()


def test_default_settings_accept_a_collection_created_the_way_the_app_always_did() -> None:
    """Backwards compatibility: no VECTOR_* set + a collection created with Chroma defaults (as the
    pre-ticket-24 app did) must verify cleanly and keep l2 / M16 / efC100 / efS100."""
    name = f"test_legacy_{uuid.uuid4().hex[:10]}"
    client = chromadb.HttpClient(host=Settings().chroma_host, port=Settings().chroma_port)
    try:
        client.get_or_create_collection(name=name)  # no configuration, exactly the old call
        legacy = ChromaStore(collection_name=name, vector_settings=resolve_vector_settings(Settings(), "chroma"))
        legacy.verify()
        legacy.add_chunks("d", [{"text": "a"}, {"text": "b"}], [[1.0, 0.0], [0.0, 2.0]])
        hits = legacy.similarity_search([1.0, 0.0], 2)
        assert [h["text"] for h in hits] == ["a", "b"] and hits[1]["score"] == pytest.approx(5.0)  # squared L2
        cfg = client.get_collection(name).configuration["hnsw"]
        assert (cfg["space"], cfg["max_neighbors"], cfg["ef_construction"], cfg["ef_search"]) == ("l2", 16, 100, 100)
    finally:
        client.delete_collection(name)


def test_get_vector_store_raises_on_mismatch_and_recovers_after_the_fix(
    isolated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import vectorstore as vs

    with real_store("pgvector", isolated_db_url, metric="cosine", dimension=1024) as s:
        table = s._table  # type: ignore[attr-defined]
        cfg = Settings(vector_store="pgvector", database_url=isolated_db_url, vector_metric="l2")
        vs._verified.clear()
        monkeypatch.setattr(
            PgVectorStore,
            "from_settings",
            classmethod(
                lambda cls, settings: PgVectorStore(
                    dsn_from_database_url(settings.database_url), resolve_vector_settings(settings, "pgvector"), table=table
                )
            ),
        )
        with pytest.raises(VectorStoreMismatchError, match="reindex"):
            get_vector_store(settings=cfg)
        with pytest.raises(VectorStoreMismatchError):  # a failed verify is never cached as "ok"
            get_vector_store(settings=cfg)
        get_vector_store(settings=cfg.model_copy(update={"vector_metric": "cosine"}))
        vs._verified.clear()
