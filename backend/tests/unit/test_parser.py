"""Unit tests for the parse seam — filetype routing, PDF/OCR/vision dispatch, DOCX→markdown.

DOCX conversion runs the real docling pipeline against a synthetic .docx fixture (docling
is a hard runtime dependency of the stack, not an external API).
"""

from __future__ import annotations

from pathlib import Path

import app.services.parser as parser_mod
import pytest
from app.config import Settings
from app.services.backends import BaseBackend
from app.services.parse_errors import DocumentLoadError, UnsupportedFileTypeError
from app.services.parser import parse
from PIL import Image


class FakeBackend(BaseBackend):
    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def name(self) -> str:
        return "fake"

    def extract_text(self, image: Image.Image) -> str:
        return self._text


def _make_pdf(path: Path, lines: list[str]) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "\n".join(lines))
    doc.save(str(path))
    doc.close()


def test_parse_routes_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_pdf(path, cfg, provider, backend) -> parser_mod.ParseResult:
        captured["path"] = path
        captured["cfg"] = cfg
        return parser_mod.ParseResult(
            text="pdf-text", format="text", filetype="pdf", provider="ocr"
        )

    monkeypatch.setattr(parser_mod, "_parse_pdf", fake_pdf)
    result = parse(Path("f.pdf"), "pdf")
    assert result.text == "pdf-text"
    assert captured["path"] == Path("f.pdf")


def test_parse_routes_docx(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_docx(path, cfg) -> parser_mod.ParseResult:
        captured["path"] = path
        return parser_mod.ParseResult(
            text="md", format="markdown", filetype="docx", provider="docling"
        )

    monkeypatch.setattr(parser_mod, "_parse_docx", fake_docx)
    result = parse(Path("f.docx"), "docx")
    assert result.provider == "docling"
    assert captured["path"] == Path("f.docx")


@pytest.mark.parametrize("filetype", ["txt", "png", "bogus", ""])
def test_parse_rejects_unsupported_filetype(filetype: str) -> None:
    with pytest.raises(UnsupportedFileTypeError, match="Unsupported file type"):
        parse(Path("f"), filetype)


def test_parse_filetype_case_and_dot_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_pdf(path, cfg, provider, backend) -> parser_mod.ParseResult:
        seen.append(str(path))
        return parser_mod.ParseResult(text="t", format="text", filetype="pdf", provider="ocr")

    monkeypatch.setattr(parser_mod, "_parse_pdf", fake_pdf)
    parse(Path("f.PDF"), "PDF")
    parse(Path("f.pdf"), ".pdf")
    assert seen == ["f.PDF", "f.pdf"]


def test_parse_docx_returns_markdown(synthetic_docx: Path) -> None:
    result = parse(synthetic_docx, "docx")
    assert result.format == "markdown"
    assert result.provider == "docling"
    assert result.filetype == "docx"
    assert "Hello RAG Parser" in result.text
    assert "12345" in result.text


def test_parse_docx_unknown_reader_raises(synthetic_docx: Path) -> None:
    settings = Settings.model_construct(docx_reader="bogus")
    with pytest.raises(DocumentLoadError, match="Unknown DOCX reader"):
        parse(synthetic_docx, "docx", settings=settings)


def test_parse_pdf_runs_backend_over_pages_and_captures_pages(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    _make_pdf(pdf, ["Alpha line", "Beta line"])

    backend = FakeBackend("page-ocr-text")
    result = parse(pdf, "pdf", backend=backend)

    assert result.format == "text"
    assert result.provider == "fake"
    assert result.page_count == 1
    assert result.page_texts == ["page-ocr-text"]
    assert result.text == "page-ocr-text"


def test_parse_pdf_multi_page_captures_per_page(tmp_path: Path) -> None:
    pdf = tmp_path / "multi.pdf"
    import fitz

    doc = fitz.open()
    for _line in range(3):
        doc.new_page()
    doc.save(str(pdf))
    doc.close()

    class PageNumberedBackend(BaseBackend):
        def __init__(self) -> None:
            self._call = 0

        @property
        def name(self) -> str:
            return "paged"

        def extract_text(self, image: Image.Image) -> str:
            self._call += 1
            return f"page-{self._call}"

    from app.services.parser import _PAGE_SEPARATOR

    result = parse(pdf, "pdf", backend=PageNumberedBackend())
    assert result.page_count == 3
    assert result.page_texts == ["page-1", "page-2", "page-3"]
    assert result.text == _PAGE_SEPARATOR.join(["page-1", "page-2", "page-3"])


def test_parse_broken_pdf_raises_document_load_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a real pdf")
    with pytest.raises(DocumentLoadError, match="Failed to load PDF"):
        parse(broken, "pdf", backend=FakeBackend("ignored"))


def test_parse_pdf_retries_transient_backend_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import retry as retry_mod
    from app.services.parse_errors import BackendCallError

    monkeypatch.setattr(retry_mod, "wait_exponential", lambda **_: 0)

    pdf = tmp_path / "flaky.pdf"
    _make_pdf(pdf, ["recovers after retries"])
    calls: list[int] = []

    class FlakyBackend(BaseBackend):
        @property
        def name(self) -> str:
            return "flaky"

        def extract_text(self, image: Image.Image) -> str:
            calls.append(1)
            if len(calls) < 3:
                raise BackendCallError("transient upstream empty reply")
            return "recovered"

    result = parse(pdf, "pdf", backend=FlakyBackend())
    assert result.text == "recovered"
    assert len(calls) == 3


def test_parse_pdf_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services import retry as retry_mod
    from app.services.parse_errors import BackendCallError

    monkeypatch.setattr(retry_mod, "wait_exponential", lambda **_: 0)

    pdf = tmp_path / "always-flaky.pdf"
    _make_pdf(pdf, ["never"])
    calls: list[int] = []

    class AlwaysFlaky(BaseBackend):
        @property
        def name(self) -> str:
            return "flaky"

        def extract_text(self, image: Image.Image) -> str:
            calls.append(1)
            raise BackendCallError("transient")

    with pytest.raises(DocumentLoadError, match="flaky backend failed"):
        parse(pdf, "pdf", backend=AlwaysFlaky())
    assert len(calls) == 3
