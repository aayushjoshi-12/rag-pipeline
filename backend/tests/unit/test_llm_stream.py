"""LLM.stream on both providers (recorded SSE shapes, no network) and the async bridge."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import httpx2
import pytest
import respx
from app.services.llm import (
    AnthropicLLM,
    Completion,
    LLMRequestError,
    LLMUnavailableError,
    OpenAICompatLLM,
    aiter_stream,
)
from httpx import Response

URL = "https://openrouter.ai/api/v1/chat/completions"


def _chunk(content: str | None, usage: dict[str, Any] | None = None, choices: bool = True) -> str:
    body: dict[str, Any] = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": (
            [{"index": 0, "delta": {"content": content}, "finish_reason": None}] if choices else []
        ),
    }
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


def _sse(*frames: str) -> Response:
    return Response(
        200,
        content="".join(frames) + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _llm(**kw: Any) -> OpenAICompatLLM:
    return OpenAICompatLLM(
        "k",
        "m",
        base_url="https://openrouter.ai/api/v1",
        http_client=httpx.Client(timeout=30),
        **kw,
    )


@respx.mock
def test_openai_stream_yields_deltas_then_final_completion_with_usage_and_cost() -> None:
    route = respx.post(URL).mock(
        return_value=_sse(
            _chunk("Hel"),
            _chunk("lo "),
            _chunk("[1]"),
            _chunk(
                None,
                usage={
                    "prompt_tokens": 14,
                    "completion_tokens": 3,
                    "total_tokens": 17,
                    "cost": 0.0000114,
                },
                choices=False,
            ),
        )
    )
    items = list(_llm(report_cost=True).stream("sys", "usr"))
    assert items[:-1] == ["Hel", "lo ", "[1]"]
    final = items[-1]
    assert isinstance(final, Completion)
    assert (final.text, final.prompt_tokens, final.completion_tokens) == ("Hello [1]", 14, 3)
    assert final.cost_usd == 0.0000114
    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["usage"] == {"include": True}  # OpenRouter cost reporting


@respx.mock
def test_openai_stream_unknown_cost_stays_none_and_no_usage_flag_for_plain_openai() -> None:
    route = respx.post(URL).mock(
        return_value=_sse(
            _chunk("hi"),
            _chunk(None, usage={"prompt_tokens": 1, "completion_tokens": 1}, choices=False),
        )
    )
    final = list(_llm().stream("s", "u"))[-1]
    assert isinstance(final, Completion) and final.cost_usd is None
    assert "usage" not in json.loads(route.calls[0].request.content)


@respx.mock
def test_openai_stream_prices_tokens_when_provider_reports_no_cost() -> None:
    respx.post(URL).mock(
        return_value=_sse(
            _chunk("hi"),
            _chunk(None, usage={"prompt_tokens": 1000, "completion_tokens": 100}, choices=False),
        )
    )
    final = list(_llm(price_input_per_mtok=3.0, price_output_per_mtok=15.0).stream("s", "u"))[-1]
    assert isinstance(final, Completion) and final.cost_usd == pytest.approx(0.0045)


@respx.mock
def test_openai_stream_empty_reply_is_unavailable() -> None:
    respx.post(URL).mock(return_value=_sse(_chunk(""), _chunk(None, choices=False)))
    with pytest.raises(LLMUnavailableError):
        list(_llm().stream("s", "u"))


@respx.mock
@pytest.mark.parametrize(
    ("code", "exc"),
    [(429, LLMUnavailableError), (503, LLMUnavailableError), (401, LLMRequestError)],
)
def test_openai_stream_http_errors_map_like_complete(code: int, exc: type[Exception]) -> None:
    respx.post(URL).mock(return_value=Response(code, json={"error": {"message": "x"}}))
    with pytest.raises(exc):
        list(_llm().stream("s", "u"))


@respx.mock
def test_openai_stream_inband_error_chunk_is_a_failure() -> None:
    err = 'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m","choices":[],"error":{"message":"upstream died","code":502}}\n\n'
    respx.post(URL).mock(return_value=_sse(_chunk("partial"), err))
    got: list[Any] = []
    with pytest.raises(LLMUnavailableError, match="upstream died"):
        for item in _llm().stream("s", "u"):
            got.append(item)
    assert got == ["partial"]  # the delta before the failure was delivered; no final Completion


def _anthropic_events() -> str:
    def ev(name: str, data: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"

    return "".join(
        [
            ev(
                "message_start",
                {
                    "message": {
                        "id": "m",
                        "type": "message",
                        "role": "assistant",
                        "model": "c",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1000, "output_tokens": 1},
                    }
                },
            ),
            ev("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ev(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
            ),
            ev(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "lo"}},
            ),
            ev("content_block_stop", {"index": 0}),
            ev(
                "message_delta",
                {
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 100},
                },
            ),
            ev("message_stop", {}),
        ]
    )


def test_anthropic_stream_deltas_usage_and_priced_cost() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        assert json.loads(req.content)["stream"] is True
        return httpx2.Response(
            200, content=_anthropic_events(), headers={"content-type": "text/event-stream"}
        )

    llm = AnthropicLLM(
        "k",
        "c",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        price_input_per_mtok=3.0,
        price_output_per_mtok=15.0,
    )
    items = list(llm.stream("s", "u"))
    assert items[:-1] == ["Hel", "lo"]
    final = items[-1]
    assert isinstance(final, Completion)
    assert (final.text, final.prompt_tokens, final.completion_tokens) == ("Hello", 1000, 100)
    assert final.cost_usd == pytest.approx(0.0045)


def test_anthropic_stream_http_error_maps() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"type": "error", "error": {"type": "x", "message": "no"}})

    llm = AnthropicLLM("k", "c", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(LLMRequestError):
        list(llm.stream("s", "u"))


# ── async bridge ─────────────────────────────────────────────────────────────────────────────
class _Gen:
    """Fake LLM whose stream records whether it was closed (= upstream connection released)."""

    model = "fake"

    def __init__(self, n: int = 3, fail_after: int | None = None) -> None:
        self.n, self.fail_after = n, fail_after
        self.closed = threading.Event()
        self.emitted = 0
        self.gate = threading.Event()
        self.gate.set()

    def complete(self, system: str, user: str) -> Completion:  # pragma: no cover - unused
        raise AssertionError

    def stream(self, system: str, user: str) -> Iterator[str | Completion]:
        try:
            for i in range(self.n):
                self.gate.wait(5)
                time.sleep(0.005)
                if self.fail_after is not None and i == self.fail_after:
                    raise LLMUnavailableError("boom")
                self.emitted += 1
                yield f"t{i} "
            yield Completion("".join(f"t{i} " for i in range(self.n)), 5, 3, 0.0)
        finally:
            self.closed.set()


async def test_bridge_delivers_everything_in_order() -> None:
    llm = _Gen(3)
    got = [x async for x in aiter_stream(llm, "s", "u")]  # type: ignore[arg-type]
    assert got[:3] == ["t0 ", "t1 ", "t2 "]
    assert isinstance(got[3], Completion)
    assert llm.closed.is_set()


async def test_bridge_propagates_midstream_failure_after_delivered_tokens() -> None:
    llm = _Gen(3, fail_after=2)
    got: list[Any] = []
    with pytest.raises(LLMUnavailableError, match="boom"):
        async for x in aiter_stream(llm, "s", "u"):  # type: ignore[arg-type]
            got.append(x)
    assert got == ["t0 ", "t1 "]
    assert llm.closed.is_set()


async def test_bridge_cancel_closes_the_upstream_stream() -> None:
    llm = _Gen(1000)
    seen: list[Any] = []

    async def consume() -> None:
        async for x in aiter_stream(llm, "s", "u"):  # type: ignore[arg-type]
            seen.append(x)
            if len(seen) == 2:
                await asyncio.sleep(30)  # client "hangs here" until cancelled

    task = asyncio.create_task(consume())
    for _ in range(200):
        if len(seen) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(llm.closed.wait, 5), "upstream generator was never closed"
    assert llm.emitted < 1000  # it stopped producing instead of draining the whole answer


async def test_bridge_degrades_to_complete_when_llm_cannot_stream() -> None:
    class Plain:
        model = "p"

        def complete(self, system: str, user: str) -> Completion:
            return Completion("whole answer", 1, 2, 0.5)

    got = [x async for x in aiter_stream(Plain(), "s", "u")]  # type: ignore[arg-type]
    assert got[0] == "whole answer" and isinstance(got[1], Completion)


@respx.mock
async def test_reasoning_chunks_are_liveness_ticks_never_tokens() -> None:
    def reasoning(text: str) -> str:
        body = {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": None, "reasoning": text}}
            ],
        }
        return f"data: {json.dumps(body)}\n\n"

    respx.post(URL).mock(
        return_value=_sse(
            reasoning("hmm"),
            reasoning("let me think"),
            _chunk("Answer"),
            _chunk(None, usage={"prompt_tokens": 5, "completion_tokens": 40}, choices=False),
        )
    )
    raw = list(_llm().stream("s", "u"))
    assert raw[:3] == ["", "", "Answer"]  # ticks are visible to the bridge ...
    got = [x async for x in aiter_stream(_llm(), "s", "u")]
    assert got[0] == "Answer" and isinstance(got[1], Completion)  # ... and never reach the consumer
    assert isinstance(raw[-1], Completion) and raw[-1].completion_tokens == 40


async def test_bridge_cancel_during_silent_phase_closes_upstream() -> None:
    """A reasoning model emits no text for seconds: the cancel must not wait for the first real token."""

    class Thinking:
        model = "t"

        def __init__(self) -> None:
            self.closed = threading.Event()

        def complete(self, system: str, user: str) -> Completion:  # pragma: no cover
            raise AssertionError

        def stream(self, system: str, user: str) -> Iterator[str | Completion]:
            try:
                for _ in range(2000):
                    time.sleep(0.005)
                    yield ""  # tick, never any text
                yield "never"
            finally:
                self.closed.set()

    llm = Thinking()

    async def consume() -> None:
        async for _ in aiter_stream(llm, "s", "u"):  # type: ignore[arg-type]
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(llm.closed.wait, 2), "cancel waited for the first token"
