"""Unit tests — LLM clients (respx / httpx2 transport), factory, prompt building."""

from __future__ import annotations

import json
import uuid

import httpx
import httpx2
import pytest
import respx
from app.config import Settings
from app.services.llm import (
    AnthropicLLM,
    Completion,
    LLMConfigError,
    LLMRequestError,
    LLMUnavailableError,
    OpenAICompatLLM,
    get_llm,
)
from app.services.qa import NO_CONTEXT_ANSWER, SYSTEM_PROMPT, answer_question, build_user_prompt
from app.services.retriever import ChunkResult
from httpx import Response
from tests.fakes import FakeLLM

URL = "https://openrouter.ai/api/v1/chat/completions"


def _llm() -> OpenAICompatLLM:
    return OpenAICompatLLM(
        "k", "m", base_url="https://openrouter.ai/api/v1", http_client=httpx.Client(timeout=30)
    )


def _chunk(text: str = "alpha", idx: int = 0) -> ChunkResult:
    return ChunkResult(
        text=text, document_id=uuid.uuid4(), chunk_index=idx, filename="f.pdf", score=0.1
    )


@respx.mock
def test_openai_compat_reports_provider_token_counts_or_none() -> None:
    respx.post(URL).mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
            },
        )
    )
    done = _llm().complete("s", "u")
    assert done == Completion("ok", 123, 45)
    respx.post(URL).mock(return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    assert _llm().complete("s", "u") == Completion("ok", None, None)


@respx.mock
def test_openai_compat_returns_text_and_sends_messages() -> None:
    route = respx.post(URL).mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "42"}}]})
    )
    assert _llm().complete("sys", "usr").text == "42"
    body = json.loads(route.calls.last.request.content)
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert body["model"] == "m"


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses_map_to_unavailable(status: int) -> None:
    respx.post(URL).mock(return_value=Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(LLMUnavailableError):
        _llm().complete("s", "u")


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403])
def test_client_errors_map_to_request_error(status: int) -> None:
    respx.post(URL).mock(return_value=Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(LLMRequestError):
        _llm().complete("s", "u")


@respx.mock
def test_empty_and_missing_choices_are_unavailable() -> None:
    respx.post(URL).mock(return_value=Response(200, json={}))
    with pytest.raises(LLMUnavailableError, match="no completion"):
        _llm().complete("s", "u")
    respx.post(URL).mock(
        return_value=Response(200, json={"choices": [{"message": {"content": ""}}]})
    )
    with pytest.raises(LLMUnavailableError, match="empty"):
        _llm().complete("s", "u")


@respx.mock
def test_connection_error_is_unavailable() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(LLMUnavailableError):
        _llm().complete("s", "u")


def test_anthropic_llm_round_trip() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.update(json.loads(req.content))
        return httpx2.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "c",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    llm = AnthropicLLM("k", "c", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    done = llm.complete("sys", "usr")
    assert done.text == "hi" and (done.prompt_tokens, done.completion_tokens) == (1, 1)
    assert seen["system"] == "sys"


@pytest.mark.parametrize(
    ("provider", "kw"),
    [
        ("openrouter", "openrouter_api_key"),
        ("openai", "openai_api_key"),
        ("anthropic", "anthropic_api_key"),
    ],
)
def test_factory_requires_key_and_builds(provider: str, kw: str) -> None:
    with pytest.raises(LLMConfigError):
        get_llm(Settings(_env_file=None, llm_provider=provider, **{kw: ""}))
    llm = get_llm(Settings(_env_file=None, llm_provider=provider, **{kw: "k"}))
    assert llm.model == Settings().default_llm_model


def test_prompt_numbers_sources_and_includes_question() -> None:
    p = build_user_prompt("What?", [_chunk("first", 0), _chunk("second", 3)])
    assert "[1] (source: f.pdf, chunk 0)\nfirst" in p
    assert "[2] (source: f.pdf, chunk 3)\nsecond" in p
    assert p.rstrip().endswith("Answer:") and "Question: What?" in p
    assert "ONLY" in SYSTEM_PROMPT


def test_prompt_caps_context_but_keeps_first_chunk() -> None:
    p = build_user_prompt("q", [_chunk("FIRST" + "x" * 20_000), _chunk("SECOND_MARKER")])
    assert "FIRST" in p
    assert "SECOND_MARKER" not in p


async def test_answer_skips_llm_without_context() -> None:
    llm = FakeLLM("  grounded answer [1]  ", usage=(11, 5), cost=None)
    out = await answer_question("q", [], llm)
    assert out.answer == NO_CONTEXT_ANSWER and out.sources == [] and llm.calls == 0


async def test_answer_returns_stripped_text_and_sources() -> None:
    out = await answer_question("q", [_chunk()], FakeLLM("  grounded answer [1]  ", usage=(11, 5), cost=None))
    assert out.answer == "grounded answer [1]" and len(out.sources) == 1
    assert (out.prompt_tokens, out.completion_tokens) == (11, 5)


async def test_answer_without_llm_but_with_context_raises_config_error() -> None:
    with pytest.raises(LLMConfigError):
        await answer_question("q", [_chunk()], None)
