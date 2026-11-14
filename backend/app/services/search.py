"""The ONE retrieval path shared by ``POST /chat`` and ``POST /search``.

``retrieve_ranked`` = retrieve (any mode) -> rank the hits 1..n -> optional neighbour context.
It returns the hits and the context separately so each endpoint can present them its own way:
``/search`` groups the context under its hit, ``/chat`` feeds the flat reading-order passages to the LLM.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace

import structlog
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import observability
from app.services.context import add_neighbor_context
from app.services.embeddings import Embedder
from app.services.keyword import KeywordIndexMismatchError
from app.services.retriever import ChunkResult, RetrievalOptions, retrieve
from app.services.vectorstore import VectorStore, VectorStoreMismatchError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Retrieval:
    #: The search hits, best first, ``rank`` = 1..n.
    hits: list[ChunkResult]
    #: Hits plus their neighbour-context chunks in reading order (equals ``hits`` when neighbors == 0).
    passages: list[ChunkResult]

    def context_for(self, hit: ChunkResult) -> list[ChunkResult]:
        """The context chunks that accompany ``hit`` (document order, unscored)."""
        key = (hit.document_id, hit.chunk_index)
        return [p for p in self.passages if p.context_for == key]


@contextmanager
def retrieval_errors() -> Iterator[None]:
    """Map retrieval failures to the 503s both endpoints return (the message names the fix)."""
    try:
        yield
    except KeywordIndexMismatchError as exc:
        logger.error("keyword_index_mismatch", error=str(exc))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except VectorStoreMismatchError as exc:
        logger.error("vector_store_mismatch", problems=exc.problems)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("retrieval_failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Retrieval backend unavailable.") from exc


async def retrieve_ranked(
    query: str,
    *,
    top_k: int,
    neighbors: int,
    options: RetrievalOptions,
    document_ids: Sequence[uuid.UUID] | None,
    embedder: Embedder,
    store: VectorStore,
    session: AsyncSession,
    span_prefix: str,
) -> Retrieval:
    """Retrieve, rank and (optionally) add neighbour context. Raises ``HTTPException(503)`` on failure."""
    with retrieval_errors():
        with observability.span(f"{span_prefix}.retrieve", top_k=top_k, **options.trace_meta()) as sp:
            found = await retrieve(
                query,
                top_k=top_k,
                embedder=embedder,
                store=store,
                session=session,
                document_ids=list(document_ids) if document_ids else None,
                options=options,
            )
            sp.update(metadata={"hits": len(found)})
        hits = [replace(h, rank=i) for i, h in enumerate(found, 1)]
        passages = hits
        if neighbors:
            with observability.span(f"{span_prefix}.context", neighbors=neighbors):
                passages = await add_neighbor_context(hits, neighbors, session=session)
    return Retrieval(hits=hits, passages=passages)
