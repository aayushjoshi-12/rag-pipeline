from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import celery_app
from app.config import Settings
from app.db import ops
from app.db.enums import JobStatus
from app.observability import configure_for_server
from app.services import observability
from app.services.embeddings import get_embedder
from app.services.ingestion import ingest_document
from app.services.langfuse import configure_from_settings
from app.services.parser import parse
from app.services.vectorstore import get_vector_store


async def _run(job_id: str) -> str:
    settings = Settings()
    configure_for_server(level=logging.getLevelName(settings.log_level))
    configure_from_settings(settings)
    # A fresh NullPool engine per task: asyncio.run() makes a new event loop each call and
    # asyncpg connections are bound to the loop that opened them.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            try:
                embedder = get_embedder()
                store = get_vector_store()
            except Exception as exc:  # misconfiguration: record it on the job, don't crash
                await ops.update_job_status(
                    uuid.UUID(job_id),
                    JobStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}"[:2000],
                    session=session,
                )
                return str(JobStatus.FAILED.value)
            status = await ingest_document(
                uuid.UUID(job_id),
                session=session,
                settings=settings,
                parse_fn=lambda path, filetype, cfg: parse(path, filetype, settings=cfg),
                embedder=embedder,
                store=store,
            )
            return str(status.value)
    finally:
        await engine.dispose()
        observability.flush()


@celery_app.task(name="app.tasks.ingest.process_document")  # type: ignore[untyped-decorator]
def process_document(job_id: str) -> str:
    """Process a doc job: parse → chunk → embed → store. Returns the final job status.

    Failures are recorded on the job row (status ``failed`` + error); the task itself does
    not raise, so a bad document never poisons the worker or triggers broker redelivery loops.
    Re-running an already-processed job restarts the ingest cleanly.
    """
    return asyncio.run(_run(job_id))
