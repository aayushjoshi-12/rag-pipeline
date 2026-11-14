"""Fusion math with hand-computed expectations (services/fusion.py)."""

from __future__ import annotations

import pytest
from app.services import fusion


def keys(rows: list[tuple[str, float]]) -> list[str]:
    return [k for k, _ in rows]


# ── RRF ───────────────────────────────────────────────────────────────────────────────────
def test_rrf_sums_reciprocal_ranks_and_breaks_ties_by_first_appearance() -> None:
    # A: 1/61 + 1/62, B: 1/62 + 1/61 (equal), C: 1/63 (list 1 only), D: 1/63 (list 2 only)
    out = fusion.rrf([["A", "B", "C"], ["B", "A", "D"]], k=60)
    assert keys(out) == ["A", "B", "C", "D"]
    assert out[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert out[1][1] == pytest.approx(1 / 61 + 1 / 62)
    assert out[2][1] == pytest.approx(1 / 63) and out[3][1] == pytest.approx(1 / 63)


def test_rrf_item_in_both_lists_beats_two_rank_one_singletons() -> None:
    out = fusion.rrf([["A", "B"], ["C", "B"]], k=60)
    # B = 1/62 + 1/62 = 0.032258 > A = C = 1/61 = 0.016393 ; A before C (appears first)
    assert keys(out) == ["B", "A", "C"]
    assert out[0][1] == pytest.approx(2 / 62)
    assert out[1][1] == pytest.approx(1 / 61)


@pytest.mark.parametrize(
    ("k", "expected_a", "expected_c"),
    [(0, 1 / 1 + 1 / 2, 1 / 3), (1, 1 / 2 + 1 / 3, 1 / 4), (60, 1 / 61 + 1 / 62, 1 / 63)],
)
def test_rrf_k_changes_the_scores(k: int, expected_a: float, expected_c: float) -> None:
    out = dict(fusion.rrf([["A", "B", "C"], ["B", "A", "D"]], k=k))
    assert out["A"] == pytest.approx(expected_a)
    assert out["C"] == pytest.approx(expected_c)


def test_rrf_k_can_flip_the_winner() -> None:
    # X: rank 1 in list 1 only; Y: rank 3 in both. k=0: X=1, Y=2/3 -> X. k=60: X=1/61=.01639, Y=2/63=.03175 -> Y
    lists = [["X", "p", "Y"], ["q", "r", "Y"]]
    assert keys(fusion.rrf(lists, k=0))[0] == "X"
    assert keys(fusion.rrf(lists, k=60))[0] == "Y"


def test_rrf_single_list_and_empty_inputs() -> None:
    assert fusion.rrf([["x", "y"]], k=60) == [("x", pytest.approx(1 / 61)), ("y", pytest.approx(1 / 62))]
    assert fusion.rrf([[], []], k=60) == []
    assert fusion.rrf([], k=60) == []


def test_rrf_repeated_key_counts_once_at_its_best_rank() -> None:
    out = fusion.rrf([["A", "A", "B"]], k=60)
    assert out == [("A", pytest.approx(1 / 61)), ("B", pytest.approx(1 / 62))]  # B is rank 2, not 3


def test_rrf_rejects_negative_k() -> None:
    with pytest.raises(ValueError):
        fusion.rrf([["a"]], k=-1)


# ── min-max ───────────────────────────────────────────────────────────────────────────────
def test_min_max_higher_is_better() -> None:
    assert fusion.min_max([("a", 10.0), ("b", 5.0), ("c", 0.0)], higher_is_better=True) == {
        "a": 1.0, "b": 0.5, "c": 0.0,
    }


def test_min_max_lower_is_better_inverts() -> None:
    out = fusion.min_max([("a", 0.2), ("b", 0.4), ("c", 0.6)], higher_is_better=False)
    assert out == {"a": pytest.approx(1.0), "b": pytest.approx(0.5), "c": pytest.approx(0.0)}


@pytest.mark.parametrize("hib", [True, False])
def test_min_max_all_equal_and_single_item_map_to_one(hib: bool) -> None:
    assert fusion.min_max([("a", 3.0), ("b", 3.0)], higher_is_better=hib) == {"a": 1.0, "b": 1.0}
    assert fusion.min_max([("a", 7.0)], higher_is_better=hib) == {"a": 1.0}
    assert fusion.min_max([], higher_is_better=hib) == {}


# ── weighted ──────────────────────────────────────────────────────────────────────────────
def test_weighted_hand_computed() -> None:
    semantic = [("A", 0.2), ("B", 0.4), ("C", 0.6)]  # distances -> norm A=1, B=.5, C=0
    keyword = [("B", 10.0), ("D", 5.0)]  # norm B=1, D=0
    out = fusion.weighted([semantic, keyword], [0.6, 0.4], [False, True])
    scores = dict(out)
    assert scores["A"] == pytest.approx(0.6)
    assert scores["B"] == pytest.approx(0.6 * 0.5 + 0.4 * 1.0)  # 0.7
    assert scores["C"] == pytest.approx(0.0) and scores["D"] == pytest.approx(0.0)
    assert keys(out) == ["B", "A", "C", "D"]  # C before D: appears first


def test_weighted_weights_are_normalised() -> None:
    semantic = [("A", 1.0), ("B", 0.0)]
    keyword = [("B", 1.0), ("A", 0.0)]
    # (3, 1) -> (0.75, 0.25): A = .75*1 + .25*0 ; B = .75*0 + .25*1
    a = dict(fusion.weighted([semantic, keyword], [3, 1], [True, True]))
    b = dict(fusion.weighted([semantic, keyword], [0.75, 0.25], [True, True]))
    assert a == pytest.approx(b) and a["A"] == pytest.approx(0.75) and a["B"] == pytest.approx(0.25)


def test_weighted_zero_weight_ignores_that_list_but_keeps_its_items() -> None:
    out = fusion.weighted([[("A", 1.0), ("B", 0.0)], [("C", 9.0)]], [1.0, 0.0], [True, True])
    assert dict(out) == {"A": pytest.approx(1.0), "B": pytest.approx(0.0), "C": pytest.approx(0.0)}


def test_weighted_all_equal_scores_each_get_full_weight() -> None:
    out = dict(fusion.weighted([[("A", 5.0), ("B", 5.0)], [("B", 2.0)]], [0.5, 0.5], [True, True]))
    assert out == {"A": pytest.approx(0.5), "B": pytest.approx(1.0)}


def test_weighted_item_only_in_one_list_gets_zero_from_the_other() -> None:
    out = dict(fusion.weighted([[("A", 2.0), ("B", 1.0)], []], [0.5, 0.5], [True, True]))
    assert out == {"A": pytest.approx(0.5), "B": pytest.approx(0.0)}


def test_normalise_weights_and_validation() -> None:
    assert fusion.normalise_weights([6, 4]) == [pytest.approx(0.6), pytest.approx(0.4)]
    for bad in ([0, 0], [-1, 2], []):
        with pytest.raises(ValueError):
            fusion.normalise_weights(bad)
    with pytest.raises(ValueError):
        fusion.weighted([[("a", 1.0)]], [1, 1], [True])
