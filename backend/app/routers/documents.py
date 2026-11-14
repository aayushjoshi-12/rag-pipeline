"""Documents + jobs routes. The API only stores the upload, records a Job and enqueues the
Celery task — it never parses, chunks or embeds (ingest runs in the worker)."""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import ops
from app.db.enums import DocumentStatus, JobStatus
from app.routers.auth_deps import require
from app.routers.deps import Enqueue, get_enqueue, get_session, get_settings, get_store
from app.schema.documents import (
    ChunkOut,
    DocumentDetail,
    DocumentOut,
    JobOut,
    UploadResponse,
)
from app.services.auth.permissions import Permission
from app.services.vectorstore import VectorStore

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_EXTENSIONS: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
}
# Browsers/clients disagree on markdown and generic types, so the extension is authoritative;
# the declared content type only has to be plausible.
_GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream", ""}
_PLAUSIBLE = {
    *ALLOWED_EXTENSIONS.values(),
    "text/x-markdown",
    "application/x-markdown",
    "application/msword",
}
_CHUNK = 1024 * 1024

Session = Annotated[AsyncSession, Depends(get_session)]
Cfg = Annotated[Settings, Depends(get_settings)]


def _safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
    return cleaned[:200] or "upload"


async def _store_upload(file: UploadFile, cfg: Settings) -> tuple[str, Path, str]:
    """Validate and stream an upload to disk. Returns (filename, relative path, extension)."""
    filename = _safe_name(file.filename or "")
    ext = Path(filename).suffix.lstrip(".").lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}.",
        )
    declared = (file.content_type or "").split(";")[0].strip().lower()
    if declared not in _GENERIC_TYPES and declared not in _PLAUSIBLE:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"Unsupported content type '{declared}'."
        )

    rel_dir = Path("uploads") / uuid.uuid4().hex
    target_dir = cfg.data_dir / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with (target_dir / filename).open("wb") as out:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > cfg.max_upload_size_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"File exceeds the {cfg.max_upload_size_bytes} byte limit.",
                    )
                out.write(chunk)
        if size == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty.")
    except BaseException:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    return filename, rel_dir / filename, ext


def _remove_upload_dir(cfg: Settings, source_path: str | None) -> None:
    """Remove a per-upload directory (only ever our own ``uploads/<id>`` dirs)."""
    if source_path:
        parent = (cfg.data_dir / source_path).parent
        if parent.parent == cfg.data_dir / "uploads":
            shutil.rmtree(parent, ignore_errors=True)


async def _enqueue_job(
    session: AsyncSession, enqueue: Enqueue, doc_id: uuid.UUID
) -> JobOut:
    job = await ops.create_job(document_id=doc_id, session=session)
    try:
        task_id = enqueue(str(job.id))
    except Exception as exc:
        logger.exception("could not enqueue ingest for job %s", job.id)
        await ops.update_job_status(
            job.id, JobStatus.FAILED, error=f"enqueue failed: {exc}", session=session
        )
        await ops.update_document_status(doc_id, DocumentStatus.FAILED, session=session)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not enqueue ingestion (broker unavailable)."
        ) from exc
    refreshed = await ops.update_job_status(
        job.id, JobStatus.PENDING, task_id=task_id, session=session
    )
    return JobOut.model_validate(refreshed or job)


@router.post("/documents", response_model=UploadResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require(Permission.DOCUMENTS_WRITE))])
async def upload_document(
    file: UploadFile,
    session: Session,
    cfg: Cfg,
    enqueue: Annotated[Enqueue, Depends(get_enqueue)],
) -> UploadResponse:
    filename, rel_path, ext = await _store_upload(file, cfg)
    doc = await ops.create_document(
        filename=filename,
        content_type=ALLOWED_EXTENSIONS[ext],
        source_path=str(rel_path),
        session=session,
    )
    job = await _enqueue_job(session, enqueue, doc.id)
    return UploadResponse(document=DocumentOut.model_validate(doc), job=job)


@router.put("/documents/{document_id}", response_model=UploadResponse, dependencies=[Depends(require(Permission.DOCUMENTS_WRITE))])
async def replace_document(
    document_id: uuid.UUID,
    file: UploadFile,
    session: Session,
    cfg: Cfg,
    enqueue: Annotated[Enqueue, Depends(get_enqueue)],
) -> UploadResponse:
    """Upload a new version of an existing document; only changed chunks are re-embedded."""
    existing = await ops.get_document(document_id, session=session)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    old_source = existing.source_path
    filename, rel_path, ext = await _store_upload(file, cfg)
    doc = await ops.replace_document_source(
        document_id,
        filename=filename,
        content_type=ALLOWED_EXTENSIONS[ext],
        source_path=str(rel_path),
        session=session,
    )
    if doc is None:  # deleted while uploading
        _remove_upload_dir(cfg, str(rel_path))
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    job = await _enqueue_job(session, enqueue, doc.id)
    _remove_upload_dir(cfg, old_source)
    return UploadResponse(document=DocumentOut.model_validate(doc), job=job)


@router.get("/documents", response_model=list[DocumentOut], dependencies=[Depends(require(Permission.DOCUMENTS_READ))])
async def list_documents(session: Session, skip: int = 0, limit: int = 100) -> list[DocumentOut]:
    limit = max(1, min(limit, 500))
    docs = await ops.list_documents(skip=max(0, skip), limit=limit, session=session)
    return [DocumentOut.model_validate(d) for d in docs]


@router.get("/documents/{document_id}", response_model=DocumentDetail, dependencies=[Depends(require(Permission.DOCUMENTS_READ))])
async def get_document(document_id: uuid.UUID, session: Session) -> DocumentDetail:
    doc = await ops.get_document(document_id, session=session)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    jobs = await ops.list_jobs_by_document(document_id, session=session)
    return DocumentDetail(
        **DocumentOut.model_validate(doc).model_dump(),
        jobs=[JobOut.model_validate(j) for j in jobs],
    )


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut], dependencies=[Depends(require(Permission.DOCUMENTS_READ))])
async def list_chunks(document_id: uuid.UUID, session: Session) -> list[ChunkOut]:
    if await ops.get_document(document_id, session=session) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    rows = await ops.list_chunks_by_document(document_id, session=session)
    return [ChunkOut(id=r.id, index=r.index, text=r.text, metadata=dict(r.metadata_)) for r in rows]


@router.get("/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require(Permission.JOBS_READ))])
async def get_job(job_id: uuid.UUID, session: Session) -> JobOut:
    job = await ops.get_job(job_id, session=session)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return JobOut.model_validate(job)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require(Permission.DOCUMENTS_DELETE))])
async def delete_document(
    document_id: uuid.UUID,
    session: Session,
    cfg: Cfg,
    store: Annotated[VectorStore, Depends(get_store)],
) -> None:
    doc = await ops.get_document(document_id, session=session)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    source = doc.source_path
    store.delete_doc(str(document_id))
    await ops.delete_document(document_id, session=session)
    _remove_upload_dir(cfg, source)
