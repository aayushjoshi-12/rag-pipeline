"""Ticket 45/40: usage survives failed LLM calls on both providers (complete + stream); LLM_MAX_TOKENS."""

from __future__ import annotations

import json
from typing import Any

import httpx
import httpx2
import pytest
import respx
from app.config import Settings
from app.services.llm import (
    AnthropicLLM,
    LLMUnavailableError,
    OpenAICompatLLM,
    get_llm,
)
from httpx import Response

URL = "https://openrouter.ai/api/v1/chat/completions"
USAGE = {"prompt_tokens": 2500, "completion_tokens": 2048, "total_tokens": 4548, "cost": 0.0001}


def _llm(**kw: Any) -> OpenAICompatLLM:
    return OpenAICompatLLM(
        "k",
        "m",
        base_url="https://openrouter.ai/api/v1",
        http_client=httpx.Client(timeout=30),
        report_cost=True,
        **kw,
    )


def _frame(delta: str | None, finish: str | None = None, usage: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": finish}] if usage is None else [],
    }
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


def _sse(*frames: str) -> Response:
    return Response(200, content="".join(frames) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})


# ── OpenAI-compatible ────────────────────────────────────────────────────────────────────────
@respx.mock
def test_openai_complete_empty_answer_keeps_usage_and_says_out_of_tokens() -> None:
    respx.post(URL).mock(
        return_value=Response(
            200,
            json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": USAGE},
        )
    )
    with pytest.raises(LLMUnavailableError, match="ran out of output tokens") as ei:
        _llm(max_tokens=2048).complete("s", "u")
    assert ei.value.usage is not None
    assert (ei.value.usage.prompt_tokens, ei.value.usage.completion_tokens, ei.value.usage.cost_usd) == (
        2500,
        2048,
        0.0001,
    )
    assert "max_tokens=2048" in str(ei.value)


@respx.mock
def test_openai_complete_empty_answer_without_length_is_a_plain_empty_error_and_priced() -> None:
    respx.post(URL).mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"content": " "}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
            },
        )
    )
    llm = OpenAICompatLLM(
        "k", "m", base_url="https://openrouter.ai/api/v1", http_client=httpx.Client(timeout=30),
        price_input_per_mtok=3.0, price_output_per_mtok=15.0,
    )
    with pytest.raises(LLMUnavailableError, match="empty answer") as ei:
        llm.complete("s", "u")
    assert "output tokens" not in str(ei.value)
    assert ei.value.usage is not None and ei.value.usage.cost_usd == pytest.approx(0.0045)


@respx.mock
def test_openai_complete_without_usage_has_no_usage_on_the_error() -> None:
    respx.post(URL).mock(return_value=Response(200, json={"choices": [{"message": {"content": ""}}]}))
    with pytest.raises(LLMUnavailableError) as ei:
        _llm().complete("s", "u")
    assert ei.value.usage is None


@respx.mock
def test_openai_complete_no_choices_keeps_usage() -> None:
    respx.post(URL).mock(return_value=Response(200, json={"choices": [], "usage": USAGE}))
    with pytest.raises(LLMUnavailableError, match="no completion choices") as ei:
        _llm().complete("s", "u")
    assert ei.value.usage is not None and ei.value.usage.prompt_tokens == 2500


@respx.mock
def test_openai_stream_empty_answer_keeps_usage_and_says_out_of_tokens() -> None:
    respx.post(URL).mock(return_value=_sse(_frame(""), _frame(None, finish="length"), _frame(None, usage=USAGE)))
    with pytest.raises(LLMUnavailableError, match="ran out of output tokens") as ei:
        list(_llm(max_tokens=2048).stream("s", "u"))
    assert ei.value.usage is not None
    assert (ei.value.usage.prompt_tokens, ei.value.usage.completion_tokens, ei.value.usage.cost_usd) == (
        2500,
        2048,
        0.0001,
    )


@respx.mock
def test_openai_stream_inband_error_keeps_usage_seen_so_far() -> None:
    err = 'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m","choices":[],"error":{"message":"died","code":502}}\n\n'
    respx.post(URL).mock(return_value=_sse(_frame("part"), _frame(None, usage=USAGE), err))
    with pytest.raises(LLMUnavailableError, match="died") as ei:
        list(_llm().stream("s", "u"))
    assert ei.value.usage is not None and ei.value.usage.completion_tokens == 2048


@respx.mock
def test_openai_stream_empty_without_usage_has_none() -> None:
    respx.post(URL).mock(return_value=_sse(_frame(""), _frame(None, finish="stop")))
    with pytest.raises(LLMUnavailableError, match="empty answer") as ei:
        list(_llm().stream("s", "u"))
    assert ei.value.usage is None


# ── Anthropic ────────────────────────────────────────────────────────────────────────────────
def _anthropic(handler: Any, **kw: Any) -> AnthropicLLM:
    return AnthropicLLM(
        "k", "c", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        price_input_per_mtok=3.0, price_output_per_mtok=15.0, **kw,
    )


def test_anthropic_complete_empty_answer_keeps_usage_and_says_out_of_tokens() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "m", "type": "message", "role": "assistant", "model": "c",
                "content": [{"type": "thinking", "thinking": "hmm", "signature": "s"}],
                "stop_reason": "max_tokens", "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            },
        )

    with pytest.raises(LLMUnavailableError, match="ran out of output tokens") as ei:
        _anthropic(handler, max_tokens=100).complete("s", "u")
    assert ei.value.usage is not None
    assert (ei.value.usage.prompt_tokens, ei.value.usage.completion_tokens) == (1000, 100)
    assert ei.value.usage.cost_usd == pytest.approx(0.0045)


def test_anthropic_stream_empty_answer_keeps_usage() -> None:
    def ev(name: str, data: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"

    body = "".join(
        [
            ev("message_start", {"message": {
                "id": "m", "type": "message", "role": "assistant", "model": "c", "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 1}}}),
            ev("message_delta", {"delta": {"stop_reason": "max_tokens", "stop_sequence": None},
                                 "usage": {"output_tokens": 100}}),
            ev("message_stop", {}),
        ]
    )

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with pytest.raises(LLMUnavailableError, match="ran out of output tokens") as ei:
        list(_anthropic(handler).stream("s", "u"))
    assert ei.value.usage is not None
    assert (ei.value.usage.prompt_tokens, ei.value.usage.completion_tokens) == (1000, 100)
    assert ei.value.usage.cost_usd == pytest.approx(0.0045)


# ── LLM_MAX_TOKENS ───────────────────────────────────────────────────────────────────────────
@respx.mock
def test_max_tokens_is_sent_to_the_provider() -> None:
    route = respx.post(URL).mock(return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    _llm(max_tokens=777).complete("s", "u")
    assert json.loads(route.calls[0].request.content)["max_completion_tokens"] == 777


def test_default_is_2048_and_get_llm_passes_the_setting_for_every_provider() -> None:
    assert Settings().llm_max_tokens == 2048
    for provider, key in (("openrouter", "openrouter_api_key"), ("openai", "openai_api_key"), ("anthropic", "anthropic_api_key")):
        llm = get_llm(Settings(llm_provider=provider, llm_max_tokens=321, **{key: "k"}))  # type: ignore[arg-type]
        assert llm.max_tokens == 321  # type: ignore[attr-defined]  # both clients expose it


@pytest.mark.parametrize("bad", [0, -1, 200_001])
def test_max_tokens_is_validated(bad: int) -> None:
    with pytest.raises(ValueError, match="llm_max_tokens"):
        Settings(llm_max_tokens=bad)


def test_max_tokens_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
    assert Settings().llm_max_tokens == 4096
