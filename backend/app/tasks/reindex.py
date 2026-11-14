from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import celery_app
from app.config import Settings
from app.observability import configure_for_server
from app.services.embeddings import get_embedder
from app.services.reindex import reindex_all
from app.services.vectorstore import get_vector_store


async def _run(force: bool) -> dict[str, Any]:
    settings = Settings()
    configure_for_server(level=logging.getLevelName(settings.log_level))
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            # verify=False: the whole point is that the stored index may not match the settings yet.
            result = await reindex_all(
                session, embedder=get_embedder(), store=get_vector_store(settings=settings, verify=False), force=force
            )
            return {"documents": result.documents, "vectors": result.vectors, "seconds": round(result.seconds, 1)}
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.reindex.reindex_vectors")  # type: ignore[untyped-decorator]
def reindex_vectors(force: bool = False) -> dict[str, Any]:
    """Re-embed every stored chunk into a freshly created vector store (see services.reindex)."""
    return asyncio.run(_run(force))
