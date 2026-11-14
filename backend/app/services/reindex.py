"""Rebuild the vector index from the chunk text already stored in Postgres (no re-parsing).

Needed after changing ``VECTOR_STORE`` / ``VECTOR_METRIC`` / ``VECTOR_INDEX`` (index type, HNSW M /
ef_construction, IVFFlat lists) / the embedding model or dimension. ``reindex_all`` drops the
vector store, recreates it with the *configured* settings and re-embeds every chunk row. The
vectors get the same ids and metadata an ingest would give them, so later incremental updates
keep working. Search is unavailable/partial while it runs: pause ingestion and treat it as a
maintenance window. A failure part-way leaves a partial index; just run it again.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import JobStatus
from app.db.models import Chunk, Job
from app.services.embeddings import Embedder
from app.services.vectorstore import VectorStore

logger = structlog.get_logger(__name__)

_EMBED_BATCH = 32


class ReindexError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReindexResult:
    documents: int
    vectors: int
    seconds: float


async def expected_vector_count(session: AsyncSession) -> int:
    """Distinct vectors the database says should exist (chunk rows share a vector per content hash)."""
    rows = (await session.execute(select(Chunk.document_id, Chunk.metadata_))).all()
    return len({(r[0], str(r[1].get("vector_id") or "")) for r in rows})


async def store_status(session: AsyncSession, store: VectorStore) -> dict[str, Any]:
    """``store.describe()`` plus the database cross-check: an empty store while chunk rows exist means
    the store was switched/wiped and needs a reindex (searches would silently return nothing)."""
    info = await asyncio.to_thread(store.describe)
    expected = await expected_vector_count(session)
    info["expected_vectors"] = expected
    problems = list(info.get("problems", []))
    if expected > 0 and info.get("count", 0) == 0:
        problems.append(f"the vector store is empty but the database holds {expected} chunks (store switched or wiped)")
    info["problems"] = problems
    return info


async def reindex_all(
    session: AsyncSession,
    *,
    embedder: Embedder,
    store: VectorStore,
    force: bool = False,
) -> ReindexResult:
    started = time.perf_counter()
    if not force:
        running = (
            await session.execute(select(func.count()).select_from(Job).where(Job.status == JobStatus.RUNNING))
        ).scalar_one()
        if running:
            raise ReindexError(f"{running} ingest job(s) are running; wait for them or pass force=True.")

    rows = (await session.execute(select(Chunk).order_by(Chunk.document_id, Chunk.index))).scalars().all()
    by_doc: dict[uuid.UUID, list[Chunk]] = {}
    for r in rows:
        by_doc.setdefault(r.document_id, []).append(r)

    had_flag = hasattr(store, "defer_index_build")  # pgvector: train ivfflat once, after the load
    if had_flag:
        setattr(store, "defer_index_build", True)  # noqa: B010 - optional, backend-specific attribute
    vectors = 0
    try:
        await asyncio.to_thread(store.reset)
        for doc_id, chunks in by_doc.items():
            doc_key = str(doc_id)
            seen: set[str] = set()
            items: list[dict[str, Any]] = []
            for c in chunks:  # first occurrence of a content hash wins, as in ingestion
                vid = str(c.metadata_.get("vector_id") or f"{doc_key}_{c.index}")
                if vid in seen:
                    continue
                seen.add(vid)
                items.append({"id": vid, "text": c.text, "metadata": {**dict(c.metadata_), "index": c.index}})
            for i in range(0, len(items), _EMBED_BATCH):
                batch = items[i : i + _EMBED_BATCH]
                vecs = await asyncio.to_thread(embedder.embed_documents, [b["text"] for b in batch])
                await asyncio.to_thread(store.add_chunks, doc_key, batch, vecs)
                vectors += len(batch)
    finally:
        if had_flag:
            setattr(store, "defer_index_build", False)  # noqa: B010 - see above
    rebuild = getattr(store, "rebuild_index", None)
    if callable(rebuild):
        await asyncio.to_thread(rebuild)  # ivfflat: train the centroids on the full data set
    await asyncio.to_thread(store.verify)
    result = ReindexResult(documents=len(by_doc), vectors=vectors, seconds=time.perf_counter() - started)
    logger.info("reindex_done", documents=result.documents, vectors=result.vectors, seconds=round(result.seconds, 1))
    return result
