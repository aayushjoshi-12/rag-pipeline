"""Ticket 45: traces keep the usage of failed LLM calls and record embedding tokens (search, chat meta, ingest)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import ops
from app.db.enums import JobStatus
from app.db.models import Trace
from app.routers import deps
from app.services.ingestion import ingest_document
from app.services.llm import LLMUnavailableError, Usage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeEmbedder, FakeStore, MeteredEmbedder
from tests.integration.test_chat_api import (
    FakeLLM,
    _doc,
    _hit,
    chat_client,  # noqa: F401  (imported fixture)
)
from tests.integration.test_chat_api import FakeStore as ChatStore
from tests.integration.test_chat_stream_api import (  # noqa: F401  (imported fixtures)
    StreamLLM,
    _trace,
    _with_doc,
    cctx,
    needs_redis,
    parse_sse,
    sctx,
)
from tests.integration.test_ingestion import LONG_TEXT, _parse_ok, _setup

FAILED_USAGE = Usage(prompt_tokens=2500, completion_tokens=2048, cost_usd=0.00011)


async def _last(session: AsyncSession, kind: str) -> Trace:
    row = (
        await session.execute(select(Trace).where(Trace.kind == kind).order_by(Trace.created_at.desc()))
    ).scalars().first()
    assert row is not None
    await session.refresh(row)
    return row


# ── usage on failures ────────────────────────────────────────────────────────────────────────
async def test_failed_chat_keeps_the_usage_the_provider_reported(
    chat_client: Any, db_session: AsyncSession  # noqa: F811
) -> None:
    c, state = chat_client
    doc = await _doc(db_session)
    state["store"] = ChatStore([_hit(doc, 0, "text")])
    state["llm"] = FakeLLM(LLMUnavailableError("LLM ran out of output tokens", usage=FAILED_USAGE))
    assert (await c.post("/chat", json={"question": "q"})).status_code == 503
    t = await _last(db_session, "chat")
    assert t.status == "error"
    assert (t.prompt_tokens, t.completion_tokens, t.cost_usd) == (2500, 2048, 0.00011)


async def test_failed_chat_without_usage_stays_unknown(
    chat_client: Any, db_session: AsyncSession  # noqa: F811
) -> None:
    c, state = chat_client
    doc = await _doc(db_session)
    state["store"] = ChatStore([_hit(doc, 0, "text")])
    state["llm"] = FakeLLM(LLMUnavailableError("429"))
    await c.post("/chat", json={"question": "q"})
    t = await _last(db_session, "chat")
    assert (t.prompt_tokens, t.completion_tokens, t.cost_usd) == (None, None, None)


async def test_failed_stream_keeps_the_usage_on_the_error_trace(sctx: dict[str, Any]) -> None:  # noqa: F811
    await _with_doc(sctx)
    sctx["llm"] = StreamLLM(exc=LLMUnavailableError("out of tokens", usage=FAILED_USAGE), fail_after=1)
    ev = parse_sse((await sctx["c"].post("/chat/stream", json={"question": "q"})).text)
    assert ev[-1][0] == "error"
    row = await _trace(sctx, ev[-1][1]["trace_id"])
    assert row.status == "error"
    assert (row.prompt_tokens, row.completion_tokens, row.cost_usd) == (2500, 2048, 0.00011)


# ── embedding usage ──────────────────────────────────────────────────────────────────────────
async def test_search_trace_records_query_embedding_tokens_and_zero_local_cost(
    sctx: dict[str, Any], db_session: AsyncSession  # noqa: F811
) -> None:
    await _with_doc(sctx)
    from app.main import app

    app.dependency_overrides[deps.get_embedder_dep] = lambda: MeteredEmbedder()
    r = await sctx["c"].post("/search", json={"query": "life", "mode": "semantic"})
    assert r.status_code == 200, r.text
    t = await _last(db_session, "search")
    assert (t.prompt_tokens, t.completion_tokens, t.cost_usd) == (10, 0, 0.0)
    assert t.meta["embedding"] == {"tokens": 10}


async def test_search_with_an_embedder_that_reports_nothing_is_unknown(
    sctx: dict[str, Any], db_session: AsyncSession  # noqa: F811
) -> None:
    await _with_doc(sctx)
    await sctx["c"].post("/search", json={"query": "life"})  # FakeEmbedder: no usage
    t = await _last(db_session, "search")
    assert (t.prompt_tokens, t.cost_usd) == (None, None) and "embedding" not in t.meta


async def test_chat_keeps_llm_tokens_and_puts_embedding_usage_in_meta(
    sctx: dict[str, Any], db_session: AsyncSession  # noqa: F811
) -> None:
    await _with_doc(sctx)
    from app.main import app

    app.dependency_overrides[deps.get_embedder_dep] = lambda: MeteredEmbedder()
    ev = parse_sse((await sctx["c"].post("/chat/stream", json={"question": "life"})).text)
    row = await _trace(sctx, ev[-1][1]["trace_id"])
    assert (row.prompt_tokens, row.completion_tokens) == (120, 9)  # the LLM's, not the embedder's
    assert row.meta["embedding"] == {"tokens": 10}


@needs_redis
async def test_cached_query_embedding_is_zero_and_marked_cached(
    cctx: dict[str, Any], db_session: AsyncSession  # noqa: F811
) -> None:
    await _with_doc(cctx)
    from app.main import app

    app.dependency_overrides[deps.get_embedder_dep] = lambda: MeteredEmbedder()
    body = {"query": "life", "mode": "semantic"}
    await cctx["c"].post("/search", json=body)
    first = await _last(db_session, "search")
    assert first.meta["embedding"] == {"tokens": 10}
    await cctx["c"].post("/search", json=body)
    second = await _last(db_session, "search")
    assert second.id != first.id
    assert (second.prompt_tokens, second.cost_usd) == (0, 0.0)
    assert second.meta["embedding"] == {"tokens": 0, "cached": True}


async def test_ingest_trace_records_total_embedded_tokens(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    doc, job = await _setup(db_session, tmp_path)
    status = await ingest_document(
        job.id, session=db_session, settings=Settings(), parse_fn=_parse_ok(LONG_TEXT),
        embedder=MeteredEmbedder(), store=FakeStore(),
    )
    assert status == JobStatus.DONE
    t = await _last(db_session, "ingest")
    chunks = len(await ops.list_chunks_by_document(doc.id, session=db_session))
    assert t.meta["embedded"] == chunks and chunks > 1
    assert t.prompt_tokens == 10 * chunks == t.meta["embedding_tokens"]
    assert (t.completion_tokens, t.cost_usd) == (0, 0.0)


async def test_ingest_with_an_embedder_that_reports_nothing_is_unknown(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _, job = await _setup(db_session, tmp_path)
    await ingest_document(
        job.id, session=db_session, settings=Settings(), parse_fn=_parse_ok(LONG_TEXT),
        embedder=FakeEmbedder(), store=FakeStore(),
    )
    t = await _last(db_session, "ingest")
    assert (t.prompt_tokens, t.cost_usd) == (None, None) and "embedding_tokens" not in t.meta


async def test_incremental_ingest_that_embeds_nothing_records_a_real_zero(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _, job = await _setup(db_session, tmp_path)
    kw: dict[str, Any] = dict(session=db_session, settings=Settings(), embedder=MeteredEmbedder(), store=FakeStore())
    await ingest_document(job.id, parse_fn=_parse_ok(LONG_TEXT), **kw)
    await ingest_document(job.id, parse_fn=_parse_ok(LONG_TEXT), **kw)
    t = await _last(db_session, "ingest")
    assert t.meta["embedded"] == 0 and t.prompt_tokens == 0
