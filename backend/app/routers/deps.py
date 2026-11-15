"""FastAPI dependencies — overridable in tests (fakes for DB, broker, vector store)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import async_session
from app.services import runtime_settings
from app.services.embeddings import Embedder, get_embedder
from app.services.llm import LLM, LLMConfigError, get_llm
from app.services.vectorstore import VectorStore, get_vector_store

#: Enqueues an ingest job and returns the broker task id.
Enqueue = Callable[[str], str]


async def get_settings() -> Settings:
    """Effective settings for this request: env > admin UI (database) > config.json > defaults.

    Refreshes the UI layer when it is older than SETTINGS_REFRESH_SECONDS (never raises).
    """
    await runtime_settings.ensure_fresh()
    return Settings()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session


def get_enqueue() -> Enqueue:
    def _enqueue(job_id: str) -> str:
        from app.tasks.ingest import process_document

        return str(process_document.delay(job_id).id)

    return _enqueue


#: Enqueues a URL-ingest job: (job_id, full_url) -> broker task id.
EnqueueUrl = Callable[[str, str], str]


def get_enqueue_url() -> EnqueueUrl:
    def _enqueue(job_id: str, url: str) -> str:
        from app.tasks.ingest import process_document

        return str(process_document.delay(job_id, url).id)

    return _enqueue


def get_store() -> VectorStore:
    return get_vector_store()


def get_embedder_dep() -> Embedder:
    return get_embedder()


def get_llm_dep() -> LLM | None:
    """The configured LLM, or ``None`` when its key is missing (the route 503s only if needed)."""
    try:
        return get_llm(Settings())
    except LLMConfigError:
        return None


#: Queues a settings-triggered maintenance task (``"reindex"`` | ``"rebuild_fts"``) and returns its task id.
EnqueueReindex = Callable[[str], str]


def get_enqueue_reindex() -> EnqueueReindex:
    def _enqueue(kind: str) -> str:
        from app.tasks.reindex import rebuild_fts_task, reindex_vectors

        task = rebuild_fts_task if kind == "rebuild_fts" else reindex_vectors
        return str(task.delay().id)

    return _enqueue


#: Looks up a Celery task: ``(state, result, error)`` with state in pending|running|done|failed|unknown.
TaskStatus = Callable[[str], tuple[str, dict[str, object] | None, str | None]]


def get_task_status() -> TaskStatus:
    def _status(task_id: str) -> tuple[str, dict[str, object] | None, str | None]:
        from app.celery_app import celery_app

        res = celery_app.AsyncResult(task_id)
        state = str(res.state)
        if state == "SUCCESS":
            out = res.result if isinstance(res.result, dict) else None
            return "done", out, None
        if state == "FAILURE":
            return "failed", None, type(res.result).__name__
        if state in ("STARTED", "RETRY"):
            return "running", None, None
        return "pending", None, None

    return _status
