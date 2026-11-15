"""Documents + jobs routes. The API only stores the upload, records a Job and enqueues the
Celery task — it never parses, chunks or embeds (ingest runs in the worker)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import ops
from app.db.enums import DocumentStatus, JobStatus
from app.routers.auth_deps import require
from app.routers.deps import (
    Enqueue,
    EnqueueUrl,
    get_enqueue,
    get_enqueue_url,
    get_session,
    get_settings,
    get_store,
)
from app.schema.documents import (
    ChunkOut,
    DocumentDetail,
    DocumentOut,
    DocumentPage,
    FromUrlRequest,
    JobOut,
    UploadResponse,
)
from app.services.auth.permissions import Permission
from app.services.filenames import ALLOWED_EXTENSIONS, name_from_url_path, safe_name
from app.services.storage import Storage, get_storage, new_key
from app.services.urlguard import (
    UrlRejected,
    check_http_target,
    parse_s3_link,
    parse_url,
    redact_url,
)
from app.services.vectorstore import VectorStore

logger = logging.getLogger(__name__)
router = APIRouter()
#: ``GET /v1/documents`` (paginated envelope). Mounted only under /v1.
v1_router = APIRouter()
#: ``GET /documents`` (historic bare array). Mounted only as the deprecated unversioned alias.
legacy_router = APIRouter()

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


_safe_name = safe_name


async def _store_upload(file: UploadFile, cfg: Settings) -> tuple[str, str, str, Storage]:
    """Validate and stream an upload, then hand it to the configured storage.

    Returns (filename, storage key, extension, storage).
    """
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

    storage = get_storage(cfg)
    key = new_key(filename)
    tmp_dir = Path(tempfile.mkdtemp(prefix="rag-upload-"))
    tmp = tmp_dir / "upload"
    size = 0
    try:
        with tmp.open("wb") as out:
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
        await asyncio.to_thread(storage.put_file, key, tmp, ALLOWED_EXTENSIONS[ext])
    except BaseException:
        await asyncio.to_thread(_drop, storage, key)
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return filename, key, ext, storage


def _drop(storage: Storage, key: str | None) -> None:
    """Remove a stored file's directory; storage failures never mask the caller's real outcome."""
    if not key:
        return
    try:
        storage.delete_dir(key)
    except Exception:
        logger.warning("could not remove stored file %s", key, exc_info=True)


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


@router.post("/documents", summary="Upload a document and queue ingestion", response_model=UploadResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require(Permission.DOCUMENTS_WRITE))])
async def upload_document(
    file: UploadFile,
    session: Session,
    cfg: Cfg,
    enqueue: Annotated[Enqueue, Depends(get_enqueue)],
) -> UploadResponse:
    filename, key, ext, storage = await _store_upload(file, cfg)
    doc = await ops.create_document(
        filename=filename,
        content_type=ALLOWED_EXTENSIONS[ext],
        source_path=key,
        storage_backend=storage.name,
        session=session,
    )
    job = await _enqueue_job(session, enqueue, doc.id)
    return UploadResponse(document=DocumentOut.model_validate(doc), job=job)


_ERR = {"description": "Error", "content": {"application/json": {"example": {"detail": "message"}}}}


@router.post(
    "/documents/from-url",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.DOCUMENTS_WRITE))],
    summary="Ingest a document from an http(s) link or s3:// object",
    responses={
        400: {**_ERR, "description": "Link refused (bad scheme, credentials in URL, private/internal address, bucket not allowed, unresolvable host, S3 not configured)."},
        401: _ERR,
        403: _ERR,
        503: {**_ERR, "description": "Broker unavailable; nothing was queued."},
    },
)
async def ingest_from_url(
    body: FromUrlRequest,
    session: Session,
    cfg: Cfg,
    enqueue: Annotated[EnqueueUrl, Depends(get_enqueue_url)],
) -> UploadResponse:
    """Create a document from a link and queue its ingestion (`202`-style: returns immediately).

    The API only **validates** the link (scheme allow-list `URL_INGEST_SCHEMES`, no embedded
    credentials, SSRF check: the host is resolved and every address must be public). The
    **download happens in the worker**, with a streaming size cap (`URL_INGEST_MAX_BYTES`),
    timeouts, redirect re-validation and file-type checks; then the normal parse/chunk/embed
    pipeline runs. Poll `GET /jobs/{job.id}`: `stats.source_url` (query string removed) and
    `stats.bytes_downloaded` appear once the file is fetched; `error` explains failures.

    The document's `filename` is provisional until the download finishes. Submitting a link that
    was ingested before creates a **new** document - there is no de-duplication by URL.
    `s3://bucket/key` reads with the server's S3 credentials and only from buckets listed in
    `URL_INGEST_S3_BUCKETS` (default: `S3_BUCKET`).
    """
    url = body.url.strip()
    try:
        parsed = parse_url(url, schemes=cfg.url_schemes)
        if parsed.scheme == "s3":
            if not cfg.s3_configured and not cfg.url_ingest_s3_buckets:
                raise UrlRejected("s3:// links need S3 to be configured on the server.")
            link = parse_s3_link(url, allowed_buckets=cfg.url_s3_buckets)
            name = safe_name(link.key.rsplit("/", 1)[-1])
        else:
            await asyncio.to_thread(
                check_http_target,
                url,
                schemes=cfg.url_schemes,
                allow_private=cfg.url_ingest_allow_private,
            )
            name = name_from_url_path(parsed.path_query.split("?", 1)[0]) or "download"
    except UrlRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    doc = await ops.create_document(
        filename=name,
        content_type="application/octet-stream",
        source_url=redact_url(url),
        storage_backend=cfg.storage_backend,
        session=session,
    )
    job = await ops.create_job(document_id=doc.id, session=session)
    try:
        task_id = enqueue(str(job.id), url)
    except Exception as exc:
        logger.exception("could not enqueue url ingest for job %s", job.id)
        await ops.update_job_status(
            job.id, JobStatus.FAILED, error=f"enqueue failed: {type(exc).__name__}", session=session
        )
        await ops.update_document_status(doc.id, DocumentStatus.FAILED, session=session)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not enqueue ingestion (broker unavailable)."
        ) from exc
    refreshed = await ops.update_job_status(job.id, JobStatus.PENDING, task_id=task_id, session=session)
    return UploadResponse(document=DocumentOut.model_validate(doc), job=JobOut.model_validate(refreshed or job))


@router.put("/documents/{document_id}", summary="Replace a document with a new version", response_model=UploadResponse, dependencies=[Depends(require(Permission.DOCUMENTS_WRITE))])
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
    old_source, old_backend = existing.source_path, existing.storage_backend
    filename, key, ext, storage = await _store_upload(file, cfg)
    doc = await ops.replace_document_source(
        document_id,
        filename=filename,
        content_type=ALLOWED_EXTENSIONS[ext],
        source_path=key,
        storage_backend=storage.name,
        session=session,
    )
    if doc is None:  # deleted while uploading
        await asyncio.to_thread(_drop, storage, key)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    job = await _enqueue_job(session, enqueue, doc.id)
    await asyncio.to_thread(_drop, get_storage(cfg, old_backend), old_source)
    return UploadResponse(document=DocumentOut.model_validate(doc), job=job)


@v1_router.get(
    "/documents",
    response_model=DocumentPage,
    summary="List documents (paginated)",
    dependencies=[Depends(require(Permission.DOCUMENTS_READ))],
)
async def list_documents_page(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500, description="Page size.")] = 50,
    offset: Annotated[int, Query(ge=0, description="Documents to skip.")] = 0,
) -> DocumentPage:
    """Documents, newest first, as `{items, total, limit, offset, has_more}`.

    Page through with `offset += limit` until `has_more` is false. Out-of-range `limit`/`offset`
    is a `422` (never silently clamped).
    """
    docs = await ops.list_documents(skip=offset, limit=limit, session=session)
    total = await ops.count_documents(session=session)
    return DocumentPage(
        items=[DocumentOut.model_validate(d) for d in docs],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(docs) < total,
    )


@legacy_router.get("/documents", response_model=list[DocumentOut], dependencies=[Depends(require(Permission.DOCUMENTS_READ))])
async def list_documents(session: Session, skip: int = 0, limit: int = 100) -> list[DocumentOut]:
    """Deprecated: unversioned bare-array list used by the dashboard. Use `GET /v1/documents`."""
    limit = max(1, min(limit, 500))
    docs = await ops.list_documents(skip=max(0, skip), limit=limit, session=session)
    return [DocumentOut.model_validate(d) for d in docs]


@router.get("/documents/{document_id}", summary="Get a document with its jobs", response_model=DocumentDetail, dependencies=[Depends(require(Permission.DOCUMENTS_READ))])
async def get_document(document_id: uuid.UUID, session: Session) -> DocumentDetail:
    doc = await ops.get_document(document_id, session=session)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    jobs = await ops.list_jobs_by_document(document_id, session=session)
    return DocumentDetail(
        **DocumentOut.model_validate(doc).model_dump(),
        jobs=[JobOut.model_validate(j) for j in jobs],
    )


@router.get("/documents/{document_id}/chunks", summary="List a document's chunks", response_model=list[ChunkOut], dependencies=[Depends(require(Permission.DOCUMENTS_READ))])
async def list_chunks(document_id: uuid.UUID, session: Session) -> list[ChunkOut]:
    if await ops.get_document(document_id, session=session) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    rows = await ops.list_chunks_by_document(document_id, session=session)
    return [ChunkOut(id=r.id, index=r.index, text=r.text, metadata=dict(r.metadata_)) for r in rows]


@router.get("/jobs/{job_id}", summary="Get an ingestion job (poll for status)", response_model=JobOut, dependencies=[Depends(require(Permission.JOBS_READ))])
async def get_job(job_id: uuid.UUID, session: Session) -> JobOut:
    job = await ops.get_job(job_id, session=session)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return JobOut.model_validate(job)


@router.delete("/documents/{document_id}", summary="Delete a document and its vectors", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require(Permission.DOCUMENTS_DELETE))])
async def delete_document(
    document_id: uuid.UUID,
    session: Session,
    cfg: Cfg,
    store: Annotated[VectorStore, Depends(get_store)],
) -> None:
    doc = await ops.get_document(document_id, session=session)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    source, backend = doc.source_path, doc.storage_backend
    store.delete_doc(str(document_id))
    await ops.delete_document(document_id, session=session)
    # Best effort: a failure leaves an orphan the scheduled cleanup removes (never an error to the caller).
    await asyncio.to_thread(_drop, get_storage(cfg, backend), source)
