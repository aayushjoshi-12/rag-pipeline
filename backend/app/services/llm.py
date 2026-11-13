"""Chat-completion clients for answer synthesis (openrouter | openai | anthropic)."""

from __future__ import annotations

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


class LLM(Protocol):
    model: str

    def complete(self, system: str, user: str) -> str: ...


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
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    def complete(self, system: str, user: str) -> str:
        with observability.generation("llm.chat", model=self.model, input=user) as gen:
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_completion_tokens=self.max_tokens,
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
            return str(text)


class AnthropicLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_tokens: int = 1024,
        http_client: Any | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=0, http_client=http_client
        )

    def complete(self, system: str, user: str) -> str:
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
            return text


def get_llm(settings: Settings | None = None) -> LLM:
    """Build the configured chat LLM. Missing keys raise ``LLMConfigError`` (no silent fallback)."""
    cfg = settings or Settings()
    provider, model = cfg.llm_provider, cfg.default_llm_model
    if provider == "openrouter":
        if not cfg.openrouter_api_key:
            raise LLMConfigError("OPENROUTER_API_KEY is required for llm_provider='openrouter'.")
        return OpenAICompatLLM(cfg.openrouter_api_key, model, base_url=cfg.openrouter_base_url)
    if provider == "openai":
        if not cfg.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is required for llm_provider='openai'.")
        return OpenAICompatLLM(cfg.openai_api_key, model)
    if provider == "anthropic":
        if not cfg.anthropic_api_key:
            raise LLMConfigError("ANTHROPIC_API_KEY is required for llm_provider='anthropic'.")
        return AnthropicLLM(cfg.anthropic_api_key, model)
    raise LLMConfigError(f"Unknown LLM provider '{provider}'.")
