"""Normalise extracted text before chunking (ticket 33).

PDF text layers carry typographic glyphs that are the same *word* to a reader but different
*characters* to a search index: ``conﬁrm`` (U+FB01) never matches the query ``confirm`` in BM25 / FTS
and tokenises differently for the embedder. This folds only what is safe to fold.

Why a targeted map and not blanket NFKC (decision, ticket 33)
-------------------------------------------------------------
NFKC folds the ligatures, but it also rewrites characters whose meaning matters in real documents:

* superscripts ``m²`` -> ``m2``, ``x³`` -> ``x3``, footnote marks ``word¹`` -> ``word1`` (changes units,
  merges a footnote number into the word);
* vulgar fractions ``½`` -> ``1⁄2`` (digit + U+2044 FRACTION SLASH: not what anyone types, matches nothing);
* ``™`` -> ``TM``, ``№`` -> ``No``, ``…`` -> ``...``, ``µ`` (micro sign) -> Greek ``μ``, ``㎏`` -> ``kg``,
  circled/roman numerals ``①`` -> ``1``, ``Ⅳ`` -> ``IV``, ordinal ``ª`` -> ``a``;
* full-width forms ``Ａ１`` -> ``A1`` (usually desirable in CJK text, but it also rewrites
  half-width katakana to full-width, i.e. edits the source language text).

and NFKC does *not* touch the curly quotes / NBSP-like spaces that also hurt matching. So we apply:

1. the Latin ligatures U+FB00..FB06 (ff, fi, fl, ffi, ffl, long-s-t, st) -> their letters;
2. whitespace variants (no-break, narrow no-break, thin, hair, figure, ideographic... spaces) -> one space;
3. invisible junk that splits words: soft hyphen U+00AD, zero-width space/joiner/non-joiner, word joiner, BOM -> removed;
4. curly quotes and apostrophes -> ASCII ``'`` / ``"`` (what users type);
5. canonical composition (NFC): ``e`` + combining acute -> ``é`` (lossless, canonical-equivalent only).

Everything else is left exactly as extracted: CJK, full-width forms, superscripts, fractions, symbols,
dashes (en/em dashes carry meaning and the evaluation matcher already folds them).
"""

# ruff: noqa: RUF001, RUF002  (this module is about ambiguous Unicode characters; they are the data)
from __future__ import annotations

import unicodedata

_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",  # long s + t
    "ﬆ": "st",
}
_SPACES = {
    ord(c): " "
    for c in (
        " ",  # no-break space
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",  # figure space
        " ",
        " ",  # thin
        " ",  # hair
        " ",  # narrow no-break
        " ",
        "　",  # ideographic space
    )
}
_INVISIBLE = {ord(c): None for c in ("­", "​", "‌", "‍", "⁠", "﻿")}
_QUOTES = {
    ord("‘"): "'",
    ord("’"): "'",
    ord("‚"): "'",
    ord("‛"): "'",
    ord("′"): "'",  # prime, often used as an apostrophe in PDFs
    ord("“"): '"',
    ord("”"): '"',
    ord("„"): '"',
    ord("‟"): '"',
}

_TABLE: dict[int, str | None] = {
    **{ord(k): v for k, v in _LIGATURES.items()},
    **_SPACES,
    **_INVISIBLE,
    **_QUOTES,
}


def normalise_text(text: str) -> str:
    """Return ``text`` with ligatures, odd spaces, invisible characters and curly quotes folded."""
    if text.isascii():
        return text
    return unicodedata.normalize("NFC", text.translate(_TABLE))

