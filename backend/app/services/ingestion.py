"""Ingest orchestration: parse → chunk → embed → store, with job/document status tracking.

Pure async function with injected dependencies so it is testable with fakes and reusable
from the Celery task. Re-running a document (even a ``done`` one) restarts cleanly: its
previous chunks are replaced wholesale. The content-hash diff update path (ADR-0003) builds
on the ``content_hash`` / ``chunker_fingerprint`` stored in each chunk's metadata.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import ops
from app.db.enums import DocumentStatus, JobStatus
from app.db.models import Document
from app.services.chunking import chunk_text
from app.services.embeddings import Embedder
from app.services.parser import ParseResult
from app.services.vectorstore import VectorStore

logger = logging.getLogger(__name__)

ParseFn = Callable[[Path, str, Settings], ParseResult]

_EMBED_BATCH = 32
_MAX_ERROR_CHARS = 2000


class IngestError(Exception):
    """A user-visible ingest failure (recorded on the job)."""


def resolve_source_path(doc: Document, settings: Settings) -> Path:
    if not doc.source_path:
        raise IngestError("document has no source_path")
    path = Path(doc.source_path)
    return path if path.is_absolute() else settings.data_dir / path


def _filetype(doc: Document) -> str:
    suffix = Path(doc.filename).suffix.lstrip(".").lower()
    if suffix:
        return suffix
    return "pdf" if "pdf" in doc.content_type else ""


async def ingest_document(
    job_id: uuid.UUID,
    *,
    session: AsyncSession,
    settings: Settings,
    parse_fn: ParseFn,
    embedder: Embedder,
    store: VectorStore,
) -> JobStatus:
    """Run the ingest for ``job_id``. Never raises; the outcome lives on the job row."""
    job = await ops.get_job(job_id, session=session)
    if job is None:
        logger.error("ingest: job %s not found", job_id)
        return JobStatus.FAILED
    doc_id = job.document_id
    doc_key = str(doc_id)
    wrote_vectors = False

    try:
        await ops.update_job_status(job_id, JobStatus.RUNNING, session=session)
        await ops.update_document_status(doc_id, DocumentStatus.INGESTING, session=session)

        doc = await ops.get_document(doc_id, session=session)
        if doc is None:
            raise IngestError(f"document {doc_id} not found")
        path = resolve_source_path(doc, settings)
        if not path.is_file():
            raise IngestError(f"source file not found: {path}")

        parsed = await asyncio.to_thread(parse_fn, path, _filetype(doc), settings)
        specs = chunk_text(
            parsed.text,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )
        if not specs:
            raise IngestError("no text could be extracted from the document")

        texts = [s.text for s in specs]
        vectors: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            batch = texts[i : i + _EMBED_BATCH]
            vectors.extend(await asyncio.to_thread(embedder.embed_documents, batch))

        chunk_dicts: list[dict[str, Any]] = [
            {
                "index": s.index,
                "text": s.text,
                "metadata": {
                    "content_hash": s.content_hash,
                    "chunker_fingerprint": s.chunker_fingerprint,
                    "start": s.start,
                    "end": s.end,
                    "filename": doc.filename,
                },
            }
            for s in specs
        ]

        # Replace any previous ingest of this document (idempotent re-run).
        await asyncio.to_thread(store.delete_doc, doc_key)
        await ops.delete_chunks_by_document(doc_id, session=session)
        wrote_vectors = True
        await asyncio.to_thread(store.add_chunks, doc_key, chunk_dicts, vectors)
        await ops.create_chunks(document_id=doc_id, chunks=chunk_dicts, session=session)

        await ops.update_job_status(job_id, JobStatus.DONE, session=session)
        await ops.update_document_status(doc_id, DocumentStatus.READY, session=session)
        logger.info("ingest %s done: %d chunks", job_id, len(specs))
        return JobStatus.DONE
    except Exception as exc:
        logger.exception("ingest %s failed", job_id)
        await _record_failure(session, store, job_id, doc_id, doc_key, wrote_vectors, exc)
        return JobStatus.FAILED


async def _record_failure(
    session: AsyncSession,
    store: VectorStore,
    job_id: uuid.UUID,
    doc_id: uuid.UUID,
    doc_key: str,
    wrote_vectors: bool,
    exc: Exception,
) -> None:
    message = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
    try:
        await session.rollback()
        if wrote_vectors:
            await asyncio.to_thread(store.delete_doc, doc_key)
            await ops.delete_chunks_by_document(doc_id, session=session)
        await ops.update_job_status(job_id, JobStatus.FAILED, error=message, session=session)
        await ops.update_document_status(doc_id, DocumentStatus.FAILED, session=session)
    except Exception:
        logger.exception("ingest %s: could not record failure", job_id)
