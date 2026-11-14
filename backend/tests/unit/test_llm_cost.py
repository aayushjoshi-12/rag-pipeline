"""LLM cost capture: parsing the provider-reported cost (recorded response shapes), request
flags per provider, configured token prices, and the unknown-is-not-zero rule."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import pytest
import respx
from app.config import Settings
from app.services.llm import (
    AnthropicLLM,
    OpenAICompatLLM,
    get_llm,
    parse_reported_cost,
    price_tokens,
)
from httpx import Response
from pydantic import ValidationError

URL = "https://openrouter.ai/api/v1/chat/completions"

# Shapes recorded from OpenRouter (usage.include=true): a paid model, a free model, and a
# response from before the flag existed.
PAID = {
    "choices": [{"message": {"content": "hi"}}],
    "usage": {
        "prompt_tokens": 14,
        "completion_tokens": 9,
        "total_tokens": 23,
        "cost": 0.0000114,
        "cost_details": {"upstream_inference_cost": None},
    },
}
FREE = {
    "choices": [{"message": {"content": "hi"}}],
    "usage": {"prompt_tokens": 14, "completion_tokens": 9, "total_tokens": 23, "cost": 0},
}
NO_COST = {
    "choices": [{"message": {"content": "hi"}}],
    "usage": {"prompt_tokens": 14, "completion_tokens": 9, "total_tokens": 23},
}


def _llm(**kw: Any) -> OpenAICompatLLM:
    return OpenAICompatLLM(
        "k", "m", base_url="https://openrouter.ai/api/v1", http_client=httpx.Client(timeout=30), **kw
    )


@respx.mock
def test_paid_model_cost_is_parsed_exactly() -> None:
    respx.post(URL).mock(return_value=Response(200, json=PAID))
    assert _llm(report_cost=True).complete("s", "u").cost_usd == 0.0000114


@respx.mock
def test_free_model_reports_zero_which_is_a_known_cost() -> None:
    respx.post(URL).mock(return_value=Response(200, json=FREE))
    done = _llm(report_cost=True).complete("s", "u")
    assert done.cost_usd == 0.0 and done.cost_usd is not None


@respx.mock
def test_missing_cost_is_unknown_not_zero() -> None:
    respx.post(URL).mock(return_value=Response(200, json=NO_COST))
    assert _llm(report_cost=True).complete("s", "u").cost_usd is None


@respx.mock
def test_usage_include_is_sent_only_when_report_cost_is_on() -> None:
    route = respx.post(URL).mock(return_value=Response(200, json=PAID))
    _llm(report_cost=True).complete("s", "u")
    assert json.loads(route.calls.last.request.content)["usage"] == {"include": True}
    _llm().complete("s", "u")
    assert "usage" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_without_report_cost_a_cost_field_in_the_reply_is_ignored() -> None:
    respx.post(URL).mock(return_value=Response(200, json=PAID))
    assert _llm().complete("s", "u").cost_usd is None


@respx.mock
def test_configured_prices_fill_in_when_the_provider_reports_nothing() -> None:
    respx.post(URL).mock(return_value=Response(200, json=NO_COST))
    done = _llm(price_input_per_mtok=2.0, price_output_per_mtok=10.0).complete("s", "u")
    assert done.cost_usd == pytest.approx((14 * 2.0 + 9 * 10.0) / 1e6)


@respx.mock
def test_the_provider_reported_cost_wins_over_configured_prices() -> None:
    respx.post(URL).mock(return_value=Response(200, json=PAID))
    done = _llm(report_cost=True, price_input_per_mtok=99.0, price_output_per_mtok=99.0).complete("s", "u")
    assert done.cost_usd == 0.0000114


@pytest.mark.parametrize("raw", [None, "0.5", True, -1, math.nan, math.inf, [1]])
def test_parse_reported_cost_rejects_garbage(raw: object) -> None:
    assert parse_reported_cost(SimpleNamespace(cost=raw)) is None


def test_parse_reported_cost_reads_pydantic_extra_and_handles_no_usage() -> None:
    assert parse_reported_cost(SimpleNamespace(model_extra={"cost": 0.25})) == 0.25
    assert parse_reported_cost(None) is None


@pytest.mark.parametrize(
    ("p", "c", "pi", "po", "want"),
    [
        (1000, 500, 3.0, 15.0, 0.0105),
        (0, 0, 3.0, 15.0, 0.0),
        (None, 5, 3.0, 15.0, None),
        (5, None, 3.0, 15.0, None),
        (5, 5, None, 15.0, None),
        (5, 5, 3.0, None, None),
    ],
)
def test_price_tokens(
    p: int | None, c: int | None, pi: float | None, po: float | None, want: float | None
) -> None:
    got = price_tokens(p, c, pi, po)
    assert got == (pytest.approx(want) if want is not None else None)


def test_anthropic_uses_configured_prices_and_is_unknown_without() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
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
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            },
        )

    def make(**kw: Any) -> AnthropicLLM:
        return AnthropicLLM(
            "k", "c", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **kw
        )

    assert make().complete("s", "u").cost_usd is None
    priced = make(price_input_per_mtok=3.0, price_output_per_mtok=15.0).complete("s", "u")
    assert priced.cost_usd == pytest.approx(0.0045)


def test_factory_enables_cost_reporting_only_for_openrouter() -> None:
    router = get_llm(Settings(llm_provider="openrouter", openrouter_api_key="k"))
    plain = get_llm(Settings(llm_provider="openai", openai_api_key="k"))
    assert isinstance(router, OpenAICompatLLM) and router.report_cost is True
    assert isinstance(plain, OpenAICompatLLM) and plain.report_cost is False


def test_factory_passes_price_settings_through() -> None:
    cfg = Settings(
        llm_provider="openai",
        openai_api_key="k",
        llm_price_input_per_mtok=1.5,
        llm_price_output_per_mtok=6.0,
    )
    llm = get_llm(cfg)
    assert isinstance(llm, OpenAICompatLLM)
    assert (llm.price_input_per_mtok, llm.price_output_per_mtok) == (1.5, 6.0)


def test_price_settings_are_validated() -> None:
    with pytest.raises(ValidationError, match="llm_price_input_per_mtok"):
        Settings(llm_price_input_per_mtok=-1, llm_price_output_per_mtok=1)
    with pytest.raises(ValidationError, match="both"):
        Settings(llm_price_input_per_mtok=1)
    assert Settings().llm_price_input_per_mtok is None
