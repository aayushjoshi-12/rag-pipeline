"""Unit tests — content-defined chunking (ADR-0003)."""

from __future__ import annotations

import random
from itertools import pairwise

import pytest
from app.config import Settings
from app.services.chunking import ChunkSpec, chunk_text, chunker_fingerprint, content_hash

SIZE = 512
MIN = 64


def _doc(n_words: int = 4000, seed: int = 7) -> str:
    """Synthetic prose-ish text: deterministic, no real content."""
    rng = random.Random(seed)
    vocab = [f"w{rng.randrange(10_000)}x" for _ in range(800)]
    return " ".join(rng.choice(vocab) for _ in range(n_words))


def _chunks(text: str) -> list[ChunkSpec]:
    return chunk_text(text, chunk_size=SIZE, chunk_overlap=MIN)


def test_defaults_come_from_settings() -> None:
    cfg = Settings()
    text = _doc(2000)
    assert chunk_text(text) == chunk_text(
        text, chunk_size=cfg.chunk_size, chunk_overlap=cfg.chunk_overlap
    )


def test_size_cap_and_min_floor_honored() -> None:
    text = _doc()
    chunks = _chunks(text)
    assert len(chunks) > 10
    for c in chunks:
        assert c.end - c.start <= SIZE
    # every chunk except possibly the last meets the floor (raw span, before strip)
    for c in chunks[:-1]:
        assert c.end - c.start >= MIN


def test_deterministic() -> None:
    text = _doc()
    assert _chunks(text) == _chunks(text)


def test_ordering_indexes_and_contiguous_coverage() -> None:
    text = _doc(1500)
    chunks = _chunks(text)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for prev, nxt in pairwise(chunks):
        assert prev.end == nxt.start
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)


def test_every_chunk_has_hash_and_fingerprint() -> None:
    chunks = _chunks(_doc(800))
    fp = chunker_fingerprint(SIZE, MIN)
    for c in chunks:
        assert c.chunker_fingerprint == fp
        assert c.content_hash == content_hash(c.text)
        assert len(c.content_hash) == 64


def _survival(before: list[ChunkSpec], after: list[ChunkSpec]) -> float:
    old = {c.content_hash for c in before}
    new = {c.content_hash for c in after}
    return len(old & new) / len(old)


def test_boundaries_stable_under_local_edit() -> None:
    """The CDC property: an insert mid-document leaves nearly all chunks identical."""
    text = _doc(6000)
    mid = len(text) // 2
    edited = text[:mid] + " INSERTED SYNTHETIC SENTENCE ABOUT NOTHING. " + text[mid:]

    before, after = _chunks(text), _chunks(edited)
    assert _survival(before, after) >= 0.9

    # chunks entirely before the edit are byte-identical
    untouched = [c for c in before if c.end <= mid - 200]
    assert untouched
    assert [c.text for c in untouched] == [c.text for c in after[: len(untouched)]]


def test_fixed_size_chunking_would_fail_the_stability_check() -> None:
    """Negative control: the survival metric really discriminates CDC from fixed windows."""
    text = _doc(6000)
    mid = len(text) // 2
    edited = text[:mid] + " INSERTED SYNTHETIC SENTENCE ABOUT NOTHING. " + text[mid:]

    def fixed(t: str) -> list[ChunkSpec]:
        return [
            ChunkSpec(i, t[s : s + SIZE], content_hash(t[s : s + SIZE]), "fixed", s, s + SIZE)
            for i, s in enumerate(range(0, len(t), SIZE))
        ]

    assert _survival(fixed(text), fixed(edited)) < 0.7


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t  \n"])
def test_empty_and_whitespace_input(text: str) -> None:
    assert _chunks(text) == []


def test_short_input_is_single_chunk() -> None:
    chunks = _chunks("hello world")
    assert len(chunks) == 1
    assert chunks[0].text == "hello world"


def test_long_input_without_natural_breaks_is_force_split_at_cap() -> None:
    text = "a" * (SIZE * 5 + 17)  # constant content: the hash never hits the mask pattern
    chunks = _chunks(text)
    assert all(c.end - c.start <= SIZE for c in chunks)
    assert "".join(c.text for c in chunks) == text
    assert len(chunks) >= 5


def test_unicode_never_splits_inside_a_character() -> None:
    text = ("héllo 🌍 世界 " * 400).strip()
    chunks = _chunks(text)
    assert "".join(c.text.replace(" ", "") for c in chunks) == text.replace(" ", "")
    for c in chunks:
        c.text.encode("utf-8")  # would raise on a lone surrogate


def test_crlf_normalized() -> None:
    a = _chunks("line one\r\nline two\r\n" * 100)
    b = _chunks("line one\nline two\n" * 100)
    assert [c.content_hash for c in a] == [c.content_hash for c in b]


def test_content_hash_ignores_whitespace_differences() -> None:
    assert content_hash("a  b\n c ") == content_hash("a b c")
    assert content_hash("a b") != content_hash("a c")


def test_fingerprint_changes_with_config() -> None:
    base = chunker_fingerprint(512, 64)
    assert chunker_fingerprint(256, 64) != base
    assert chunker_fingerprint(512, 32) != base
    assert chunker_fingerprint(512, 64) == base


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (-1, 0), (100, -1), (100, 100), (100, 150)])
def test_invalid_args_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="chunk_"):
        chunk_text("some text", chunk_size=size, chunk_overlap=overlap)
