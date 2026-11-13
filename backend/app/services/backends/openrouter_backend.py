"""OpenRouter backend — OpenRouter speaks the OpenAI chat-completions API (ADR-0002)."""

from __future__ import annotations

from typing import Any

from app.services.backends.openai_backend import OpenAIBackend


class OpenRouterBackend(OpenAIBackend):
    def __init__(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 120.0,
        max_completion_tokens: int | None = None,
        http_client: Any | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_completion_tokens=max_completion_tokens,
            http_client=http_client,
        )

    @property
    def name(self) -> str:
        return "openrouter"
