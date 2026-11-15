from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"question": "What is the minimum credit score for a conventional loan?", "top_k": 5},
                {"question": "late fees on a conforming loan", "mode": "hybrid", "neighbors": 1},
            ]
        }
    )

    question: str = Field(min_length=1, max_length=4000)
    document_ids: list[uuid.UUID] | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    #: Override RETRIEVAL_NEIGHBORS for this request (chunks of context around each hit).
    neighbors: int | None = Field(default=None, ge=0, le=5)
    #: Override RETRIEVAL_MODE for this request.
    mode: Literal["semantic", "keyword", "hybrid"] | None = None
    #: Override RETRIEVAL_FUSION for this request (only used by ``hybrid``; validated in every mode).
    fusion: Literal["rrf", "weighted"] | None = None
    #: ``false`` bypasses the query cache for this request (no read, no write). Same as ``Cache-Control: no-cache``.
    cache: bool | None = None


class ChunkRef(BaseModel):
    """Identifies one chunk: the hit that a context chunk accompanies."""

    document_id: uuid.UUID
    chunk_index: int


class SourceOut(BaseModel):
    text: str
    document_id: uuid.UUID
    chunk_index: int
    filename: str
    #: Meaning depends on ``score_kind``. Among the hits (``is_hit``) the order is best first.
    #: ``null`` for context chunks (``is_hit`` false): they were not scored and never borrow their hit's score.
    score: float | None
    #: ``distance`` (lower = closer; semantic) | ``bm25`` | ``ts_rank_cd`` | ``rrf`` | ``weighted`` (higher = better).
    score_kind: str = "distance"
    #: Hybrid only: 1-based rank in the semantic / keyword candidate list (None = not in that list).
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    #: False when this chunk is neighbouring context rather than a search hit.
    is_hit: bool = True
    #: 1-based position among the hits, best first; ``null`` for context chunks.
    rank: int | None = None
    #: Context chunks only: the hit (document_id, chunk_index) this chunk accompanies.
    context_for: ChunkRef | None = None


class ChatResponse(BaseModel):
    answer: str
    #: The passages given to the LLM, in prompt order (citation ``[n]`` = position n). Hits keep their
    #: best-first order relative to each other; context chunks sit next to their hit in reading order.
    sources: list[SourceOut]
    #: Query-cache verdict for this request: hit | miss | bypass | off (also in the ``X-Cache`` header).
    cache: str | None = None
