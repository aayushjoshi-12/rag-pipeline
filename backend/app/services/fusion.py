"""Rank fusion for hybrid retrieval: pure functions, no I/O.

Every function takes ranked candidate lists (best first) and returns ``(key, fused_score)`` pairs,
best first, **higher = better**. An item missing from a list simply gets no contribution from it.

* **RRF** (reciprocal rank fusion): ``score(d) = sum_i 1 / (k + rank_i(d))`` with 1-based ranks. It
  ignores the raw scores, so distances and BM25 values never have to be made comparable.
* **Weighted**: each list's raw scores are min-max normalised to ``[0, 1]`` (best = 1, worst = 0;
  a list whose scores are all equal, including a single item, maps every item to 1.0), then
  ``score(d) = sum_i w_i * norm_i(d)`` with the weights normalised to sum to 1.

Ties are broken by first appearance: items of the first list in rank order, then the new items of the
second list, and so on (so, at equal fused score, the semantic hit wins; the order is deterministic).
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence


def normalise_weights(weights: Sequence[float]) -> list[float]:
    """Scale ``weights`` to sum to 1. Negative weights, or all zero, are rejected."""
    if any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("weights must be >= 0 and not all zero")
    total = float(sum(weights))
    return [w / total for w in weights]


def _sorted_by_score[K: Hashable](scores: dict[K, float]) -> list[tuple[K, float]]:
    # dicts keep insertion order = first appearance; sorted() is stable, so ties keep that order.
    return sorted(scores.items(), key=lambda kv: -kv[1])


def rrf[K: Hashable](ranked_lists: Sequence[Sequence[K]], k: int = 60) -> list[tuple[K, float]]:
    """Reciprocal rank fusion. A key repeated inside one list counts once, at its best rank."""
    if k < 0:
        raise ValueError("k must be >= 0")
    scores: dict[K, float] = {}
    for items in ranked_lists:
        seen: set[K] = set()
        rank = 0
        for key in items:
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return _sorted_by_score(scores)


def min_max[K: Hashable](items: Sequence[tuple[K, float]], *, higher_is_better: bool) -> dict[K, float]:
    """Normalise raw scores to ``[0, 1]`` where 1 is the best. A repeated key keeps its first score."""
    first: dict[K, float] = {}
    for key, score in items:
        first.setdefault(key, score)
    if not first:
        return {}
    low, high = min(first.values()), max(first.values())
    if high == low:
        return dict.fromkeys(first, 1.0)
    span = high - low
    if higher_is_better:
        return {key: (s - low) / span for key, s in first.items()}
    return {key: (high - s) / span for key, s in first.items()}


def weighted[K: Hashable](
    scored_lists: Sequence[Sequence[tuple[K, float]]],
    weights: Sequence[float],
    higher_is_better: Sequence[bool],
) -> list[tuple[K, float]]:
    """Weighted sum of min-max normalised scores (see module docstring)."""
    if not len(scored_lists) == len(weights) == len(higher_is_better):
        raise ValueError("scored_lists, weights and higher_is_better must have the same length")
    w = normalise_weights(weights)
    scores: dict[K, float] = {}
    for items, weight, hib in zip(scored_lists, w, higher_is_better, strict=True):
        for key, norm in min_max(items, higher_is_better=hib).items():
            scores[key] = scores.get(key, 0.0) + weight * norm
    return _sorted_by_score(scores)
