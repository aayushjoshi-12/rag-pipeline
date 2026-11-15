"""POST /chat/stream (ticket 38): real Postgres (+ real Redis db 13 for the cache cases), fake store/embedder/LLM.

No real LLM is ever called; the fake LLM streams scripted chunks and records whether its upstream was closed.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import redis as redis_lib
from app.config import Settings
from app.db.models import Trace
from app.main import app
from app.services.llm import Completion, LLMRequestError, LLMUnavailableError
from app.services.qa import NO_CONTEXT_ANSWER, answer_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeEmbedder, HitStore, StreamLLM, api_client, hit, make_doc, make_settings

REDIS_URL = "redis://localhost:6379/13"


def parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for frame in raw.split("\n\n"):
        if not frame.strip():
            continue
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [e for e, _ in events]


def _settings(**kw: Any) -> Settings:
    return make_settings(default_top_k=4, trace_capture_content=False, **kw)


@pytest.fixture
async def sctx(db_session: AsyncSession) -> AsyncIterator[dict[str, Any]]:
    state: dict[str, Any] = {
        "store": HitStore([]),
        "llm": StreamLLM(),
        "settings": _settings(),
        "session": db_session,
    }

    async with api_client(
        db_session,
        settings=lambda: state["settings"],
        embedder_dep=lambda: FakeEmbedder(),
        store=lambda: state["store"],
        llm_dep=lambda: state["llm"],
    ) as c:
        state["c"] = c
        yield state


async def _with_doc(s: dict[str, Any], text: str = "Life is 42.") -> uuid.UUID:
    doc = await make_doc(s["session"], "facts.pdf")
    s["store"] = HitStore([hit(doc, 1, text)])
    return doc


async def _trace(s: dict[str, Any], trace_id: str) -> Trace:
    row = (
        await s["session"].execute(select(Trace).where(Trace.id == uuid.UUID(trace_id)))
    ).scalar_one()
    return row


# ── event order and payloads ─────────────────────────────────────────────────────────────────
async def test_event_order_usage_cost_and_trace(sctx: dict[str, Any]) -> None:
    doc = await _with_doc(sctx)
    r = await sctx["c"].post("/chat/stream", json={"question": "What is life?"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    ev = parse_sse(r.text)
    assert names(ev) == ["sources", "token", "token", "token", "done"]  # sources FIRST, done LAST
    sources = ev[0][1]["sources"]
    assert sources[0]["filename"] == "facts.pdf" and sources[0]["document_id"] == str(doc)
    assert [d["text"] for e, d in ev if e == "token"] == ["The answer ", "is 42 ", "[1]."]
    done = ev[-1][1]
    assert done["answer"] == "The answer is 42 [1]."  # stripped full text
    assert done["usage"] == {"prompt_tokens": 120, "completion_tokens": 9}
    assert done["cost_usd"] == 0.0123
    assert done["cache"] == "off"
    assert "Life is 42." in sctx["llm"].prompts[0]
    row = await _trace(sctx, done["trace_id"])
    assert (
        row.kind,
        row.status,
        row.prompt_tokens,
        row.completion_tokens,
        row.cost_usd,
        row.hits,
    ) == (
        "chat",
        "ok",
        120,
        9,
        0.0123,
        1,
    )
    assert row.meta["streamed"] is True and isinstance(row.meta["ttft_ms"], int)
    assert row.model == "fake-stream" and row.error is None


async def test_sources_event_matches_non_streaming_response(sctx: dict[str, Any]) -> None:
    await _with_doc(sctx)
    sctx["llm"] = _CompleteToo()
    streamed = parse_sse((await sctx["c"].post("/chat/stream", json={"question": "q"})).text)
    plain = (await sctx["c"].post("/chat", json={"question": "q"})).json()
    assert streamed[0][1]["sources"] == plain["sources"]


class _CompleteToo(StreamLLM):
    def complete(self, system: str, user: str) -> Completion:
        return Completion("x", 1, 1)


async def test_no_hits_skips_llm_and_streams_the_fallback_answer(sctx: dict[str, Any]) -> None:
    r = await sctx["c"].post("/chat/stream", json={"question": "anything"})
    ev = parse_sse(r.text)
    assert names(ev)[0] == "sources" and ev[0][1]["sources"] == []
    assert names(ev)[-1] == "done" and ev[-1][1]["answer"] == NO_CONTEXT_ANSWER
    assert "".join(d["text"] for e, d in ev if e == "token") == NO_CONTEXT_ANSWER
    assert sctx["llm"].calls == 0


async def test_unknown_cost_is_null_not_zero(sctx: dict[str, Any]) -> None:
    await _with_doc(sctx)
    sctx["llm"] = StreamLLM(cost=None)
    done = parse_sse((await sctx["c"].post("/chat/stream", json={"question": "q"})).text)[-1][1]
    assert done["cost_usd"] is None


# ── failures before the stream: plain HTTP errors ────────────────────────────────────────────
async def test_missing_llm_is_a_plain_503_before_the_stream(sctx: dict[str, Any]) -> None:
    await _with_doc(sctx)
    sctx["llm"] = None
    r = await sctx["c"].post("/chat/stream", json={"question": "q"})
    assert r.status_code == 503 and r.headers["content-type"].startswith("application/json")
    assert "No LLM is configured" in r.json()["detail"]


async def test_validation_error_is_a_plain_422(sctx: dict[str, Any]) -> None:
    r = await sctx["c"].post("/chat/stream", json={"question": ""})
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/json")


async def test_retrieval_failure_is_a_plain_http_error_and_traced(sctx: dict[str, Any]) -> None:
    class Broken(HitStore):
        def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("index exploded")

    sctx["store"] = Broken([])
    before = len((await sctx["session"].execute(select(Trace))).scalars().all())
    r = await sctx["c"].post("/chat/stream", json={"question": "q"})
    assert r.status_code >= 400 and r.headers["content-type"].startswith("application/json")
    rows = (await sctx["session"].execute(select(Trace).order_by(Trace.created_at))).scalars().all()
    assert len(rows) == before + 1
    assert rows[-1].status == "error" and rows[-1].error == "Retrieval backend unavailable."


# ── failures during the stream: an error event, never a half-open stream ──────────────────────
@pytest.mark.parametrize(
    ("exc", "code"), [(LLMUnavailableError("slow"), 503), (LLMRequestError("bad key"), 502)]
)
async def test_midstream_failure_is_an_error_event_after_the_delivered_tokens(
    sctx: dict[str, Any], exc: Exception, code: int
) -> None:
    await _with_doc(sctx)
    sctx["llm"] = StreamLLM(exc=exc, fail_after=2)
    r = await sctx["c"].post("/chat/stream", json={"question": "q"})
    assert r.status_code == 200  # headers were already sent: the failure is in-band
    ev = parse_sse(r.text)
    assert names(ev) == ["sources", "token", "token", "error"]  # no done after error
    err = ev[-1][1]
    assert err["status"] == code and str(exc) in err["detail"]
    row = await _trace(sctx, err["trace_id"])
    assert row.status == "error" and str(exc) in (row.error or "")
    assert sctx["llm"].closed.is_set()


async def test_failure_when_opening_the_upstream_stream_is_an_error_event(
    sctx: dict[str, Any],
) -> None:
    await _with_doc(sctx)
    sctx["llm"] = StreamLLM(exc=LLMUnavailableError("provider down"))
    ev = parse_sse((await sctx["c"].post("/chat/stream", json={"question": "q"})).text)
    assert names(ev) == ["sources", "error"] and ev[-1][1]["status"] == 503


async def test_unexpected_error_does_not_leak_internals(sctx: dict[str, Any]) -> None:
    await _with_doc(sctx)
    sctx["llm"] = StreamLLM(exc=ValueError("secret-internal-detail"), fail_after=1)
    ev = parse_sse((await sctx["c"].post("/chat/stream", json={"question": "q"})).text)
    assert names(ev)[-1] == "error"
    assert ev[-1][1]["status"] == 500 and "secret-internal-detail" not in ev[-1][1]["detail"]


# ── client disconnect (driven through the real ASGI app) ─────────────────────────────────────
async def _post_and_disconnect_after(
    path: str, body: dict[str, Any], *, after_event: str, extra_events: int = 0
) -> tuple[list[bytes], int]:
    """POST over raw ASGI; send ``http.disconnect`` once ``after_event`` (+ ``extra_events`` more frames) arrived."""
    payload = json.dumps(body).encode()
    sent_request = False
    disconnect = asyncio.Event()
    frames: list[bytes] = []
    status_code = 0

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(msg: dict[str, Any]) -> None:
        nonlocal status_code
        if msg["type"] == "http.response.start":
            status_code = msg["status"]
        elif msg["type"] == "http.response.body" and msg.get("body"):
            frames.append(msg["body"])
            seen = b"".join(frames).decode().count("event: ")
            if f"event: {after_event}" in b"".join(frames).decode() and seen >= 1 + extra_events:
                disconnect.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"host", b"t")],
        "client": ("127.0.0.1", 1),
        "server": ("t", 80),
        "scheme": "http",
    }
    await asyncio.wait_for(app(scope, receive, send), 15)  # type: ignore[arg-type]
    return frames, status_code


async def test_client_disconnect_cancels_upstream_and_records_a_cancelled_trace(
    sctx: dict[str, Any],
) -> None:
    await _with_doc(sctx)
    llm = StreamLLM(deltas=tuple(f"w{i} " for i in range(400)), delay=0.01)
    sctx["llm"] = llm
    frames, code = await _post_and_disconnect_after(
        "/chat/stream", {"question": "q"}, after_event="token", extra_events=2
    )
    assert code == 200
    text = b"".join(frames).decode()
    assert "event: done" not in text and "event: error" not in text
    assert await asyncio.to_thread(llm.closed.wait, 5), "the upstream stream was not closed"
    assert llm.emitted < 400  # it did not keep generating for a client that left
    row = (
        await sctx["session"].execute(select(Trace).order_by(Trace.created_at.desc()).limit(1))
    ).scalar_one()
    assert row.status == "error" and row.error == "client_disconnected"
    assert row.meta["cancelled"] is True and row.meta["streamed"] is True


# ── answer cache (real Redis db 13) ──────────────────────────────────────────────────────────
def _redis_up() -> bool:
    try:
        return bool(redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping())
    except Exception:
        return False


needs_redis = pytest.mark.skipif(not _redis_up(), reason="no Redis on localhost:6379")


@pytest.fixture
async def cctx(
    sctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("CACHE_REDIS_URL", REDIS_URL)
    monkeypatch.setattr("app.services.cache._process_settings", None)
    r = redis_lib.Redis.from_url(REDIS_URL)
    r.flushdb()
    sctx["settings"] = _settings(cache_enabled=True, cache_redis_url=REDIS_URL)
    sctx["redis"] = r
    try:
        yield sctx
    finally:
        r.flushdb()


def _stored(r: Any, llm: StreamLLM) -> bool:
    """Is the answer for the prompt this LLM last received in the cache? (Keyed, not a db-wide count, so a
    concurrent run sharing Redis db 13 cannot confuse it.)"""
    return bool(r.exists(answer_key(llm, llm.prompts[-1])))


@needs_redis
async def test_miss_streams_live_then_hit_replays_without_calling_the_llm(
    cctx: dict[str, Any],
) -> None:
    await _with_doc(cctx)
    c, llm = cctx["c"], cctx["llm"]
    a = await c.post("/chat/stream", json={"question": "What is life?"})
    assert a.headers["x-cache"] == "miss"
    ev_a = parse_sse(a.text)
    assert _stored(cctx["redis"], llm)  # stored after the clean finish
    b = await c.post("/chat/stream", json={"question": "What is life?"})
    assert b.headers["x-cache"] == "hit" and "answer=hit" in b.headers["x-cache-layers"]
    ev_b = parse_sse(b.text)
    assert llm.calls == 1  # the hit did not touch the LLM
    assert names(ev_b)[0] == "sources" and names(ev_b)[-1] == "done"
    assert "".join(d["text"] for e, d in ev_b if e == "token") == ev_a[-1][1]["answer"]
    done = ev_b[-1][1]
    assert done["cache"] == "hit" and done["answer"] == ev_a[-1][1]["answer"]
    assert done["usage"] == {"prompt_tokens": 0, "completion_tokens": 0} and done["cost_usd"] == 0.0
    assert ev_a[0][1]["sources"] == ev_b[0][1]["sources"]
    # the cached answer is shared with non-streaming /chat in both directions
    plain = await c.post("/chat", json={"question": "What is life?"})
    assert plain.headers["x-cache"] == "hit" and plain.json()["answer"] == done["answer"]


@needs_redis
async def test_bypass_streams_live_and_writes_nothing(cctx: dict[str, Any]) -> None:
    await _with_doc(cctx)
    r = await cctx["c"].post("/chat/stream", json={"question": "q", "cache": False})
    assert r.headers["x-cache"] == "bypass" and cctx["llm"].calls == 1
    assert not _stored(cctx["redis"], cctx["llm"])
    r = await cctx["c"].post(
        "/chat/stream", json={"question": "q"}, headers={"Cache-Control": "no-cache"}
    )
    assert r.headers["x-cache"] == "bypass" and not _stored(cctx["redis"], cctx["llm"])


@needs_redis
async def test_midstream_failure_writes_nothing_to_the_cache(cctx: dict[str, Any]) -> None:
    await _with_doc(cctx)
    cctx["llm"] = StreamLLM(exc=LLMUnavailableError("boom"), fail_after=2)
    r = await cctx["c"].post("/chat/stream", json={"question": "q"})
    assert names(parse_sse(r.text))[-1] == "error"
    assert not _stored(cctx["redis"], cctx["llm"])
    # the next request is a genuine miss that calls the (now healthy) LLM
    cctx["llm"] = StreamLLM()
    r = await cctx["c"].post("/chat/stream", json={"question": "q"})
    assert r.headers["x-cache"] == "miss" and cctx["llm"].calls == 1
    assert _stored(cctx["redis"], cctx["llm"])


@needs_redis
async def test_client_disconnect_writes_nothing_to_the_cache(cctx: dict[str, Any]) -> None:
    await _with_doc(cctx)
    llm = StreamLLM(deltas=tuple(f"w{i} " for i in range(400)), delay=0.01)
    cctx["llm"] = llm
    await _post_and_disconnect_after(
        "/chat/stream", {"question": "q"}, after_event="token", extra_events=2
    )
    assert await asyncio.to_thread(llm.closed.wait, 5)
    assert not _stored(cctx["redis"], llm)
    # and a partial answer can never be served later
    cctx["llm"] = StreamLLM()
    r = await cctx["c"].post("/chat/stream", json={"question": "q"})
    assert r.headers["x-cache"] == "miss"


@needs_redis
async def test_cache_hit_trace_records_the_saving(cctx: dict[str, Any]) -> None:
    await _with_doc(cctx)
    await cctx["c"].post("/chat/stream", json={"question": "q"})
    b = parse_sse((await cctx["c"].post("/chat/stream", json={"question": "q"})).text)
    row = await _trace(cctx, b[-1][1]["trace_id"])
    assert row.meta["cache"]["result"] == "hit" and row.meta["cache"]["llm_calls_avoided"] == 1
    assert row.cost_usd == 0.0
