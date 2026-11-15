"""Integration — ingest and chat emit the expected spans and bind log context."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import structlog
from app.config import Settings
from app.db import ops
from app.services import observability
from app.services.ingestion import ingest_document
from app.services.parser import ParseResult
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeLLM, api_client

TEXT = " ".join(f"word{i}" for i in range(400))


class Recorder:
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []

    @contextmanager
    def span(self, name: str, *, input: Any = None, **meta: Any) -> Iterator[Any]:
        self.spans.append((name, meta))
        rec = self

        class _S:
            def update(self, **kw: Any) -> None:
                rec.updates.append(kw)

        yield _S()

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.spans]


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()
    monkeypatch.setattr(observability, "span", r.span)
    return r


class Embedder:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0]


class CtxStore:
    """Fake store that records the structlog context visible while it is written to."""

    def __init__(self) -> None:
        self.ctx: dict[str, Any] = {}

    def add_chunks(self, doc_id: str, chunks: list[dict[str, Any]], embeddings: list[list[float]]) -> None:
        self.ctx = dict(structlog.contextvars.get_contextvars())

    def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return []

    def delete_doc(self, doc_id: str) -> None: ...

    def reset(self) -> None: ...


def _ok(path: Path, ft: str, cfg: Settings) -> ParseResult:
    return ParseResult(text=TEXT, format="text", filetype=ft, provider="fake")


def _boom(path: Path, ft: str, cfg: Settings) -> ParseResult:
    raise RuntimeError("parse failed")


async def _job(session: AsyncSession, tmp_path: Path) -> uuid.UUID:
    f = tmp_path / "a.txt"
    f.write_text("x")
    d = await ops.create_document(filename="a.txt", content_type="text/plain", source_path=str(f), session=session)
    return (await ops.create_job(document_id=d.id, session=session)).id


async def test_ingest_emits_stage_spans_and_binds_job_id(db_session: AsyncSession, tmp_path: Path, rec: Recorder) -> None:
    job_id = await _job(db_session, tmp_path)
    store = CtxStore()
    await ingest_document(job_id, session=db_session, settings=Settings(), parse_fn=_ok, embedder=Embedder(), store=store)
    assert rec.names == ["ingest.document", "ingest.parse", "ingest.chunk", "ingest.embed", "ingest.store"]
    assert store.ctx.get("job_id") == str(job_id)
    assert {"output": "done"} in rec.updates
    assert not any(u.get("level") == "ERROR" for u in rec.updates)


async def test_failed_ingest_marks_root_span_error(db_session: AsyncSession, tmp_path: Path, rec: Recorder) -> None:
    job_id = await _job(db_session, tmp_path)
    await ingest_document(job_id, session=db_session, settings=Settings(), parse_fn=_boom, embedder=Embedder(), store=CtxStore())
    assert "ingest.parse" in rec.names and "ingest.chunk" not in rec.names
    assert any(u.get("level") == "ERROR" for u in rec.updates)


async def test_context_does_not_leak_after_ingest(db_session: AsyncSession, tmp_path: Path, rec: Recorder) -> None:
    structlog.contextvars.clear_contextvars()
    job_id = await _job(db_session, tmp_path)
    await ingest_document(job_id, session=db_session, settings=Settings(), parse_fn=_ok, embedder=Embedder(), store=CtxStore())
    assert structlog.contextvars.get_contextvars() == {}


async def test_chat_emits_spans(db_session: AsyncSession, rec: Recorder) -> None:
    doc = await ops.create_document(filename="f.pdf", content_type="application/pdf", session=db_session)

    class Store(CtxStore):
        def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return [{"id": "x", "score": 0.1, "text": "t", "metadata": {"doc_id": str(doc.id), "index": 0}}]

    async with api_client(
        db_session,
        settings=lambda: Settings(auth_enabled=False),
        embedder_dep=lambda: Embedder(),
        store=lambda: Store(),
        llm_dep=lambda: FakeLLM("answer", usage=(7, 3), cost=None),
    ) as c:
        r = await c.post("/chat", json={"question": "q?"})
    assert r.status_code == 200
    assert rec.names == ["chat.request", "chat.retrieve", "chat.context", "chat.answer"]
    assert dict(rec.spans[0][1])["top_k"] == Settings().default_top_k
