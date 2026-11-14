from __future__ import annotations

from typing import Any

import app.services.vectorstore as vs
import pytest


class FakeCollection:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, Any]] = []
        self.query_results: dict[str, Any] | None = None
        self.delete_calls: list[dict[str, Any]] = []
        self.name = "documents"

    def upsert(self, **kwargs: Any) -> None:
        self.upsert_calls.append(kwargs)

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return self.query_results or {}

    def delete(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)


class FakeClient:
    def __init__(self, tmp_path: object) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.deleted: list[str] = []
        self.configurations: list[tuple[str, Any]] = []

    def get_or_create_collection(self, name: str, configuration: Any = None) -> FakeCollection:
        self.configurations.append((name, configuration))
        if name not in self.collections:
            self.collections[name] = FakeCollection()
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        self.deleted.append(name)
        self.collections.pop(name, None)


def _make_store(monkeypatch: pytest.MonkeyPatch) -> tuple[vs.ChromaStore, FakeClient, FakeCollection]:
    client = FakeClient(tmp_path=None)
    monkeypatch.setattr("chromadb.HttpClient", lambda **kwargs: client)
    store = vs.ChromaStore(host="localhost", port=8001)
    collection = client.collections["documents"]
    return store, client, collection


def test_get_vector_store_chroma(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubStore:
        def __init__(self, **_: Any) -> None: ...

        def verify(self) -> None: ...

    monkeypatch.setattr(vs, "ChromaStore", StubStore)
    assert isinstance(vs.get_vector_store("chroma"), StubStore)


def test_get_vector_store_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown vector store"):
        vs.get_vector_store("pinecone")


def test_config_rejects_unknown_vector_store() -> None:
    from app.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Unknown vector store"):
        Settings(vector_store="pinecone")


def test_add_chunks_upserts_with_expected_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _client, collection = _make_store(monkeypatch)

    store.add_chunks(
        "doc-1",
        [{"text": "hello", "metadata": {"page": 1}}, {"text": "world", "metadata": {"page": 2}}],
        [[0.1, 0.2], [0.3, 0.4]],
    )

    assert len(collection.upsert_calls) == 1
    call = collection.upsert_calls[0]
    assert call["ids"] == ["doc-1_0", "doc-1_1"]
    assert call["documents"] == ["hello", "world"]
    assert call["embeddings"] == [[0.1, 0.2], [0.3, 0.4]]
    assert call["metadatas"] == [
        {"doc_id": "doc-1", "index": 0, "page": 1},
        {"doc_id": "doc-1", "index": 1, "page": 2},
    ]


def test_add_chunks_drops_non_scalar_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _client, collection = _make_store(monkeypatch)

    store.add_chunks(
        "doc-1",
        [{"text": "x", "metadata": {"nested": {"a": 1}, "ok": "kept"}}],
        [[1.0]],
    )

    metadatas = collection.upsert_calls[0]["metadatas"]
    assert metadatas == [{"doc_id": "doc-1", "index": 0, "ok": "kept"}]


def test_add_chunks_length_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _client, _collection = _make_store(monkeypatch)

    with pytest.raises(ValueError, match="must have equal length"):
        store.add_chunks("doc-1", [{"text": "a"}, {"text": "b"}], [[1.0]])


def test_similarity_search_maps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _client, collection = _make_store(monkeypatch)
    collection.query_results = {
        "ids": [["doc-1_0", "doc-1_1"]],
        "documents": [["hello", "world"]],
        "metadatas": [[{"doc_id": "doc-1", "index": 0}, {"doc_id": "doc-1", "index": 1}]],
        "distances": [[0.1, 0.2]],
    }

    results = store.similarity_search([1.0, 2.0], top_k=2)
    assert len(results) == 2
    assert results[0] == {
        "id": "doc-1_0",
        "score": 0.1,
        "text": "hello",
        "metadata": {"doc_id": "doc-1", "index": 0},
    }
    assert results[1]["id"] == "doc-1_1"


def test_similarity_search_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _client, collection = _make_store(monkeypatch)
    collection.query_results = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

    assert store.similarity_search([1.0]) == []


def test_delete_doc_delegates_to_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _client, collection = _make_store(monkeypatch)

    store.delete_doc("doc-1")
    assert collection.delete_calls[0] == {"where": {"doc_id": "doc-1"}}


def test_reset_deletes_and_recreates_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    store, client, _collection = _make_store(monkeypatch)

    store.reset()
    assert client.deleted == ["documents"]
    assert "documents" in client.collections

def test_chroma_collection_is_created_with_the_configured_hnsw_options(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(tmp_path=None)
    monkeypatch.setattr("chromadb.HttpClient", lambda **kwargs: client)
    vs.ChromaStore(
        collection_name="c",
        vector_settings=vs.VectorSettings(
            store="chroma", metric="cosine", index="hnsw", hnsw_m=8, hnsw_ef_construction=50, hnsw_ef_search=30
        ),
    )
    assert client.configurations == [
        ("c", {"hnsw": {"space": "cosine", "max_neighbors": 8, "ef_construction": 50, "ef_search": 30}})
    ]


def test_default_chroma_collection_config_only_names_the_default_space(monkeypatch: pytest.MonkeyPatch) -> None:
    store, client, _c = _make_store(monkeypatch)
    assert client.configurations == [("documents", {"hnsw": {"space": "l2"}})]
    store.reset()
    assert client.configurations[-1] == ("documents", {"hnsw": {"space": "l2"}})


def test_get_vector_store_pgvector_builds_the_pg_store_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings
    from app.services import pgvector_store

    calls: list[str] = []

    class StubPg:
        @classmethod
        def from_settings(cls, settings: Settings) -> StubPg:
            calls.append("built")
            return cls()

        def verify(self) -> None:
            calls.append("verified")

    monkeypatch.setattr(pgvector_store, "PgVectorStore", StubPg)
    vs._verified.clear()
    cfg = Settings(vector_store="pgvector")
    assert isinstance(vs.get_vector_store(settings=cfg), StubPg)
    vs.get_vector_store(settings=cfg)  # second call within the TTL does not re-verify
    assert calls == ["built", "verified", "built"]
    vs.get_vector_store(settings=cfg, verify=False)
    vs._verified.clear()
