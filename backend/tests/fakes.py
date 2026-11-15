"""Shared in-memory fakes for the embedder and vector store (honour explicit chunk ids)."""

from __future__ import annotations

from typing import Any


class FakeEmbedder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.embedded_texts: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedder down")
        self.embedded_texts.extend(texts)
        return [[float(len(t)), 1.0, 0.0] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 1.0, 0.0]


class FakeStore:
    """id -> {text, metadata, embedding}; mirrors ChromaStore's id/metadata semantics."""

    def __init__(self) -> None:
        self.vectors: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def add_chunks(
        self, doc_id: str, chunks: list[dict[str, Any]], embeddings: list[list[float]]
    ) -> None:
        assert len(chunks) == len(embeddings)
        self.calls.append("add")
        ids = [str(c.get("id") or f"{doc_id}_{i}") for i, c in enumerate(chunks)]
        assert len(set(ids)) == len(ids), "duplicate ids in one add (Chroma rejects this)"
        for i, c, e in zip(ids, chunks, embeddings, strict=True):
            meta = {"doc_id": doc_id, "index": 0, **c.get("metadata", {})}
            self.vectors[i] = {"text": c["text"], "metadata": meta, "embedding": e}

    def similarity_search(
        self, query_embedding: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    def delete_doc(self, doc_id: str) -> None:
        self.calls.append("delete_doc")
        self.vectors = {k: v for k, v in self.vectors.items() if v["metadata"]["doc_id"] != doc_id}

    def delete_ids(self, ids: list[str]) -> None:
        self.calls.append("delete_ids")
        for i in ids:
            self.vectors.pop(i, None)

    def update_metadata(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        self.calls.append("update_metadata")
        for i, m in zip(ids, metadatas, strict=True):
            self.vectors[i]["metadata"].update(m)

    def reset(self) -> None:
        self.vectors.clear()

    def texts_for(self, doc_id: str) -> set[str]:
        return {v["text"] for v in self.vectors.values() if v["metadata"]["doc_id"] == doc_id}


class MeteredEmbedder(FakeEmbedder):
    """A FakeEmbedder that reports provider usage like the real Ollama/OpenAI embedders (10 tokens per text)."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from app.services.embeddings import _report

        out = super().embed_documents(texts)
        _report(10 * len(texts))
        return out

    def embed_query(self, text: str) -> list[float]:
        from app.services.embeddings import _report

        _report(10)
        return super().embed_query(text)
