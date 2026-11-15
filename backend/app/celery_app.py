from __future__ import annotations

from celery import Celery
from celery.signals import worker_ready

from app.config import Settings

_settings = Settings()

celery_app = Celery(
    "rag_pipeline",
    broker=_settings.celery_broker_url,
    backend=_settings.celery_result_backend,
    include=["app.tasks.ingest", "app.tasks.cleanup", "app.tasks.reindex"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    worker_concurrency=_settings.worker_concurrency,
)

# Scheduled cleanup runs daily. The task bodies are stubs until their tickets land.
celery_app.conf.beat_schedule = {
    "cleanup-temp-and-tmp": {
        "task": "app.tasks.cleanup.cleanup_temp",
        "schedule": 24 * 60 * 60.0,
    },
}


@worker_ready.connect  # type: ignore[untyped-decorator]  # celery's signal decorator is untyped
def _log_vector_settings(**_: object) -> None:
    """Worker start-up: log the effective vector settings and flag a stored-index mismatch."""
    from app.services.vectorstore import log_effective_settings

    log_effective_settings()
