"""Unit tests for SQLAlchemy models — metadata and structure checks (no DB required)."""

from __future__ import annotations

from app.db.enums import DocumentStatus, JobStatus
from app.db.models import Base, Chunk, Document, Job


def test_document_table_name() -> None:
    assert Document.__tablename__ == "documents"


def test_chunk_table_name() -> None:
    assert Chunk.__tablename__ == "chunks"


def test_job_table_name() -> None:
    assert Job.__tablename__ == "jobs"


def test_document_has_expected_columns() -> None:
    cols = {c.name for c in Document.__table__.columns}
    assert cols == {"id", "filename", "content_type", "status", "source_path", "storage_backend", "source_url", "created_at", "updated_at"}


def test_chunk_has_expected_columns() -> None:
    cols = {c.name for c in Chunk.__table__.columns}
    assert cols == {"id", "document_id", "index", "text", "metadata", "vector", "fts", "fts_len"}


def test_job_has_expected_columns() -> None:
    cols = {c.name for c in Job.__table__.columns}
    assert cols == {"id", "document_id", "task_id", "status", "error", "stats", "created_at", "updated_at"}


def test_all_tables_registered_in_metadata() -> None:
    table_names = set(Base.metadata.tables.keys())
    assert {"documents", "chunks", "jobs"}.issubset(table_names)


def test_document_status_enum_values() -> None:
    assert set(DocumentStatus) == {
        DocumentStatus.PENDING,
        DocumentStatus.INGESTING,
        DocumentStatus.READY,
        DocumentStatus.FAILED,
    }


def test_job_status_enum_values() -> None:
    assert set(JobStatus) == {
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.DONE,
        JobStatus.FAILED,
    }


def test_document_status_is_str_enum() -> None:
    assert DocumentStatus.PENDING == "pending"
    assert DocumentStatus.READY == "ready"


def test_job_status_is_str_enum() -> None:
    assert JobStatus.PENDING == "pending"
    assert JobStatus.DONE == "done"


def test_utcnow_is_naive_and_current_utc() -> None:
    from datetime import UTC, datetime, timedelta

    from app.db.models import utcnow

    now = utcnow()
    assert now.tzinfo is None
    assert abs(now - datetime.now(UTC).replace(tzinfo=None)) < timedelta(seconds=5)
