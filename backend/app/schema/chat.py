from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    document_ids: list[uuid.UUID] | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)


class SourceOut(BaseModel):
    text: str
    document_id: uuid.UUID
    chunk_index: int
    filename: str
    score: float


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceOut]
