"""pgvector specifics on a real Postgres: indexes exist (pg_indexes), are USED (EXPLAIN), schema
creation is idempotent/concurrent-safe, dimension comes from config, filtered ANN queries do not
starve. The isolated test database is used; the developer's ``rag`` database is never touched.

How "the index is used" is established without faking it: the exact SQL ``similarity_search`` runs is
EXPLAINed (``PgVectorStore.explain``) after ``ANALYZE`` on a 2,000-row, 16-dimension table with the
planner's default settings (no ``enable_seqscan=off``). On a ~300-row table the planner correctly prefers
a sequential scan even though the index exists, which is why the row count is 2,000 here. A control
(index=none, or an index built for a different metric) must show a Seq Scan, otherwise the assertions
would prove nothing.
"""

from __future__ import annotations

import random
import threading
from typing import Any

import psycopg
import pytest
from app.services.pgvector_store import PgVectorStore, dsn_from_database_url
from app.services.vectorstore import VectorStoreMismatchError
from tests.vector_helpers import real_store, settings_for

DIM = 16
N = 2000


def indexdefs(url: str, table: str) -> list[str]:
    with psycopg.connect(dsn_from_database_url(url)) as conn:
        rows = conn.execute("SELECT indexdef FROM pg_indexes WHERE tablename = %s", (table,)).fetchall()
    return [r[0] for r in rows]


def vectors(n: int, seed: int = 1, dim: int = DIM) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.random() for _ in range(dim)] for _ in range(n)]


def fill(store: Any, vecs: list[list[float]], doc: str = "d") -> None:
    for i in range(0, len(vecs), 500):
        part = vecs[i : i + 500]
        store.add_chunks(f"{doc}{i}", [{"text": f"t{i + j}"} for j in range(len(part))], part)


def plan_nodes(store: Any, q: list[float], **kw: Any) -> str:
    return "\n".join(store.explain(q, 5, **kw))


# ── indexes exist ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("metric", "opclass"), [("cosine", "vector_cosine_ops"), ("l2", "vector_l2_ops"), ("ip", "vector_ip_ops")])
def test_hnsw_index_is_created_with_the_metric_opclass_and_params(isolated_db_url: str, metric: str, opclass: str) -> None:
    with real_store("pgvector", isolated_db_url, metric=metric, index="hnsw", hnsw_m=8, hnsw_ef_construction=32) as s:
        defs = [d for d in indexdefs(isolated_db_url, s._table) if "hnsw" in d]  # type: ignore[attr-defined]
        assert len(defs) == 1
        assert f"USING hnsw (embedding {opclass})" in defs[0]
        assert "m='8'" in defs[0] and "ef_construction='32'" in defs[0]
        assert s.stored_settings() == {  # type: ignore[attr-defined]
            "dimension": 3, "index": "hnsw", "metric": metric, "hnsw_m": 8, "hnsw_ef_construction": 32,
        }


def test_default_hnsw_params_are_16_and_64(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url) as s:
        (d,) = [d for d in indexdefs(isolated_db_url, s._table) if "hnsw" in d]  # type: ignore[attr-defined]
        assert "m='16'" in d and "ef_construction='64'" in d


def test_index_none_creates_no_ann_index_but_keeps_the_doc_id_btree(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="none") as s:
        defs = indexdefs(isolated_db_url, s._table)  # type: ignore[attr-defined]
        assert not any("hnsw" in d or "ivfflat" in d for d in defs)
        assert any("(doc_id)" in d for d in defs)
        s.add_chunks("d", [{"text": "x"}], [[1.0, 0.0, 0.0]])
        assert s.similarity_search([1.0, 0.0, 0.0], 1)[0]["text"] == "x"


# ── indexes are used ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("metric", ["cosine", "l2", "ip"])
@pytest.mark.parametrize("index", ["hnsw", "ivfflat"])
def test_planner_uses_the_ann_index(isolated_db_url: str, index: str, metric: str) -> None:
    with real_store("pgvector", isolated_db_url, metric=metric, index=index, dimension=DIM, ivfflat_lists=20) as s:
        vecs = vectors(N)
        fill(s, vecs)
        s.analyze()  # type: ignore[attr-defined]
        plan = plan_nodes(s, vecs[0])
        assert "Index Scan using" in plan and "_embedding_idx" in plan, plan
        assert "Seq Scan" not in plan, plan


def test_control_without_an_index_the_plan_is_a_seq_scan(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="none", dimension=DIM) as s:
        vecs = vectors(N)
        fill(s, vecs)
        s.analyze()  # type: ignore[attr-defined]
        plan = plan_nodes(s, vecs[0])
        assert "Seq Scan" in plan and "Index Scan" not in plan, plan


