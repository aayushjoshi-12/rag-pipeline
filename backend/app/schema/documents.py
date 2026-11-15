"""Pydantic request/response shapes for the documents + jobs API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import DocumentStatus, JobStatus


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class JobOut(_ORM):
    id: uuid.UUID
    document_id: uuid.UUID
    task_id: str | None
    status: JobStatus
    error: str | None
    stats: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class DocumentOut(_ORM):
    id: uuid.UUID
    filename: str
    content_type: str
    status: DocumentStatus
    source_url: str | None = Field(
        default=None,
        description="Link the document was ingested from, with credentials and query string removed. null for uploads.",
        examples=["https://www.rfc-editor.org/rfc/rfc8259.txt"],
    )
    storage_backend: str = Field(default="local", description="Where the source file is stored: local | s3.")
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentOut):
    jobs: list[JobOut]


class FromUrlRequest(BaseModel):
    url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "http(s):// link to a .pdf/.docx/.txt/.md file, or s3://<bucket>/<key> (bucket must be "
            "allowed by URL_INGEST_S3_BUCKETS). No embedded credentials. Private/loopback/link-local/"
            "metadata addresses are refused. Submitting a link that was ingested before creates a NEW "
            "document (no de-duplication by URL)."
        ),
        examples=["https://www.rfc-editor.org/rfc/rfc8259.txt", "s3://my-bucket/reports/q3.pdf"],
    )


class DocumentPage(BaseModel):
    """One page of documents, newest first. `GET /v1/documents` returns this envelope."""

    items: list[DocumentOut]
    total: int = Field(description="Number of documents in total (all pages).", examples=[137])
    limit: int = Field(description="Page size that was applied.", examples=[50])
    offset: int = Field(description="Number of documents skipped.", examples=[0])
    has_more: bool = Field(
        description="True when `offset + len(items) < total`: request the next page with "
        "`offset = offset + limit`."
    )


class UploadResponse(BaseModel):
    document: DocumentOut
    job: JobOut


class ChunkOut(BaseModel):
    id: uuid.UUID
    index: int
    text: str
    metadata: dict[str, Any]
