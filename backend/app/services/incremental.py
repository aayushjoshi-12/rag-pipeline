"""Plan an incremental re-index of a document from content hashes (ADR-0003).

Pure functions: given the chunks stored for the previous version and the freshly chunked new
version, decide which vectors to keep, add and remove — or fall back to a full rebuild.
Vectors are content-addressed (``vector_id``), so a chunk whose text did not change keeps its
vector even if its position in the document shifted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal


def vector_id(doc_id: str, content_hash: str) -> str:
    """Content-addressed vector id: identical text in the same document shares one vector."""
    return f"{doc_id}_{content_hash[:20]}"


@dataclass(frozen=True)
class OldChunk:
    content_hash: str
    fingerprint: str | None
    has_vector_id: bool


@dataclass(frozen=True)
class UpdatePlan:
    mode: Literal["full", "incremental"]
    reason: str
    #: unique content hashes whose vectors must be embedded + upserted
    add_hashes: list[str] = field(default_factory=list)
    #: unique content hashes whose vectors must be deleted
    remove_hashes: list[str] = field(default_factory=list)
    #: unique content hashes whose vectors are reused as-is
    keep_hashes: list[str] = field(default_factory=list)


def old_chunks_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[OldChunk]:
    """Build ``OldChunk``s from stored chunk metadata dicts."""
    out: list[OldChunk] = []
    for meta in rows:
        h = meta.get("content_hash")
        if not isinstance(h, str):
            continue
        fp = meta.get("chunker_fingerprint")
        out.append(OldChunk(h, fp if isinstance(fp, str) else None, bool(meta.get("vector_id"))))
    return out


def plan_update(
    old: Sequence[OldChunk],
    new_hashes: Sequence[str],
    new_fingerprint: str,
    *,
    max_churn: float | None = None,
) -> UpdatePlan:
    """Decide between a full rebuild and an incremental update.

    Incremental never embeds more than a full rebuild (it embeds only the added chunks), so by
    default a heavily edited document is still updated incrementally. ``max_churn`` is an opt-in
    guard: when (added + removed) / old chunk count exceeds it, rebuild from scratch instead.
    """
    new_unique = list(dict.fromkeys(new_hashes))
    if not old:
        return UpdatePlan("full", "first ingest", add_hashes=new_unique)
    if any(o.fingerprint != new_fingerprint for o in old):
        return UpdatePlan("full", "chunker config changed", add_hashes=new_unique)
    if not all(o.has_vector_id for o in old):
        return UpdatePlan("full", "legacy index without content-addressed vectors", add_hashes=new_unique)

    old_unique = list(dict.fromkeys(o.content_hash for o in old))
    old_set, new_set = set(old_unique), set(new_unique)
    add = [h for h in new_unique if h not in old_set]
    remove = [h for h in old_unique if h not in new_set]
    keep = [h for h in new_unique if h in old_set]

    if max_churn is not None and (len(add) + len(remove)) > max_churn * len(old_unique):
        return UpdatePlan("full", "most chunks changed", add_hashes=new_unique)
    return UpdatePlan(
        "incremental",
        "unchanged chunks reused" if keep else "no chunks in common",
        add_hashes=add,
        remove_hashes=remove,
        keep_hashes=keep,
    )
