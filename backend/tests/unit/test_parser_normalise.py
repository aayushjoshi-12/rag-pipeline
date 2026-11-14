# ruff: noqa: RUF001  (ligature / typographic characters are the test data)
"""The parse seam folds ligatures etc. for every file type, switchable with PARSE_NORMALIZE_TEXT."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import Settings
from app.services import parser as parser_mod
from app.services.parser import parse

RAW = "Please conﬁrm the oﬀer: the ﬁrst oﬃce “opens”."
FOLDED = 'Please confirm the offer: the first office "opens".'


def test_text_and_markdown_files_are_normalised(tmp_path: Path) -> None:
    for name, ft in (("a.txt", "txt"), ("a.md", "md")):
        f = tmp_path / name
        f.write_text(RAW, encoding="utf-8")
        assert parse(f, ft, settings=Settings()).text == FOLDED


def test_switch_off_keeps_the_text_exactly_as_extracted(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text(RAW, encoding="utf-8")
    assert parse(f, "txt", settings=Settings(parse_normalize_text=False)).text == RAW


def test_pdf_text_and_page_texts_are_both_normalised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [RAW + " " + "x" * 60, "Second page: " + RAW + " " + "y" * 60]
    monkeypatch.setattr(parser_mod, "extract_native_texts", lambda path: pages)
    pdf = tmp_path / "f.pdf"
    import fitz

    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    doc.save(str(pdf))
    res = parse(pdf, "pdf", settings=Settings(pdf_text_source="native"))
    assert res.provider == "native"
    assert all("ﬁ" not in t and "ﬀ" not in t for t in res.page_texts)
    assert "confirm" in res.page_texts[0] and "office" in res.page_texts[1]
    assert "ﬁ" not in res.text and "--- PAGE BREAK ---" in res.text
    # joined text equals joining the folded pages (the separator is ASCII)
    assert res.text == "\n\n--- PAGE BREAK ---\n\n".join(res.page_texts)
    raw = parse(pdf, "pdf", settings=Settings(pdf_text_source="native", parse_normalize_text=False))
    assert "ﬁ" in raw.text


def test_setting_default_is_on() -> None:
    assert Settings().parse_normalize_text is True
