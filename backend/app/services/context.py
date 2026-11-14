"""Neighbour-context expansion: a post-retrieval step, independent of how hits were found.

Rules and definitions are often split across adjacent chunks, so the chunk that completes an
answer sits next to a hit without being a hit itself. ``expand_neighbors`` adds chunks
``i-N .. i+N`` of every hit (same document) to the LLM context. It only consumes ``ChunkResult``s,
so it works unchanged whatever retrieval mode produced them.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ops
from app.services.retriever import ChunkResult

#: Character budget for the context block of the prompt (also enforced by ``qa.build_user_prompt``).
MAX_CONTEXT_CHARS = 12_000
_BLOCK_OVERHEAD = 48  # "[n] (source: , chunk n)\n" + separator, plus slack


def block_cost(filename: str, text: str) -> int:
    return len(filename) + len(text) + _BLOCK_OVERHEAD


def expand_neighbors(
    hits: Sequence[ChunkResult],
    neighbors: int,
    available: Mapping[tuple[uuid.UUID, int], str],
    *,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> list[ChunkResult]:
    """Return ``hits`` plus their neighbours, de-duplicated and in reading order.

    Output order: hits in their original (rank) order; each hit is followed/preceded by its
    selected neighbours in document order, and a chunk appears once, at its first position.
    Hits are never dropped for the budget; neighbours are added nearest-first (distance 1 for
    every hit before distance 2, ...) only while the total stays within ``max_chars``.
    ``available`` maps ``(document_id, chunk_index)`` to chunk text (missing = does not exist).
    """
    if neighbors <= 0 or not hits:
        return list(hits)
    hit_keys = {(h.document_id, h.chunk_index) for h in hits}
    first: dict[tuple[uuid.UUID, int], ChunkResult] = {}
    for h in hits:
        first.setdefault((h.document_id, h.chunk_index), h)
    used = sum(block_cost(h.filename, h.text) for h in first.values())
    selected: set[tuple[uuid.UUID, int]] = set(hit_keys)
    for dist in range(1, neighbors + 1):
        for h in hits:
            for idx in (h.chunk_index - dist, h.chunk_index + dist):
                key = (h.document_id, idx)
                text = available.get(key)
                if text is None or key in selected:
                    continue
                cost = block_cost(h.filename, text)
                if used + cost > max_chars:
                    continue
                selected.add(key)
                used += cost
    out: list[ChunkResult] = []
    emitted: set[tuple[uuid.UUID, int]] = set()
    for h in hits:
        for idx in range(h.chunk_index - neighbors, h.chunk_index + neighbors + 1):
            key = (h.document_id, idx)
            if key in emitted or key not in selected:
                continue
            emitted.add(key)
            if key in first:
                out.append(first[key])
            else:
                out.append(
                    replace(
                        h,
                        text=available[key],
                        chunk_index=idx,
                        is_hit=False,
                    )
                )
    return out


async def add_neighbor_context(
    hits: Sequence[ChunkResult],
    neighbors: int,
    *,
    session: AsyncSession,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> list[ChunkResult]:
    """Fetch the neighbouring chunks of ``hits`` from Postgres and expand (see ``expand_neighbors``)."""
    if neighbors <= 0 or not hits:
        return list(hits)
    wanted: dict[uuid.UUID, set[int]] = {}
    for h in hits:
        wanted.setdefault(h.document_id, set()).update(
            i for i in range(h.chunk_index - neighbors, h.chunk_index + neighbors + 1) if i >= 0
        )
    available: dict[tuple[uuid.UUID, int], str] = {}
    for doc_id, indexes in wanted.items():
        for row in await ops.get_chunks_by_indexes(doc_id, sorted(indexes), session=session):
            available[(doc_id, row.index)] = row.text
    return expand_neighbors(hits, neighbors, available, max_chars=max_chars)
