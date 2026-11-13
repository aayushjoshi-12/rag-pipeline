"""FastAPI dependencies — overridable in tests (fakes for DB, broker, vector store)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import async_session
from app.services.embeddings import Embedder, get_embedder
from app.services.llm import LLM, LLMConfigError, get_llm
from app.services.vectorstore import VectorStore, get_vector_store

#: Enqueues an ingest job and returns the broker task id.
Enqueue = Callable[[str], str]


@lru_cache
def get_settings() -> Settings:
    return Settings()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session


def get_enqueue() -> Enqueue:
    def _enqueue(job_id: str) -> str:
        from app.tasks.ingest import process_document

        return str(process_document.delay(job_id).id)

    return _enqueue


def get_store() -> VectorStore:
    return get_vector_store()


def get_embedder_dep() -> Embedder:
    return get_embedder()


def get_llm_dep() -> LLM | None:
    """The configured LLM, or ``None`` when its key is missing (the route 503s only if needed)."""
    try:
        return get_llm(get_settings())
    except LLMConfigError:
        return None
