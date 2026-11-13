"""Similarity retrieval: embed the query, search the vector store, enrich from Postgres."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ops
from app.services.embeddings import Embedder
from app.services.vectorstore import VectorStore


@dataclass(frozen=True)
class ChunkResult:
    text: str
    document_id: uuid.UUID
    chunk_index: int
    filename: str
    score: float


async def retrieve(
    query: str,
    *,
    top_k: int,
    embedder: Embedder,
    store: VectorStore,
    session: AsyncSession,
    document_ids: list[uuid.UUID] | None = None,
) -> list[ChunkResult]:
    """Return the ``top_k`` nearest chunks, optionally restricted to ``document_ids``.

    Hits whose document no longer exists in Postgres (e.g. deleted mid-flight) are dropped.
    Lower ``score`` = closer (vector-store distance).
    """
    if top_k <= 0:
        return []
    vector = await asyncio.to_thread(embedder.embed_query, query)
    filters = None
    if document_ids:
        ids = [str(d) for d in document_ids]
        filters = {"doc_id": ids[0]} if len(ids) == 1 else {"doc_id": {"$in": ids}}
    hits = await asyncio.to_thread(store.similarity_search, vector, top_k, filters)

    names: dict[uuid.UUID, str | None] = {}
    results: list[ChunkResult] = []
    for hit in hits:
        meta = hit.get("metadata") or {}
        try:
            doc_id = uuid.UUID(str(meta["doc_id"]))
            index = int(meta["index"])
        except (KeyError, ValueError):
            continue
        if doc_id not in names:
            doc = await ops.get_document(doc_id, session=session)
            names[doc_id] = doc.filename if doc else None
        filename = names[doc_id]
        if filename is None:
            continue
        results.append(
            ChunkResult(
                text=str(hit.get("text", "")),
                document_id=doc_id,
                chunk_index=index,
                filename=filename,
                score=float(hit.get("score", 0.0)),
            )
        )
    return results
