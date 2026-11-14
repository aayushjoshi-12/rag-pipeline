"""BM25 scoring with hand-computed values (services/keyword.py). N=10 chunks throughout."""

from __future__ import annotations

import pytest
from app.services import keyword as kw

# ln(4.4) = 1.4816045 (df=2); ln(7.3333) = 1.9924302 (df=1); ln(1 + .5/10.5) = 0.0465200 (df=10)


def test_idf_values() -> None:
    assert kw.bm25_idf(10, 2) == pytest.approx(1.4816045, abs=1e-6)
    assert kw.bm25_idf(10, 1) == pytest.approx(1.9924302, abs=1e-6)
    assert kw.bm25_idf(10, 10) == pytest.approx(0.0465200, abs=1e-6)  # a term in every chunk still >= 0


def test_idf_is_never_negative_and_rarer_is_higher() -> None:
    assert all(kw.bm25_idf(100, df) > 0 for df in range(0, 101))
    assert kw.bm25_idf(100, 1) > kw.bm25_idf(100, 10) > kw.bm25_idf(100, 90)


def test_tf_one_at_average_length_scores_exactly_idf() -> None:
    # tf (k1+1) / (tf + k1 (1 - b + b)) = 2.2 / 2.2 = 1
    assert kw.bm25_term_score(1, 100, 100, 1.4816045, 1.2, 0.75) == pytest.approx(1.4816045, abs=1e-6)


def test_term_frequency_saturates() -> None:
    idf = 1.4816045
    # tf=3, dl=avg: 6.6 / 4.2 = 1.5714286
    assert kw.bm25_term_score(3, 100, 100, idf, 1.2, 0.75) == pytest.approx(2.32824, abs=1e-4)
    # tf=100: 220 / 101.2 = 2.1739 -> bounded by k1+1 = 2.2 times idf
    assert kw.bm25_term_score(100, 100, 100, idf, 1.2, 0.75) < 2.2 * idf
    assert kw.bm25_term_score(0, 100, 100, idf, 1.2, 0.75) == 0.0


def test_longer_chunks_score_lower_shorter_higher() -> None:
    idf = 1.4816045
    # dl=200: norm = 1 - .75 + .75*2 = 1.75 -> 6.6 / (3 + 2.1) = 1.2941176
    assert kw.bm25_term_score(3, 200, 100, idf, 1.2, 0.75) == pytest.approx(1.917371, abs=1e-4)
    # dl=50: norm = .25 + .375 = .625 -> 6.6 / (3 + .75) = 1.76
    assert kw.bm25_term_score(3, 50, 100, idf, 1.2, 0.75) == pytest.approx(1.481604541 * 1.76, abs=1e-4)


def test_b_controls_length_normalisation() -> None:
    idf = 1.4816045
    # b=0: length is ignored -> same as the average-length value 6.6/4.2
    assert kw.bm25_term_score(3, 200, 100, idf, 1.2, 0.0) == pytest.approx(2.32824, abs=1e-4)
    # b=1, dl=50: 6.6 / (3 + 1.2*.5) = 1.8333333
    assert kw.bm25_term_score(3, 50, 100, idf, 1.2, 1.0) == pytest.approx(2.716275, abs=1e-4)


def test_k1_zero_makes_tf_irrelevant() -> None:
    idf = 1.4816045
    assert kw.bm25_term_score(1, 100, 100, idf, 0.0, 0.75) == pytest.approx(idf, abs=1e-6)
    assert kw.bm25_term_score(9, 300, 100, idf, 0.0, 0.75) == pytest.approx(idf, abs=1e-6)


def test_zero_average_length_does_not_divide_by_zero() -> None:
    assert kw.bm25_term_score(1, 0, 0, 2.0, 1.2, 0.75) == pytest.approx(2.0)


def test_score_sums_terms_and_ignores_terms_without_a_document_frequency() -> None:
    # a: df=1, tf=1 -> 1.9924302 ; b: df=2, tf=3 -> 2.32824 ; z: not a query term
    s = kw.bm25_score(
        {"a": 1, "b": 3, "z": 5}, doc_len=100, avg_len=100, n_docs=10, doc_freqs={"a": 1, "b": 2}, k1=1.2, b=0.75
    )
    assert s == pytest.approx(1.99243 + 2.32824, abs=1e-4)
    assert kw.bm25_score({}, doc_len=1, avg_len=1, n_docs=10, doc_freqs={"a": 1}, k1=1.2, b=0.75) == 0.0


def test_rare_term_outweighs_a_common_one() -> None:
    rare = kw.bm25_score({"r": 1}, doc_len=100, avg_len=100, n_docs=1000, doc_freqs={"r": 2}, k1=1.2, b=0.75)
    common = kw.bm25_score({"c": 1}, doc_len=100, avg_len=100, n_docs=1000, doc_freqs={"c": 600}, k1=1.2, b=0.75)
    assert rare > 10 * common


def test_quote_lexeme_neutralises_tsquery_syntax() -> None:
    assert kw.quote_lexeme("abc") == "'abc'"
    assert kw.quote_lexeme("it's") == "'it''s'"
    assert kw.quote_lexeme("a\\b") == "'a\\\\b'"
    assert kw.or_tsquery(["b3", "-5.3", "a&b"]) == "'b3' | '-5.3' | 'a&b'"