def test_control_an_index_for_another_metric_is_not_used_and_is_reported_as_a_mismatch(isolated_db_url: str) -> None:
    """The cosine index cannot serve an L2 query; that is exactly the silent slowdown verify() catches."""
    with real_store("pgvector", isolated_db_url, metric="cosine", dimension=DIM) as cosine:
        fill(cosine, vectors(N))
        cosine.analyze()  # type: ignore[attr-defined]
        other = PgVectorStore(
            dsn_from_database_url(isolated_db_url), settings_for("pgvector", "l2", "hnsw", DIM), table=cosine._table  # type: ignore[attr-defined]
        )
        assert "Seq Scan" in plan_nodes(other, vectors(1)[0]) and "Index Scan" not in plan_nodes(other, vectors(1)[0])
        with pytest.raises(VectorStoreMismatchError, match="VECTOR_METRIC: stored cosine, configured l2"):
            other.verify()


def test_search_through_the_index_matches_brute_force(isolated_db_url: str) -> None:
    """The index path is approximate; on this data it must still find (nearly) the true neighbours."""
    with real_store("pgvector", isolated_db_url, metric="l2", dimension=DIM) as s:
        vecs = vectors(N)
        fill(s, vecs)
        s.analyze()  # type: ignore[attr-defined]
        queries = vectors(15, seed=99)
        hit = total = 0
        for q in queries:
            truth = sorted(range(N), key=lambda i: sum((a - b) ** 2 for a, b in zip(vecs[i], q, strict=True)))[:5]
            got = {h["text"] for h in s.similarity_search(q, 5)}
            hit += len(got & {f"t{i}" for i in truth})
            total += 5
        assert hit / total >= 0.9, hit / total


# ── ivfflat on empty / small tables ───────────────────────────────────────────────────────
def test_ivfflat_on_an_empty_table_is_deferred_then_built_once_enough_rows_exist(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="ivfflat", dimension=DIM, ivfflat_lists=10) as s:
        table = s._table  # type: ignore[attr-defined]
        assert not any("ivfflat" in d for d in indexdefs(isolated_db_url, table))
        info = s.describe()
        assert info["problems"] == [] and "deferred" in info["index_state"]
        assert s.similarity_search(vectors(1)[0], 3) == []  # empty table: honest empty result

        s.add_chunks("a", [{"text": f"t{i}"} for i in range(5)], vectors(5))  # 5 < 10 lists
        assert not any("ivfflat" in d for d in indexdefs(isolated_db_url, table))
        assert len(s.similarity_search(vectors(1)[0], 3)) == 3  # exact scan still answers

        s.add_chunks("b", [{"text": f"u{i}"} for i in range(40)], vectors(40, seed=5))
        (d,) = [d for d in indexdefs(isolated_db_url, table) if "ivfflat" in d]
        assert "lists='10'" in d
        assert s.describe()["index_state"] == "ok"
        s.verify()


def test_rebuild_index_retrains_ivfflat_on_the_current_rows(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="ivfflat", dimension=DIM, ivfflat_lists=10) as s:
        s.add_chunks("a", [{"text": f"t{i}"} for i in range(20)], vectors(20))
        table = s._table  # type: ignore[attr-defined]
        with psycopg.connect(dsn_from_database_url(isolated_db_url)) as c:
            oid_before = c.execute("SELECT to_regclass(%s)::oid", (f"{table}_embedding_idx",)).fetchone()
        s.rebuild_index()  # type: ignore[attr-defined]
        with psycopg.connect(dsn_from_database_url(isolated_db_url)) as c:
            oid_after = c.execute("SELECT to_regclass(%s)::oid", (f"{table}_embedding_idx",)).fetchone()
        assert oid_before != oid_after and oid_after is not None  # a new index object, not a no-op


def test_ivfflat_results_are_exact_when_all_lists_are_probed(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="ivfflat", metric="l2", dimension=DIM, ivfflat_lists=10, ivfflat_probes=10) as s:
        vecs = vectors(200)
        fill(s, vecs)
        q = vectors(1, seed=7)[0]
        truth = sorted(range(200), key=lambda i: sum((a - b) ** 2 for a, b in zip(vecs[i], q, strict=True)))[:5]
        assert [h["text"] for h in s.similarity_search(q, 5)] == [f"t{i}" for i in truth]


# ── dimension ─────────────────────────────────────────────────────────────────────────────
def test_dimension_comes_from_config_not_a_hardcoded_1024(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, dimension=5) as s:
        assert s.stored_settings()["dimension"] == 5  # type: ignore[attr-defined]
        s.add_chunks("d", [{"text": "x"}], [[1, 0, 0, 0, 0]])


