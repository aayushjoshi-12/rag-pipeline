"""Unit tests — Anthropic backend, HTTP error mapping, PDF renderer, OCR failure mode."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import httpx2
import pytest
import respx
from app.services.backends import AnthropicBackend, OCRBackend, OpenRouterBackend
from app.services.parse_errors import BackendCallError, BackendRequestError
from app.services.pdf_pages import page_count, render_page
from httpx import Response
from PIL import Image

_OR_URL = "https://openrouter.ai/api/v1/chat/completions"


def _image() -> Image.Image:
    return Image.new("RGB", (4, 4), color=(255, 255, 255))


def _openrouter() -> OpenRouterBackend:
    return OpenRouterBackend(
        api_key="sk-or-test",
        model="m",
        system_prompt="p",
        http_client=httpx.Client(timeout=30.0),
    )


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_http_statuses_are_retryable(status: int) -> None:
    respx.post(_OR_URL).mock(return_value=Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(BackendCallError):
        _openrouter().extract_text(_image())


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403])
def test_client_errors_are_not_retryable(status: int) -> None:
    respx.post(_OR_URL).mock(return_value=Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(BackendRequestError):
        _openrouter().extract_text(_image())


@respx.mock
def test_connection_failure_is_retryable() -> None:
    respx.post(_OR_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(BackendCallError):
        _openrouter().extract_text(_image())


def _anthropic_message(content: list[dict[str, str]]) -> dict[str, object]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }


def _anthropic(handler: Callable[[httpx2.Request], httpx2.Response]) -> AnthropicBackend:
    # The anthropic SDK (like openai) bundles httpx2, which respx cannot intercept, so mock
    # at the transport level with httpx2's own MockTransport.
    return AnthropicBackend(
        api_key="sk-ant-test",
        model="claude-test",
        system_prompt="transcribe",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


def test_anthropic_backend_sends_image_block_and_returns_text() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200, json=_anthropic_message([{"type": "text", "text": "page text"}])
        )

    assert _anthropic(handler).extract_text(_image()) == "page text"

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "claude-test"
    assert body["system"] == "transcribe"
    block = body["messages"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"


def test_anthropic_backend_empty_reply_is_retryable() -> None:
    backend = _anthropic(lambda _r: httpx2.Response(200, json=_anthropic_message([])))
    with pytest.raises(BackendCallError, match="empty transcription"):
        backend.extract_text(_image())


@pytest.mark.parametrize(("status", "exc"), [(529, BackendCallError), (401, BackendRequestError)])
def test_anthropic_backend_maps_http_statuses(status: int, exc: type[Exception]) -> None:
    backend = _anthropic(
        lambda _r: httpx2.Response(
            status, json={"type": "error", "error": {"type": "x", "message": "m"}}
        )
    )
    with pytest.raises(exc):
        backend.extract_text(_image())


def test_pdf_renderer_renders_one_image_per_page(tmp_path: Path) -> None:
    import pymupdf

    pdf = tmp_path / "two.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    doc.save(str(pdf))
    doc.close()

    assert page_count(pdf) == 2
    low = [render_page(pdf, i, 72) for i in range(2)]
    high = [render_page(pdf, i, 144) for i in range(2)]
    assert len(low) == len(high) == 2
    assert high[0].width == low[0].width * 2


def test_pdf_renderer_rejects_bad_dpi() -> None:
    with pytest.raises(ValueError, match="dpi"):
        render_page(Path("unused.pdf"), 0, 0)


def test_ocr_missing_tesseract_is_a_permanent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.backends.ocr_backend as ocr_mod

    def boom(*_: object, **__: object) -> str:
        raise ocr_mod.pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(ocr_mod.pytesseract, "image_to_string", boom)
    with pytest.raises(BackendRequestError, match="Tesseract"):
        OCRBackend().extract_text(_image())
