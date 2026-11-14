"""One contract suite, run against BOTH vector stores on real services.

Chroma = the compose server on :8001 (throwaway collection per test); pgvector = the isolated,
migrated test database (throwaway table per test). Vectors are synthetic; nothing calls an LLM.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.services.vectorstore import VectorStore, VectorStoreMismatchError
from tests.vector_helpers import real_store

KINDS = ["chroma", "pgvector"]
METRICS = ["cosine", "l2", "ip"]

# query [1,0,0] against these (a..d); expected distances per the documented convention
VECTORS = {"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 0.0], "c": [2.0, 0.1, 0.0], "d": [0.5, 0.5, 0.0]}
EXPECTED = {  # id -> distance, per metric
    "cosine": {"a": 0.0, "c": 0.0012476, "d": 0.2928932, "b": 1.0},
    "l2": {"a": 0.0, "d": 0.5, "c": 1.01, "b": 2.0},  # squared euclidean
    "ip": {"c": -1.0, "a": 0.0, "d": 0.5, "b": 1.0},  # 1 - dot
}


def load(store: VectorStore, doc: str = "doc", ids: dict[str, list[float]] | None = None) -> None:
    ids = ids or VECTORS
    store.add_chunks(
        doc,
        [{"id": f"{doc}_{k}", "text": f"text-{k}", "metadata": {"index": i, "tag": k}} for i, k in enumerate(ids)],
        list(ids.values()),
    )


@pytest.fixture(params=KINDS)
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def store(kind: str, isolated_db_url: str) -> Iterator[VectorStore]:
    with real_store(kind, isolated_db_url, metric="cosine") as s:
        yield s


# ── metric correctness: ordering AND the numbers, identical on both stores ────────────────
@pytest.mark.parametrize("metric", METRICS)
def test_metric_ordering_and_scores_match_the_convention(kind: str, metric: str, isolated_db_url: str) -> None:
    with real_store(kind, isolated_db_url, metric=metric) as s:
        load(s)
        hits = s.similarity_search([1.0, 0.0, 0.0], top_k=4)
    expected = EXPECTED[metric]
    assert [h["id"].removeprefix("doc_") for h in hits] == sorted(expected, key=lambda k: expected[k])
    for h in hits:
        assert h["score"] == pytest.approx(expected[h["id"].removeprefix("doc_")], abs=1e-4)
    assert [h["score"] for h in hits] == sorted(h["score"] for h in hits)  # ascending = lower is closer


def test_metrics_really_differ(isolated_db_url: str, kind: str) -> None:
    """Guards the test above: with these vectors the three metrics must not all agree."""
    orders = set()
    for metric in METRICS:
        with real_store(kind, isolated_db_url, metric=metric) as s:
            load(s)
            orders.add(tuple(h["id"] for h in s.similarity_search([1.0, 0.0, 0.0], top_k=4)))
    assert len(orders) == 3


# ── basic behaviour ──────────────────────────────────────────────────────────────────────
def test_empty_store_returns_nothing(store: VectorStore) -> None:
    assert store.similarity_search([1.0, 0.0, 0.0], top_k=3) == []


def test_top_k_is_respected_and_larger_k_returns_everything(store: VectorStore) -> None:
    load(store)
    assert len(store.similarity_search([1.0, 0.0, 0.0], top_k=2)) == 2
    assert len(store.similarity_search([1.0, 0.0, 0.0], top_k=50)) == 4


def test_hits_carry_text_and_metadata_shape(store: VectorStore) -> None:
    store.add_chunks(
        "doc-x",
        [{"id": "doc-x_h1", "text": "hello", "metadata": {"page": 3, "label": "z", "flag": True, "nested": {"a": 1}}}],
        [[1.0, 0.0, 0.0]],
    )
    (hit,) = store.similarity_search([1.0, 0.0, 0.0], top_k=1)
    assert hit["id"] == "doc-x_h1" and hit["text"] == "hello"
    meta = hit["metadata"]
    assert meta["doc_id"] == "doc-x" and meta["index"] == 0
    assert meta["page"] == 3 and meta["label"] == "z" and meta["flag"] is True
    assert "nested" not in meta  # non-primitive metadata is dropped on every store


def test_ids_default_to_position_and_explicit_ids_win(store: VectorStore) -> None:
    store.add_chunks("d1", [{"text": "p0"}, {"text": "p1"}], [[1.0, 0, 0], [0, 1.0, 0]])
    store.add_chunks("d2", [{"id": "custom", "text": "c"}], [[0, 0, 1.0]])
    ids = {h["id"] for h in store.similarity_search([1.0, 1.0, 1.0], top_k=10)}
    assert ids == {"d1_0", "d1_1", "custom"}


def test_add_is_an_upsert_replacing_text_and_embedding_and_merging_metadata(store: VectorStore) -> None:
    store.add_chunks("d", [{"id": "k", "text": "old", "metadata": {"keep": "yes", "x": 1}}], [[1.0, 0, 0]])
    store.add_chunks("d", [{"id": "k", "text": "new", "metadata": {"x": 2}}], [[0, 1.0, 0]])
    hits = store.similarity_search([0, 1.0, 0], top_k=5)
    assert len(hits) == 1 and hits[0]["text"] == "new"
    assert hits[0]["metadata"]["x"] == 2 and hits[0]["metadata"]["keep"] == "yes"
    assert hits[0]["score"] == pytest.approx(0.0, abs=1e-5)  # the embedding moved too


def test_duplicate_ids_in_one_call_are_rejected(store: VectorStore) -> None:
    with pytest.raises(Exception, match=r"(?i)dup|unique"):
        store.add_chunks("d", [{"id": "x", "text": "1"}, {"id": "x", "text": "2"}], [[1.0, 0, 0], [0, 1.0, 0]])


def test_length_mismatch_is_rejected(store: VectorStore) -> None:
    with pytest.raises(ValueError, match="equal length"):
        store.add_chunks("d", [{"text": "a"}], [[1.0, 0, 0], [0, 1.0, 0]])


def test_add_nothing_is_a_noop(store: VectorStore) -> None:
    store.add_chunks("d", [], [])
    assert store.similarity_search([1.0, 0, 0], top_k=3) == []


# ── filters ──────────────────────────────────────────────────────────────────────────────
def _two_docs(store: VectorStore) -> None:
    load(store, "d1")
    load(store, "d2", {"a": [0.9, 0.1, 0.0], "b": [0.0, 0.9, 0.1]})


def test_filter_by_doc_id(store: VectorStore) -> None:
    _two_docs(store)
    hits = store.similarity_search([1.0, 0, 0], top_k=10, filters={"doc_id": "d2"})
    assert {h["metadata"]["doc_id"] for h in hits} == {"d2"} and len(hits) == 2


def test_filter_doc_id_in_list(store: VectorStore) -> None:
    _two_docs(store)
    load(store, "d3")
    hits = store.similarity_search([1.0, 0, 0], top_k=20, filters={"doc_id": {"$in": ["d1", "d3"]}})
    assert {h["metadata"]["doc_id"] for h in hits} == {"d1", "d3"} and len(hits) == 8


def test_filter_returns_top_k_within_the_filter_even_when_closer_rows_are_excluded(store: VectorStore) -> None:
    """A filtered query must still fill top_k from the allowed rows (no post-filter starvation)."""
    _two_docs(store)
    hits = store.similarity_search([1.0, 0, 0], top_k=3, filters={"doc_id": "d1"})
    assert len(hits) == 3 and all(h["metadata"]["doc_id"] == "d1" for h in hits)


def test_filter_on_metadata_keys_combined_with_doc_id(store: VectorStore) -> None:
    _two_docs(store)
    hits = store.similarity_search([1.0, 0, 0], top_k=10, filters={"doc_id": "d1", "tag": "b"})
    assert [h["id"] for h in hits] == ["d1_b"]
    hits = store.similarity_search([1.0, 0, 0], top_k=10, filters={"tag": {"$in": ["a", "zzz"]}})
    assert {h["id"] for h in hits} == {"d1_a", "d2_a"}
    hits = store.similarity_search([1.0, 0, 0], top_k=10, filters={"index": 1})
    assert {h["id"] for h in hits} == {"d1_b", "d2_b"}


def test_filter_matching_nothing_returns_empty(store: VectorStore) -> None:
    _two_docs(store)
    assert store.similarity_search([1.0, 0, 0], top_k=5, filters={"doc_id": "nope"}) == []
    assert store.similarity_search([1.0, 0, 0], top_k=5, filters={"doc_id": {"$in": ["nope", "nada"]}}) == []


# ── incremental-update primitives (ticket 15) ─────────────────────────────────────────────
def test_delete_doc_removes_only_that_document(store: VectorStore) -> None:
    _two_docs(store)
    store.delete_doc("d1")
    hits = store.similarity_search([1.0, 0, 0], top_k=20)
    assert {h["metadata"]["doc_id"] for h in hits} == {"d2"}
    store.delete_doc("never-existed")  # no error


def test_delete_ids_removes_those_vectors_ignores_unknown_and_empty(store: VectorStore) -> None:
    load(store)
    store.delete_ids(["doc_a", "does-not-exist"])
    store.delete_ids([])
    assert {h["id"] for h in store.similarity_search([1.0, 0, 0], top_k=10)} == {"doc_b", "doc_c", "doc_d"}


def test_update_metadata_merges_and_leaves_text_and_embedding_alone(store: VectorStore) -> None:
    load(store)
    before = next(h for h in store.similarity_search([0, 1.0, 0], top_k=10) if h["id"] == "doc_b")
    store.update_metadata(["doc_b"], [{"index": 7, "start": 11}])
    after = next(h for h in store.similarity_search([0, 1.0, 0], top_k=10) if h["id"] == "doc_b")
    assert after["metadata"]["index"] == 7 and after["metadata"]["start"] == 11
    assert after["metadata"]["doc_id"] == "doc" and after["metadata"]["tag"] == "b"  # merged, not replaced
    assert after["text"] == before["text"] and after["score"] == pytest.approx(before["score"])


def test_update_metadata_length_mismatch_rejected(store: VectorStore) -> None:
    with pytest.raises(ValueError, match="equal length"):
        store.update_metadata(["a", "b"], [{"x": 1}])
    store.update_metadata([], [])  # no-op


# ── lifecycle / settings ──────────────────────────────────────────────────────────────────
def test_reset_empties_the_store_and_it_stays_usable(store: VectorStore) -> None:
    load(store)
    store.reset()
    assert store.similarity_search([1.0, 0, 0], top_k=5) == []
    load(store)
    assert len(store.similarity_search([1.0, 0, 0], top_k=5)) == 4
    store.verify()  # the recreated store still matches the configuration


def test_verify_and_describe_on_a_matching_store(store: VectorStore, kind: str) -> None:
    load(store)
    store.verify()
    info = store.describe()
    assert info["store"] == kind and info["count"] == 4 and info["problems"] == []
    assert info["configured"]["metric"] == "cosine"
    assert info["stored"]["metric"] == "cosine" and info["stored"]["dimension"] == 3


def test_wrong_embedding_dimension_is_a_clear_error_not_a_driver_traceback(store: VectorStore) -> None:
    load(store)
    with pytest.raises(VectorStoreMismatchError, match="reindex"):
        store.add_chunks("d", [{"text": "x"}], [[1.0, 0.0]])
    with pytest.raises(VectorStoreMismatchError, match="reindex"):
        store.similarity_search([1.0, 0.0], top_k=1)


def test_bad_filter_operator_is_rejected_on_pgvector_and_not_silently_ignored(kind: str, isolated_db_url: str) -> None:
    if kind != "pgvector":
        pytest.skip("Chroma supports a wider operator set; the app only uses equality and $in")
    with real_store(kind, isolated_db_url) as s, pytest.raises(ValueError, match="Unsupported filter"):
        s.similarity_search([1.0, 0, 0], top_k=1, filters={"index": {"$gt": 1}})

