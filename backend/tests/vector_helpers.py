"""Build real vector stores on throwaway names for integration tests.

Chroma: a uniquely named collection on the compose server (only that collection is ever deleted).
pgvector: a uniquely named table in the isolated test database (``isolated_db_url``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from app.services.pgvector_store import PgVectorStore, dsn_from_database_url
from app.services.vectorstore import ChromaStore, VectorSettings, VectorStore


def settings_for(kind: str, metric: str = "cosine", index: str = "hnsw", dimension: int | None = 3, **kw: Any) -> VectorSettings:
    if kind == "chroma":
        return VectorSettings(store="chroma", metric=metric, index="hnsw", dimension=dimension, **kw)
    defaults: dict[str, Any] = {
        "hnsw_m": 16, "hnsw_ef_construction": 64, "hnsw_ef_search": 40, "ivfflat_lists": 100, "ivfflat_probes": 10,
    }
    return VectorSettings(store="pgvector", metric=metric, index=index, dimension=dimension, **{**defaults, **kw})


@contextmanager
def real_store(
    kind: str,
    pg_url: str,
    *,
    metric: str = "cosine",
    index: str = "hnsw",
    dimension: int | None = 3,
    name: str | None = None,
    verify: bool = True,
    **kw: Any,
) -> Iterator[VectorStore]:
    """A verified real store; its throwaway collection/table is removed on exit."""
    vs = settings_for(kind, metric, index, dimension, **kw)
    name = name or f"vi_{uuid.uuid4().hex[:12]}"
    if kind == "chroma":
        store: Any = ChromaStore(collection_name=f"test_{name}", vector_settings=vs)
        try:
            if verify:
                store.verify()
            yield store
        finally:
            store._client.delete_collection(name=f"test_{name}")
        return
    store = PgVectorStore(dsn_from_database_url(pg_url), vs, table=name)
    try:
        if verify:
            store.verify()
        yield store
    finally:
        with psycopg.connect(dsn_from_database_url(pg_url), autocommit=True) as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')


Factory = Callable[..., Any]


class InspectableStore:
    """Wraps a real store so tests written against ``tests.fakes.FakeStore`` (``.vectors``, ``.calls``,
    ``.texts_for``) run unchanged against Chroma/pgvector. Reads go straight to the backing service."""

    def __init__(self, inner: Any, reader: Callable[[], dict[str, dict[str, Any]]]) -> None:
        self._inner = inner
        self._reader = reader
        self.calls: list[str] = []

    @property
    def vectors(self) -> dict[str, dict[str, Any]]:
        return self._reader()

    def texts_for(self, doc_id: str) -> set[str]:
        return {v["text"] for v in self.vectors.values() if v["metadata"]["doc_id"] == doc_id}

    def _rec(self, name: str, *a: Any) -> Any:
        self.calls.append({"add_chunks": "add"}.get(name, name))
        return getattr(self._inner, name)(*a)

    def add_chunks(self, *a: Any) -> Any:
        return self._rec("add_chunks", *a)

    def delete_doc(self, *a: Any) -> Any:
        return self._rec("delete_doc", *a)

    def delete_ids(self, *a: Any) -> Any:
        return self._rec("delete_ids", *a)

    def update_metadata(self, *a: Any) -> Any:
        return self._rec("update_metadata", *a)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def reader_for(kind: str, store: Any, pg_url: str) -> Callable[[], dict[str, dict[str, Any]]]:
    if kind == "chroma":

        def read_chroma() -> dict[str, dict[str, Any]]:
            got = store._collection.get(include=["documents", "metadatas"])
            return {
                i: {"text": t, "metadata": dict(m)}
                for i, t, m in zip(got["ids"], got["documents"] or [], got["metadatas"] or [], strict=True)
            }

        return read_chroma

    def read_pg() -> dict[str, dict[str, Any]]:
        with psycopg.connect(dsn_from_database_url(pg_url)) as conn:
            rows = conn.execute(f'SELECT id, text, metadata FROM "{store._table}"').fetchall()
        return {r[0]: {"text": r[1], "metadata": r[2]} for r in rows}

    return read_pg
