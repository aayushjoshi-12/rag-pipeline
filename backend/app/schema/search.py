from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"query": "waiting period after a foreclosure", "top_k": 5},
                {
                    "query": "Z9-4.2-07",
                    "mode": "hybrid",
                    "fusion": "rrf",
                    "top_k": 3,
                    "neighbors": 1,
                    "include_text": False,
                },
            ]
        }
    )

    query: str = Field(min_length=1, max_length=4000, description="Natural-language or keyword query.")
    top_k: int | None = Field(
        default=None, ge=1, le=50, description="Number of hits (default: the deployment's DEFAULT_TOP_K)."
    )
    mode: Literal["semantic", "keyword", "hybrid"] | None = Field(
        default=None, description="Override RETRIEVAL_MODE: embeddings, Postgres BM25, or both fused."
    )
    fusion: Literal["rrf", "weighted"] | None = Field(
        default=None, description="Override RETRIEVAL_FUSION (only used by `hybrid`; validated in every mode)."
    )
    neighbors: int = Field(
        default=0,
        ge=0,
        le=5,
        description=(
            "Return this many chunks before and after each hit (same document) under `results[].context`. "
            "Default 0 (hits only), independent of RETRIEVAL_NEIGHBORS, which only shapes the /chat prompt. "
            "Context is best-effort within a 12,000-character budget; hits are never dropped for it."
        ),
    )
    document_ids: list[uuid.UUID] | None = Field(
        default=None, description="Restrict the search to these documents."
    )
    include_text: bool = Field(
        default=True,
        description="false returns ids, ranks and scores only (`text` is null): a cheap ranking probe.",
    )


class ContextChunk(BaseModel):
    """A neighbouring chunk of a hit. Unscored: it was not retrieved, it only surrounds the hit."""

    chunk_index: int
    text: str | None = None


class SearchResult(BaseModel):
    rank: int = Field(description="1-based position, best first. Results are always sorted by rank.")
    document_id: uuid.UUID
    chunk_index: int
    filename: str
    text: str | None = Field(description="The chunk text; null when `include_text` is false.")
    score: float = Field(description="Meaning depends on `score_kind`; comparable only within one response.")
    score_kind: str = Field(
        description=(
            "`distance` (lower = closer; semantic) | `bm25` | `ts_rank_cd` | `rrf` | `weighted` "
            "(higher = better)."
        )
    )
    semantic_rank: int | None = Field(default=None, description="Hybrid only: 1-based rank in the semantic list.")
    keyword_rank: int | None = Field(default=None, description="Hybrid only: 1-based rank in the keyword list.")
    is_hit: Literal[True] = True
    context: list[ContextChunk] = Field(
        default_factory=list, description="Neighbouring chunks in document order; empty unless `neighbors` > 0."
    )


class SearchResponse(BaseModel):
    query: str
    mode: str = Field(description="The retrieval mode actually used.")
    fusion: str | None = Field(description="The fusion method actually used; null unless mode is `hybrid`.")
    top_k: int
    neighbors: int
    returned: int = Field(description="Number of hits in `results`.")
    took_ms: int = Field(description="Server-side time spent retrieving.")
    results: list[SearchResult]
