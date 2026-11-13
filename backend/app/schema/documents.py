"""Pydantic request/response shapes for the documents + jobs API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.enums import DocumentStatus, JobStatus


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class JobOut(_ORM):
    id: uuid.UUID
    document_id: uuid.UUID
    task_id: str | None
    status: JobStatus
    error: str | None
    created_at: datetime
    updated_at: datetime


class DocumentOut(_ORM):
    id: uuid.UUID
    filename: str
    content_type: str
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentOut):
    jobs: list[JobOut]


class UploadResponse(BaseModel):
    document: DocumentOut
    job: JobOut


class ChunkOut(BaseModel):
    id: uuid.UUID
    index: int
    text: str
    metadata: dict[str, Any]
