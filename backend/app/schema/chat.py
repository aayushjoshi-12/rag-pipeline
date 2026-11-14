from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    document_ids: list[uuid.UUID] | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    #: Override RETRIEVAL_NEIGHBORS for this request (chunks of context around each hit).
    neighbors: int | None = Field(default=None, ge=0, le=5)
    #: Override RETRIEVAL_MODE for this request.
    mode: Literal["semantic", "keyword", "hybrid"] | None = None
    #: Override RETRIEVAL_FUSION for this request (only used by ``hybrid``; validated in every mode).
    fusion: Literal["rrf", "weighted"] | None = None


class SourceOut(BaseModel):
    text: str
    document_id: uuid.UUID
    chunk_index: int
    filename: str
    #: Meaning depends on ``score_kind``; sources are always ordered best first.
    score: float
    #: ``distance`` (lower = closer; semantic) | ``bm25`` | ``ts_rank_cd`` | ``rrf`` | ``weighted`` (higher = better).
    score_kind: str = "distance"
    #: Hybrid only: 1-based rank in the semantic / keyword candidate list (None = not in that list).
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    #: False when this chunk is neighbouring context rather than a search hit.
    is_hit: bool = True


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceOut]
