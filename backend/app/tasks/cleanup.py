from __future__ import annotations

from app.celery_app import celery_app


@celery_app.task(name="app.tasks.cleanup.cleanup_temp")  # type: ignore[untyped-decorator]
def cleanup_temp() -> None:
    """Clean expired temp/data artifacts (implemented in a later ticket)."""
    raise NotImplementedError("cleanup_temp implemented in a later ticket")