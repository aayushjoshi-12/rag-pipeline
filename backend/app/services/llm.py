"""Chat-completion clients for answer synthesis (openrouter | openai | anthropic)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
import openai

from app.config import Settings
from app.services import observability


class LLMError(Exception):
    """Base class for LLM call failures."""


class LLMUnavailableError(LLMError):
    """Transient provider failure (timeout, rate limit, 5xx, empty reply) — maps to HTTP 503."""


class LLMRequestError(LLMError):
    """Permanent provider failure (bad key, rejected request) — maps to HTTP 502."""


class LLMConfigError(LLMError):
    """Provider is not configured (e.g. missing API key)."""


@dataclass(frozen=True)
class Completion:
    """A model reply plus the provider's token counts (None when the provider omits them)."""

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: What the call cost in USD. ``None`` = unknown (never 0): the provider did not report a
    #: cost and no price per token is configured. A free model that reports 0 gives ``0.0``.
    cost_usd: float | None = None


def parse_reported_cost(usage: Any) -> float | None:
    """The provider-reported cost (OpenRouter ``usage.cost``, USD), or ``None`` if absent/invalid."""
    if usage is None:
        return None
    raw = getattr(usage, "cost", None)
    if raw is None:
        extra = getattr(usage, "model_extra", None)
        raw = extra.get("cost") if isinstance(extra, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    return value if value >= 0 and value == value and value != float("inf") else None


def price_tokens(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    input_per_mtok: float | None,
    output_per_mtok: float | None,
) -> float | None:
    """Cost from configured per-million-token prices; ``None`` unless both prices and counts are known."""
    if None in (prompt_tokens, completion_tokens, input_per_mtok, output_per_mtok):
        return None
    assert prompt_tokens is not None and completion_tokens is not None  # narrowed for mypy
    assert input_per_mtok is not None and output_per_mtok is not None
    return (prompt_tokens * input_per_mtok + completion_tokens * output_per_mtok) / 1_000_000


class LLM(Protocol):
    model: str

    def complete(self, system: str, user: str) -> Completion: ...


class OpenAICompatLLM:
    """OpenAI-compatible chat completions (OpenAI itself, or OpenRouter via ``base_url``)."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
        max_tokens: int = 1024,
        http_client: Any | None = None,
        report_cost: bool = False,
        price_input_per_mtok: float | None = None,
        price_output_per_mtok: float | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        #: Ask the provider for the request cost (OpenRouter only: ``usage: {include: true}``).
        self.report_cost = report_cost
        self.price_input_per_mtok = price_input_per_mtok
        self.price_output_per_mtok = price_output_per_mtok
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    def complete(self, system: str, user: str) -> Completion:
        with observability.generation("llm.chat", model=self.model, input=user) as gen:
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_completion_tokens=self.max_tokens,
                    extra_body={"usage": {"include": True}} if self.report_cost else None,
                )
            except (openai.APITimeoutError, openai.APIConnectionError) as exc:
                raise LLMUnavailableError(f"LLM request failed: {exc}") from exc
            except openai.APIStatusError as exc:
                if exc.status_code == 429 or exc.status_code >= 500:
                    raise LLMUnavailableError(f"LLM provider returned {exc.status_code}") from exc
                raise LLMRequestError(f"LLM provider returned {exc.status_code}") from exc
            if not resp.choices:
                raise LLMUnavailableError("LLM returned no completion choices")
            text = resp.choices[0].message.content
            if not text or not text.strip():
                raise LLMUnavailableError("LLM returned an empty answer")
            usage = resp.usage
            gen.update(
                output=observability.maybe_content(text),
                usage_details=observability.usage_details(
                    usage.prompt_tokens if usage else None,
                    usage.completion_tokens if usage else None,
                ),
            )
            prompt = usage.prompt_tokens if usage else None
            completion = usage.completion_tokens if usage else None
            cost = parse_reported_cost(usage) if self.report_cost else None
            if cost is None:
                cost = price_tokens(
                    prompt, completion, self.price_input_per_mtok, self.price_output_per_mtok
                )
            return Completion(str(text), prompt, completion, cost)


class AnthropicLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_tokens: int = 1024,
        http_client: Any | None = None,
        price_input_per_mtok: float | None = None,
        price_output_per_mtok: float | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.price_input_per_mtok = price_input_per_mtok
        self.price_output_per_mtok = price_output_per_mtok
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=0, http_client=http_client
        )

    def complete(self, system: str, user: str) -> Completion:
        with observability.generation("llm.chat", model=self.model, input=user) as gen:
            try:
                resp = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
                raise LLMUnavailableError(f"LLM request failed: {exc}") from exc
            except anthropic.APIStatusError as exc:
                if exc.status_code == 429 or exc.status_code >= 500:
                    raise LLMUnavailableError(f"LLM provider returned {exc.status_code}") from exc
                raise LLMRequestError(f"LLM provider returned {exc.status_code}") from exc
            text = "".join(b.text for b in resp.content if b.type == "text")
            if not text.strip():
                raise LLMUnavailableError("LLM returned an empty answer")
            gen.update(
                output=observability.maybe_content(text),
                usage_details=observability.usage_details(
                    resp.usage.input_tokens, resp.usage.output_tokens
                ),
            )
            cost = price_tokens(
                resp.usage.input_tokens,
                resp.usage.output_tokens,
                self.price_input_per_mtok,
                self.price_output_per_mtok,
            )
            return Completion(text, resp.usage.input_tokens, resp.usage.output_tokens, cost)


def get_llm(settings: Settings | None = None) -> LLM:
    """Build the configured chat LLM. Missing keys raise ``LLMConfigError`` (no silent fallback)."""
    cfg = settings or Settings()
    provider, model = cfg.llm_provider, cfg.default_llm_model
    if provider == "openrouter":
        if not cfg.openrouter_api_key:
            raise LLMConfigError("OPENROUTER_API_KEY is required for llm_provider='openrouter'.")
        return OpenAICompatLLM(
            cfg.openrouter_api_key,
            model,
            base_url=cfg.openrouter_base_url,
            report_cost=True,
            price_input_per_mtok=cfg.llm_price_input_per_mtok,
            price_output_per_mtok=cfg.llm_price_output_per_mtok,
        )
    if provider == "openai":
        if not cfg.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is required for llm_provider='openai'.")
        return OpenAICompatLLM(
            cfg.openai_api_key,
            model,
            price_input_per_mtok=cfg.llm_price_input_per_mtok,
            price_output_per_mtok=cfg.llm_price_output_per_mtok,
        )
    if provider == "anthropic":
        if not cfg.anthropic_api_key:
            raise LLMConfigError("ANTHROPIC_API_KEY is required for llm_provider='anthropic'.")
        return AnthropicLLM(
            cfg.anthropic_api_key,
            model,
            price_input_per_mtok=cfg.llm_price_input_per_mtok,
            price_output_per_mtok=cfg.llm_price_output_per_mtok,
        )
    raise LLMConfigError(f"Unknown LLM provider '{provider}'.")
