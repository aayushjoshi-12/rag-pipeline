"""Retrieval: semantic (embeddings), keyword (Postgres FTS / BM25) or hybrid (both, fused).

Score semantics. ``ChunkResult.score`` is **mode-specific**; ``score_kind`` says which and results are
always returned best first:

=============  ==================================================  =================
``score_kind``  meaning                                             better
=============  ==================================================  =================
``distance``    vector-store distance (semantic mode)               lower
``bm25``        BM25 score (keyword mode, default method)           higher
``ts_rank_cd``  Postgres cover-density rank (keyword, postgres_fts)  higher
``rrf``         sum of 1 / (k + rank) over the lists (hybrid)       higher
``weighted``    weighted sum of min-max normalised scores, 0..1     higher
=============  ==================================================  =================

Fused scores are only comparable within one response. In hybrid mode ``semantic_rank`` /
``keyword_rank`` give the 1-based rank in each candidate list (None = not among that list's candidates).
Neighbour-context expansion (services/context.py) is a separate step applied after retrieval.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Hashable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import FUSION_METHODS, KEYWORD_METHODS, RETRIEVAL_MODES, Settings
from app.db import ops
from app.services import fusion, keyword
from app.services.embeddings import Embedder
from app.services.vectorstore import VectorStore

logger = logging.getLogger(__name__)

#: ``ChunkResult.score_kind`` values whose scores are "lower = better".
LOWER_IS_BETTER = frozenset({"distance"})


@dataclass(frozen=True)
class ChunkResult:
    text: str
    document_id: uuid.UUID
    chunk_index: int
    filename: str
    #: Meaning depends on ``score_kind`` (see module docstring).
    score: float
    #: False for a neighbouring chunk added as context (see services/context.py)
    is_hit: bool = True
    score_kind: str = "distance"
    semantic_rank: int | None = None
    keyword_rank: int | None = None


@dataclass(frozen=True)
class RetrievalOptions:
    """Effective retrieval settings for one request (deployment settings + per-request overrides)."""

    mode: str = "semantic"
    keyword_method: str = "bm25"
    fusion: str = "rrf"
    rrf_k: int = 60
    weight_semantic: float = 0.6
    weight_keyword: float = 0.4
    candidates: int = 50
    keyword_pool: int = 200
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    fts_config: str = "english"
    dedupe: bool = True

    def __post_init__(self) -> None:
        for name, value, allowed in (
            ("mode", self.mode, RETRIEVAL_MODES),
            ("keyword_method", self.keyword_method, KEYWORD_METHODS),
            ("fusion", self.fusion, FUSION_METHODS),
        ):
            if value not in allowed:
                raise ValueError(f"Unknown retrieval {name} {value!r}; allowed: {', '.join(allowed)}")

    @classmethod
    def from_settings(cls, cfg: Settings, *, mode: str | None = None, fusion: str | None = None) -> RetrievalOptions:
        """Deployment settings, with the per-request ``mode`` / ``fusion`` taking precedence when given."""
        return cls(
            mode=mode or cfg.retrieval_mode,
            keyword_method=cfg.retrieval_keyword_method,
            fusion=fusion or cfg.retrieval_fusion,
            rrf_k=cfg.retrieval_rrf_k,
            weight_semantic=cfg.retrieval_weight_semantic,
            weight_keyword=cfg.retrieval_weight_keyword,
            candidates=cfg.retrieval_candidates,
            keyword_pool=cfg.retrieval_keyword_pool,
            bm25_k1=cfg.bm25_k1,
            bm25_b=cfg.bm25_b,
            fts_config=cfg.retrieval_fts_config,
            dedupe=cfg.retrieval_dedupe,
        )

    def trace_meta(self) -> dict[str, Any]:
        """What a trace records: only the knobs that apply to the mode."""
        meta: dict[str, Any] = {"mode": self.mode}
        if self.mode in ("keyword", "hybrid"):
            meta["keyword_method"] = self.keyword_method
        if self.mode == "hybrid":
            meta["fusion"] = self.fusion
        if self.mode != "semantic":
            meta["candidates"] = self.candidates
        return meta


def text_key(text: str) -> str:
    """Identity of a passage for de-duplication: whitespace-collapsed, case-folded text."""
    return " ".join(text.split()).casefold()


def _dedupe(results: Sequence[ChunkResult], enabled: bool) -> list[ChunkResult]:
    """Keep the best-ranked copy of identical chunk text (``results`` are best first)."""
    if not enabled:
        return list(results)
    seen: set[str] = set()
    out: list[ChunkResult] = []
    for r in results:
        key = text_key(r.text)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


async def _semantic_raw(
    query: str, fetch: int, embedder: Embedder, store: VectorStore, document_ids: Sequence[uuid.UUID] | None
) -> list[dict[str, Any]]:
    vector = await asyncio.to_thread(embedder.embed_query, query)
    filters = None
    if document_ids:
        ids = [str(d) for d in document_ids]
        filters = {"doc_id": ids[0]} if len(ids) == 1 else {"doc_id": {"$in": ids}}
    return await asyncio.to_thread(store.similarity_search, vector, fetch, filters)


async def _enrich(hits: Sequence[dict[str, Any]], session: AsyncSession) -> list[ChunkResult]:
    """Vector-store hits -> results; hits whose document no longer exists in Postgres are dropped."""
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
                score_kind="distance",
            )
        )
    return results


async def _keyword(
    query: str, fetch: int, opts: RetrievalOptions, session: AsyncSession, document_ids: Sequence[uuid.UUID] | None
) -> list[ChunkResult]:
    hits = await keyword.keyword_search(
        session,
        query,
        limit=fetch,
        method=opts.keyword_method,
        config=opts.fts_config,
        pool=opts.keyword_pool,
        k1=opts.bm25_k1,
        b=opts.bm25_b,
        document_ids=document_ids,
    )
    kind = "bm25" if opts.keyword_method == "bm25" else "ts_rank_cd"
    return [ChunkResult(h.text, h.document_id, h.index, h.filename, h.score, score_kind=kind) for h in hits]


def _fuse(
    semantic: Sequence[ChunkResult], keyword_results: Sequence[ChunkResult], top_k: int, opts: RetrievalOptions
) -> list[ChunkResult]:
    """Fuse the two candidate lists (each best first) into one ranking of at most ``top_k`` results.

    With de-duplication on, a passage is identified by its text, so the same text in a semantic hit and a
    keyword hit from two duplicate documents is one item that collects both lists' contributions; otherwise
    by ``(document, chunk index)``.
    """

    def key_of(r: ChunkResult) -> Hashable:
        return text_key(r.text) if opts.dedupe else (r.document_id, r.chunk_index)

    reps: dict[Hashable, ChunkResult] = {}
    sem_rank: dict[Hashable, int] = {}
    kw_rank: dict[Hashable, int] = {}
    sem_scored: list[tuple[Hashable, float]] = []
    kw_scored: list[tuple[Hashable, float]] = []
    for results, ranks, scored in ((semantic, sem_rank, sem_scored), (keyword_results, kw_rank, kw_scored)):
        for r in results:
            k = key_of(r)
            if k in ranks:
                continue
            ranks[k] = len(ranks) + 1
            scored.append((k, r.score))
            reps.setdefault(k, r)
    if opts.fusion == "rrf":
        fused = fusion.rrf([[k for k, _ in sem_scored], [k for k, _ in kw_scored]], opts.rrf_k)
        kind = "rrf"
    else:
        fused = fusion.weighted(
            [sem_scored, kw_scored], [opts.weight_semantic, opts.weight_keyword], [False, True]
        )
        kind = "weighted"
    return [
        replace(
            reps[k], score=score, score_kind=kind, semantic_rank=sem_rank.get(k), keyword_rank=kw_rank.get(k)
        )
        for k, score in fused[:top_k]
    ]


async def retrieve(
    query: str,
    *,
    top_k: int,
    embedder: Embedder,
    store: VectorStore,
    session: AsyncSession,
    document_ids: list[uuid.UUID] | None = None,
    options: RetrievalOptions | None = None,
) -> list[ChunkResult]:
    """Return the best ``top_k`` chunks for ``query`` under ``options`` (default: semantic).

    ``document_ids`` restricts every mode to those documents. Hits whose document no longer exists
    in Postgres (e.g. deleted mid-flight) are dropped. See the module docstring for the score meaning.
    """
    if top_k <= 0:
        return []
    opts = options or RetrievalOptions()
    # Over-fetch when de-duplicating so that dropped duplicates do not leave the answer short.
    pool = max(opts.candidates, top_k)
    if opts.mode == "semantic":
        raw = await _semantic_raw(query, pool if opts.dedupe else top_k, embedder, store, document_ids)
        return _dedupe(await _enrich(raw, session), opts.dedupe)[:top_k]
    if opts.mode == "keyword":
        return _dedupe(await _keyword(query, pool, opts, session, document_ids), opts.dedupe)[:top_k]
    # hybrid: the vector search runs in a worker thread while the SQL search runs on the session
    raw, kw = await asyncio.gather(
        _semantic_raw(query, pool, embedder, store, document_ids),
        _keyword(query, pool, opts, session, document_ids),
    )
    semantic = await _enrich(raw, session)
    return _fuse(semantic, kw, top_k, opts)


def log_effective_settings(cfg: Settings) -> None:
    """Startup log line with the effective retrieval settings."""
    opts = RetrievalOptions.from_settings(cfg)
    logger.info("retrieval: %s", asdict(opts))
