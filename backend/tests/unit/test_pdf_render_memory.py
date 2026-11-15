"""Scanned PDFs render one page at a time (ticket 48): a 200-page scan used to hold ~5 GB of page images."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from app.config import Settings
from app.services import parser, pdf_pages
from app.services.backends import BaseBackend
from PIL import Image


def _scan_pdf(path: Path, pages: int) -> Path:
    src = pymupdf.open()
    src.new_page().insert_text((50, 60), "scan")
    png = src.load_page(0).get_pixmap(dpi=40).tobytes("png")
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, stream=png)  # image only: no text layer
    doc.save(str(path))
    return path


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


def test_render_page_shrinks_the_mupdf_image_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without the shrink a 60-page scan held ~600 MB RSS after rendering (flat ~115 MB with it).
    calls: list[int] = []
    monkeypatch.setattr(pymupdf.TOOLS, "store_shrink", lambda percent: calls.append(percent))
    img = pdf_pages.render_page(_scan_pdf(tmp_path / "s.pdf", 2), 1, 72)
    assert img.size[0] > 0 and calls == [100]
    assert pdf_pages.page_count(tmp_path / "s.pdf") == 2


def test_render_page_rejects_bad_dpi(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dpi"):
        pdf_pages.render_page(_scan_pdf(tmp_path / "s.pdf", 1), 0, 0)
