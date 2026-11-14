"""PDF text-layer extraction: born-digital pages skip OCR/vision; scanned pages still use it."""

from __future__ import annotations

import io
from pathlib import Path

import fitz
import pytest
from app.config import Settings
from app.services.backends import BaseBackend
from app.services.parse_errors import DocumentLoadError
from app.services.parser import parse
from PIL import Image, ImageDraw


class CountingBackend(BaseBackend):
    def __init__(self, text: str = "OCR TEXT") -> None:
        self.calls = 0
        self._text = text

    @property
    def name(self) -> str:
        return "fake"

    def extract_text(self, image: Image.Image) -> str:
        self.calls += 1
        return self._text


LONG = "The borrower must document twelve months of payment history on the account. " * 2


def _scan_png() -> bytes:
    img = Image.new("RGB", (300, 120), "white")
    ImageDraw.Draw(img).rectangle((20, 20, 200, 60), fill="black")  # pixels only, no text layer
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_pdf(path: Path, pages: list[str | None]) -> Path:
    """Text page for a string, image-only (scanned) page for None."""
    doc = fitz.open()
    for content in pages:
        page = doc.new_page()
        if content is None:
            page.insert_image(fitz.Rect(72, 72, 372, 192), stream=_scan_png())
        else:
            page.insert_text((72, 72), content)
    doc.save(str(path))
    doc.close()
    return path


def test_auto_uses_the_text_layer_and_never_calls_a_backend(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "born-digital.pdf", [LONG, LONG.upper()])
    backend = CountingBackend()
    # no API key configured: a text PDF must not need one
    res = parse(pdf, "pdf", settings=Settings(openrouter_api_key="", parser_provider="openrouter"))
    assert res.provider == "native"
    assert res.page_count == 2 and len(res.page_texts) == 2
    assert "twelve months of payment history" in res.page_texts[0]
    assert "TWELVE MONTHS" in res.page_texts[1]
    assert backend.calls == 0


def test_auto_falls_back_per_page_for_scanned_pages_only(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "mixed.pdf", [LONG, None, LONG])
    backend = CountingBackend("scanned page text")
    res = parse(pdf, "pdf", settings=Settings(), backend=backend)
    assert backend.calls == 1  # only the image-only page
    assert res.provider == "native+fake"
    assert res.page_texts[1] == "scanned page text"
    assert "twelve months" in res.page_texts[0] and "twelve months" in res.page_texts[2]


def test_short_text_layer_counts_as_scanned(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "thin.pdf", ["p. 3"])  # page number / stray label, no real text
    backend = CountingBackend("ocr says hello")
    res = parse(pdf, "pdf", settings=Settings(pdf_native_min_chars=40), backend=backend)
    assert backend.calls == 1 and res.page_texts == ["ocr says hello"]
    res2 = parse(pdf, "pdf", settings=Settings(pdf_native_min_chars=1), backend=CountingBackend())
    assert res2.provider == "native" and "p. 3" in res2.text


def test_native_mode_never_calls_a_backend_and_leaves_scans_empty(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "scan-only.pdf", [None])
    backend = CountingBackend()
    res = parse(pdf, "pdf", settings=Settings(pdf_text_source="native"), backend=backend)
    assert backend.calls == 0 and res.text.strip() == "" and res.provider == "native"


def test_image_mode_ignores_the_text_layer(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "born-digital.pdf", [LONG])
    backend = CountingBackend("from the image")
    res = parse(pdf, "pdf", settings=Settings(pdf_text_source="image"), backend=backend)
    assert backend.calls == 1 and res.provider == "fake" and res.text == "from the image"


def test_corrupt_pdf_is_a_document_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 definitely not a pdf")
    with pytest.raises(DocumentLoadError):
        parse(bad, "pdf", settings=Settings())


def test_settings_validation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="pdf_text_source"):
        Settings(pdf_text_source="magic")
    with pytest.raises(ValidationError, match="pdf_native_min_chars"):
        Settings(pdf_native_min_chars=-1)
    assert Settings().pdf_text_source == "auto"
