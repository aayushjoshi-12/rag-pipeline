"""Query cache core (ticket 37). Pure helpers run anywhere; the Redis-backed behaviour needs a real Redis
(db 13, namespace flushed per test) and is skipped when none is reachable."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import redis as redis_lib
from app.config import Settings
from app.routers.cache import trace_summary
from app.services import cache as cache_mod
from app.services.cache import (
    CacheCall,
    QueryCache,
    decode_vector,
    encode_vector,
    normalize_query,
    wants_bypass,
)
from app.services.retriever import RetrievalOptions
from app.services.search import (
    decode_retrieval,
    encode_retrieval,
    options_fingerprint,
    retrieval_key,
)

URL = "redis://localhost:6379/13"


def make(**kw: Any) -> QueryCache:
    return QueryCache(
        Settings(**{"cache_enabled": True, "cache_redis_url": URL, "cache_timeout_ms": 2000, **kw})
    )


def _redis_up() -> bool:
    try:
        return bool(redis_lib.Redis.from_url(URL, socket_connect_timeout=0.5).ping())
    except Exception:
        return False


needs_redis = pytest.mark.skipif(not _redis_up(), reason="no Redis on localhost:6379")


@pytest.fixture
def qc() -> Any:
    c = make()
    redis_lib.Redis.from_url(URL).flushdb()
    yield c
    redis_lib.Redis.from_url(URL).flushdb()


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────


def test_normalize_query_collapses_whitespace_nfc_keeps_case() -> None:
    assert normalize_query("  Hello \n\t World ") == "Hello World"
    assert normalize_query("é") == "é"  # NFC
    assert normalize_query("ABC") != normalize_query("abc")  # embedding models are case sensitive


def test_vector_codec_is_bit_identical() -> None:
    vec = [0.1, -2.5e-9, 3.14159265358979, 0.0]
    assert decode_vector(encode_vector(vec)) == vec


@pytest.mark.parametrize(
    ("body", "header", "want"),
    [
        (False, None, True),
        (True, None, False),
        (None, "no-cache", True),
        (None, "max-age=0, No-Store", True),
        (None, "max-age=60", False),
        (None, None, False),
    ],
)
def test_wants_bypass(body: bool | None, header: str | None, want: bool) -> None:
    assert wants_bypass(body, header) is want


def test_options_fingerprint_ignores_knobs_of_other_modes() -> None:
    a = options_fingerprint(RetrievalOptions(mode="semantic", rrf_k=60))
    b = options_fingerprint(RetrievalOptions(mode="semantic", rrf_k=10, bm25_k1=2.0))
    assert a == b
    h1 = options_fingerprint(RetrievalOptions(mode="hybrid", rrf_k=60))
    h2 = options_fingerprint(RetrievalOptions(mode="hybrid", rrf_k=10))
    assert h1 != h2


def test_retrieval_key_canonicalisation_and_sensitivity() -> None:
    c = make()
    o = RetrievalOptions(mode="semantic")
    d1, d2 = uuid.uuid4(), uuid.uuid4()
    base = retrieval_key(
        c, "v1", "what  is X?", top_k=5, neighbors=0, options=o, document_ids=[d1, d2]
    )
    assert base == retrieval_key(
        c, "v1", " what is X? ", top_k=5, neighbors=0, options=o, document_ids=[d2, d1]
    )
    for changed in (
        retrieval_key(
            c, "v2", "what is X?", top_k=5, neighbors=0, options=o, document_ids=[d1, d2]
        ),
        retrieval_key(
            c, "v1", "what is X?", top_k=6, neighbors=0, options=o, document_ids=[d1, d2]
        ),
        retrieval_key(
            c, "v1", "what is X?", top_k=5, neighbors=1, options=o, document_ids=[d1, d2]
        ),
        retrieval_key(c, "v1", "what is X?", top_k=5, neighbors=0, options=o, document_ids=[d1]),
        retrieval_key(
            c, "v1", "what is x?", top_k=5, neighbors=0, options=o, document_ids=[d1, d2]
        ),
    ):
        assert changed != base


def test_retrieval_codec_roundtrip() -> None:
    from app.services.retriever import ChunkResult
    from app.services.search import Retrieval

    doc = uuid.uuid4()
    hit = ChunkResult(
        text="t", document_id=doc, chunk_index=3, filename="f.pdf", score=0.5, rank=1, is_hit=True
    )
    ctx = ChunkResult(
        text="c",
        document_id=doc,
        chunk_index=4,
        filename="f.pdf",
        score=None,
        is_hit=False,
        context_for=(doc, 3),
    )
    r = Retrieval(hits=[hit], passages=[hit, ctx])
    assert decode_retrieval(encode_retrieval(r)) == r


def test_trace_summary_hit_rate_and_savings() -> None:
    rows = [
        ("search", 10, {"cache": {"result": "hit", "retrieval": "hit"}}),
        ("search", 100, {"cache": {"result": "miss", "retrieval": "miss"}}),
        ("search", 90, {"cache": {"result": "bypass"}}),
        ("search", 50, {}),
        (
            "chat",
            5,
            {
                "cache": {
                    "result": "hit",
                    "answer": "hit",
                    "llm_calls_avoided": 1,
                    "saved_usd": 0.002,
                }
            },
        ),
        ("ingest", 1, {}),
    ]
    s = trace_summary(rows)
    assert (
        s["search"]["hit_rate"] == 0.5
        and s["search"]["bypassed"] == 1
        and s["search"]["requests"] == 4
    )
    assert s["search"]["avg_hit_ms"] == 10 and s["search"]["avg_miss_ms"] == 100
    assert s["chat"]["llm_calls_avoided"] == 1 and s["chat"]["saved_usd"] == pytest.approx(0.002)


def test_cache_call_verdicts() -> None:
    c = make()
    call = CacheCall(c)
    call.layers["retrieval"] = "hit"
    assert call.result("answer") == "hit" and call.header("answer")["X-Cache"] == "hit"
    assert CacheCall(c, bypass=True).result("answer") == "bypass"
    assert CacheCall(make(cache_enabled=False)).result("answer") == "off"


# ── Redis-backed behaviour ───────────────────────────────────────────────────────────────────


async def _enc(v: str) -> bytes:
    return v.encode()


@needs_redis
async def test_hit_after_miss_and_value_roundtrip(qc: QueryCache) -> None:
    calls = 0

    async def compute() -> str:
        nonlocal calls
        calls += 1
        return "v"

    first = await qc.cached(
        "retrieval", "rag:cache:v1:retrieval:k", compute, lambda s: s.encode(), bytes.decode
    )
    second = await qc.cached(
        "retrieval", "rag:cache:v1:retrieval:k", compute, lambda s: s.encode(), bytes.decode
    )
    assert first == ("v", "miss") and second == ("v", "hit") and calls == 1
    assert qc.counters["retrieval"].hit == 1 and qc.counters["retrieval"].miss == 1


@needs_redis
async def test_ttl_expiry() -> None:
    c = make(cache_retrieval_ttl_seconds=1)
    redis_lib.Redis.from_url(URL).flushdb()
    calls = 0

    async def compute() -> str:
        nonlocal calls
        calls += 1
        return "v"

    for _ in range(2):
        await c.cached(
            "retrieval", "rag:cache:v1:retrieval:t", compute, lambda s: s.encode(), bytes.decode
        )
    assert calls == 1
    await asyncio.sleep(1.3)
    await c.cached(
        "retrieval", "rag:cache:v1:retrieval:t", compute, lambda s: s.encode(), bytes.decode
    )
    assert calls == 2


@needs_redis
async def test_bypass_neither_reads_nor_writes(qc: QueryCache) -> None:
    calls = 0

    async def compute() -> str:
        nonlocal calls
        calls += 1
        return "v"

    k = "rag:cache:v1:retrieval:b"
    _, s1 = await qc.cached(
        "retrieval", k, compute, lambda s: s.encode(), bytes.decode, bypass=True
    )
    assert s1 == "bypass"
    assert redis_lib.Redis.from_url(URL).get(k) is None  # no write
    await qc.cached("retrieval", k, compute, lambda s: s.encode(), bytes.decode)
    _, s3 = await qc.cached(
        "retrieval", k, compute, lambda s: s.encode(), bytes.decode, bypass=True
    )
    assert s3 == "bypass" and calls == 3  # no read either


@needs_redis
async def test_errors_and_vetoed_values_are_not_stored(qc: QueryCache) -> None:
    async def boom() -> str:
        raise RuntimeError("llm down")

    with pytest.raises(RuntimeError):
        await qc.cached("answer", "rag:cache:v1:answer:e", boom, lambda s: s.encode(), bytes.decode)

    async def empty() -> str:
        return ""

    await qc.cached(
        "answer", "rag:cache:v1:answer:e2", empty, lambda s: None, bytes.decode
    )  # encode vetoes
    r = redis_lib.Redis.from_url(URL)
    assert r.get("rag:cache:v1:answer:e") is None and r.get("rag:cache:v1:answer:e2") is None


@needs_redis
async def test_oversize_value_skipped() -> None:
    c = make(cache_max_value_bytes=1024)
    redis_lib.Redis.from_url(URL).flushdb()

    async def big() -> str:
        return "x" * 5000

    v, s = await c.cached(
        "retrieval", "rag:cache:v1:retrieval:big", big, lambda s: s.encode(), bytes.decode
    )
    assert (len(v), s) == (5000, "miss") and c.counters["retrieval"].skipped_size == 1
    assert redis_lib.Redis.from_url(URL).get("rag:cache:v1:retrieval:big") is None


@needs_redis
async def test_single_flight_one_compute_for_concurrent_identical_misses(qc: QueryCache) -> None:
    calls = 0

    async def compute() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return "v"

    res = await asyncio.gather(
        *[
            qc.cached(
                "retrieval",
                "rag:cache:v1:retrieval:sf",
                compute,
                lambda s: s.encode(),
                bytes.decode,
            )
            for _ in range(8)
        ]
    )
    assert calls == 1
    assert sorted(s for _, s in res) == ["coalesced"] * 7 + ["miss"]


@needs_redis
async def test_index_bump_changes_version_and_hides_old_entries(qc: QueryCache) -> None:
    v1 = await qc.index_version()
    assert v1 and await qc.index_version() == v1
    assert await asyncio.to_thread(qc.bump_sync)
    v2 = await qc.index_version()
    assert v2 and v2 != v1
    r = redis_lib.Redis.from_url(URL)
    assert int(r.get(cache_mod.BUMPS_KEY) or 0) >= 1


@needs_redis
async def test_purge_removes_only_cache_namespace(qc: QueryCache) -> None:
    r = redis_lib.Redis.from_url(URL)
    r.set("celery-like-key", "1")
    r.set("rag:cache:v1:answer:x", "1")
    removed = await qc.purge()
    assert removed is not None and removed >= 1
    assert r.get("rag:cache:v1:answer:x") is None and r.get("celery-like-key") == b"1"


async def test_fail_open_when_redis_is_unreachable() -> None:
    c = QueryCache(
        Settings(
            cache_enabled=True,
            cache_redis_url="redis://localhost:1/12",
            cache_timeout_ms=50,
            cache_breaker_seconds=30,
        )
    )
    assert await c.index_version() is None

    async def compute() -> str:
        return "v"

    value, state = await c.cached(
        "retrieval", "rag:cache:v1:retrieval:z", compute, lambda s: s.encode(), bytes.decode
    )
    assert (value, state) == ("v", "miss")  # served, never an error
    assert c.breaker_open and c.last_error
    assert c.bump_sync(attempts=1) is False  # records the need to purge on recovery


def test_cache_url_must_not_be_celery_db() -> None:
    with pytest.raises(ValueError, match="Celery"):
        Settings(
            cache_enabled=True,
            cache_redis_url="redis://localhost:6379/0",
            celery_broker_url="redis://localhost:6379/0",
        )
