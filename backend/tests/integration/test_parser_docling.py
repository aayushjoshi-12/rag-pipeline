"""Integration tests for the parse seam against real local tools.

DOCX goes through the real docling pipeline (synthetic fixture, no external calls).
OCR uses real Tesseract (auto-skips when tesseract isn't installed). These are local
dependencies of the stack, not external paid APIs — same tier as the Postgres/Chroma/
Ollama integration tests.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from app.config import Settings
from app.services.parse_errors import DocumentLoadError
from app.services.parser import parse
from tests.pdfmaker import write_pdf


def test_parse_docx_via_docling_returns_markdown(synthetic_docx: Path) -> None:
    result = parse(synthetic_docx, "docx", settings=Settings())
    assert result.format == "markdown"
    assert result.provider == "docling"
    assert "Hello RAG Parser" in result.text
    assert "12345" in result.text
    assert not result.page_texts


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_parse_pdf_via_real_tesseract_ocr(tmp_path: Path) -> None:
    pdf = write_pdf(tmp_path / "ocr.pdf", ["RAG pipeline OCR smoke 2026"])

    result = parse(pdf, "pdf", settings=Settings(parser_provider="ocr"))

    assert result.provider == "ocr"
    assert result.page_count == 1
    assert len(result.page_texts) == 1
    assert "2026" in result.text


def test_parse_pdf_without_real_backend_raises_for_missing_openrouter_key(tmp_path: Path) -> None:
    pdf = write_pdf(tmp_path / "vision.pdf", [None])

    from app.services.parse_errors import InvalidBackendError

    with pytest.raises(InvalidBackendError, match="OPENROUTER_API_KEY"):
        parse(pdf, "pdf", settings=Settings(openrouter_api_key=""))


def test_parse_docx_document_load_error_for_binary_garbage(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.docx"
    bogus.write_bytes(b"this is not a zip file at all")
    with pytest.raises(DocumentLoadError, match="docling"):
        parse(bogus, "docx", settings=Settings())
