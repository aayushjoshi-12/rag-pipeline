from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import celery_app
from app.config import Settings
from app.services.cleanup import cleanup_orphan_uploads

logger = structlog.get_logger(__name__)


async def _run() -> int:
    settings = Settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            removed = await cleanup_orphan_uploads(
                session, settings.data_dir, min_age_hours=settings.cleanup_orphan_age_hours
            )
            return len(removed)
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.cleanup.cleanup_temp")  # type: ignore[untyped-decorator]
def cleanup_temp() -> int:
    """Remove orphaned upload directories. Returns how many were removed; never raises."""
    try:
        return asyncio.run(_run())
    except Exception:
        logger.exception("cleanup_failed")
        return 0
