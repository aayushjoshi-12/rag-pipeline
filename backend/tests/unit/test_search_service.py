"""Unit tests — neighbour representation, grouping, request schema and the permission catalogue."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.schema.search import SearchRequest
from app.services import traces
from app.services.auth.permissions import (
    BUILTIN_ROLES,
    DESCRIPTIONS,
    Permission,
    is_valid_permission,
)
from app.services.context import expand_neighbors
from app.services.retriever import ChunkResult
from app.services.search import Retrieval
from pydantic import ValidationError

DOC = uuid.uuid4()
OTHER = uuid.uuid4()


def hit(idx: int, doc: uuid.UUID = DOC, rank: int = 1) -> ChunkResult:
    return ChunkResult(
        text=f"hit{idx}", document_id=doc, chunk_index=idx, filename="f.pdf", score=0.5,
        score_kind="rrf", semantic_rank=2, keyword_rank=3, rank=rank,
    )


def avail(doc: uuid.UUID, n: int = 10) -> dict[tuple[uuid.UUID, int], str]:
    return {(doc, i): f"text{i}" for i in range(n)}


def test_context_chunks_are_unscored_and_unranked_and_point_at_their_hit() -> None:
    out = expand_neighbors([hit(5)], 1, avail(DOC))
    ctx = [c for c in out if not c.is_hit]
    assert [c.chunk_index for c in ctx] == [4, 6]
    for c in ctx:
        assert (c.score, c.semantic_rank, c.keyword_rank, c.rank) == (None, None, None, None)
        assert c.context_for == (DOC, 5)
    (h,) = [c for c in out if c.is_hit]
    assert (h.score, h.semantic_rank, h.keyword_rank, h.rank, h.context_for) == (0.5, 2, 3, 1, None)


def test_a_shared_neighbour_points_at_the_first_hit_in_rank_order() -> None:
    out = expand_neighbors([hit(5, rank=1), hit(7, rank=2)], 1, avail(DOC))
    ctx = {c.chunk_index: c.context_for for c in out if not c.is_hit}
    assert ctx == {4: (DOC, 5), 6: (DOC, 5), 8: (DOC, 7)}


def test_retrieval_groups_context_by_hit_in_document_order() -> None:
    hits = [hit(7, OTHER, rank=1), hit(2, DOC, rank=2)]
    passages = expand_neighbors(hits, 1, {**avail(DOC), **avail(OTHER)})
    r = Retrieval(hits=hits, passages=passages)
    assert [c.chunk_index for c in r.context_for(hits[0])] == [6, 8]
    assert [c.chunk_index for c in r.context_for(hits[1])] == [1, 3]
    assert r.context_for(hit(9, DOC)) == []


def test_search_request_defaults() -> None:
    req = SearchRequest(query="q")
    assert (req.top_k, req.mode, req.fusion, req.neighbors, req.document_ids, req.include_text) == (
        None, None, None, 0, None, True
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""}, {"query": "x" * 4001}, {"query": "q", "top_k": 0}, {"query": "q", "top_k": 51},
        {"query": "q", "mode": "x"}, {"query": "q", "fusion": "x"}, {"query": "q", "neighbors": 6},
        {"query": "q", "neighbors": -1}, {"query": "q", "document_ids": ["x"]}, {},
    ],
)
def test_search_request_rejects(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(payload)


def test_permission_catalogue_is_consistent() -> None:
    assert is_valid_permission("search:use") and Permission.SEARCH_USE.value == "search:use"
    assert set(DESCRIPTIONS) == set(Permission)  # every permission is documented
    for _, perms in BUILTIN_ROLES.values():
        assert perms <= set(Permission)
    assert {n for n, (_, p) in BUILTIN_ROLES.items() if Permission.SEARCH_USE in p} == {"admin", "user", "viewer"}


def test_search_is_a_trace_kind() -> None:
    assert "search" in traces.KINDS and "chat" in traces.KINDS and "ingest" in traces.KINDS
