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
ALGORITHM = "cdc-buzhash-v1"
_SEED = "rag-pipeline-cdc"
_WINDOW = 32
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


def _mask_bits(chunk_size: int, min_size: int) -> int:
    # Aim for an average chunk of roughly 0.6 * chunk_size: min_size + 2**bits.
    target_gap = max(2.0, 0.6 * chunk_size - min_size)
    return max(1, round(math.log2(target_gap)))


def _boundaries(text: str, min_size: int, max_size: int) -> list[int]:
    """Exclusive end offsets of each raw chunk (the last equals ``len(text)``)."""
    mask = (1 << _mask_bits(max_size, min_size)) - 1
    ends: list[int] = []
    start = 0
    h = 0
    n = len(text)
    for i, ch in enumerate(text):
        incoming = _char_value(ch)
        if i >= _WINDOW:
            # Rolling update: rotate, drop the char leaving the window, add the new one.
            h = _rotl(h, 1) ^ _rotl(_char_value(text[i - _WINDOW]), _WINDOW) ^ incoming
        else:
            h = _rotl(h, 1) ^ incoming
        length = i + 1 - start
        if length >= max_size or (length >= min_size and (h & mask) == 0):
            ends.append(i + 1)
            start = i + 1
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
