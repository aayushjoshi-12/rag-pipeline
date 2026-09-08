from __future__ import annotations

from app.celery_app import celery_app


@celery_app.task(name="app.tasks.ingest.process_document")  # type: ignore[untyped-decorator]
def process_document(job_id: str) -> None:
    """Process a doc job: parse, chunk, embed, store (lands in the ingest ticket)."""
    raise NotImplementedError("process_document implemented in the ingest ticket")