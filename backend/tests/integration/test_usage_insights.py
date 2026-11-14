"""Usage aggregates (cost, latency, errors, heatmap, RAG health, ingest) on a real Postgres."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from app.config import Settings
from app.db import ops
from app.db.models import Document, Trace
from app.main import app
from app.routers import deps
from app.services import credits, traces, usage_insights
from app.services.usage_insights import nice_step, normalize_error
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _add(s: AsyncSession, *, age_days: int = 0, at: datetime | None = None, **kw: Any) -> None:
    fields: dict[str, Any] = {"kind": "chat", "status": "ok", "duration_ms": 100, "meta": {}}
    fields.update(kw)
    when = at or (_now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=age_days))
    s.add(Trace(created_at=when, **fields))
    await s.commit()


@pytest.fixture
async def clean(isolated_session: AsyncSession) -> AsyncSession:
    await isolated_session.execute(delete(Trace))
    await isolated_session.execute(delete(Document))
    await isolated_session.commit()
    return isolated_session


# ── per-day cost ────────────────────────────────────────────────────────────


async def test_usage_sums_cost_per_day_and_keeps_unknown_distinct_from_zero(clean: AsyncSession) -> None:
    await _add(clean, cost_usd=0.002)
    await _add(clean, cost_usd=0.0005)
    await _add(clean, cost_usd=None)  # ok chat, price unknown -> unpriced
    await _add(clean, status="error", cost_usd=None)  # failed call: not "unpriced"
    await _add(clean, kind="ingest", cost_usd=None)  # embeddings are not priced either
    await _add(clean, age_days=1, cost_usd=0.0)  # free model: a known zero
    await _add(clean, age_days=2, cost_usd=None)
    days = await traces.usage(clean, days=4)
    today, yesterday, two_ago, empty = days[-1], days[-2], days[-3], days[-4]
    assert today.cost_usd == pytest.approx(0.0025) and today.unpriced_requests == 1
    assert yesterday.cost_usd == 0.0 and yesterday.unpriced_requests == 0  # known zero, not None
    assert two_ago.cost_usd is None and two_ago.unpriced_requests == 1  # unknown, not 0
    assert empty.cost_usd is None and empty.unpriced_requests == 0


# ── insights: cost ──────────────────────────────────────────────────────────


async def test_cost_totals_average_and_grouping_by_model(clean: AsyncSession) -> None:
    for cost, model in ((0.004, "big"), (0.002, "big"), (0.0, "free"), (0.0, "free"), (None, "mystery")):
        await _add(clean, cost_usd=cost, model=model, prompt_tokens=10, completion_tokens=5)
    await _add(clean, status="error", model="big", cost_usd=None)
    await _add(clean, kind="ingest", model="embed")
    out = await usage_insights.insights(clean, days=7)
    assert out.cost_usd == pytest.approx(0.006)
    assert (out.priced_chats, out.unpriced_chats) == (4, 1)
    assert out.avg_cost_per_chat == pytest.approx(0.0015)  # 0.006 over the 4 priced chats
    by = {m.model: m for m in out.by_model}
    assert set(by) == {"big", "free", "mystery"}  # the embedding model is not an LLM cost row
    assert by["big"].requests == 3 and by["big"].cost_usd == pytest.approx(0.006)
    assert (by["big"].prompt_tokens, by["big"].completion_tokens) == (20, 10)
    assert by["free"].cost_usd == 0.0 and by["free"].unpriced_requests == 0
    assert by["mystery"].cost_usd is None and by["mystery"].unpriced_requests == 1
    assert out.by_model[0].model == "big"  # most expensive first


async def test_cost_is_none_without_any_priced_chat_and_kind_filter_scopes_it(clean: AsyncSession) -> None:
    out = await usage_insights.insights(clean, days=7)
    assert out.cost_usd is None and out.avg_cost_per_chat is None and out.by_model == []
    await _add(clean, cost_usd=0.01, model="m")
    await _add(clean, age_days=30, cost_usd=5.0, model="m")  # outside the window
    assert (await usage_insights.insights(clean, days=7)).cost_usd == pytest.approx(0.01)
    assert (await usage_insights.insights(clean, days=7, kind="ingest")).by_model == []


# ── insights: performance ───────────────────────────────────────────────────


def test_nice_step_picks_1_2_5_widths_within_twelve_buckets() -> None:
    assert nice_step(0) == 1
    assert nice_step(990) == 100
    assert nice_step(5) == 1
    assert nice_step(23) == 2
    assert nice_step(119) == 10
    assert nice_step(120) == 20  # 0..120 is 121 values: 12 buckets of 10 would not cover it
    assert nice_step(2399) == 200
    assert all(nice_step(m) * 12 >= m + 1 for m in range(0, 5000))


async def test_latency_histogram_is_bucketed_in_sql_and_zero_filled(clean: AsyncSession) -> None:
    for ms in (5, 15, 25, 99, 990):
        await _add(clean, duration_ms=ms)
    out = await usage_insights.insights(clean, days=7)
    assert out.latency_bucket_ms == 100
    h = out.latency_histogram
    assert [b.lo for b in h] == list(range(0, 1000, 100)) and h[0].hi == 100
    assert [b.count for b in h] == [4, 0, 0, 0, 0, 0, 0, 0, 0, 1]  # 5,15,25,99 | ... | 990
    assert sum(b.count for b in h) == 5


async def test_latency_histogram_empty_and_kind_scoped(clean: AsyncSession) -> None:
    assert (await usage_insights.insights(clean, days=7)).latency_histogram == []
    await _add(clean, duration_ms=10)
    await _add(clean, kind="ingest", duration_ms=3000)
    assert sum(b.count for b in (await usage_insights.insights(clean, days=7, kind="chat")).latency_histogram) == 1
    only_ingest = await usage_insights.insights(clean, days=7, kind="ingest")
    assert only_ingest.latency_histogram[-1].count == 1 and only_ingest.latency_histogram[-1].hi > 3000


def test_error_normalisation_masks_ids_paths_and_long_numbers() -> None:
    a = normalize_error(f"source file not found: /data/uploads/{uuid.uuid4()}/report.pdf")
    b = normalize_error(f"source file not found: /data/uploads/{uuid.uuid4()}/other.pdf")
    assert a == b == "source file not found: <path>"
    assert normalize_error("LLM provider returned 429") == "LLM provider returned 429"  # short numbers stay
    assert normalize_error("job 1234567 failed\ntraceback...") == "job <n> failed"
    assert normalize_error(None) == normalize_error("   ") == "Unknown error"
    assert len(normalize_error("x" * 500)) == 80


async def test_errors_are_grouped_by_normalised_reason_with_an_other_bucket(clean: AsyncSession) -> None:
    for _ in range(3):
        await _add(clean, status="error", error="LLM provider returned 429")
    for _ in range(2):
        await _add(clean, status="error", error=f"document {uuid.uuid4()} not found")
    for i in range(9):
        await _add(clean, status="error", error=f"distinct failure number {i}")
    await _add(clean, status="ok", error=None)
    out = await usage_insights.insights(clean, days=7)
    reasons = out.errors_by_reason
    assert reasons[0].reason == "LLM provider returned 429" and reasons[0].count == 3
    assert reasons[1].reason == "document <id> not found" and reasons[1].count == 2
    assert len(reasons) == 9 and reasons[-1].reason == "Other"  # top 8 + the rest
    assert sum(r.count for r in reasons) == 14


async def test_heatmap_cells_count_by_utc_weekday_and_hour(clean: AsyncSession) -> None:
    base = _now().replace(minute=30, second=0, microsecond=0)
    day = base.replace(hour=9)
    for hour in (9, 9, 17):
        await _add(clean, at=day.replace(hour=hour))
    earlier = day - timedelta(days=1)
    await _add(clean, at=earlier)
    out = await usage_insights.insights(clean, days=7)
    cells = {(c.weekday, c.hour): c.count for c in out.heatmap}
    assert cells == {
        (day.weekday(), 9): 2,
        (day.weekday(), 17): 1,
        (earlier.weekday(), 9): 1,
    }
    assert [(c.weekday, c.hour) for c in out.heatmap] == sorted(cells)  # ordered, sparse


async def test_heatmap_weekday_is_monday_zero(clean: AsyncSession) -> None:
    today = _now()
    monday = (today - timedelta(days=today.weekday())).replace(hour=3, minute=0)
    await _add(clean, at=monday)
    assert (await usage_insights.insights(clean, days=14)).heatmap[0].weekday == 0


# ── insights: RAG health ────────────────────────────────────────────────────


async def _doc(s: AsyncSession, name: str) -> uuid.UUID:
    return (await ops.create_document(filename=name, content_type="application/pdf", session=s)).id


async def test_no_hit_rate_and_hits_histogram(clean: AsyncSession) -> None:
    for hits in (0, 0, 1, 3, 3, 5):
        await _add(clean, hits=hits)
    await _add(clean, age_days=1, hits=0)
    await _add(clean, status="error", hits=None)  # failed before retrieval: not a query outcome
    await _add(clean, hits=2, kind="ingest")  # not a chat
    out = await usage_insights.insights(clean, days=3)
    assert len(out.rag_days) == 3
    assert (out.rag_days[-1].queries, out.rag_days[-1].no_hit) == (6, 2)
    assert (out.rag_days[-2].queries, out.rag_days[-2].no_hit) == (1, 1)
    assert (out.rag_days[0].queries, out.rag_days[0].no_hit) == (0, 0)  # zero-filled
    assert [(b.hits, b.count) for b in out.hits_histogram] == [(0, 3), (1, 1), (3, 2), (5, 1)]


async def test_most_cited_documents_never_cited_and_deleted_docs(clean: AsyncSession) -> None:
    a, b, c = await _doc(clean, "a.pdf"), await _doc(clean, "b.pdf"), await _doc(clean, "never.pdf")
    ghost = uuid.uuid4()
    for ids in ([a, b], [a], [a, ghost], [b]):
        await _add(clean, meta={"cited_document_ids": [str(i) for i in ids]})
    await _add(clean, age_days=30, meta={"cited_document_ids": [str(c)]})  # outside the window
    await _add(clean, meta={"cited_document_ids": "not-a-list"})  # malformed meta is ignored
    await _add(clean, meta={})  # old trace without the key
    await _add(clean, kind="ingest", meta={"cited_document_ids": [str(c)]})  # only chats count
    out = await usage_insights.insights(clean, days=7)
    top = [(d.filename, d.citations) for d in out.top_documents]
    assert top[:2] == [("a.pdf", 3), ("b.pdf", 2)]
    assert top[2] == (None, 1)  # a deleted document is still counted, with no name
    assert out.cited_documents == 3 and out.total_documents == 3
    assert out.never_cited == 1  # never.pdf (cited only outside the window); the ghost is not a document


async def test_top_documents_are_capped_at_ten(clean: AsyncSession) -> None:
    ids = [await _doc(clean, f"d{i}.pdf") for i in range(12)]
    await _add(clean, meta={"cited_document_ids": [str(i) for i in ids]})
    out = await usage_insights.insights(clean, days=7)
    assert len(out.top_documents) == 10 and out.cited_documents == 12 and out.never_cited == 0


async def test_chunks_reused_vs_embedded_per_day(clean: AsyncSession) -> None:
    await _add(clean, kind="ingest", meta={"mode": "incremental", "kept": 40, "embedded": 2})
    await _add(clean, kind="ingest", meta={"mode": "incremental", "kept": 10, "embedded": 0})
    await _add(clean, kind="ingest", meta={"mode": "full", "kept": 0, "embedded": 25})
    await _add(clean, kind="ingest", status="error", meta={"kept": 99, "embedded": 99})  # failed: ignored
    await _add(clean, kind="ingest", meta={"kept": "x"})  # malformed numbers count as 0
    await _add(clean, age_days=1, kind="ingest", meta={"mode": "full", "kept": 0, "embedded": 7})
    out = await usage_insights.insights(clean, days=3)
    today, yesterday = out.reuse_days[-1], out.reuse_days[-2]
    assert (today.ingests, today.reused, today.embedded) == (4, 50, 27)
    assert (yesterday.ingests, yesterday.reused, yesterday.embedded) == (1, 0, 7)
    assert (out.reuse_days[0].ingests, out.reuse_days[0].reused) == (0, 0)


# ── insights: ingest ────────────────────────────────────────────────────────


async def test_ingest_duration_by_mode_and_failures_by_reason(clean: AsyncSession) -> None:
    for ms in (1000, 3000):
        await _add(clean, kind="ingest", duration_ms=ms, meta={"mode": "full"})
    await _add(clean, kind="ingest", duration_ms=200, meta={"mode": "incremental"})
    await _add(clean, kind="ingest", status="error", duration_ms=9, meta={"mode": "full"}, error="no text could be extracted from the document")
    await _add(clean, kind="ingest", status="error", error="no text could be extracted from the document")
    await _add(clean, kind="ingest", status="error", error=f"source file not found: /data/{uuid.uuid4()}.pdf")
    await _add(clean, status="error", error="a chat error")  # chat errors are not ingest failures
    out = await usage_insights.insights(clean, days=7)
    modes = {m.mode: m for m in out.ingest_modes}
    assert set(modes) == {"full", "incremental"}
    assert modes["full"].runs == 2 and modes["full"].avg_ms == pytest.approx(2000)
    assert 1000 <= modes["full"].p95_ms <= 3000
    assert modes["incremental"].runs == 1 and modes["incremental"].avg_ms == 200
    assert [(f.reason, f.count) for f in out.ingest_failures] == [
        ("no text could be extracted from the document", 2),
        ("source file not found: <path>", 1),
    ]


# ── HTTP: /usage/insights and /usage/credits ────────────────────────────────

OPENROUTER_KEY_BODY = {
    "data": {"label": "sk-or-v1-abc...xyz", "limit": 2.5, "limit_remaining": 2.4996, "usage": 0.0004, "is_free_tier": False}
}
SECRET = "sk-or-v1-supersecretvalue"


def _cfg(**kw: Any) -> Settings:
    return Settings(auth_enabled=False, openrouter_api_key=SECRET, **kw)


@pytest.fixture
async def api(clean: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    credits.reset_cache()

    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield clean

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: _cfg()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        credits.reset_cache()


async def test_insights_endpoint_serialises_every_section(api: httpx.AsyncClient, clean: AsyncSession) -> None:
    doc = await _doc(clean, "x.pdf")
    await _add(clean, cost_usd=0.01, model="m", hits=2, duration_ms=250, meta={"cited_document_ids": [str(doc)]})
    body = (await api.get("/usage/insights?days=7")).json()
    assert body["days"] == 7 and body["cost_usd"] == pytest.approx(0.01)
    assert body["by_model"][0]["model"] == "m"
    assert body["top_documents"] == [{"document_id": str(doc), "filename": "x.pdf", "citations": 1}]
    assert body["hits_histogram"] == [{"hits": 2, "count": 1}]
    assert body["latency_histogram"][-1]["count"] == 1 and len(body["rag_days"]) == 7
    scoped = (await api.get("/usage/insights?days=7&kind=ingest")).json()
    assert scoped["kind"] == "ingest" and scoped["by_model"] == [] and scoped["cost_usd"] is None
    assert (await api.get("/usage/insights?days=0")).status_code == 422
    assert (await api.get("/usage/insights?days=91")).status_code == 422
    assert (await api.get("/usage/insights?kind=bogus")).status_code == 422


@respx.mock
async def test_credits_endpoint_returns_the_key_balance_and_never_the_key(api: httpx.AsyncClient) -> None:
    route = respx.get("https://openrouter.ai/api/v1/key").mock(
        return_value=httpx.Response(200, json=OPENROUTER_KEY_BODY)
    )
    r = await api.get("/usage/credits")
    body = r.json()
    assert body["available"] is True
    assert (body["limit"], body["limit_remaining"], body["usage"]) == (2.5, 2.4996, 0.0004)
    assert body["is_free_tier"] is False
    assert SECRET not in r.text and "sk-or" not in r.text  # nothing key-like, not even the label
    assert route.calls.last.request.headers["authorization"] == f"Bearer {SECRET}"


@respx.mock
async def test_credits_are_cached_for_sixty_seconds(api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get("https://openrouter.ai/api/v1/key").mock(
        return_value=httpx.Response(200, json=OPENROUTER_KEY_BODY)
    )
    await api.get("/usage/credits")
    await api.get("/usage/credits")
    assert route.call_count == 1
    real = credits.time.monotonic
    monkeypatch.setattr(credits.time, "monotonic", lambda: real() + 61)
    await api.get("/usage/credits")
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize(
    "mock",
    [
        httpx.Response(401, json={"error": {"message": "bad key"}}),
        httpx.Response(500),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_credits_degrade_gracefully_when_the_key_endpoint_fails(api: httpx.AsyncClient, mock: httpx.Response) -> None:
    respx.get("https://openrouter.ai/api/v1/key").mock(return_value=mock)
    r = await api.get("/usage/credits")
    assert r.status_code == 200
    assert r.json()["available"] is False and r.json()["reason"] == "unavailable"
    assert SECRET not in r.text


@respx.mock
async def test_credits_degrade_when_the_network_is_down(api: httpx.AsyncClient) -> None:
    respx.get("https://openrouter.ai/api/v1/key").mock(side_effect=httpx.ConnectError("down"))
    assert (await api.get("/usage/credits")).json()["reason"] == "unavailable"


@respx.mock
async def test_credits_unlimited_key_has_null_limit(api: httpx.AsyncClient) -> None:
    body = {"data": {"limit": None, "limit_remaining": None, "usage": 1.5, "is_free_tier": True}}
    respx.get("https://openrouter.ai/api/v1/key").mock(return_value=httpx.Response(200, json=body))
    r = (await api.get("/usage/credits")).json()
    assert r["available"] is True and r["limit"] is None and r["usage"] == 1.5


async def test_credits_unavailable_for_other_providers_and_missing_keys(api: httpx.AsyncClient) -> None:
    app.dependency_overrides[deps.get_settings] = lambda: Settings(
        auth_enabled=False, llm_provider="openai", openai_api_key="k", openrouter_api_key=SECRET
    )
    assert (await api.get("/usage/credits")).json() == {
        "available": False, "reason": "provider_not_openrouter", "limit": None,
        "limit_remaining": None, "usage": None, "is_free_tier": None, "fetched_at": None,
    }
    app.dependency_overrides[deps.get_settings] = lambda: Settings(auth_enabled=False, openrouter_api_key="")
    assert (await api.get("/usage/credits")).json()["reason"] == "no_api_key"
