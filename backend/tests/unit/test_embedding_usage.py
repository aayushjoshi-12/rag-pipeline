"""Ticket 45: embedding providers report token usage through the request-scoped meter."""

from __future__ import annotations

import asyncio

import app.services.embeddings as emb
import pytest
import respx
from app.config import Settings
from app.services.search import embedding_usage
from httpx import Response
from tests.fakes import FakeEmbedder, MeteredEmbedder


@respx.mock
def test_ollama_reports_prompt_eval_count() -> None:
    respx.post("http://h:1/api/embed").mock(
        return_value=Response(200, json={"embeddings": [[0.1], [0.2]], "prompt_eval_count": 17})
    )
    e = emb.OllamaEmbedder("m", "http://h:1")
    with emb.measure_embeddings() as meter:
        e.embed_documents(["a", "b"])
        e.embed_query("c")
    assert meter.tokens == 34  # two calls of 17


@respx.mock
def test_ollama_without_prompt_eval_count_stays_unknown() -> None:
    respx.post("http://h:1/api/embed").mock(return_value=Response(200, json={"embeddings": [[0.1]]}))
    with emb.measure_embeddings() as meter:
        emb.OllamaEmbedder("m", "http://h:1").embed_query("c")
    assert meter.tokens is None


def test_no_meter_is_a_noop_and_fakes_never_report() -> None:
    assert FakeEmbedder().embed_query("x")  # no meter active: nothing breaks
    with emb.measure_embeddings() as meter:
        FakeEmbedder().embed_documents(["a"])
    assert meter.tokens is None


def test_openai_reports_total_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    resp = SimpleNamespace(data=[SimpleNamespace(embedding=[0.5])], usage=SimpleNamespace(total_tokens=9))

    class Client:
        def __init__(self, **_: object) -> None:
            self.embeddings = self

        def create(self, **_: object) -> SimpleNamespace:
            return resp

    monkeypatch.setattr("openai.OpenAI", Client)
    with emb.measure_embeddings() as meter:
        emb.OpenAIEmbedder("text-embedding-3-small", "k").embed_documents(["a"])
    assert meter.tokens == 9


async def test_worker_threads_share_the_meter() -> None:
    with emb.measure_embeddings() as meter:
        await asyncio.gather(*(asyncio.to_thread(MeteredEmbedder().embed_query, "x") for _ in range(5)))
    assert meter.tokens == 50


def test_meters_do_not_leak_between_scopes() -> None:
    with emb.measure_embeddings() as outer:
        with emb.measure_embeddings() as inner:
            MeteredEmbedder().embed_query("x")
        MeteredEmbedder().embed_query("x")
    assert (inner.tokens, outer.tokens) == (10, 10)


def test_embedding_cost_local_is_free_hosted_needs_a_price() -> None:
    assert emb.embedding_cost(Settings(embedding_provider="ollama"), 100) == 0.0
    assert emb.embedding_cost(Settings(embedding_provider="ollama"), None) is None
    assert emb.embedding_cost(Settings(embedding_provider="openai"), 100) is None
    priced = Settings(embedding_provider="openai", embedding_price_per_mtok=0.02)
    assert emb.embedding_cost(priced, 1_000_000) == pytest.approx(0.02)


def test_embedding_price_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="embedding_price_per_mtok"):
        Settings(embedding_price_per_mtok=-1)


def test_embedding_usage_for_traces() -> None:
    from app.services.cache import CacheCall

    class _QC:
        enabled = False

    call = CacheCall(_QC())  # type: ignore[arg-type]  # only the counters are read
    meter = emb.EmbeddingMeter()
    assert embedding_usage(meter, call, "semantic") is None  # unknown
    meter.add(12)
    assert embedding_usage(meter, call, "semantic") == {"tokens": 12}
    assert embedding_usage(meter, call, "keyword") is None  # no embedding in keyword mode
    call.embeddings_avoided = 1
    assert embedding_usage(meter, call, "hybrid") == {"tokens": 0, "cached": True}
