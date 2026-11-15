"""Shared in-memory fakes (embedder, vector store, LLM) and the ASGI test-client helper."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from app.config import Settings
from app.services.llm import Completion
from sqlalchemy.ext.asyncio import AsyncSession


class FakeEmbedder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.embedded_texts: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedder down")
        self.embedded_texts.extend(texts)
        return [[float(len(t)), 1.0, 0.0] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 1.0, 0.0]


class FakeStore:
    """id -> {text, metadata, embedding}; mirrors ChromaStore's id/metadata semantics."""

    def __init__(self) -> None:
        self.vectors: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.deleted: list[str] = []

    def add_chunks(
        self, doc_id: str, chunks: list[dict[str, Any]], embeddings: list[list[float]]
    ) -> None:
        assert len(chunks) == len(embeddings)
        self.calls.append("add")
        ids = [str(c.get("id") or f"{doc_id}_{i}") for i, c in enumerate(chunks)]
        assert len(set(ids)) == len(ids), "duplicate ids in one add (Chroma rejects this)"
        for i, c, e in zip(ids, chunks, embeddings, strict=True):
            meta = {"doc_id": doc_id, "index": 0, **c.get("metadata", {})}
            self.vectors[i] = {"text": c["text"], "metadata": meta, "embedding": e}

    def similarity_search(
        self, query_embedding: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    def delete_doc(self, doc_id: str) -> None:
        self.calls.append("delete_doc")
        self.deleted.append(doc_id)
        self.vectors = {k: v for k, v in self.vectors.items() if v["metadata"]["doc_id"] != doc_id}

    def delete_ids(self, ids: list[str]) -> None:
        self.calls.append("delete_ids")
        for i in ids:
            self.vectors.pop(i, None)

    def update_metadata(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        self.calls.append("update_metadata")
        for i, m in zip(ids, metadatas, strict=True):
            self.vectors[i]["metadata"].update(m)

    def reset(self) -> None:
        self.vectors.clear()

    def texts_for(self, doc_id: str) -> set[str]:
        return {v["text"] for v in self.vectors.values() if v["metadata"]["doc_id"] == doc_id}


class MeteredEmbedder(FakeEmbedder):
    """A FakeEmbedder that reports provider usage like the real Ollama/OpenAI embedders (10 tokens per text)."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from app.services.embeddings import _report

        out = super().embed_documents(texts)
        _report(10 * len(texts))
        return out

    def embed_query(self, text: str) -> list[float]:
        from app.services.embeddings import _report

        _report(10)
        return super().embed_query(text)


class HitStore(FakeStore):
    """A FakeStore whose ``similarity_search`` returns canned ``hits`` and records the last filters/top_k."""

    def __init__(self, hits: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.hits = hits if hits is not None else []
        self.last_filters: dict[str, Any] | None = "unset"  # type: ignore[assignment]  # sentinel: never searched
        self.last_k = 0

    def similarity_search(
        self, query_embedding: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.last_filters, self.last_k = filters, top_k
        return self.hits


def hit(doc_id: uuid.UUID, index: int, text: str, score: float = 0.2) -> dict[str, Any]:
    """A store hit as ``similarity_search`` returns it."""
    return {
        "id": f"{doc_id}_{index}",
        "score": score,
        "text": text,
        "metadata": {"doc_id": str(doc_id), "index": index},
    }


async def make_doc(session: AsyncSession, name: str = "report.pdf") -> uuid.UUID:
    from app.db import ops

    d = await ops.create_document(filename=name, content_type="application/pdf", session=session)
    return d.id


class FakeLLM:
    """Scripted non-streaming LLM. ``answer`` may contain ``{n}`` (the call number); ``exc`` is raised on use."""

    def __init__(
        self,
        answer: str = "The answer is 42 [1].",
        usage: tuple[int, int] = (120, 9),
        cost: float | None = 0.0123,
        exc: Exception | None = None,
        model: str = "fake",
    ) -> None:
        self.answer, self.usage, self.cost, self.exc, self.model = answer, usage, cost, exc, model
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> Completion:
        if self.exc:
            raise self.exc
        self.calls += 1
        self.prompts.append(user)
        return Completion(self.answer.replace("{n}", str(self.calls)), *self.usage, self.cost)


class StreamLLM(FakeLLM):
    """Scripted streaming LLM. ``fail_after`` raises ``exc`` after N deltas; ``delay`` paces deltas."""

    def __init__(
        self,
        deltas: tuple[str, ...] = ("The answer ", "is 42 ", "[1]."),
        exc: Exception | None = None,
        fail_after: int | None = None,
        delay: float = 0.0,
        cost: float | None = 0.0123,
    ) -> None:
        super().__init__(exc=exc, cost=cost, model="fake-stream")
        self.deltas, self.fail_after, self.delay = deltas, fail_after, delay
        self.closed = threading.Event()
        self.emitted = 0

    def complete(self, system: str, user: str) -> Completion:  # pragma: no cover - streaming path only
        raise AssertionError("the streaming endpoint must not use complete()")

    def stream(self, system: str, user: str) -> Iterator[str | Completion]:
        self.calls += 1
        self.prompts.append(user)
        try:
            if self.exc is not None and self.fail_after is None:
                raise self.exc
            for i, d in enumerate(self.deltas):
                if self.fail_after is not None and i == self.fail_after:
                    assert self.exc is not None
                    raise self.exc
                if self.delay:
                    time.sleep(self.delay)
                self.emitted += 1
                yield d
            yield Completion("".join(self.deltas) + "  ", 120, 9, self.cost)
        finally:
            self.closed.set()


def make_settings(**kw: Any) -> Settings:
    """Settings for API tests: auth off unless the test says otherwise."""
    kw.setdefault("auth_enabled", False)
    return Settings(**kw)


@asynccontextmanager
async def api_client(session: AsyncSession, **providers: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    """An ASGI client on the real app with the DB session and any ``deps.get_<name>`` provider overridden.

    ``api_client(s, settings=lambda: cfg, store=lambda: st)`` overrides ``deps.get_settings`` / ``deps.get_store``.
    Overrides are cleared on exit.
    """
    from app.main import app
    from app.routers import deps

    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[deps.get_session] = session_dep
    for name, provider in providers.items():
        app.dependency_overrides[getattr(deps, f"get_{name}")] = provider
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c
    finally:
        app.dependency_overrides.clear()
