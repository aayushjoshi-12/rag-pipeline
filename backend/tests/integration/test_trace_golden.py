"""Golden characterisation of the trace row + response plumbing of /chat, /chat/stream and /search (ticket 56).

These pin what the three handlers record and return TODAY (trace kind/status/error/hits/tokens/cost/user/model, the
FULL meta dict, X-Cache headers, the exact SSE event sequence and payload keys) so the request-plumbing refactor can
be proven behaviour-preserving. Real Postgres (+ real Redis db 13 for the cache cases); fake store/embedder/LLM.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.db.models import Trace
from app.main import app
from app.routers import auth_deps
from app.services.auth.permissions import Permission
from app.services.llm import LLMRequestError, LLMUnavailableError, Usage
from app.services.qa import NO_CONTEXT_ANSWER
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeLLM, HitStore, StreamLLM, api_client, hit, make_doc, make_settings
from tests.integration.test_chat_stream_api import (
    REDIS_URL,
    _post_and_disconnect_after,
    _settings,
    cctx,  # noqa: F401  (imported fixture)
    needs_redis,
    parse_sse,
    sctx,  # noqa: F401  (imported fixture)
)
from tests.integration.test_retrieval_modes import ID_QUERY, PARAPHRASE, corpus, kind  # noqa: F401

USER = "golden@example.test"
FAILED_USAGE = Usage(prompt_tokens=2500, completion_tokens=2048, cost_usd=0.00011)


def _as_user() -> None:
    app.dependency_overrides[auth_deps.get_principal] = lambda: auth_deps.Principal(
        uuid.uuid4(), USER, frozenset(p.value for p in Permission)
    )


async def _row(session: AsyncSession, kind_: str) -> Trace:
    row = (
        (
            await session.execute(
                select(Trace).where(Trace.kind == kind_).order_by(Trace.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    await session.refresh(row)
    return row


def snap(row: Trace) -> dict[str, Any]:
    """The whole trace row except ids/timestamps; durations only as 'present'."""
    meta = dict(row.meta)
    if isinstance(meta.get("ttft_ms"), int):
        meta["ttft_ms"] = "<int>"
    return {
        "kind": row.kind,
        "status": row.status,
        "error": row.error,
        "hits": row.hits,
        "prompt": row.prompt_tokens,
        "completion": row.completion_tokens,
        "cost": row.cost_usd,
        "user": row.user_email,
        "model": row.model,
        "document_id": row.document_id,
        "duration_present": isinstance(row.duration_ms, int) and row.duration_ms >= 0,
        "question": row.question,
        "meta": meta,
    }


def cache_headers(r: Any) -> dict[str, str]:
    return {k: v for k, v in r.headers.items() if k in ("x-cache", "x-cache-layers")}


def sse_shape(raw: str) -> list[tuple[str, list[str]]]:
    return [(e, sorted(d)) for e, d in parse_sse(raw)]


@pytest.fixture
async def g(sctx: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    _as_user()
    sctx["llm"] = FakeLLM()
    doc = await make_doc(sctx["session"], "facts.pdf")
    sctx["doc"] = doc
    sctx["store"] = HitStore([hit(doc, 1, "Life is 42.")])
    return sctx


HIT_META = {
    "embedding": {"tokens": 0, "cached": True},
    "cache": {"result": "hit", "retrieval": "hit", "answer": "hit", "embeddings_avoided": 1, "llm_calls_avoided": 1, "saved_usd": 0.0123},
}  # fmt: skip
BASE_META = {"top_k": 4, "neighbors": 1, "document_filter": 0, "mode": "semantic"}


def chat_row(**kw: Any) -> dict[str, Any]:
    meta = {**BASE_META, "cited_document_ids": [], **kw.pop("meta", {})}
    return {
        "kind": "chat", "status": "ok", "error": None, "hits": 1, "prompt": 120, "completion": 9, "cost": 0.0123,
        "user": USER, "model": "fake", "document_id": None, "duration_present": True, "question": None,
        **kw, "meta": meta,
    }  # fmt: skip


# ── /chat ────────────────────────────────────────────────────────────────────────────────────
async def test_chat_ok_cache_off(g: dict[str, Any]) -> None:
    r = await g["c"].post("/chat", json={"question": "What is life?"})
    assert r.status_code == 200 and cache_headers(r) == {
        "x-cache": "off",
        "x-cache-layers": "answer=off",
    }
    assert sorted(r.json()) == ["answer", "cache", "sources"] and r.json()["cache"] == "off"
    assert snap(await _row(g["session"], "chat")) == chat_row(
        meta={"cited_document_ids": [str(g["doc"])]}
    )


async def test_chat_ok_records_content_and_explicit_options(g: dict[str, Any]) -> None:
    g["settings"] = make_settings(default_top_k=4, trace_capture_content=True)
    body = {
        "question": "What is life?",
        "top_k": 2,
        "neighbors": 0,
        "document_ids": [str(g["doc"])],
    }
    assert (await g["c"].post("/chat", json=body)).status_code == 200
    assert snap(await _row(g["session"], "chat")) == chat_row(
        question="What is life?",
        meta={
            "top_k": 2,
            "neighbors": 0,
            "document_filter": 1,
            "cited_document_ids": [str(g["doc"])],
        },
    )


async def test_chat_anonymous_has_no_user(g: dict[str, Any]) -> None:
    app.dependency_overrides.pop(auth_deps.get_principal)
    assert (await g["c"].post("/chat", json={"question": "q"})).status_code == 200
    assert snap(await _row(g["session"], "chat")) == chat_row(
        user=None, meta={"cited_document_ids": [str(g["doc"])]}
    )


async def test_chat_no_context_has_unknown_usage(g: dict[str, Any]) -> None:
    g["store"] = HitStore([])
    r = await g["c"].post("/chat", json={"question": "q"})
    assert (
        r.status_code == 200
        and r.json()["answer"] == NO_CONTEXT_ANSWER
        and r.json()["sources"] == []
    )
    assert snap(await _row(g["session"], "chat")) == chat_row(
        hits=0, prompt=None, completion=None, cost=None
    )


async def test_chat_empty_answer_keeps_usage(g: dict[str, Any]) -> None:
    g["llm"] = FakeLLM(answer="   ", usage=(77, 0), cost=0.002)
    r = await g["c"].post("/chat", json={"question": "q"})
    assert r.status_code == 200 and r.json()["answer"] == ""
    assert snap(await _row(g["session"], "chat")) == chat_row(
        prompt=77, completion=0, cost=0.002, meta={"cited_document_ids": [str(g["doc"])]}
    )


@pytest.mark.parametrize(
    ("exc", "code"), [(LLMUnavailableError("slow"), 503), (LLMRequestError("bad key"), 502)]
)
async def test_chat_llm_error_mapping_and_trace(
    g: dict[str, Any], exc: Exception, code: int
) -> None:
    g["llm"] = FakeLLM(exc=exc)
    r = await g["c"].post("/chat", json={"question": "q"})
    assert (
        r.status_code == code
        and r.json()["detail"] == str(exc)
        and sorted(r.json()) == ["code", "detail", "request_id"]
    )
    assert "x-cache" not in r.headers
    assert snap(await _row(g["session"], "chat")) == chat_row(
        status="error", error=str(exc), prompt=None, completion=None, cost=None
    )


async def test_chat_llm_error_keeps_billed_usage(g: dict[str, Any]) -> None:
    g["llm"] = FakeLLM(exc=LLMUnavailableError("ran out", usage=FAILED_USAGE))
    assert (await g["c"].post("/chat", json={"question": "q"})).status_code == 503
    assert snap(await _row(g["session"], "chat")) == chat_row(
        status="error", error="ran out", prompt=2500, completion=2048, cost=0.00011
    )


async def test_chat_llm_missing_is_503(g: dict[str, Any]) -> None:
    g["llm"] = None
    r = await g["c"].post("/chat", json={"question": "q"})
    assert r.status_code == 503
    assert snap(await _row(g["session"], "chat")) == chat_row(
        status="error",
        error=r.json()["detail"],
        model=None,
        prompt=None,
        completion=None,
        cost=None,
    )


async def test_chat_retrieval_failure(g: dict[str, Any]) -> None:
    class Boom(HitStore):
        def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise ConnectionError("chroma down")

    g["store"] = Boom([])
    r = await g["c"].post("/chat", json={"question": "q"})
    assert r.status_code == 503 and r.json()["detail"] == "Retrieval backend unavailable."
    assert sorted(r.json()) == ["code", "detail", "request_id"]
    assert snap(await _row(g["session"], "chat")) == chat_row(
        status="error",
        error="Retrieval backend unavailable.",
        hits=None,
        prompt=None,
        completion=None,
        cost=None,
    )


async def test_chat_unexpected_error_is_traced_with_type_and_reraised(g: dict[str, Any]) -> None:
    g["llm"] = FakeLLM(exc=ValueError("kaboom"))
    with pytest.raises(ValueError, match="kaboom"):
        await g["c"].post("/chat", json={"question": "q"})
    assert snap(await _row(g["session"], "chat")) == chat_row(
        status="error", error="ValueError: kaboom", prompt=None, completion=None, cost=None
    )


@needs_redis
async def test_chat_cache_miss_hit_bypass(cctx: dict[str, Any], g: dict[str, Any]) -> None:  # noqa: F811
    c, s = g["c"], g["session"]
    doc = str(g["doc"])
    miss_layers = {"embedding": "miss", "retrieval": "miss", "answer": "miss"}
    r = await c.post("/chat", json={"question": "What is life?"})
    assert cache_headers(r) == {
        "x-cache": "miss",
        "x-cache-layers": "embedding=miss,retrieval=miss,answer=miss",
    }
    assert r.json()["cache"] == "miss"
    assert snap(await _row(s, "chat")) == chat_row(
        meta={"cited_document_ids": [doc], "cache": {"result": "miss", **miss_layers}}
    )

    r = await c.post("/chat", json={"question": "What is life?"})
    assert r.headers["x-cache"] == "hit" and r.json()["cache"] == "hit"
    assert r.headers["x-cache-layers"] == "retrieval=hit,answer=hit"
    assert snap(await _row(s, "chat")) == chat_row(
        prompt=0, completion=0, cost=0.0, meta=HIT_META | {"cited_document_ids": [doc]}
    )

    r = await c.post("/chat", json={"question": "What is life?", "cache": False})
    assert cache_headers(r) == {
        "x-cache": "bypass",
        "x-cache-layers": "retrieval=bypass,embedding=bypass,answer=bypass",
    }
    assert snap(await _row(s, "chat")) == chat_row(
        meta={"cited_document_ids": [doc], "cache": {"result": "bypass", "embedding": "bypass", "retrieval": "bypass", "answer": "bypass"}}
    )  # fmt: skip

    r = await c.post(
        "/chat", json={"question": "What is life?"}, headers={"Cache-Control": "no-cache"}
    )
    assert r.headers["x-cache"] == "bypass"


# ── /chat/stream ─────────────────────────────────────────────────────────────────────────────
def stream_row(**kw: Any) -> dict[str, Any]:
    meta = {"streamed": True, "ttft_ms": "<int>", **kw.pop("meta", {})}
    return chat_row(model="fake-stream", meta=meta, **kw)


async def test_stream_ok_sequence_headers_and_trace(g: dict[str, Any]) -> None:
    g["llm"] = StreamLLM()
    r = await g["c"].post("/chat/stream", json={"question": "What is life?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert cache_headers(r) == {"x-cache": "off", "x-cache-layers": "answer=off"}
    assert sse_shape(r.text) == [
        ("sources", ["cache", "sources"]),
        ("token", ["text"]),
        ("token", ["text"]),
        ("token", ["text"]),
        ("done", ["answer", "cache", "cost_usd", "trace_id", "usage"]),
    ]
    ev = parse_sse(r.text)
    assert [d["text"] for e, d in ev if e == "token"] == ["The answer ", "is 42 ", "[1]."]
    done = ev[-1][1]
    assert (
        done["usage"] == {"prompt_tokens": 120, "completion_tokens": 9}
        and done["cost_usd"] == 0.0123
    )
    assert done["cache"] == "off" and ev[0][1]["cache"] == "off"
    assert sorted(ev[0][1]["sources"][0]) == sorted(
        ["text", "document_id", "chunk_index", "filename", "score", "score_kind", "semantic_rank",
         "keyword_rank", "is_hit", "rank", "context_for"]
    )  # fmt: skip
    row = await _row(g["session"], "chat")
    assert str(row.id) == done["trace_id"]
    assert snap(row) == stream_row(meta={"cited_document_ids": [str(g["doc"])]})


async def test_stream_no_context_sequence(g: dict[str, Any]) -> None:
    g["store"] = HitStore([])
    r = await g["c"].post("/chat/stream", json={"question": "q"})
    assert parse_sse(r.text)[0][0] == "sources" and parse_sse(r.text)[-1][0] == "done"
    done = parse_sse(r.text)[-1][1]
    assert (
        done["usage"] == {"prompt_tokens": None, "completion_tokens": None}
        and done["cost_usd"] is None
    )
    assert snap(await _row(g["session"], "chat")) == chat_row(
        hits=0, prompt=None, completion=None, cost=None, meta={"streamed": True, "ttft_ms": "<int>"}
    )  # the default FakeLLM is the model on the row; it was never called


@pytest.mark.parametrize(
    ("exc", "code"), [(LLMUnavailableError("slow"), 503), (LLMRequestError("bad key"), 502)]
)
async def test_stream_midstream_llm_error(g: dict[str, Any], exc: Exception, code: int) -> None:
    g["llm"] = StreamLLM(exc=exc, fail_after=1)
    r = await g["c"].post("/chat/stream", json={"question": "q"})
    assert r.status_code == 200
    assert sse_shape(r.text) == [
        ("sources", ["cache", "sources"]), ("token", ["text"]), ("error", ["detail", "status", "trace_id"]),
    ]  # fmt: skip
    err = parse_sse(r.text)[-1][1]
    assert err["status"] == code and err["detail"] == str(exc)
    row = await _row(g["session"], "chat")
    assert str(row.id) == err["trace_id"]
    assert snap(row) == stream_row(
        status="error",
        error=f"{type(exc).__name__}: {exc}",
        prompt=None,
        completion=None,
        cost=None,
    )


async def test_stream_midstream_unexpected_error_is_500_without_leak(g: dict[str, Any]) -> None:
    g["llm"] = StreamLLM(exc=ValueError("secret"), fail_after=1)
    r = await g["c"].post("/chat/stream", json={"question": "q"})
    err = parse_sse(r.text)[-1]
    assert err[0] == "error" and err[1]["status"] == 500
    assert err[1]["detail"] == "internal error while streaming the answer"
    assert snap(await _row(g["session"], "chat")) == stream_row(
        status="error", error="ValueError: secret", prompt=None, completion=None, cost=None
    )


async def test_stream_midstream_error_keeps_billed_usage(g: dict[str, Any]) -> None:
    g["llm"] = StreamLLM(exc=LLMUnavailableError("ran out", usage=FAILED_USAGE), fail_after=1)
    await g["c"].post("/chat/stream", json={"question": "q"})
    assert snap(await _row(g["session"], "chat")) == stream_row(
        status="error",
        error="LLMUnavailableError: ran out",
        prompt=2500,
        completion=2048,
        cost=0.00011,
    )


@pytest.mark.parametrize(
    ("exc", "code"),
    [(None, 503), (LLMUnavailableError("slow"), 503), (LLMRequestError("bad key"), 502)],
)
async def test_stream_preflight_llm_error_is_http_error(
    g: dict[str, Any], monkeypatch: pytest.MonkeyPatch, exc: Exception | None, code: int
) -> None:
    if exc is None:
        g["llm"] = None  # no LLM configured: the pre-flight check raises LLMConfigError
    else:

        async def boom(*a: Any, **k: Any) -> Any:
            raise exc

        monkeypatch.setattr("app.routers.chat.prepare_stream", boom)
    r = await g["c"].post("/chat/stream", json={"question": "q"})
    assert r.status_code == code and "text/event-stream" not in r.headers.get("content-type", "")
    assert sorted(r.json()) == ["code", "detail", "request_id"]
    if exc is not None:
        assert r.json()["detail"] == str(exc)
    assert snap(await _row(g["session"], "chat")) == chat_row(
        status="error", error=r.json()["detail"], model=None if exc is None else "fake",
        prompt=None, completion=None, cost=None, meta={"streamed": True, "ttft_ms": None},
    )  # fmt: skip


async def test_stream_retrieval_failure_is_http_503_with_trace(g: dict[str, Any]) -> None:
    class Boom(HitStore):
        def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise ConnectionError("chroma down")

    g["store"] = Boom([])
    r = await g["c"].post("/chat/stream", json={"question": "q"})
    assert r.status_code == 503 and r.json()["detail"] == "Retrieval backend unavailable."
    assert sorted(r.json()) == ["code", "detail", "request_id"]
    assert snap(await _row(g["session"], "chat")) == chat_row(
        status="error", error="Retrieval backend unavailable.", hits=None, prompt=None, completion=None, cost=None,
        meta={"streamed": True, "ttft_ms": None},
    )  # fmt: skip


async def test_stream_client_disconnect_is_cancelled_trace(g: dict[str, Any]) -> None:
    g["llm"] = StreamLLM(deltas=tuple(f"w{i} " for i in range(400)), delay=0.01)
    frames, code = await _post_and_disconnect_after(
        "/chat/stream", {"question": "q"}, after_event="token", extra_events=2
    )
    assert code == 200 and b"event: done" not in b"".join(frames)
    assert snap(await _row(g["session"], "chat")) == stream_row(
        status="error", error="client_disconnected", prompt=None, completion=None, cost=None,
        meta={"cancelled": True},
    )  # fmt: skip


@needs_redis
async def test_stream_cache_miss_hit_bypass(cctx: dict[str, Any], g: dict[str, Any]) -> None:  # noqa: F811
    c, s, doc = g["c"], g["session"], str(g["doc"])
    g["llm"] = StreamLLM()
    r = await c.post("/chat/stream", json={"question": "What is life?"})
    assert cache_headers(r) == {
        "x-cache": "miss",
        "x-cache-layers": "embedding=miss,retrieval=miss,answer=miss",
    }
    assert (
        parse_sse(r.text)[0][1]["cache"] == "miss" and parse_sse(r.text)[-1][1]["cache"] == "miss"
    )
    assert snap(await _row(s, "chat")) == stream_row(
        meta={"cited_document_ids": [doc], "cache": {"result": "miss", "embedding": "miss", "retrieval": "miss", "answer": "miss"}}
    )  # fmt: skip

    r = await c.post("/chat/stream", json={"question": "What is life?"})
    assert r.headers["x-cache"] == "hit"
    assert parse_sse(r.text)[0][0] == "sources" and parse_sse(r.text)[-1][0] == "done"
    done = parse_sse(r.text)[-1][1]
    assert done["usage"] == {"prompt_tokens": 0, "completion_tokens": 0} and done["cost_usd"] == 0.0
    assert r.headers["x-cache-layers"] == "retrieval=hit,answer=hit"
    assert snap(await _row(s, "chat")) == stream_row(
        prompt=0, completion=0, cost=0.0, meta=HIT_META | {"cited_document_ids": [doc]}
    )

    r = await c.post("/chat/stream", json={"question": "What is life?", "cache": False})
    assert cache_headers(r) == {
        "x-cache": "bypass",
        "x-cache-layers": "retrieval=bypass,embedding=bypass,answer=bypass",
    }
    assert parse_sse(r.text)[-1][1]["cache"] == "bypass"


# ── /search ──────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
async def sg(corpus: dict[str, Any], db_session: AsyncSession) -> AsyncIterator[dict[str, Any]]:  # noqa: F811
    _as_user()
    state: dict[str, Any] = {
        "session": db_session,
        "corpus": corpus,
        "settings": _settings(retrieval_neighbors=0),
    }
    async with api_client(
        db_session,
        settings=lambda: state["settings"],
        embedder_dep=lambda: corpus["emb"],
        store=lambda: state["store"] if "store" in state else corpus["store"],
    ) as c:
        state["c"] = c
        yield state


SEARCH_KEYS = [
    "cache",
    "fusion",
    "mode",
    "neighbors",
    "query",
    "results",
    "returned",
    "took_ms",
    "top_k",
]


def search_row(**kw: Any) -> dict[str, Any]:
    meta = {
        "top_k": 4,
        "neighbors": 0,
        "document_filter": 0,
        "include_text": True,
        **kw.pop("meta", {}),
    }
    return {
        "kind": "search", "status": "ok", "error": None, "hits": None, "prompt": None, "completion": None,
        "cost": None, "user": USER, "model": None, "document_id": None, "duration_present": True,
        "question": None, **kw, "meta": meta,
    }  # fmt: skip


async def test_search_semantic_ok(sg: dict[str, Any]) -> None:
    r = await sg["c"].post("/search", json={"query": PARAPHRASE, "mode": "semantic"})
    assert r.status_code == 200 and cache_headers(r) == {"x-cache": "off"}
    body = r.json()
    assert sorted(body) == SEARCH_KEYS and body["cache"] == "off" and body["fusion"] is None
    row = snap(await _row(sg["session"], "search"))
    assert row == search_row(hits=body["returned"], meta={"mode": "semantic"})
    assert body["returned"] > 0


async def test_search_keyword_ok(sg: dict[str, Any]) -> None:
    r = await sg["c"].post(
        "/search", json={"query": ID_QUERY, "mode": "keyword", "include_text": False}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body) == SEARCH_KEYS and body["returned"] > 0
    assert snap(await _row(sg["session"], "search")) == search_row(
        hits=body["returned"],
        meta={"mode": "keyword", "keyword_method": "bm25", "candidates": 50, "include_text": False},
    )


async def test_search_hybrid_with_metered_embedder_and_content(sg: dict[str, Any]) -> None:
    from tests.fakes import MeteredEmbedder

    class Metered(MeteredEmbedder):
        def embed_query(self, text: str) -> list[float]:
            from app.services.embeddings import _report

            _report(10)
            return sg["corpus"]["emb"].embed_query(text)  # same vectors, but report 10 tokens

    app.dependency_overrides[__import__("app.routers.deps", fromlist=["x"]).get_embedder_dep] = (
        lambda: Metered()
    )
    sg["settings"] = make_settings(
        default_top_k=4, retrieval_neighbors=0, trace_capture_content=True
    )
    body = {
        "query": ID_QUERY,
        "mode": "hybrid",
        "top_k": 3,
        "neighbors": 1,
        "document_ids": [str(sg["corpus"]["ids"]["policy.pdf"])],
    }
    r = await sg["c"].post("/search", json=body)
    assert r.status_code == 200, r.text
    row = snap(await _row(sg["session"], "search"))
    assert row["prompt"] == 10 and row["completion"] == 0 and row["cost"] == 0.0
    assert row["question"] == ID_QUERY and row["meta"]["embedding"] == {"tokens": 10}
    assert row["meta"] == {
        "top_k": 3, "neighbors": 1, "document_filter": 1, "include_text": True, "embedding": {"tokens": 10},
        "mode": "hybrid", "keyword_method": "bm25", "fusion": "rrf", "candidates": 50,
    }  # fmt: skip


async def test_search_error(sg: dict[str, Any]) -> None:
    class Boom:
        def similarity_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise ConnectionError("store down")

    sg["store"] = Boom()
    r = await sg["c"].post("/search", json={"query": "q", "mode": "semantic"})
    assert r.status_code == 503 and r.json()["detail"] == "Retrieval backend unavailable."
    assert sorted(r.json()) == ["code", "detail", "request_id"]
    assert "x-cache" not in r.headers
    assert snap(await _row(sg["session"], "search")) == search_row(
        status="error", error="Retrieval backend unavailable.", meta={"mode": "semantic"}
    )


@needs_redis
async def test_search_cache_miss_hit_bypass(
    sg: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis as redis_lib

    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("CACHE_REDIS_URL", REDIS_URL)
    monkeypatch.setattr("app.services.cache._process_settings", None)
    redis_lib.Redis.from_url(REDIS_URL).flushdb()
    sg["settings"] = _settings(retrieval_neighbors=0, cache_enabled=True, cache_redis_url=REDIS_URL)
    try:
        body = {"query": PARAPHRASE, "mode": "semantic", "top_k": 3}
        c, s = sg["c"], sg["session"]
        r = await c.post("/search", json=body)
        assert cache_headers(r) == {
            "x-cache": "miss",
            "x-cache-layers": "embedding=miss,retrieval=miss",
        }
        n = r.json()["returned"]
        assert snap(await _row(s, "search")) == search_row(
            hits=n, meta={"top_k": 3, "mode": "semantic", "cache": {"result": "miss", "embedding": "miss", "retrieval": "miss"}}
        )  # fmt: skip
        r = await c.post("/search", json=body)
        assert r.headers["x-cache"] == "hit" and r.json()["cache"] == "hit"
        assert r.headers["x-cache-layers"] == "retrieval=hit"
        row = snap(await _row(s, "search"))
        assert row == search_row(
            hits=n, prompt=0, completion=0, cost=0.0,
            meta={"top_k": 3, "mode": "semantic", "embedding": {"tokens": 0, "cached": True},
                  "cache": {"result": "hit", "retrieval": "hit", "embeddings_avoided": 1}},
        )  # fmt: skip
        r = await c.post("/search", json={**body, "cache": False})
        assert cache_headers(r) == {
            "x-cache": "bypass",
            "x-cache-layers": "retrieval=bypass,embedding=bypass",
        }
        assert snap(await _row(s, "search"))["meta"]["cache"] == {
            "result": "bypass",
            "embedding": "bypass",
            "retrieval": "bypass",
        }
    finally:
        redis_lib.Redis.from_url(REDIS_URL).flushdb()


# ── meta key ORDER (the JSON column keeps insertion order and GET /traces exposes it) ────────────
async def test_meta_key_order_chat_stream_search(
    g: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def order(kind_: str) -> list[str]:
        return list((await _row(g["session"], kind_)).meta)

    g["llm"] = MeteredStreamLLM()
    from app.main import app as _app
    from app.routers import deps as _deps
    from tests.fakes import MeteredEmbedder

    _app.dependency_overrides[_deps.get_embedder_dep] = lambda: MeteredEmbedder()
    await g["c"].post("/chat", json={"question": "q"})
    assert await order("chat") == [
        "top_k",
        "neighbors",
        "document_filter",
        "cited_document_ids",
        "embedding",
        "mode",
    ]
    await g["c"].post("/chat/stream", json={"question": "q"})
    assert await order("chat") == [
        "top_k", "neighbors", "document_filter", "cited_document_ids", "embedding", "streamed", "ttft_ms", "mode",
    ]  # fmt: skip
    await g["c"].post("/search", json={"query": "q", "mode": "semantic"})
    assert await order("search") == [
        "top_k", "neighbors", "document_filter", "include_text", "embedding", "mode",
    ]  # fmt: skip


class MeteredStreamLLM(StreamLLM):
    def complete(self, system: str, user: str) -> Any:
        from app.services.llm import Completion

        return Completion("The answer is 42 [1].", 120, 9, 0.0123)
