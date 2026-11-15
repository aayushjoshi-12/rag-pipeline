# ruff: noqa: RUF001, RUF003  (the ambiguous Unicode characters are the test data)
from __future__ import annotations

import pytest
from app.services.textnorm import normalise_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("conﬁrm", "confirm"),  # fi
        ("ﬂow", "flow"),  # fl
        ("oﬀer", "offer"),  # ff
        ("oﬃce", "office"),  # ffi
        ("baﬄe", "baffle"),  # ffl
        ("ﬅop ﬆop", "stop stop"),  # long-s-t, st
        ("Conﬁrmed oﬀ the ﬁrst ﬂoor", "Confirmed off the first floor"),
    ],
)
def test_ligatures_are_expanded(raw: str, expected: str) -> None:
    assert normalise_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a b", "a b"),  # no-break space
        ("a b c", "a b c"),  # narrow no-break, thin
        ("a　b", "a b"),  # ideographic space
        ("co­op​erate", "cooperate"),  # soft hyphen + zero-width space removed
        ("﻿start", "start"),
        ("borrower’s", "borrower's"),
        ("“quoted” ‘x’", "\"quoted\" 'x'"),
        ("é", "é"),  # combining acute composes (NFC)
    ],
)
def test_typographic_quirks_are_folded(raw: str, expected: str) -> None:
    assert normalise_text(raw) == expected


@pytest.mark.parametrize(
    "unchanged",
    [
        "plain ASCII text, numbers 12345 and symbols !@#$%",
        "",
        "日本語のテキスト、中文文本、한국어 텍스트",  # CJK untouched
        "ＡＢＣ１２３ ｱｲｳ",  # full-width / half-width forms: NFKC would rewrite these; we do not
        "area 25 m², volume 3 m³, note¹",  # superscripts keep their meaning
        "½ cup and ¾ mile",  # vulgar fractions (NFKC -> '1⁄2')
        "Brand™ © 2026 ® No№ 5",  # trade mark, copyright, numero
        "wait… then – or — stop",  # ellipsis and dashes
        "100°C and 5 µm",  # degree sign, micro sign
        "① first, Ⅳ fourth",  # circled digit, roman numeral
        "café naïve",  # already composed
    ],
)
def test_text_that_must_not_change(unchanged: str) -> None:
    assert normalise_text(unchanged) == unchanged


def test_idempotent_and_lengths() -> None:
    raw = "conﬁrm “it” now ½"
    once = normalise_text(raw)
    assert normalise_text(once) == once
    assert normalise_text("ASCII only") is not None


def test_nfkc_would_have_changed_these() -> None:
    """Documents *why* a targeted map was chosen over NFKC (the decision in the module docstring)."""
    import unicodedata

    for ch in ("²", "½", "™", "Ａ", "…", "µ"):
        assert unicodedata.normalize("NFKC", ch) != ch
        assert normalise_text(ch) == ch