def test_unknown_dimension_is_probed_once_and_used(isolated_db_url: str) -> None:
    probes: list[int] = []

    def probe() -> int:
        probes.append(1)
        return 7

    vs = settings_for("pgvector", dimension=None)
    store = PgVectorStore(dsn_from_database_url(isolated_db_url), vs, table="vi_probe_test", dimension_probe=probe)
    try:
        store.verify()
        store.verify()  # table exists now: no second probe
        assert store.stored_settings()["dimension"] == 7 and probes == [1]  # type: ignore[index]
    finally:
        with psycopg.connect(dsn_from_database_url(isolated_db_url), autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS vi_probe_test")


def test_unknown_dimension_without_a_probe_is_a_clear_error(isolated_db_url: str) -> None:
    store = PgVectorStore(dsn_from_database_url(isolated_db_url), settings_for("pgvector", dimension=None), table="vi_nodim")
    with pytest.raises(ValueError, match="EMBEDDING_DIMENSION"):
        store.verify()


def test_indexing_more_than_2000_dimensions_is_refused_clearly_but_none_works(isolated_db_url: str) -> None:
    with pytest.raises(ValueError, match="VECTOR_INDEX=none"), real_store("pgvector", isolated_db_url, index="hnsw", dimension=2500):
        pass
    with real_store("pgvector", isolated_db_url, index="none", dimension=2500) as s:
        s.add_chunks("d", [{"text": "x"}], [[1.0] + [0.0] * 2499])
        assert s.similarity_search([1.0] + [0.0] * 2499, 1)[0]["text"] == "x"


# ── schema creation ───────────────────────────────────────────────────────────────────────
def test_verify_is_idempotent_and_safe_to_run_concurrently(isolated_db_url: str) -> None:
    store = PgVectorStore(dsn_from_database_url(isolated_db_url), settings_for("pgvector", dimension=4), table="vi_concurrent")
    errors: list[BaseException] = []

    def run() -> None:
        try:
            store.verify()
        except BaseException as exc:
            errors.append(exc)

    try:
        threads = [threading.Thread(target=run) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []
        assert len([d for d in indexdefs(isolated_db_url, "vi_concurrent") if "hnsw" in d]) == 1
    finally:
        with psycopg.connect(dsn_from_database_url(isolated_db_url), autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS vi_concurrent")


def test_verify_adds_a_missing_index_to_an_existing_unindexed_table(isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="none", dimension=3) as s:
        table = s._table  # type: ignore[attr-defined]
        s.add_chunks("d", [{"text": "x"}], [[1.0, 0.0, 0.0]])
        hnsw = PgVectorStore(dsn_from_database_url(isolated_db_url), settings_for("pgvector", index="hnsw"), table=table)
        hnsw.verify()
        assert any("USING hnsw" in d for d in indexdefs(isolated_db_url, table))


# ── filtered ANN queries ──────────────────────────────────────────────────────────────────
def test_filtered_hnsw_query_still_returns_top_k_when_the_filtered_rows_are_far_away(isolated_db_url: str) -> None:
    """Without iterative scans HNSW post-filters its ef_search candidates (all from the big document)
    and returns nothing for the small one. 40 candidates << 2,000 near rows.

    At this table size the planner (correctly) prefers a seq scan for a filtered query, so to exercise
    the HNSW path the test forces it with ``enable_seqscan/enable_bitmapscan = off`` on its own session
    and asserts via EXPLAIN that the index was in fact used. The SQL and the other SET LOCALs are the
    ones ``similarity_search`` builds.
    """
    from psycopg import sql

    with real_store("pgvector", isolated_db_url, metric="l2", dimension=DIM, hnsw_ef_search=40) as s:
        near = vectors(N)
        fill(s, near, doc="A")
        far = [[5.0 + v for v in vec] for vec in vectors(30, seed=3)]
        s.add_chunks("B", [{"text": f"far{i}", "metadata": {"tag": "far"}} for i in range(30)], far)
        s.analyze()  # type: ignore[attr-defined]
        sets, stmt, params = s._search_plan(near[0], 10, {"tag": "far"})  # type: ignore[attr-defined]
        force = [sql.SQL("SET LOCAL enable_seqscan = off"), sql.SQL("SET LOCAL enable_bitmapscan = off")]
        with psycopg.connect(dsn_from_database_url(isolated_db_url)) as conn:
            for st in [*sets, *force]:
                conn.execute(st)
            plan = "\n".join(r[0] for r in conn.execute(sql.SQL("EXPLAIN ") + stmt, params).fetchall())
            assert "Index Scan using" in plan and "_embedding_idx" in plan, plan
            rows = conn.execute(stmt, params).fetchall()
        assert len(rows) == 10 and {r[1] for r in rows} <= {f"far{i}" for i in range(30)}
        # and through the public API (whatever plan the planner picks) the answer is the same set
        assert len(s.similarity_search(near[0], 10, filters={"tag": "far"})) == 10
        assert len(s.similarity_search(near[0], 10, filters={"doc_id": "B"})) == 10
