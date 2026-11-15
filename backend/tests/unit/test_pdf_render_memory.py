"""Scanned PDFs render one page at a time (ticket 48): a 200-page scan used to hold ~5 GB of page images."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
import pytest
from app.config import Settings
from app.services import parser, pdf_pages
from app.services.backends import BaseBackend
from PIL import Image, ImageDraw
from tests.pdfmaker import write_pdf


def _scan_pdf(path: Path, pages: int) -> Path:
    img = Image.new("RGB", (200, 120), "white")
    ImageDraw.Draw(img).rectangle((10, 10, 150, 60), fill="black")
    return write_pdf(path, [img] * pages)  # image only: no text layer


class _Recorder(BaseBackend):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @property
    def name(self) -> str:
        return "recorder"

    def extract_text(self, image: Image.Image) -> str:
        self.events.append("extract")
        return "x"


def test_parser_renders_and_extracts_page_by_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    real = parser.render_page

    def spy(path: Path, index: int, dpi: int) -> Image.Image:
        events.append(f"render{index}")
        return real(path, index, dpi)

    monkeypatch.setattr(parser, "render_page", spy)
    result = parser.parse(_scan_pdf(tmp_path / "s.pdf", 3), "pdf", settings=Settings(), backend=_Recorder(events))
    assert result.page_count == 3
    # all-at-once rendering would give render0 render1 render2 extract extract extract
    assert events == ["render0", "extract", "render1", "extract", "render2", "extract"]


class _Tracker:
    """Records close() calls of the pdfium objects pdf_pages opens."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.closed: list[str] = []
        for cls in (pdfium.PdfDocument, pdfium.PdfPage, pdfium.PdfTextPage, pdfium.PdfBitmap):
            original = cls.close

            def close(obj: object, _orig: object = original, _name: str = cls.__name__) -> None:
                self.closed.append(_name)
                _orig(obj)  # type: ignore[operator]  # test shim around the unbound pypdfium2 close()

            monkeypatch.setattr(cls, "close", close)


def test_render_page_closes_document_page_and_bitmap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Handles must be released explicitly: relying on the GC is what let the old renderer pile up memory.
    pdf = _scan_pdf(tmp_path / "s.pdf", 2)
    tracker = _Tracker(monkeypatch)
    img = pdf_pages.render_page(pdf, 1, 72)
    assert img.size[0] > 0 and img.mode == "RGB"
    assert sorted(tracker.closed) == ["PdfBitmap", "PdfDocument", "PdfPage"]
    assert pdf_pages.page_count(pdf) == 2


def test_native_text_closes_every_page_and_text_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = write_pdf(tmp_path / "t.pdf", ["alpha", "beta", None])
    tracker = _Tracker(monkeypatch)
    assert pdf_pages.extract_native_texts(pdf) == ["alpha", "beta", ""]
    assert tracker.closed.count("PdfPage") == 3 and tracker.closed.count("PdfTextPage") == 3
    assert tracker.closed.count("PdfDocument") == 1


def test_render_page_honours_dpi(tmp_path: Path) -> None:
    pdf = _scan_pdf(tmp_path / "s.pdf", 1)
    low = pdf_pages.render_page(pdf, 0, 72)
    high = pdf_pages.render_page(pdf, 0, 144)
    assert low.size == (595, 842)  # A4 at 72 dpi = the page size in points
    assert high.size == (1190, 1684)


def test_render_page_rejects_bad_dpi(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dpi"):
        pdf_pages.render_page(_scan_pdf(tmp_path / "s.pdf", 1), 0, 0)
