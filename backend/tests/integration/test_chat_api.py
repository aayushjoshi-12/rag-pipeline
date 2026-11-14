"""Integration tests — retrieval + /chat with real Postgres, fake embedder/store/LLM."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.db import ops
from app.main import app
from app.routers import deps
from app.services.llm import LLMRequestError, LLMUnavailableError
from app.services.retriever import retrieve
from sqlalchemy.ext.asyncio import AsyncSession


class FakeEmbedder:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0]


class FakeStore:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits
        self.last_filters: dict[str, Any] | None = "unset"  # type: ignore[assignment]
        self.last_k = 0

    def add_chunks(self, *a: Any) -> None: ...

    def similarity_search(
        self, query_embedding: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.last_filters, self.last_k = filters, top_k
        return self.hits

    def delete_doc(self, doc_id: str) -> None: ...

    def reset(self) -> None: ...


class FakeLLM:
    model = "fake"

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> str:
        if self.exc:
            raise self.exc
        self.prompts.append(user)
        return "The answer is 42 [1]."


def _hit(doc_id: uuid.UUID, index: int, text: str, score: float = 0.2) -> dict[str, Any]:
    return {
        "id": f"{doc_id}_{index}",
        "score": score,
        "text": text,
        "metadata": {"doc_id": str(doc_id), "index": index},
    }


async def _doc(session: AsyncSession, name: str = "report.pdf") -> uuid.UUID:
    d = await ops.create_document(filename=name, content_type="application/pdf", session=session)
    return d.id


async def test_retrieve_enriches_filename_and_skips_deleted_docs(db_session: AsyncSession) -> None:
    real = await _doc(db_session)
    ghost = uuid.uuid4()
    store = FakeStore(
        [
            _hit(real, 2, "kept"),
            _hit(ghost, 0, "orphan"),
            {"id": "x", "text": "bad", "metadata": {}},
        ]
    )
    out = await retrieve("q", top_k=5, embedder=FakeEmbedder(), store=store, session=db_session)
    assert [(r.text, r.filename, r.chunk_index) for r in out] == [("kept", "report.pdf", 2)]


async def test_retrieve_builds_doc_filters(db_session: AsyncSession) -> None:
    a, b = await _doc(db_session), await _doc(db_session)
    store = FakeStore([])
    await retrieve("q", top_k=3, embedder=FakeEmbedder(), store=store, session=db_session)
    assert store.last_filters is None and store.last_k == 3
    await retrieve(
        "q", top_k=3, embedder=FakeEmbedder(), store=store, session=db_session, document_ids=[a]
    )
    assert store.last_filters == {"doc_id": str(a)}
    await retrieve(
        "q", top_k=3, embedder=FakeEmbedder(), store=store, session=db_session, document_ids=[a, b]
    )
    assert store.last_filters == {"doc_id": {"$in": [str(a), str(b)]}}
    assert (
        await retrieve("q", top_k=0, embedder=FakeEmbedder(), store=store, session=db_session) == []
    )


@pytest.fixture
async def chat_client(db_session: AsyncSession) -> AsyncIterator[Any]:
    state: dict[str, Any] = {"store": FakeStore([]), "llm": FakeLLM()}

    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: Settings(default_top_k=4, auth_enabled=False)
    app.dependency_overrides[deps.get_embedder_dep] = lambda: FakeEmbedder()
    app.dependency_overrides[deps.get_store] = lambda: state["store"]
    app.dependency_overrides[deps.get_llm_dep] = lambda: state["llm"]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            yield c, state
    finally:
        app.dependency_overrides.clear()


async def test_chat_returns_answer_with_sources(chat_client: Any, db_session: AsyncSession) -> None:
    c, state = chat_client
    doc = await _doc(db_session, "facts.pdf")
    state["store"] = FakeStore([_hit(doc, 1, "Life is 42.")])
    r = await c.post("/chat", json={"question": "What is life?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "The answer is 42 [1]."
    assert body["sources"][0]["filename"] == "facts.pdf"
    assert body["sources"][0]["chunk_index"] == 1 and body["sources"][0]["document_id"] == str(doc)
    assert state["store"].last_k == 4  # settings.default_top_k
    assert "Life is 42." in state["llm"].prompts[0] and "What is life?" in state["llm"].prompts[0]


async def test_chat_with_no_hits_skips_llm(chat_client: Any) -> None:
    c, state = chat_client
    r = await c.post("/chat", json={"question": "anything"})
    assert r.status_code == 200 and r.json()["sources"] == []
    assert state["llm"].prompts == []


async def test_chat_passes_document_filter_and_top_k(
    chat_client: Any, db_session: AsyncSession
) -> None:
    c, state = chat_client
    d = await _doc(db_session)
    await c.post("/chat", json={"question": "q", "document_ids": [str(d)], "top_k": 9})
    assert state["store"].last_filters == {"doc_id": str(d)} and state["store"].last_k == 9


@pytest.mark.parametrize(
    ("exc", "code"), [(LLMUnavailableError("slow"), 503), (LLMRequestError("bad key"), 502)]
)
async def test_chat_maps_llm_errors(
    chat_client: Any, db_session: AsyncSession, exc: Exception, code: int
) -> None:
    c, state = chat_client
    doc = await _doc(db_session)
    state["store"] = FakeStore([_hit(doc, 0, "t")])
    state["llm"] = FakeLLM(exc)
    r = await c.post("/chat", json={"question": "q"})
    assert r.status_code == code


async def test_chat_without_configured_llm_is_503_only_when_needed(
    chat_client: Any, db_session: AsyncSession
) -> None:
    c, state = chat_client
    state["llm"] = None
    assert (
        await c.post("/chat", json={"question": "q"})
    ).status_code == 200  # no context -> no LLM needed
    doc = await _doc(db_session)
    state["store"] = FakeStore([_hit(doc, 0, "t")])
    assert (await c.post("/chat", json={"question": "q"})).status_code == 503


async def test_chat_retrieval_failure_is_503(chat_client: Any) -> None:
    c, state = chat_client

    class Boom(FakeStore):
        def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise ConnectionError("chroma down")

    state["store"] = Boom([])
    assert (await c.post("/chat", json={"question": "q"})).status_code == 503


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "q", "top_k": 0},
        {"question": "q", "document_ids": ["nope"]},
    ],
)
async def test_chat_validates_input(chat_client: Any, payload: dict[str, Any]) -> None:
    c, _ = chat_client
    assert (await c.post("/chat", json=payload)).status_code == 422
