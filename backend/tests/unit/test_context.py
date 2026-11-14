"""Unit tests — neighbour-context expansion (services/context.py)."""

from __future__ import annotations

import uuid

import pytest
from app.services.context import block_cost, expand_neighbors
from app.services.retriever import ChunkResult

DOC = uuid.uuid4()
OTHER = uuid.uuid4()


def _hit(idx: int, doc: uuid.UUID = DOC, score: float = 0.1) -> ChunkResult:
    return ChunkResult(text=f"hit{idx}", document_id=doc, chunk_index=idx, filename="f.pdf", score=score)


def _avail(doc: uuid.UUID, n: int) -> dict[tuple[uuid.UUID, int], str]:
    return {(doc, i): f"text{i}" for i in range(n)}


def _idx(rows: list[ChunkResult]) -> list[tuple[uuid.UUID, int]]:
    return [(r.document_id, r.chunk_index) for r in rows]


def test_zero_neighbors_returns_hits_unchanged() -> None:
    hits = [_hit(3), _hit(1)]
    assert expand_neighbors(hits, 0, _avail(DOC, 10)) == hits
    assert expand_neighbors([], 2, _avail(DOC, 10)) == []


def test_adds_previous_and_next_in_document_order() -> None:
    out = expand_neighbors([_hit(5)], 1, _avail(DOC, 10))
    assert [r.chunk_index for r in out] == [4, 5, 6]
    assert [r.is_hit for r in out] == [False, True, False]
    assert out[0].text == "text4" and out[1].text == "hit5"
    assert out[0].filename == "f.pdf" and out[0].document_id == DOC


def test_first_and_last_chunk_of_a_document_have_no_missing_neighbours() -> None:
    out = expand_neighbors([_hit(0)], 2, _avail(DOC, 3))
    assert [r.chunk_index for r in out] == [0, 1, 2]
    out = expand_neighbors([_hit(2)], 2, _avail(DOC, 3))
    assert [r.chunk_index for r in out] == [0, 1, 2]


def test_overlapping_windows_are_deduplicated_and_hits_stay_hits() -> None:
    out = expand_neighbors([_hit(5), _hit(6)], 1, _avail(DOC, 10))
    assert [r.chunk_index for r in out] == [4, 5, 6, 7]
    assert {r.chunk_index: r.is_hit for r in out} == {4: False, 5: True, 6: True, 7: False}


def test_rank_order_of_hits_is_preserved_across_documents() -> None:
    avail = {**_avail(DOC, 10), **_avail(OTHER, 10)}
    out = expand_neighbors([_hit(7, OTHER), _hit(2, DOC)], 1, avail)
    assert _idx(out) == [(OTHER, 6), (OTHER, 7), (OTHER, 8), (DOC, 1), (DOC, 2), (DOC, 3)]


def test_neighbours_never_cross_documents() -> None:
    out = expand_neighbors([_hit(0, DOC)], 1, _avail(OTHER, 5))
    assert _idx(out) == [(DOC, 0)]


def test_context_cap_drops_neighbours_but_never_hits() -> None:
    hits = [_hit(2), _hit(8)]
    avail = {(DOC, i): "x" * 100 for i in range(12)}
    hit_cost = sum(block_cost("f.pdf", h.text) for h in hits)
    # room for exactly one extra 100-char neighbour
    cap = hit_cost + block_cost("f.pdf", "x" * 100)
    out = expand_neighbors(hits, 1, avail, max_chars=cap)
    assert sum(r.is_hit for r in out) == 2
    assert sum(not r.is_hit for r in out) == 1
    assert sum(block_cost(r.filename, r.text) for r in out) <= cap
    # a cap below the hits themselves still returns every hit
    tiny = expand_neighbors(hits, 3, avail, max_chars=1)
    assert [r.chunk_index for r in tiny] == [2, 8]


def test_nearest_neighbours_win_the_budget() -> None:
    avail = {(DOC, i): "y" * 50 for i in range(20)}
    hits = [_hit(5), _hit(15)]
    cap = sum(block_cost("f.pdf", h.text) for h in hits) + 4 * block_cost("f.pdf", "y" * 50)
    out = expand_neighbors(hits, 3, avail, max_chars=cap)
    # distance-1 neighbours of both hits are kept before any distance-2 chunk
    assert {r.chunk_index for r in out} == {4, 5, 6, 14, 15, 16}


@pytest.mark.parametrize("n", [1, 2, 4])
def test_result_has_no_duplicate_chunks(n: int) -> None:
    hits = [_hit(i) for i in (3, 4, 9, 5)]
    out = expand_neighbors(hits, n, _avail(DOC, 12))
    keys = _idx(out)
    assert len(keys) == len(set(keys))
