"""Vision backend for OpenAI-compatible chat-completions APIs (OpenAI, OpenRouter, ...)."""

from __future__ import annotations

from typing import Any

import openai
from PIL import Image

from app.services import observability
from app.services.backends.base import BaseBackend, image_to_png_base64
from app.services.parse_errors import BackendCallError, BackendRequestError


class OpenAIBackend(BaseBackend):
    def __init__(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        max_completion_tokens: int | None = None,
        # Any: the openai stubs type this as the SDK's bundled transport client, while tests
        # inject a plain httpx.Client so respx can intercept requests.
        http_client: Any | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.max_completion_tokens = max_completion_tokens
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,  # retries are owned by services.retry, per page
            http_client=http_client,
        )

    @property
    def name(self) -> str:
        return "openai"

    def extract_text(self, image: Image.Image) -> str:
        data_url = f"data:image/png;base64,{image_to_png_base64(image)}"
        params: dict[str, Any] = {}
        if self.max_completion_tokens is not None:
            params["max_completion_tokens"] = self.max_completion_tokens

        with observability.generation(
            f"{self.name}.extract_text", model=self.model, input=self.system_prompt
        ) as gen:
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {
                            "role": "user",
                            "content": [{"type": "image_url", "image_url": {"url": data_url}}],
                        },
                    ],
                    **params,
                )
            except (openai.APITimeoutError, openai.APIConnectionError) as exc:
                raise BackendCallError(f"{self.name} request failed: {exc}") from exc
            except openai.APIStatusError as exc:
                if exc.status_code == 429 or exc.status_code >= 500:
                    raise BackendCallError(f"{self.name} returned {exc.status_code}") from exc
                raise BackendRequestError(f"{self.name} returned {exc.status_code}: {exc}") from exc

            if not response.choices:
                raise BackendCallError(f"{self.name} returned no completion choices")
            text = response.choices[0].message.content
            if not text or not text.strip():
                raise BackendCallError(f"{self.name} returned an empty transcription")

            usage = response.usage
            gen.update(
                output=observability.maybe_content(text),
                usage_details=observability.usage_details(
                    usage.prompt_tokens if usage else None,
                    usage.completion_tokens if usage else None,
                ),
            )
            return str(text)
