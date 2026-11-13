"""Parser backend selection — maps app ``Settings`` onto backend constructor args."""

from __future__ import annotations

from app.config import Settings
from app.services.backends.anthropic_backend import AnthropicBackend
from app.services.backends.base import BaseBackend
from app.services.backends.ocr_backend import OCRBackend
from app.services.backends.openai_backend import OpenAIBackend
from app.services.backends.openrouter_backend import OpenRouterBackend
from app.services.parse_errors import InvalidBackendError

_settings = Settings()

VALID_PARSER_PROVIDERS = ("openrouter", "openai", "anthropic", "ocr")


def get_parser_backend(
    provider: str | None = None,
    settings: Settings | None = None,
) -> BaseBackend:
    """Return the text-extraction backend for the configured parser provider.

    ``provider``/``settings`` default to ``Settings`` when omitted. Never silently swaps a
    missing provider key — missing keys raise so misconfig surfaces immediately.
    """
    cfg = settings or _settings
    provider = provider or cfg.parser_provider

    if provider not in VALID_PARSER_PROVIDERS:
        raise InvalidBackendError(
            f"Unknown parser backend '{provider}'. Supported: {', '.join(VALID_PARSER_PROVIDERS)}."
        )

    if provider == "openrouter":
        if not cfg.openrouter_api_key:
            raise InvalidBackendError(
                "OPENROUTER_API_KEY is required for the 'openrouter' parser backend."
            )
        return OpenRouterBackend(
            api_key=cfg.openrouter_api_key,
            model=cfg.parser_llm_model,
            system_prompt=cfg.parser_system_prompt,
            base_url=cfg.openrouter_base_url,
            timeout_seconds=cfg.parser_timeout_seconds,
            max_completion_tokens=cfg.parser_max_completion_tokens,
        )

    if provider == "openai":
        if not cfg.openai_api_key:
            raise InvalidBackendError("OPENAI_API_KEY is required for the 'openai' parser backend.")
        return OpenAIBackend(
            api_key=cfg.openai_api_key,
            model=cfg.parser_llm_model,
            system_prompt=cfg.parser_system_prompt,
            timeout_seconds=cfg.parser_timeout_seconds,
            max_completion_tokens=cfg.parser_max_completion_tokens,
        )

    if provider == "anthropic":
        if not cfg.anthropic_api_key:
            raise InvalidBackendError(
                "ANTHROPIC_API_KEY is required for the 'anthropic' parser backend."
            )
        return AnthropicBackend(
            api_key=cfg.anthropic_api_key,
            model=cfg.parser_llm_model,
            system_prompt=cfg.parser_system_prompt,
            timeout_seconds=cfg.parser_timeout_seconds,
        )

    if provider == "ocr":
        return OCRBackend(tesseract_cmd=cfg.tesseract_cmd, lang=cfg.ocr_lang)

    # Unreachable — VALID_PARSER_PROVIDERS guard above.
    raise InvalidBackendError(f"Unhandled parser backend '{provider}'.")  # pragma: no cover


__all__ = [
    "VALID_PARSER_PROVIDERS",
    "AnthropicBackend",
    "BaseBackend",
    "OCRBackend",
    "OpenAIBackend",
    "OpenRouterBackend",
    "get_parser_backend",
]
