"""Ingest orchestration: parse → chunk → embed → store, with job/document status tracking.

Pure async function with injected dependencies so it is testable with fakes and reusable
from the Celery task. Re-running a document (even a ``done`` one) restarts cleanly: its
previous chunks are replaced wholesale. The content-hash diff update path (ADR-0003) builds
on the ``content_hash`` / ``chunker_fingerprint`` stored in each chunk's metadata.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import ops
from app.db.enums import DocumentStatus, JobStatus
from app.db.models import Document
from app.observability import log_context
from app.services import observability, traces
from app.services.chunking import ChunkSpec, chunk_text
from app.services.embeddings import Embedder
from app.services.incremental import old_chunks_from_rows, plan_update, vector_id
from app.services.parser import ParseResult
from app.services.vectorstore import VectorStore

logger = structlog.get_logger(__name__)

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
    with log_context(job_id=str(job_id)), observability.span("ingest.document", job_id=str(job_id)) as root:
        started = time.perf_counter()
        status = await _ingest(job_id, session, settings, parse_fn, embedder, store)
        await _record_trace(job_id, status, started, session, settings)
        root.update(output=status.value)
        if status != JobStatus.DONE:
            root.update(level="ERROR", status_message="ingest failed")
        return status


async def _record_trace(
    job_id: uuid.UUID, status: JobStatus, started: float, session: AsyncSession, settings: Settings
) -> None:
    job = await ops.get_job(job_id, session=session)
    await traces.record(
        session,
        kind="ingest",
        status="ok" if status == JobStatus.DONE else "error",
        duration_ms=int((time.perf_counter() - started) * 1000),
        document_id=job.document_id if job else None,
        model=settings.embedding_model,
        error=job.error if job else None,
        meta=dict(job.stats or {}) if job else {},
    )


async def _ingest(
    job_id: uuid.UUID,
    session: AsyncSession,
    settings: Settings,
    parse_fn: ParseFn,
    embedder: Embedder,
    store: VectorStore,
) -> JobStatus:
    job = await ops.get_job(job_id, session=session)
    if job is None:
        logger.error("ingest_job_not_found")
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

        with observability.span("ingest.parse", filetype=_filetype(doc)) as sp:
            parsed = await asyncio.to_thread(parse_fn, path, _filetype(doc), settings)
            sp.update(metadata={"provider": parsed.provider, "chars": len(parsed.text)})
        with observability.span("ingest.chunk") as sp:
            specs = chunk_text(
                parsed.text,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
            )
            sp.update(metadata={"chunks": len(specs)})
        if not specs:
            raise IngestError("no text could be extracted from the document")

        old_rows = await ops.list_chunks_by_document(doc_id, session=session)
        plan = plan_update(
            old_chunks_from_rows([dict(r.metadata_) for r in old_rows]),
            [s.content_hash for s in specs],
            specs[0].chunker_fingerprint,
            max_churn=settings.incremental_max_churn,
        )
        first_spec: dict[str, ChunkSpec] = {}
        for sp_ in specs:
            first_spec.setdefault(sp_.content_hash, sp_)

        # Embed only the vectors that are new (a full rebuild "adds" everything).
        add_specs = [first_spec[h] for h in plan.add_hashes]
        vectors: list[list[float]] = []
        with observability.span("ingest.embed", chunks=len(add_specs), mode=plan.mode):
            for i in range(0, len(add_specs), _EMBED_BATCH):
                batch = [c.text for c in add_specs[i : i + _EMBED_BATCH]]
                vectors.extend(await asyncio.to_thread(embedder.embed_documents, batch))

        def meta(c: ChunkSpec) -> dict[str, Any]:
            return {
                "content_hash": c.content_hash,
                "chunker_fingerprint": c.chunker_fingerprint,
                "vector_id": vector_id(doc_key, c.content_hash),
                "start": c.start,
                "end": c.end,
                "filename": doc.filename,
            }

        chunk_dicts: list[dict[str, Any]] = [
            {"index": c.index, "text": c.text, "metadata": meta(c)} for c in specs
        ]

        with observability.span("ingest.store", chunks=len(chunk_dicts), mode=plan.mode):
            wrote_vectors = True  # from here a failure leaves a clean slate (next run = full rebuild)
            if plan.mode == "full":
                await asyncio.to_thread(store.delete_doc, doc_key)
            else:
                await asyncio.to_thread(
                    store.delete_ids, [vector_id(doc_key, h) for h in plan.remove_hashes]
                )
            if add_specs:
                await asyncio.to_thread(
                    store.add_chunks,
                    doc_key,
                    [
                        {
                            "id": vector_id(doc_key, c.content_hash),
                            "text": c.text,
                            "metadata": {**meta(c), "index": c.index},
                        }
                        for c in add_specs
                    ],
                    vectors,
                )
            # Reused vectors only need their position metadata refreshed.
            old_first_index: dict[str, int] = {}
            for r in sorted(old_rows, key=lambda r: r.index):
                old_first_index.setdefault(str(r.metadata_.get("content_hash")), r.index)
            moved = [h for h in plan.keep_hashes if old_first_index.get(h) != first_spec[h].index]
            if moved:
                await asyncio.to_thread(
                    store.update_metadata,
                    [vector_id(doc_key, h) for h in moved],
                    [
                        {"index": first_spec[h].index, "start": first_spec[h].start, "end": first_spec[h].end}
                        for h in moved
                    ],
                )
            # Chunk rows carry no vectors, so they are simply replaced.
            await ops.delete_chunks_by_document(doc_id, session=session)
            await ops.create_chunks(document_id=doc_id, chunks=chunk_dicts, session=session)

        stats: dict[str, object] = {
            "mode": plan.mode,
            "reason": plan.reason,
            "chunks_total": len(specs),
            "kept": len(plan.keep_hashes),
            "added": len(plan.add_hashes),
            "removed": len(plan.remove_hashes),
            "embedded": len(add_specs),
        }
        await ops.update_job_status(job_id, JobStatus.DONE, stats=stats, session=session)
        await ops.update_document_status(doc_id, DocumentStatus.READY, session=session)
        logger.info("ingest_done", doc_id=doc_key, **stats)
        return JobStatus.DONE
    except Exception as exc:
        logger.exception("ingest_failed", doc_id=doc_key)
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
        logger.exception("ingest_failure_not_recorded")
