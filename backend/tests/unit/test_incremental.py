"""Unit tests — incremental update planning (pure)."""

from __future__ import annotations

import pytest
from app.services.incremental import (
    FULL_REBUILD_CHURN,
    OldChunk,
    old_chunks_from_rows,
    plan_update,
    vector_id,
)

FP = "fp1"


def old(*hashes: str, fp: str = FP, vid: bool = True) -> list[OldChunk]:
    return [OldChunk(h, fp, vid) for h in hashes]


def test_first_ingest_is_full_and_adds_everything_once() -> None:
    plan = plan_update([], ["a", "b", "a"], FP)
    assert plan.mode == "full" and plan.add_hashes == ["a", "b"] and not plan.remove_hashes


def test_identical_document_embeds_nothing() -> None:
    plan = plan_update(old("a", "b", "c", "d"), ["a", "b", "c", "d"], FP)
    assert plan.mode == "incremental"
    assert (
        plan.add_hashes == []
        and plan.remove_hashes == []
        and plan.keep_hashes == ["a", "b", "c", "d"]
    )


def test_single_edit_replaces_only_that_chunk() -> None:
    plan = plan_update(old(*"abcdefgh"), list("abcXefgh"), FP)
    assert plan.mode == "incremental"
    assert plan.add_hashes == ["X"] and plan.remove_hashes == ["d"] and len(plan.keep_hashes) == 7


def test_insertion_keeps_all_old_chunks() -> None:
    plan = plan_update(old(*"abcdefgh"), list("abcNdefgh"), FP)
    assert plan.mode == "incremental" and plan.add_hashes == ["N"] and plan.remove_hashes == []


def test_deletion_removes_only_dropped_chunks() -> None:
    plan = plan_update(old(*"abcdefgh"), list("abefgh"), FP)
    assert plan.mode == "incremental" and plan.remove_hashes == ["c", "d"] and plan.add_hashes == []


def test_duplicate_chunks_share_one_vector() -> None:
    plan = plan_update(old("a", "a", "b"), ["a", "b", "a", "b"], FP)
    assert plan.mode == "incremental" and plan.add_hashes == [] and plan.keep_hashes == ["a", "b"]
    # removing one of two identical chunks must not delete the shared vector
    assert plan_update(old("a", "a", "b", "c"), ["a", "b", "c"], FP).remove_hashes == []


def test_majority_change_falls_back_to_full_rebuild() -> None:
    plan = plan_update(old(*"abcd"), list("wxyz"), FP)
    assert plan.mode == "full" and plan.reason == "most chunks changed"
    assert plan.add_hashes == list("wxyz") and plan.remove_hashes == []


def test_churn_threshold_boundary() -> None:
    # 10 old chunks: churn of exactly 50% (add 3 + remove 2 = 5) is still incremental, 6 is not
    base = list("abcdefghij")
    assert FULL_REBUILD_CHURN == 0.5
    five = plan_update(old(*base), [*base[:8], "x", "y", "z"], FP)  # remove i,j ; add x,y,z = 5
    six = plan_update(old(*base), [*base[:7], "x", "y", "z"], FP)  # remove h,i,j ; add 3 = 6
    assert five.mode == "incremental" and six.mode == "full"


def test_fingerprint_mismatch_forces_full() -> None:
    plan = plan_update(old("a", "b", fp="OLD"), ["a", "b"], FP)
    assert plan.mode == "full" and "config" in plan.reason


def test_legacy_chunks_without_vector_ids_force_full() -> None:
    plan = plan_update(old("a", "b", vid=False), ["a", "b"], FP)
    assert plan.mode == "full" and "legacy" in plan.reason


def test_old_chunks_from_rows_parses_metadata_and_skips_garbage() -> None:
    rows = [
        {"content_hash": "a", "chunker_fingerprint": "f", "vector_id": "v"},
        {"content_hash": "b"},
        {"nope": 1},
        {"content_hash": 5},
    ]
    parsed = old_chunks_from_rows(rows)
    assert parsed == [OldChunk("a", "f", True), OldChunk("b", None, False)]


def test_vector_id_is_content_addressed_and_doc_scoped() -> None:
    h = "ab" * 32
    assert vector_id("d1", h) == f"d1_{h[:20]}"
    assert vector_id("d1", h) != vector_id("d2", h)


@pytest.mark.parametrize("n", [1, 5, 50])
def test_plan_never_loses_a_new_chunk(n: int) -> None:
    new = [f"n{i}" for i in range(n)]
    plan = plan_update(old(*[f"o{i}" for i in range(n)]), new, FP)
    assert set(plan.add_hashes) | set(plan.keep_hashes) == set(new)
