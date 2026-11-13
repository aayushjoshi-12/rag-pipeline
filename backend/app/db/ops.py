"""Typed async CRUD operations for Document, Chunk, and Job models."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AsyncExitStack

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import async_session as _default_session
from app.db.enums import DocumentStatus, JobStatus
from app.db.models import Chunk, Document, Job


async def _acquire(
    session: AsyncSession | None, stack: AsyncExitStack
) -> AsyncSession:
    """Return *session* as-is, or create + enter a fresh session via *stack*."""
    if session is not None:
        return session
    new = _default_session()
    return await stack.enter_async_context(new)


# ── Documents ─────────────────────────────────────────────────────────────────


async def create_document(
    *,
    filename: str,
    content_type: str,
    source_path: str | None = None,
    status: DocumentStatus = DocumentStatus.PENDING,
    session: AsyncSession | None = None,
) -> Document:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        doc = Document(
            filename=filename,
            content_type=content_type,
            source_path=source_path,
            status=status,
        )
        sess.add(doc)
        await sess.commit()
        await sess.refresh(doc)
    return doc


async def get_document(
    document_id: uuid.UUID, *, session: AsyncSession | None = None
) -> Document | None:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        return await sess.get(Document, document_id)


async def list_documents(
    *, skip: int = 0, limit: int = 100, session: AsyncSession | None = None
) -> list[Document]:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        result = await sess.execute(
            select(Document).order_by(Document.created_at.desc()).offset(skip).limit(limit)
        )
        return list(result.scalars().all())


async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    *,
    session: AsyncSession | None = None,
) -> Document | None:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        doc = await sess.get(Document, document_id)
        if doc is None:
            return None
        doc.status = status
        await sess.commit()
        await sess.refresh(doc)
        return doc


# ── Jobs ──────────────────────────────────────────────────────────────────────


async def create_job(
    *,
    document_id: uuid.UUID,
    task_id: str | None = None,
    status: JobStatus = JobStatus.PENDING,
    session: AsyncSession | None = None,
) -> Job:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        job = Job(document_id=document_id, task_id=task_id, status=status)
        sess.add(job)
        await sess.commit()
        await sess.refresh(job)
    return job


async def get_job(
    job_id: uuid.UUID, *, session: AsyncSession | None = None
) -> Job | None:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        return await sess.get(Job, job_id)


async def list_jobs_by_document(
    document_id: uuid.UUID, *, session: AsyncSession | None = None
) -> list[Job]:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        result = await sess.execute(
            select(Job).where(Job.document_id == document_id).order_by(Job.created_at.desc())
        )
        return list(result.scalars().all())


async def update_job_status(
    job_id: uuid.UUID,
    status: JobStatus,
    *,
    error: str | None = None,
    task_id: str | None = None,
    session: AsyncSession | None = None,
) -> Job | None:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        job = await sess.get(Job, job_id)
        if job is None:
            return None
        job.status = status
        if error is not None:
            job.error = error
        if task_id is not None:
            job.task_id = task_id
        await sess.commit()
        await sess.refresh(job)
        return job


# ── Chunks ────────────────────────────────────────────────────────────────────


async def create_chunks(
    *,
    document_id: uuid.UUID,
    chunks: Sequence[dict[str, object]],  # each dict: {"index": int, "text": str, "metadata": dict}
    session: AsyncSession | None = None,
) -> list[Chunk]:
    """Bulk-create chunks for a document. Each dict must have 'index', 'text', 'metadata'."""
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        objects = [
            Chunk(
                document_id=document_id,
                index=c["index"],
                text=c["text"],
                metadata_=c.get("metadata", {}),
            )
            for c in chunks
        ]
        sess.add_all(objects)
        await sess.commit()
        for obj in objects:
            await sess.refresh(obj)
    return objects


async def list_chunks_by_document(
    document_id: uuid.UUID, *, session: AsyncSession | None = None
) -> list[Chunk]:
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        result = await sess.execute(
            select(Chunk)
            .where(Chunk.document_id == document_id)
            .order_by(Chunk.index)
        )
        return list(result.scalars().all())


async def delete_chunks_by_document(
    document_id: uuid.UUID, *, session: AsyncSession | None = None
) -> int:
    """Delete every chunk row of a document; returns the number removed."""
    async with AsyncExitStack() as stack:
        sess = await _acquire(session, stack)
        result = await sess.execute(delete(Chunk).where(Chunk.document_id == document_id))
        await sess.commit()
        return int(getattr(result, "rowcount", 0) or 0)
