"""Unit tests for parser backend selection + the vision/OCR backends."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx
from app.config import Settings
from app.services.backends import AnthropicBackend, OCRBackend, OpenAIBackend, get_parser_backend
from app.services.backends.openrouter_backend import OpenRouterBackend
from app.services.parse_errors import BackendCallError, InvalidBackendError
from httpx import Response
from PIL import Image


def _image() -> Image.Image:
    img = Image.new("RGB", (4, 4), color=(255, 255, 255))
    img.load()
    return img


def _test_http_client() -> httpx.Client:
    # openai SDK ≥3 uses its bundled httpx2 transport, which respx cannot intercept —
    # inject a plain httpx.Client so the mocked route is actually used.
    return httpx.Client(timeout=60.0)


def test_factory_returns_openrouter_by_default() -> None:
    backend = get_parser_backend(
        provider="openrouter",
        settings=Settings(openrouter_api_key="sk-or-test"),
    )
    assert isinstance(backend, OpenRouterBackend)
    assert isinstance(backend, OpenAIBackend)
    assert backend.name == "openrouter"
    assert backend.model == Settings().parser_llm_model


def test_factory_returns_openai() -> None:
    backend = get_parser_backend(
        provider="openai",
        settings=Settings(openai_api_key="sk-test"),
    )
    assert isinstance(backend, OpenAIBackend)
    assert backend.name == "openai"


def test_factory_returns_anthropic() -> None:
    backend = get_parser_backend(
        provider="anthropic",
        settings=Settings(anthropic_api_key="sk-ant-test"),
    )
    assert isinstance(backend, AnthropicBackend)
    assert backend.name == "anthropic"


def test_factory_returns_ocr_with_config_knobs() -> None:
    backend = get_parser_backend(
        provider="ocr",
        settings=Settings(tesseract_cmd="/usr/bin/tesseract", ocr_lang="eng"),
    )
    assert isinstance(backend, OCRBackend)
    assert backend.name == "ocr"


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(InvalidBackendError, match="Unknown parser backend"):
        get_parser_backend(provider="gemini", settings=Settings())


def test_factory_requires_openrouter_key() -> None:
    with pytest.raises(InvalidBackendError, match="OPENROUTER_API_KEY"):
        get_parser_backend(provider="openrouter", settings=Settings(openrouter_api_key=""))


def test_factory_requires_openai_key() -> None:
    with pytest.raises(InvalidBackendError, match="OPENAI_API_KEY"):
        get_parser_backend(provider="openai", settings=Settings(openai_api_key=""))


def test_factory_requires_anthropic_key() -> None:
    with pytest.raises(InvalidBackendError, match="ANTHROPIC_API_KEY"):
        get_parser_backend(provider="anthropic", settings=Settings(anthropic_api_key=""))


def test_openrouter_backend_carries_openrouter_config(tmp_path) -> None:
    settings = Settings(
        openrouter_api_key="sk-or-test",
        openrouter_base_url="https://custom.example/v1",
        parser_llm_model="free/model",
        parser_system_prompt="transcribe",
        parser_timeout_seconds=12.0,
    )
    backend = get_parser_backend(provider="openrouter", settings=settings)
    assert isinstance(backend, OpenRouterBackend)
    assert backend.model == "free/model"


def test_ocr_backend_calls_tesseract_with_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.backends.ocr_backend as ocr_mod

    captured: dict[str, object] = {}

    def fake_image_to_string(image, lang: str = "eng") -> str:
        captured["lang"] = lang
        return "ocr-text"

    monkeypatch.setattr(ocr_mod.pytesseract, "image_to_string", fake_image_to_string)
    backend = OCRBackend(tesseract_cmd="/usr/bin/tesseract", lang="eng")
    assert backend.extract_text(_image()) == "ocr-text"
    assert captured["lang"] == "eng"


@respx.mock
def test_openai_backend_extracts_text_via_chat_completions() -> None:
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"content": "extracted text"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )
    )

    backend = OpenAIBackend(
        api_key="sk-test",
        model="gpt-test",
        system_prompt="transcribe me",
        http_client=_test_http_client(),
    )
    text = backend.extract_text(_image())

    assert text == "extracted text"
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "gpt-test"
    assert body["messages"][0]["content"] == "transcribe me"
    img_url = body["messages"][1]["content"][0]["image_url"]["url"]
    assert img_url.startswith("data:image/png;base64,")
    base64.b64decode(img_url.split(",", 1)[1])


@respx.mock
def test_openrouter_backend_points_at_openrouter_base_url() -> None:
    url = "https://openrouter.ai/api/v1/chat/completions"
    route = respx.post(url).mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"content": "vision text"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )
    )

    backend = OpenRouterBackend(
        api_key="sk-or-test",
        model="free/model",
        system_prompt="transcribe",
        base_url="https://openrouter.ai/api/v1",
        http_client=_test_http_client(),
    )
    text = backend.extract_text(_image())

    assert text == "vision text"
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "free/model"


@respx.mock
def test_openrouter_backend_sends_configured_max_completion_tokens() -> None:
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={"choices": [{"message": {"content": "vision text", "role": "assistant"}}]},
        )
    )
    backend = OpenRouterBackend(
        api_key="sk-or-test",
        model="free/model",
        system_prompt="transcribe",
        base_url="https://openrouter.ai/api/v1",
        max_completion_tokens=16032,
        http_client=_test_http_client(),
    )
    assert backend.extract_text(_image()) == "vision text"
    body = json.loads(route.calls.last.request.content)
    assert body["max_completion_tokens"] == 16032


@respx.mock
def test_openrouter_backend_raises_retryable_on_missing_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(200, json={})
    )

    backend = OpenRouterBackend(
        api_key="sk-or-test",
        model="free/model",
        system_prompt="transcribe",
        base_url="https://openrouter.ai/api/v1",
        http_client=_test_http_client(),
    )
    with pytest.raises(BackendCallError, match="no completion choices"):
        backend.extract_text(_image())


@respx.mock
def test_openrouter_backend_raises_retryable_on_empty_content() -> None:
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"content": None, "refusal": None, "role": "assistant"}}]
            },
        )
    )

    backend = OpenRouterBackend(
        api_key="sk-or-test",
        model="nvidia/x",
        system_prompt="transcribe",
        base_url="https://openrouter.ai/api/v1",
        http_client=_test_http_client(),
    )
    with pytest.raises(BackendCallError, match="empty transcription"):
        backend.extract_text(_image())
