"""Content-defined chunking (CDC) — see docs/adr/0003-chunking.md.

Boundaries are chosen by a rolling hash over a small local window, so a boundary depends only
on the text near it (plus the min/max size rules). Editing one paragraph therefore leaves the
chunks before and after it — and their SHA-256 content hashes — untouched, which is what
lets ingest re-embed only what changed.

Size semantics (CDC does not slice at fixed offsets):

- ``chunk_size``    — hard **maximum** chunk length in characters.
- ``chunk_overlap`` — **minimum** chunk length: no boundary is placed before it. (No text is
  repeated between chunks; the name is kept for config compatibility.)

The chunker is a pure, deterministic function of ``(text, chunk_size, chunk_overlap)``.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass

from app.config import Settings

#: Bump when the algorithm changes in a way that moves boundaries.
ALGORITHM = "cdc-buzhash-v2-words"
_SEED = "rag-pipeline-cdc"
_WINDOW = 32
_AVG_FRACTION = 0.5  # target average chunk = this * chunk_size
_SENTENCE_GAP = 110.0  # typical characters between sentence ends in prose
_WORD_GAP = 7.0  # typical characters between word starts
_SENTENCE_SHARE = 0.8  # intended share of cuts that land on a sentence end / blank line
_MASK32 = 0xFFFFFFFF



def _build_table() -> tuple[int, ...]:
    """256 pseudo-random 32-bit values derived from SHA-256 (no RNG state)."""
    return tuple(
        int.from_bytes(hashlib.sha256(f"{_SEED}:{i}".encode()).digest()[:4], "big")
        for i in range(256)
    )


_TABLE = _build_table()


def _rotl(value: int, shift: int) -> int:
    shift %= 32
    return ((value << shift) | (value >> (32 - shift))) & _MASK32 if shift else value


def _char_value(ch: str) -> int:
    cp = ord(ch)
    return (
        _TABLE[cp & 0xFF]
        ^ _rotl(_TABLE[(cp >> 8) & 0xFF], 13)
        ^ _rotl(_TABLE[(cp >> 16) & 0xFF], 7)
    )


@dataclass(frozen=True)
class ChunkSpec:
    index: int
    text: str
    content_hash: str
    chunker_fingerprint: str
    start: int  # offset into the newline-normalized input
    end: int


def chunker_fingerprint(chunk_size: int, chunk_overlap: int) -> str:
    """Identifies the chunking config; a change invalidates any stored chunk-hash ledger."""
    raw = f"{ALGORITHM}|{chunk_size}|{chunk_overlap}|{_SEED}|{_WINDOW}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def content_hash(text: str) -> str:
    """SHA-256 over NFC-normalized text with whitespace runs collapsed and ends stripped."""
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _normalize_input(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


_SENTENCE_END = ".!?:;"
_CLOSERS = "\"')]}\u201d\u2019"


def _tier(text: str, p: int) -> int:
    """How natural a cut before ``p`` is: 2 = blank line, 1 = after a sentence end, 0 = any word start.

    A lone newline does not count: PDF/OCR text wraps lines mid-sentence.
    """
    j = p - 1
    while j >= 0 and text[j].isspace():
        if text[j] == "\n" and j > 0 and text[j - 1] == "\n":
            return 2
        j -= 1
    while j >= 0 and text[j] in _CLOSERS:
        j -= 1
    return 1 if j >= 0 and text[j] in _SENTENCE_END else 0


def _rolling_hashes(text: str) -> list[int]:
    """Per offset ``i``: hash of the last ``_WINDOW`` non-whitespace characters up to ``i``.

    Whitespace is skipped so re-wrapped lines or changed spacing (common between two renderings
    of the same document) do not alter the hash.
    """
    out: list[int] = []
    ring: list[int] = []  # char values currently in the window, oldest first
    h = 0
    for ch in text:
        if not ch.isspace():
            incoming = _char_value(ch)
            if len(ring) >= _WINDOW:
                h = _rotl(h, 1) ^ _rotl(ring.pop(0), _WINDOW) ^ incoming
            else:
                h = _rotl(h, 1) ^ incoming
            ring.append(incoming)
        out.append(h)
    return out


def _mask_bits(max_size: int, min_size: int) -> tuple[int, int]:
    """(sentence, word) mask widths for an average chunk of ``_AVG_FRACTION * max_size``.

    Past ``min_size`` a sentence end fires with probability ``2**-strong`` and a mid-sentence word
    start with the far smaller ``2**-weak``; the widths are chosen so that ~``_SENTENCE_SHARE`` of
    cuts land on sentence ends (the rest are a fallback for long sentences). Widths are whole
    bits, so the realised average is only approximately ``_AVG_FRACTION * max_size``.
    """
    gap = max(2.0, _AVG_FRACTION * max_size - min_size)
    strong = max(1, round(math.log2(max(2.0, gap / (_SENTENCE_SHARE * _SENTENCE_GAP)))))
    weak = max(strong + 1, round(math.log2(max(2.0, gap / ((1 - _SENTENCE_SHARE) * _WORD_GAP)))))
    return strong, weak


def _boundaries(text: str, min_size: int, max_size: int) -> list[int]:
    """Exclusive end offsets of each raw chunk (the last equals ``len(text)``).

    Candidate cuts are word starts (``text[p-1]`` is whitespace, ``text[p]`` is not), so a chunk
    never begins or ends mid-word. After ``min_size`` characters a candidate fires when the hash of
    the characters before it hits a mask; sentence ends and blank lines use a much looser mask than
    a plain word start, so most cuts land on sentence ends and a mid-sentence cut happens only in a
    long run-on. A chunk reaching ``max_size`` is cut at its last candidate (a hard cut only if it
    holds no whitespace at all).
    """
    strong_bits, weak_bits = _mask_bits(max_size, min_size)
    strong_mask, weak_mask = (1 << strong_bits) - 1, (1 << weak_bits) - 1
    hashes = _rolling_hashes(text)
    n = len(text)
    ends: list[int] = []
    start = 0
    last_candidate = -1
    p = 1
    while p < n:
        length = p - start
        if text[p - 1].isspace() and not text[p].isspace():
            if length >= min_size:
                mask = strong_mask if _tier(text, p) else weak_mask
                if (hashes[p - 1] & mask) == 0:
                    ends.append(p)
                    start, last_candidate = p, -1
                    p += 1
                    continue
            last_candidate = p
        if length >= max_size:
            cut = last_candidate if last_candidate > start else p
            ends.append(cut)
            start, last_candidate = cut, -1
            p = cut + 1
            continue
        p += 1
    if start < n:
        # Fold a too-short tail into the previous chunk when that still respects the cap.
        if ends and n - start < min_size and n - (ends[-2] if len(ends) > 1 else 0) <= max_size:
            ends[-1] = n
        else:
            ends.append(n)
    return ends


def chunk_text(
    text: str,
    *,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[ChunkSpec]:
    """Split ``text`` into content-defined chunks. Empty/whitespace-only input → ``[]``."""
    cfg = Settings() if chunk_size is None or chunk_overlap is None else None
    size = chunk_size if chunk_size is not None else (cfg.chunk_size if cfg else 0)
    min_size = chunk_overlap if chunk_overlap is not None else (cfg.chunk_overlap if cfg else 0)
    if size <= 0:
        raise ValueError("chunk_size must be positive")
    if not 0 <= min_size < size:
        raise ValueError("chunk_overlap must satisfy 0 <= chunk_overlap < chunk_size")

    normalized = _normalize_input(text)
    if not normalized.strip():
        return []

    fingerprint = chunker_fingerprint(size, min_size)
    specs: list[ChunkSpec] = []
    start = 0
    for end in _boundaries(normalized, min_size, size):
        piece = normalized[start:end]
        stripped = piece.strip()
        if stripped:
            specs.append(
                ChunkSpec(
                    index=len(specs),
                    text=stripped,
                    content_hash=content_hash(stripped),
                    chunker_fingerprint=fingerprint,
                    start=start,
                    end=end,
                )
            )
        start = end
    return specs
