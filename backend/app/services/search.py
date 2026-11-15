"""The ONE retrieval path shared by ``POST /chat`` and ``POST /search``.

``retrieve_ranked`` = retrieve (any mode) -> rank the hits 1..n -> optional neighbour context.
It returns the hits and the context separately so each endpoint can present them its own way:
``/search`` groups the context under its hit, ``/chat`` feeds the flat reading-order passages to the LLM.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import observability
from app.services.cache import (
    PREFIX,
    CacheCall,
    QueryCache,
    decode_vector,
    digest,
    encode_vector,
    normalize_query,
)
from app.services.context import add_neighbor_context
from app.services.embeddings import Embedder, EmbeddingMeter
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


def embedding_usage(meter: EmbeddingMeter, call: CacheCall, mode: str) -> dict[str, Any] | None:
    """Query-embedding usage for a trace: ``{tokens, cached?}``, or ``None`` when nothing is known/applicable.

    A cache hit (embedding or retrieval layer) means no provider call happened: 0 tokens, marked cached."""
    if mode == "keyword":
        return None
    if call.embeddings_avoided:
        return {"tokens": 0, "cached": True}
    if meter.tokens is not None:
        return {"tokens": meter.tokens}
    return None


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
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Retrieval backend unavailable."
        ) from exc


# ── query-cache keys and codecs ──────────────────────────────────────────────────────────────────
def options_fingerprint(opts: RetrievalOptions) -> dict[str, Any]:
    """Only the knobs that change the results of ``opts.mode`` (a hybrid-only knob must not split semantic keys)."""
    fp: dict[str, Any] = {"mode": opts.mode, "dedupe": opts.dedupe, "candidates": opts.candidates}
    if opts.mode in ("keyword", "hybrid"):
        fp.update(
            keyword_method=opts.keyword_method,
            fts_config=opts.fts_config,
            keyword_pool=opts.keyword_pool,
        )
        if opts.keyword_method == "bm25":
            fp.update(bm25_k1=opts.bm25_k1, bm25_b=opts.bm25_b)
    if opts.mode == "hybrid":
        fp["fusion"] = opts.fusion
        if opts.fusion == "rrf":
            fp["rrf_k"] = opts.rrf_k
        else:
            fp.update(weight_semantic=opts.weight_semantic, weight_keyword=opts.weight_keyword)
    return fp


def retrieval_key(
    cache: QueryCache,
    version: str,
    query: str,
    *,
    top_k: int,
    neighbors: int,
    options: RetrievalOptions,
    document_ids: Sequence[uuid.UUID] | None,
) -> str:
    docs = sorted({str(d) for d in document_ids or []})
    return (
        PREFIX
        + "retrieval:"
        + digest(
            cache.fingerprint,
            version,
            normalize_query(query),
            top_k,
            neighbors,
            docs,
            options_fingerprint(options),
        )
    )


def embedding_key(cache: QueryCache, embedder: Embedder, query: str) -> str:
    ident = f"{type(embedder).__name__}:{getattr(embedder, 'model', '')}"
    return PREFIX + "embedding:" + digest(ident, normalize_query(query))


def _chunk_to_json(c: ChunkResult) -> dict[str, Any]:
    return {
        "text": c.text,
        "doc": str(c.document_id),
        "idx": c.chunk_index,
        "file": c.filename,
        "score": c.score,
        "is_hit": c.is_hit,
        "kind": c.score_kind,
        "sr": c.semantic_rank,
        "kr": c.keyword_rank,
        "rank": c.rank,
        "ctx": [str(c.context_for[0]), c.context_for[1]] if c.context_for else None,
    }


def _chunk_from_json(d: dict[str, Any]) -> ChunkResult:
    ctx = d["ctx"]
    return ChunkResult(
        text=d["text"],
        document_id=uuid.UUID(d["doc"]),
        chunk_index=d["idx"],
        filename=d["file"],
        score=d["score"],
        is_hit=d["is_hit"],
        score_kind=d["kind"],
        semantic_rank=d["sr"],
        keyword_rank=d["kr"],
        rank=d["rank"],
        context_for=(uuid.UUID(ctx[0]), ctx[1]) if ctx else None,
    )


def encode_retrieval(r: Retrieval) -> bytes:
    return json.dumps(
        {
            "hits": [_chunk_to_json(h) for h in r.hits],
            "passages": [_chunk_to_json(p) for p in r.passages],
        },
        separators=(",", ":"),
    ).encode()


def decode_retrieval(raw: bytes) -> Retrieval:
    d = json.loads(raw)
    return Retrieval(
        hits=[_chunk_from_json(h) for h in d["hits"]],
        passages=[_chunk_from_json(p) for p in d["passages"]],
    )


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
    cache: CacheCall | None = None,
) -> Retrieval:
    """Retrieve, rank and (optionally) add neighbour context. Raises ``HTTPException(503)`` on failure.

    With ``cache`` the result is served from / stored in the L2 retrieval cache, and the query embedding goes
    through L1. Errors are never cached (``QueryCache.cached`` stores only after a successful compute).
    """
    query = normalize_query(query)  # the same text is embedded/searched with and without the cache
    if cache is None or not cache.cache.enabled:
        return await _retrieve_ranked(
            query,
            top_k,
            neighbors,
            options,
            document_ids,
            embedder,
            store,
            session,
            span_prefix,
            None,
        )
    qc = cache.cache
    version = await qc.index_version() if qc.layer_on("retrieval") and not cache.bypass else None

    async def compute() -> Retrieval:
        return await _retrieve_ranked(
            query,
            top_k,
            neighbors,
            options,
            document_ids,
            embedder,
            store,
            session,
            span_prefix,
            cache,
        )

    if (
        version is None
    ):  # bypass, layer off, or Redis unavailable: compute (L1 still applies unless bypassed)
        cache.layers["retrieval"] = "bypass" if cache.bypass else "off"
        if cache.bypass:  # ``cached()`` is skipped here, so count the bypass ourselves (stats parity with L1/L3)
            qc.counters["retrieval"].bypass += 1
        return await compute()
    key = retrieval_key(
        qc,
        version,
        query,
        top_k=top_k,
        neighbors=neighbors,
        options=options,
        document_ids=document_ids,
    )
    result, state = await qc.cached("retrieval", key, compute, encode_retrieval, decode_retrieval)
    cache.layers["retrieval"] = state
    if state in ("hit", "coalesced") and options.mode != "keyword":
        cache.embeddings_avoided += 1
    return result


async def _retrieve_ranked(
    query: str,
    top_k: int,
    neighbors: int,
    options: RetrievalOptions,
    document_ids: Sequence[uuid.UUID] | None,
    embedder: Embedder,
    store: VectorStore,
    session: AsyncSession,
    span_prefix: str,
    cache: CacheCall | None,
) -> Retrieval:
    embed_query = None
    if cache is not None and options.mode != "keyword" and cache.cache.layer_on("embedding"):
        call, qc = cache, cache.cache

        async def embed_query(text: str) -> list[float]:
            async def compute() -> list[float]:
                return await asyncio.to_thread(embedder.embed_query, text)

            vec, state = await qc.cached(
                "embedding",
                embedding_key(qc, embedder, text),
                compute,
                encode_vector,
                decode_vector,
                bypass=call.bypass,
            )
            call.layers["embedding"] = state
            if state in ("hit", "coalesced"):
                call.embeddings_avoided += 1
            return vec

    with retrieval_errors():
        with observability.span(
            f"{span_prefix}.retrieve", top_k=top_k, **options.trace_meta()
        ) as sp:
            found = await retrieve(
                query,
                top_k=top_k,
                embedder=embedder,
                store=store,
                session=session,
                document_ids=list(document_ids) if document_ids else None,
                options=options,
                embed_query=embed_query,
            )
            sp.update(metadata={"hits": len(found)})
        hits = [replace(h, rank=i) for i, h in enumerate(found, 1)]
        passages = hits
        if neighbors:
            with observability.span(f"{span_prefix}.context", neighbors=neighbors):
                passages = await add_neighbor_context(hits, neighbors, session=session)
    return Retrieval(hits=hits, passages=passages)
