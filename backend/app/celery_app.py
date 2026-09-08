from __future__ import annotations

from celery import Celery

from app.config import Settings

_settings = Settings()

celery_app = Celery(
    "rag_pipeline",
    broker=_settings.celery_broker_url,
    backend=_settings.celery_result_backend,
    include=["app.tasks.ingest", "app.tasks.cleanup"],
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
)

# Scheduled cleanup runs daily. The task bodies are stubs until their tickets land.
celery_app.conf.beat_schedule = {
    "cleanup-temp-and-tmp": {
        "task": "app.tasks.cleanup.cleanup_temp",
        "schedule": 24 * 60 * 60.0,
    },
}
