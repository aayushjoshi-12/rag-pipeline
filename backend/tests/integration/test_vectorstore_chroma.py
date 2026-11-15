"""Integration tests for ChromaStore against the compose Chroma server.

Prereq: the throwaway test Chroma on :8002 (`make chroma-test-up`; `make check` does it). Never the dev Chroma.
These tests hit the real Chroma API; Chroma is not mocked here by design, but the
embedding vectors are synthetic (no Ollama/LLM calls).
"""

from __future__ import annotations

import pytest
from app.services.vectorstore import ChromaStore


@pytest.fixture
def chroma_store() -> ChromaStore:
    store = ChromaStore(collection_name="test_store")
    store.reset()
    yield store
    store.reset()


def test_add_and_retrieve_chunks(chroma_store: ChromaStore) -> None:
    embedded = [[1.0, 0.0], [0.0, 1.0], [0.707, 0.707]]
    chroma_store.add_chunks(
        "doc-1",
        [
            {"text": "alpha", "metadata": {"index": 0}},
            {"text": "beta", "metadata": {"index": 1}},
            {"text": "gamma", "metadata": {"index": 2}},
        ],
        embedded,
    )

    results = chroma_store.similarity_search([1.0, 0.0], top_k=1)
    assert len(results) == 1
    # Nearest to [1.0, 0.0] is alpha ([1.0, 0.0]).
    assert results[0]["text"] == "alpha"


def test_similarity_search_respects_top_k(chroma_store: ChromaStore) -> None:
    chroma_store.add_chunks(
        "doc-2",
        [
            {"text": "a", "metadata": {}},
            {"text": "b", "metadata": {}},
            {"text": "c", "metadata": {}},
        ],
        [[1.0, 0.0], [0.9, 0.1], [0.5, 0.5]],
    )

    results = chroma_store.similarity_search([1.0, 0.0], top_k=2)
    assert len(results) == 2


def test_delete_doc_removes_chunks(chroma_store: ChromaStore) -> None:
    chroma_store.add_chunks("doc-3", [{"text": "x", "metadata": {}}], [[1.0, 0.0]])

    before = chroma_store.similarity_search([1.0, 0.0], top_k=10)
    assert any(r["id"].startswith("doc-3_") for r in before)

    chroma_store.delete_doc("doc-3")

    after = chroma_store.similarity_search([1.0, 0.0], top_k=10)
    assert not any(r["id"].startswith("doc-3_") for r in after)

def test_explicit_ids_delete_ids_and_update_metadata(chroma_store: ChromaStore) -> None:
    chroma_store.add_chunks(
        "doc-9",
        [
            {"id": "doc-9_aaa", "text": "alpha", "metadata": {"index": 0}},
            {"id": "doc-9_bbb", "text": "beta", "metadata": {"index": 1}},
            {"id": "doc-9_ccc", "text": "gamma", "metadata": {"index": 2}},
        ],
        [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]],
    )
    hits = chroma_store.similarity_search([1.0, 0.0], top_k=3)
    assert {h["id"] for h in hits} == {"doc-9_aaa", "doc-9_bbb", "doc-9_ccc"}

    chroma_store.update_metadata(["doc-9_bbb"], [{"index": 7}])
    moved = next(h for h in chroma_store.similarity_search([0.0, 1.0], top_k=3) if h["id"] == "doc-9_bbb")
    assert moved["metadata"]["index"] == 7 and moved["metadata"]["doc_id"] == "doc-9"
    assert moved["text"] == "beta"  # embedding and text untouched

    chroma_store.delete_ids(["doc-9_aaa", "does-not-exist"])
    assert {h["id"] for h in chroma_store.similarity_search([1.0, 0.0], top_k=3)} == {"doc-9_bbb", "doc-9_ccc"}
    chroma_store.delete_ids([])  # no-op


def test_update_metadata_length_mismatch_rejected(chroma_store: ChromaStore) -> None:
    with pytest.raises(ValueError, match="equal length"):
        chroma_store.update_metadata(["a", "b"], [{"x": 1}])
