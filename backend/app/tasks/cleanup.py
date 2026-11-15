from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import celery_app
from app.config import Settings
from app.services import runtime_settings, traces
from app.services.cleanup import cleanup_orphan_objects, cleanup_orphan_uploads
from app.services.storage import S3Storage

logger = structlog.get_logger(__name__)


async def _run() -> int:
    engine = create_async_engine(Settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await runtime_settings.refresh_quietly(session)
            settings = Settings()
            removed = await cleanup_orphan_uploads(
                session, settings.data_dir, min_age_hours=settings.cleanup_orphan_age_hours
            )
            if settings.storage_backend == "s3":
                try:
                    removed += await cleanup_orphan_objects(
                        session,
                        S3Storage(settings),
                        backend="s3",
                        min_age_hours=settings.cleanup_orphan_age_hours,
                    )
                except Exception:
                    logger.exception("s3_orphan_cleanup_failed")
            purged = await traces.purge_older_than(session, settings.trace_retention_days)
            if purged:
                logger.info("traces_purged", count=purged)
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
