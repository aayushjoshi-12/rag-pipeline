"""Integration tests — retrieval + /chat with real Postgres, fake embedder/store/LLM."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.config import Settings
from app.db import ops
from app.main import app
from app.routers import deps
from app.services import traces
from app.services.llm import LLMRequestError, LLMUnavailableError
from app.services.retriever import RetrievalOptions, retrieve
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeEmbedder, FakeLLM, HitStore, api_client, hit, make_doc


async def test_retrieve_enriches_filename_and_skips_deleted_docs(db_session: AsyncSession) -> None:
    real = await make_doc(db_session)
    ghost = uuid.uuid4()
    store = HitStore(
        [
            hit(real, 2, "kept"),
            hit(ghost, 0, "orphan"),
            {"id": "x", "text": "bad", "metadata": {}},
        ]
    )
    out = await retrieve("q", top_k=5, embedder=FakeEmbedder(), store=store, session=db_session)
    assert [(r.text, r.filename, r.chunk_index) for r in out] == [("kept", "report.pdf", 2)]


async def test_retrieve_builds_doc_filters(db_session: AsyncSession) -> None:
    a, b = await make_doc(db_session), await make_doc(db_session)
    store = HitStore([])
    await retrieve("q", top_k=3, embedder=FakeEmbedder(), store=store, session=db_session)
    # de-duplication on (default): the store is asked for the candidate pool, the result is cut to top_k
    assert store.last_filters is None and store.last_k == 50
    await retrieve(
        "q", top_k=3, embedder=FakeEmbedder(), store=store, session=db_session, document_ids=[a]
    )
    assert store.last_filters == {"doc_id": str(a)}
    await retrieve(
        "q", top_k=3, embedder=FakeEmbedder(), store=store, session=db_session, document_ids=[a, b]
    )
    assert store.last_filters == {"doc_id": {"$in": [str(a), str(b)]}}
    await retrieve(
        "q", top_k=3, embedder=FakeEmbedder(), store=store, session=db_session,
        options=RetrievalOptions(dedupe=False),
    )
    assert store.last_k == 3  # no dedupe -> exactly top_k, the pre-ticket-23 behaviour
    assert (
        await retrieve("q", top_k=0, embedder=FakeEmbedder(), store=store, session=db_session) == []
    )


@pytest.fixture
async def chat_client(db_session: AsyncSession) -> AsyncIterator[Any]:
    state: dict[str, Any] = {"store": HitStore([]), "llm": FakeLLM()}
    async with api_client(
        db_session,
        settings=lambda: Settings(default_top_k=4, auth_enabled=False),
        embedder_dep=lambda: FakeEmbedder(),
        store=lambda: state["store"],
        llm_dep=lambda: state["llm"],
    ) as c:
        yield c, state


async def test_chat_returns_answer_with_sources(chat_client: Any, db_session: AsyncSession) -> None:
    c, state = chat_client
    doc = await make_doc(db_session, "facts.pdf")
    state["store"] = HitStore([hit(doc, 1, "Life is 42.")])
    r = await c.post("/chat", json={"question": "What is life?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "The answer is 42 [1]."
    assert body["sources"][0]["filename"] == "facts.pdf"
    assert body["sources"][0]["chunk_index"] == 1 and body["sources"][0]["document_id"] == str(doc)
    assert state["store"].last_k == 50  # candidate pool (>= settings.default_top_k=4), cut to 4 after de-duplication
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
    d = await make_doc(db_session)
    await c.post("/chat", json={"question": "q", "document_ids": [str(d)], "top_k": 9})
    assert state["store"].last_filters == {"doc_id": str(d)} and state["store"].last_k == 50


@pytest.mark.parametrize(
    ("exc", "code"), [(LLMUnavailableError("slow"), 503), (LLMRequestError("bad key"), 502)]
)
async def test_chat_maps_llm_errors(
    chat_client: Any, db_session: AsyncSession, exc: Exception, code: int
) -> None:
    c, state = chat_client
    doc = await make_doc(db_session)
    state["store"] = HitStore([hit(doc, 0, "t")])
    state["llm"] = FakeLLM(exc=exc)
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
    doc = await make_doc(db_session)
    state["store"] = HitStore([hit(doc, 0, "t")])
    assert (await c.post("/chat", json={"question": "q"})).status_code == 503


async def test_chat_retrieval_failure_is_503(chat_client: Any) -> None:
    c, state = chat_client

    class Boom(HitStore):
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


# ── trace log ────────────────────────────────────────────────────────────────


async def _traces(session: AsyncSession, kind: str = "chat") -> list[Any]:
    rows, _ = await traces.list_traces(session, kind=kind, limit=200)
    return rows


async def test_successful_chat_records_a_trace_with_tokens_model_and_hits(
    chat_client: Any, db_session: AsyncSession
) -> None:
    c, state = chat_client
    doc = await make_doc(db_session, "facts.pdf")
    state["store"] = HitStore([hit(doc, 1, "Life is 42."), hit(doc, 2, "More.")])
    before = len(await _traces(db_session))
    assert (await c.post("/chat", json={"question": "What is life?"})).status_code == 200
    rows = await _traces(db_session)
    assert len(rows) == before + 1
    t = rows[0]  # newest first
    assert (t.kind, t.status, t.model) == ("chat", "ok", "fake")
    assert (t.prompt_tokens, t.completion_tokens, t.hits) == (120, 9, 2)
    assert t.error is None and t.duration_ms >= 0
    assert t.question is None  # content capture is off by default
    assert t.cost_usd == pytest.approx(0.0123)
    # ids only (no text) of the documents the answer was grounded on, de-duplicated
    assert t.meta == {
        "top_k": 4,
        "neighbors": 1,
        "document_filter": 0,
        "cited_document_ids": [str(doc)],
        "mode": "semantic",
    }
    assert t.user_email is None  # auth disabled in this harness => anonymous


async def test_trace_stores_the_question_only_when_capture_is_enabled(
    chat_client: Any, db_session: AsyncSession
) -> None:
    c, _ = chat_client
    app.dependency_overrides[deps.get_settings] = lambda: Settings(
        default_top_k=4, auth_enabled=False, trace_capture_content=True
    )
    await c.post("/chat", json={"question": "secret question text"})
    assert (await _traces(db_session))[0].question == "secret question text"


async def test_failed_chat_records_an_error_trace_and_still_returns_the_error(
    chat_client: Any, db_session: AsyncSession
) -> None:
    c, state = chat_client
    doc = await make_doc(db_session)
    state["store"] = HitStore([hit(doc, 0, "text")])
    state["llm"] = FakeLLM(exc=LLMUnavailableError("LLM provider returned 429"))
    r = await c.post("/chat", json={"question": "q"})
    assert r.status_code == 503
    t = (await _traces(db_session))[0]
    assert t.status == "error" and "429" in (t.error or "")
    assert t.hits == 1 and t.prompt_tokens is None and t.cost_usd is None


async def test_a_request_with_no_hits_is_traced_as_ok_without_tokens(
    chat_client: Any, db_session: AsyncSession
) -> None:
    c, _ = chat_client
    await c.post("/chat", json={"question": "nothing here"})
    t = (await _traces(db_session))[0]
    assert (t.status, t.hits, t.prompt_tokens) == ("ok", 0, None)
    assert t.cost_usd is None and t.meta["cited_document_ids"] == []  # unknown, never 0


async def _chunks(session: AsyncSession, doc: uuid.UUID, n: int) -> None:
    await ops.create_chunks(
        document_id=doc,
        chunks=[{"index": i, "text": f"chunk-{i}", "metadata": {}} for i in range(n)],
        session=session,
    )


async def test_chat_neighbors_adds_context_and_marks_hits(
    chat_client: Any, db_session: AsyncSession
) -> None:
    c, state = chat_client
    doc = await make_doc(db_session, "rules.pdf")
    await _chunks(db_session, doc, 6)
    state["store"] = HitStore([hit(doc, 3, "chunk-3")])
    r = await c.post("/chat", json={"question": "q", "neighbors": 1})
    assert r.status_code == 200, r.text
    srcs = r.json()["sources"]
    assert [(s["chunk_index"], s["is_hit"]) for s in srcs] == [(2, False), (3, True), (4, False)]
    prompt = state["llm"].prompts[0]
    assert "chunk-2" in prompt and "chunk-4" in prompt
    assert prompt.index("chunk-2") < prompt.index("chunk-3") < prompt.index("chunk-4")


async def test_chat_default_is_hits_only_and_setting_applies(
    chat_client: Any, db_session: AsyncSession
) -> None:
    c, state = chat_client
    doc = await make_doc(db_session)
    await _chunks(db_session, doc, 6)
    state["store"] = HitStore([hit(doc, 3, "chunk-3")])
    app.dependency_overrides[deps.get_settings] = lambda: Settings(auth_enabled=False, retrieval_neighbors=0)
    srcs = (await c.post("/chat", json={"question": "q"})).json()["sources"]
    assert [s["chunk_index"] for s in srcs] == [3] and srcs[0]["is_hit"] is True
    app.dependency_overrides[deps.get_settings] = lambda: Settings(auth_enabled=False, retrieval_neighbors=2)
    srcs = (await c.post("/chat", json={"question": "q"})).json()["sources"]
    assert [s["chunk_index"] for s in srcs] == [1, 2, 3, 4, 5]
    # per-request override wins over the setting
    srcs = (await c.post("/chat", json={"question": "q", "neighbors": 0})).json()["sources"]
    assert [s["chunk_index"] for s in srcs] == [3]


async def test_chat_neighbors_validated(chat_client: Any) -> None:
    c, _ = chat_client
    assert (await c.post("/chat", json={"question": "q", "neighbors": 9})).status_code == 422
    assert (await c.post("/chat", json={"question": "q", "neighbors": -1})).status_code == 422
