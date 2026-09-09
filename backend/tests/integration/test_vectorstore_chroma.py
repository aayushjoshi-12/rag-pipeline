"""Integration tests for ChromaStore against the compose Chroma server.

Prereq: `make db-up` (Docker running) — host :8001 maps to the Chroma container.
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