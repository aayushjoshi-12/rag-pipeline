"""Integration tests for async DB operations — against compose Postgres.

Prereq: make db-up && make migrate  (real Postgres, not mocked).
"""

from __future__ import annotations

import uuid

from app.db.enums import DocumentStatus, JobStatus
from app.db.models import Document
from app.db.ops import (
    create_chunks,
    create_document,
    create_job,
    get_document,
    list_chunks_by_document,
    list_documents,
    list_jobs_by_document,
    update_document_status,
    update_job_status,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def _create_doc(
    session: AsyncSession, filename: str = "test.pdf"
) -> Document:
    return await create_document(
        filename=filename, content_type="application/pdf", session=session
    )


async def test_create_and_get_document(db_session: AsyncSession) -> None:
    doc = await _create_doc(db_session, "hello.pdf")
    fetched = await get_document(doc.id, session=db_session)
    assert fetched is not None
    assert fetched.filename == "hello.pdf"
    assert fetched.status == DocumentStatus.PENDING


async def test_list_documents(db_session: AsyncSession) -> None:
    await _create_doc(db_session, "list_a.pdf")
    await _create_doc(db_session, "list_b.pdf")
    docs = await list_documents(session=db_session)
    filenames = {d.filename for d in docs}
    assert "list_a.pdf" in filenames
    assert "list_b.pdf" in filenames


async def test_update_document_status(db_session: AsyncSession) -> None:
    doc = await _create_doc(db_session, "status.pdf")
    updated = await update_document_status(
        doc.id, DocumentStatus.INGESTING, session=db_session
    )
    assert updated is not None
    assert updated.status == DocumentStatus.INGESTING


async def test_update_document_status_nonexistent(db_session: AsyncSession) -> None:
    result = await update_document_status(
        uuid.uuid4(), DocumentStatus.READY, session=db_session
    )
    assert result is None


async def test_create_and_list_jobs(db_session: AsyncSession) -> None:
    doc = await _create_doc(db_session, "job_doc.pdf")
    job = await create_job(
        document_id=doc.id, task_id="celery-123", session=db_session
    )
    assert job.status == JobStatus.PENDING
    assert job.task_id == "celery-123"

    jobs = await list_jobs_by_document(doc.id, session=db_session)
    assert len(jobs) == 1
    assert jobs[0].id == job.id


async def test_update_job_status(db_session: AsyncSession) -> None:
    doc = await _create_doc(db_session, "job_update.pdf")
    job = await create_job(document_id=doc.id, session=db_session)
    updated = await update_job_status(
        job.id, JobStatus.RUNNING, session=db_session
    )
    assert updated is not None
    assert updated.status == JobStatus.RUNNING

    done = await update_job_status(job.id, JobStatus.DONE, session=db_session)
    assert done is not None
    assert done.status == JobStatus.DONE


async def test_update_job_status_with_error(db_session: AsyncSession) -> None:
    doc = await _create_doc(db_session, "job_err.pdf")
    job = await create_job(document_id=doc.id, session=db_session)
    updated = await update_job_status(
        job.id, JobStatus.FAILED, error="parse failed", session=db_session
    )
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    assert updated.error == "parse failed"


async def test_update_job_status_nonexistent(db_session: AsyncSession) -> None:
    result = await update_job_status(
        uuid.uuid4(), JobStatus.DONE, session=db_session
    )
    assert result is None


async def test_create_and_list_chunks(db_session: AsyncSession) -> None:
    doc = await _create_doc(db_session, "chunk_doc.pdf")
    created = await create_chunks(
        document_id=doc.id,
        chunks=[
            {"index": 0, "text": "Hello world", "metadata": {"page": 1}},
            {"index": 1, "text": "Second chunk", "metadata": {"page": 1}},
        ],
        session=db_session,
    )
    assert len(created) == 2
    assert created[0].index == 0
    assert created[1].text == "Second chunk"

    chunks = await list_chunks_by_document(doc.id, session=db_session)
    assert len(chunks) == 2
    assert chunks[0].index < chunks[1].index
