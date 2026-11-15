from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import celery_app
from app.config import Settings
from app.observability import configure_for_server
from app.services import runtime_settings
from app.services.embeddings import get_embedder
from app.services.reindex import reindex_all
from app.services.vectorstore import get_vector_store


async def _run(force: bool) -> dict[str, Any]:
    engine = create_async_engine(Settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await runtime_settings.refresh_quietly(session)  # pick up admin-UI changes made since this worker started
            settings = Settings()
            configure_for_server(level=logging.getLevelName(settings.log_level))
            # verify=False: the whole point is that the stored index may not match the settings yet.
            result = await reindex_all(
                session, embedder=get_embedder(), store=get_vector_store(settings=settings, verify=False), force=force
            )
            return {"documents": result.documents, "vectors": result.vectors, "seconds": round(result.seconds, 1)}
    finally:
        await engine.dispose()


async def _run_fts() -> dict[str, Any]:
    from app.services.keyword import rebuild_fts

    engine = create_async_engine(Settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await runtime_settings.refresh_quietly(session)
            config = Settings().retrieval_fts_config
            await rebuild_fts(session, config)
            return {"fts_config": config}
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.reindex.reindex_vectors")  # type: ignore[untyped-decorator]
def reindex_vectors(force: bool = False) -> dict[str, Any]:
    """Re-embed every stored chunk into a freshly created vector store (see services.reindex)."""
    return asyncio.run(_run(force))


@celery_app.task(name="app.tasks.reindex.rebuild_fts")  # type: ignore[untyped-decorator]
def rebuild_fts_task() -> dict[str, Any]:
    """Rebuild the keyword (full-text) index with the configured text-search configuration."""
    return asyncio.run(_run_fts())
