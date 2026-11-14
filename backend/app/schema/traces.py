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
