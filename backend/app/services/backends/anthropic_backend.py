"""Vision backend for the Anthropic Messages API."""

from __future__ import annotations

from typing import Any

import anthropic
from PIL import Image

from app.services import observability
from app.services.backends.base import BaseBackend, image_to_png_base64
from app.services.parse_errors import BackendCallError, BackendRequestError


class AnthropicBackend(BaseBackend):
    def __init__(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        timeout_seconds: float = 120.0,
        max_tokens: int = 8192,
        http_client: Any | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,  # retries are owned by services.retry, per page
            http_client=http_client,
        )

    @property
    def name(self) -> str:
        return "anthropic"

    def extract_text(self, image: Image.Image) -> str:
        with observability.generation(
            f"{self.name}.extract_text", model=self.model, input=self.system_prompt
        ) as gen:
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self.system_prompt,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": image_to_png_base64(image),
                                    },
                                }
                            ],
                        }
                    ],
                )
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
                raise BackendCallError(f"{self.name} request failed: {exc}") from exc
            except anthropic.APIStatusError as exc:
                if exc.status_code == 429 or exc.status_code >= 500:
                    raise BackendCallError(f"{self.name} returned {exc.status_code}") from exc
                raise BackendRequestError(f"{self.name} returned {exc.status_code}: {exc}") from exc

            text = "".join(b.text for b in response.content if b.type == "text")
            if not text.strip():
                raise BackendCallError(f"{self.name} returned an empty transcription")

            gen.update(
                output=observability.maybe_content(text),
                usage_details=observability.usage_details(
                    response.usage.input_tokens, response.usage.output_tokens
                ),
            )
            return text
