from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class TraceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    kind: str
    status: str
    duration_ms: int
    user_email: str | None
    document_id: uuid.UUID | None
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    hits: int | None
    cost_usd: float | None
    error: str | None
    question: str | None
    meta: dict[str, Any]


class TracePage(BaseModel):
    items: list[TraceOut]
    total: int


class DayUsageOut(BaseModel):
    day: date
    requests: int
    errors: int
    avg_ms: float | None
    p95_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    unpriced_requests: int


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ModelCostOut(_Out):
    model: str | None
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    unpriced_requests: int


class BucketOut(_Out):
    lo: int
    hi: int
    count: int


class ReasonOut(_Out):
    reason: str
    count: int


class HeatCellOut(_Out):
    weekday: int
    hour: int
    count: int


class DayRagOut(_Out):
    day: date
    queries: int
    no_hit: int


class HitsBinOut(_Out):
    hits: int
    count: int


class CitedDocumentOut(_Out):
    document_id: str
    filename: str | None
    citations: int


class DayReuseOut(_Out):
    day: date
    ingests: int
    reused: int
    embedded: int


class ModeStatOut(_Out):
    mode: str
    runs: int
    avg_ms: float
    p95_ms: float


class InsightsOut(_Out):
    days: int
    kind: str | None
    cost_usd: float | None
    priced_chats: int
    unpriced_chats: int
    avg_cost_per_chat: float | None
    by_model: list[ModelCostOut]
    latency_bucket_ms: int
    latency_histogram: list[BucketOut]
    errors_by_reason: list[ReasonOut]
    heatmap: list[HeatCellOut]
    rag_days: list[DayRagOut]
    hits_histogram: list[HitsBinOut]
    top_documents: list[CitedDocumentOut]
    cited_documents: int
    total_documents: int
    never_cited: int
    reuse_days: list[DayReuseOut]
    ingest_modes: list[ModeStatOut]
    ingest_failures: list[ReasonOut]


class CreditsOut(_Out):
    available: bool
    reason: str | None = None
    limit: float | None = None
    limit_remaining: float | None = None
    usage: float | None = None
    is_free_tier: bool | None = None
    fetched_at: float | None = None
