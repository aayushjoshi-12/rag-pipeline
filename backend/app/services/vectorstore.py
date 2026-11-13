from __future__ import annotations

from typing import Any, Protocol, cast, runtime_checkable

from app.config import Settings

_settings = Settings()

_VALID_VECTOR_STORES = ("chroma",)


@runtime_checkable
class VectorStore(Protocol):
    """Interface for vector-store backends."""

    def add_chunks(
        self,
        doc_id: str,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        """Persist chunks and their pre-computed embeddings."""
        ...

    def similarity_search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the *top_k* nearest neighbours with scores."""
        ...

    def delete_doc(self, doc_id: str) -> None:
        """Remove all chunks belonging to *doc_id*."""
        ...

    def delete_ids(self, ids: list[str]) -> None:
        """Remove specific vectors by id (no-op for ids that do not exist)."""
        ...

    def update_metadata(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        """Merge new metadata into existing vectors without touching their embeddings."""
        ...

    def reset(self) -> None:
        """Drop and recreate the collection (dev/test only)."""
        ...


class ChromaStore:
    """ChromaDB vector-store adapter (server mode via ``chromadb.HttpClient``)."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        collection_name: str = "documents",
    ) -> None:
        import chromadb

        self._host = host or _settings.chroma_host
        self._port = port or _settings.chroma_port
        self._client = chromadb.HttpClient(host=self._host, port=self._port)
        self._collection = self._client.get_or_create_collection(name=collection_name)

    def add_chunks(
        self,
        doc_id: str,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) must have equal length."
            )

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for idx, chunk in enumerate(chunks):
            # An explicit id (content-addressed, see services.incremental) wins over position.
            chunk_id = str(chunk.get("id") or f"{doc_id}_{idx}")
            ids.append(chunk_id)
            documents.append(chunk["text"])
            metadatas.append(
                {
                    "doc_id": doc_id,
                    "index": idx,
                    **{k: v for k, v in chunk.get("metadata", {}).items() if isinstance(v, (str, int, float, bool))},
                }
            )

        self._collection.upsert(
            ids=ids,
            documents=documents,
            # chromadb's stubs expect numpy arrays / Sequence[float]; list[list[float]] is
            # accepted at runtime and converted internally.
            embeddings=cast(Any, embeddings),
            metadatas=cast(Any, metadatas),
        )

    def similarity_search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] | None = None
        if filters:
            # Chroma where filter expects a flat dict for simple equality.
            if len(filters) == 1:
                key, value = next(iter(filters.items()))
                where = {key: value}
            else:
                where = {"$and": [{k: v} for k, v in filters.items()]}

        results = self._collection.query(
            query_embeddings=cast(Any, [query_embedding]),
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        output: list[dict[str, Any]] = []
        ids = results["ids"]
        if not ids or not ids[0]:
            return output
        distances = results["distances"] or []
        documents = results["documents"] or []
        metadatas = results["metadatas"] or []

        for i, chunk_id in enumerate(ids[0]):
            output.append(
                {
                    "id": chunk_id,
                    "score": distances[0][i],
                    "text": documents[0][i] if documents else "",
                    "metadata": metadatas[0][i] if metadatas else {},
                }
            )
        return output

    def delete_doc(self, doc_id: str) -> None:
        self._collection.delete(where={"doc_id": doc_id})

    def delete_ids(self, ids: list[str]) -> None:
        if ids:
            self._collection.delete(ids=ids)

    def update_metadata(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        if len(ids) != len(metadatas):
            raise ValueError("ids and metadatas must have equal length.")
        if ids:
            self._collection.update(ids=ids, metadatas=cast(Any, metadatas))

    def reset(self) -> None:
        name = self._collection.name
        self._client.delete_collection(name=name)
        self._collection = self._client.get_or_create_collection(name=name)


def get_vector_store(vector_store: str | None = None) -> VectorStore:
    """Factory that returns the configured vector-store backend."""
    vector_store = vector_store or _settings.vector_store

    if vector_store not in _VALID_VECTOR_STORES:
        raise ValueError(
            f"Unknown vector store '{vector_store}'. "
            f"Supported: {', '.join(_VALID_VECTOR_STORES)}."
        )

    if vector_store == "chroma":
        return ChromaStore()

    raise ValueError(f"Unhandled vector store '{vector_store}'.")  # pragma: no cover
