"""Keyword retrieval on Postgres full-text search, with optional true BM25 re-scoring.

Storage (migration 0005): ``chunks.fts`` = ``to_tsvector(<config>, text)`` and ``chunks.fts_len`` = the
token count of that tsvector, both *generated stored* columns (never written by the app), plus a GIN
index on ``fts``. ``<config>`` is the Postgres text-search configuration (``RETRIEVAL_FTS_CONFIG``,
default ``english``); it is baked into the columns, so changing it needs ``python -m app.manage rebuild-fts``
and a mismatch is detected and reported instead of silently searching with the wrong tokenisation.

Query processing. The question is tokenised by Postgres with the same configuration (so IDs, numbers,
stemming and stopwords are treated identically on both sides), producing lexemes. They are OR-ed into
a tsquery: natural-language questions contain words that no single chunk has, so an AND query (what
``websearch_to_tsquery`` would build) returns nothing for them.

Two methods:

Identifiers. Postgres splits ``B3-5.3-05`` into the lexemes ``b3``, ``-5.3``, ``-05`` (the sign sticks to the number
part). Matching those separately lets ``B3-5.3-04`` and ``B3-4.2-05`` look almost as good as the real one, so every
ID-like token of the question (letters/digits joined by ``- . / _``) is also searched as an adjacent-lexeme phrase
(``'b3' <-> '-5.3' <-> '-05'``) and, for BM25, scored as one extra term (idf from the phrase's document
frequency, tf = 1 when the chunk contains it). Exact IDs therefore outrank partial matches.

* ``postgres_fts``: candidates ranked by ``ts_rank_cd(fts, query, 1)``; score = that value (higher = better).
* ``bm25`` (default): the same FTS match selects a candidate pool (``pool`` rows by ``ts_rank_cd``), then each
  candidate is re-scored with BM25 (Lucene idf ``ln(1 + (N - df + .5) / (df + .5))``) using corpus
  statistics computed by SQL over *all* chunks: ``N`` (chunk count), average chunk token length, the document
  frequency of every query term (``count(*) WHERE fts @@ term``, GIN-backed) and per-chunk term frequency
  (position count in the tsvector). **Approximation:** idf, length normalisation and tf are exact; only the
  candidate pool is FTS-limited, so a chunk that BM25 would rank highly but that ``ts_rank_cd`` ranks below the
  pool cut is not seen. Token counts come from tsvector positions (Postgres keeps at most 256 positions per
  lexeme; stopwords are not counted).
"""

from __future__ import annotations

import math
import re
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import cache

#: Query text beyond this is ignored (the API caps questions at 4,000 characters anyway).
MAX_QUERY_CHARS = 2000
#: At most this many distinct lexemes are looked at; the rarest ``MAX_QUERY_TERMS`` of them are used.
MAX_LEXEMES = 128
MAX_QUERY_TERMS = 64

#: Tokens like ``B3-5.3-05``, ``2025-11-05``, ``Chapter-7``: alphanumerics joined by - . / _
_ID_RE = re.compile(r"[A-Za-z0-9]+(?:[-./_][A-Za-z0-9]+)+")
MAX_ID_PHRASES = 8

_CONFIG_RE = re.compile(r"'([a-z0-9_]+)'::regconfig")


class KeywordIndexMismatchError(RuntimeError):
    """The keyword index is missing or was built with a different text-search configuration."""


@dataclass(frozen=True)
class KeywordHit:
    document_id: uuid.UUID
    index: int
    text: str
    filename: str
    #: ``bm25`` or ``ts_rank_cd`` value: higher = better.
    score: float


# ── pure scoring ─────────────────────────────────────────────────────────────────────────────
def bm25_idf(n_docs: int, doc_freq: int) -> float:
    """Lucene/BM25+ style idf: ``ln(1 + (N - df + 0.5) / (df + 0.5))``, always >= 0."""
    return math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))


def bm25_term_score(
    tf: int, doc_len: float, avg_len: float, idf: float, k1: float, b: float
) -> float:
    """One term's contribution: ``idf * tf (k1 + 1) / (tf + k1 (1 - b + b dl / avgdl))``."""
    if tf <= 0:
        return 0.0
    ratio = doc_len / avg_len if avg_len > 0 else 1.0
    return idf * tf * (k1 + 1.0) / (tf + k1 * (1.0 - b + b * ratio))


def bm25_score(
    term_freqs: Mapping[str, int],
    *,
    doc_len: float,
    avg_len: float,
    n_docs: int,
    doc_freqs: Mapping[str, int],
    k1: float,
    b: float,
) -> float:
    """BM25 of one chunk: the sum of ``bm25_term_score`` over the query terms it contains."""
    return sum(
        bm25_term_score(tf, doc_len, avg_len, bm25_idf(n_docs, doc_freqs[term]), k1, b)
        for term, tf in term_freqs.items()
        if term in doc_freqs
    )


def quote_lexeme(lexeme: str) -> str:
    """A lexeme as a quoted tsquery operand (``'it''s'``), so ``& | ! : * ( )`` inside it stay literal."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def or_tsquery(lexemes: Sequence[str]) -> str:
    return " | ".join(quote_lexeme(x) for x in lexemes)


def phrase_tsquery(lexemes: Sequence[str]) -> str:
    """Adjacent-lexeme phrase: ``'b3' <-> '-5.3' <-> '-05'``."""
    return " <-> ".join(quote_lexeme(x) for x in lexemes)


# ── storage / drift ──────────────────────────────────────────────────────────────────────────
async def stored_fts_config(session: AsyncSession) -> str | None:
    """The configuration baked into ``chunks.fts``; None if the column does not exist."""
    row = (
        await session.execute(
            text(
                "SELECT pg_get_expr(ad.adbin, ad.adrelid) FROM pg_attribute a "
                "JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
                "WHERE a.attrelid = 'chunks'::regclass AND a.attname = 'fts' AND NOT a.attisdropped"
            )
        )
    ).first()
    if row is None:
        return None
    m = _CONFIG_RE.search(str(row[0]))
    return m.group(1) if m else None


async def ensure_fts(session: AsyncSession, config: str) -> None:
    """Raise ``KeywordIndexMismatchError`` unless the keyword index exists with ``config``."""
    stored = await stored_fts_config(session)
    if stored is None:
        raise KeywordIndexMismatchError(
            "The keyword index (chunks.fts) does not exist: run `alembic upgrade head`."
        )
    if stored != config:
        raise KeywordIndexMismatchError(
            f"The keyword index was built with the '{stored}' text-search configuration but "
            f"RETRIEVAL_FTS_CONFIG is '{config}'. Run `python -m app.manage rebuild-fts` "
            f"(or set RETRIEVAL_FTS_CONFIG={stored})."
        )


async def rebuild_fts(session: AsyncSession, config: str) -> None:
    """Recreate ``chunks.fts`` / ``fts_len`` / the GIN index with ``config`` (takes an exclusive lock)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", config):
        raise ValueError(f"invalid text-search configuration name {config!r}")
    found = (
        await session.execute(text("SELECT 1 FROM pg_ts_config WHERE cfgname = :n"), {"n": config})
    ).first()
    if found is None:
        raise ValueError(f"Postgres has no text-search configuration named {config!r}")
    vec = f"to_tsvector('{config}'::regconfig, text)"
    await session.execute(text("DROP INDEX IF EXISTS ix_chunks_fts"))
    await session.execute(
        text("ALTER TABLE chunks DROP COLUMN IF EXISTS fts, DROP COLUMN IF EXISTS fts_len")
    )
    await session.execute(
        text(f"ALTER TABLE chunks ADD COLUMN fts tsvector GENERATED ALWAYS AS ({vec}) STORED")
    )
    await session.execute(
        text(
            f"ALTER TABLE chunks ADD COLUMN fts_len integer GENERATED ALWAYS AS (rag_fts_len({vec})) STORED"
        )
    )
    await session.execute(text("CREATE INDEX ix_chunks_fts ON chunks USING gin (fts)"))
    await session.commit()
    await cache.abump_index_version()  # keyword scores/ranking depend on the tsvector configuration


# ── search ───────────────────────────────────────────────────────────────────────────────────
async def query_lexemes(session: AsyncSession, query: str, config: str) -> list[str]:
    """Distinct lexemes Postgres extracts from ``query`` (stopwords dropped, config's stemming applied)."""
    rows = await session.execute(
        text(
            "SELECT u.lexeme FROM unnest(to_tsvector(CAST(CAST(:cfg AS text) AS regconfig), :q)) AS u(lexeme, positions, weights) "
            "ORDER BY u.lexeme LIMIT :lim"
        ),
        {"cfg": config, "q": query[:MAX_QUERY_CHARS], "lim": MAX_LEXEMES},
    )
    return [str(r[0]) for r in rows]


async def id_phrases(session: AsyncSession, query: str, config: str) -> list[list[str]]:
    """For each ID-like token of ``query`` that tokenises into >= 2 lexemes, those lexemes in order."""
    phrases: list[list[str]] = []
    for token in list(dict.fromkeys(_ID_RE.findall(query[:MAX_QUERY_CHARS])))[:MAX_ID_PHRASES]:
        rows = await session.execute(
            text(
                "SELECT u.lexeme FROM unnest(to_tsvector(CAST(CAST(:cfg AS text) AS regconfig), :t)) AS u(lexeme, positions, weights), "
                "LATERAL unnest(u.positions) AS p ORDER BY p"
            ),
            {"cfg": config, "t": token},
        )
        lexemes = [str(r[0]) for r in rows]
        if len(lexemes) >= 2:
            phrases.append(lexemes)
    return phrases


def phrase_key(lexemes: Sequence[str]) -> str:
    return "\x1f" + "\x1f".join(lexemes)  # control char: cannot collide with a real lexeme


async def _doc_freqs(session: AsyncSession, queries: Mapping[str, str]) -> dict[str, int]:
    """Exact document frequency of each term (``key -> tsquery text``; GIN-backed, one UNION ALL branch each)."""
    if not queries:
        return {}
    params: dict[str, object] = {}
    branches = []
    for i, (key, tsq) in enumerate(queries.items()):
        params[f"t{i}"], params[f"l{i}"] = tsq, key
        branches.append(
            f"SELECT CAST(:l{i} AS text) AS term, count(*) AS df FROM chunks WHERE fts @@ CAST(:t{i} AS tsquery)"
        )
    rows = await session.execute(text(" UNION ALL ".join(branches)), params)
    return {str(r[0]): int(r[1]) for r in rows}


async def keyword_search(
    session: AsyncSession,
    query: str,
    *,
    limit: int,
    method: str = "bm25",
    config: str = "english",
    pool: int = 200,
    k1: float = 1.2,
    b: float = 0.75,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> list[KeywordHit]:
    """Top ``limit`` chunks for ``query`` by BM25 (or plain ``ts_rank_cd``), best first. [] if nothing matches."""
    if limit <= 0:
        return []
    await ensure_fts(session, config)
    lexemes = await query_lexemes(session, query, config)
    if not lexemes:
        return []
    phrases = await id_phrases(session, query, config)
    doc_filter = "AND c.document_id = ANY(CAST(:docs AS uuid[]))" if document_ids else ""
    base: dict[str, object] = {"docs": list(document_ids)} if document_ids else {}
    phrase_queries = {phrase_key(p): phrase_tsquery(p) for p in phrases}

    df: dict[str, int] = {}
    if method == "postgres_fts":
        terms = lexemes
        found_phrases = phrase_queries
    else:
        df = await _doc_freqs(session, {**{t: quote_lexeme(t) for t in lexemes}, **phrase_queries})
        # keep the rarest terms: they carry the most weight and bound the query size
        terms = sorted((t for t in lexemes if df.get(t, 0) > 0), key=lambda t: (df[t], t))[
            :MAX_QUERY_TERMS
        ]
        found_phrases = {k: v for k, v in phrase_queries.items() if df.get(k, 0) > 0}
        if not terms:
            return []
    tsq = " | ".join([or_tsquery(terms), *(f"({q})" for q in found_phrases.values())])
    take = limit if method == "postgres_fts" else max(pool, limit)
    rows = (
        await session.execute(
            text(
                "SELECT c.id, c.document_id, c.index AS chunk_index, c.text, c.fts_len, d.filename, "
                "ts_rank_cd(c.fts, CAST(:tsq AS tsquery), 1) AS rank "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                f"WHERE c.fts @@ CAST(:tsq AS tsquery) {doc_filter} "
                "ORDER BY rank DESC, c.document_id, chunk_index LIMIT :take"
            ),
            {**base, "tsq": tsq, "take": take},
        )
    ).all()
    if not rows:
        return []
    if method == "postgres_fts":
        return [
            KeywordHit(
                r.document_id, int(r.chunk_index), str(r.text), str(r.filename), float(r.rank)
            )
            for r in rows[:limit]
        ]

    stats = (
        await session.execute(
            text("SELECT count(*) AS n, coalesce(avg(fts_len), 0) AS avgdl FROM chunks")
        )
    ).one()
    n_docs, avg_len = int(stats.n), float(stats.avgdl)
    ids = [r.id for r in rows]
    tf_rows = await session.execute(
        text(
            "SELECT c.id, u.lexeme, cardinality(u.positions) AS tf FROM chunks c, "
            "LATERAL unnest(c.fts) AS u(lexeme, positions, weights) "
            "WHERE c.id = ANY(CAST(:ids AS uuid[])) AND u.lexeme = ANY(CAST(:terms AS text[]))"
        ),
        {"ids": ids, "terms": terms},
    )
    tfs: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    for r in tf_rows:
        tfs[r.id][str(r.lexeme)] = int(
            r.tf or 1
        )  # position-less lexemes (stripped tsvector) count once
    for (
        key,
        phrase_q,
    ) in found_phrases.items():  # an ID phrase is one extra term with tf = 1 where present
        matched = await session.execute(
            text(
                "SELECT id FROM chunks WHERE id = ANY(CAST(:ids AS uuid[])) AND fts @@ CAST(:q AS tsquery)"
            ),
            {"ids": ids, "q": phrase_q},
        )
        for (cid,) in matched:
            tfs[cid][key] = 1
    scored: list[KeywordHit] = []
    for r in rows:
        freqs = tfs.get(r.id)
        if not freqs:  # chunk vanished between the queries (document deleted mid-flight)
            continue
        score = bm25_score(
            freqs,
            doc_len=float(r.fts_len or 0),
            avg_len=avg_len,
            n_docs=n_docs,
            doc_freqs=df,
            k1=k1,
            b=b,
        )
        scored.append(
            KeywordHit(r.document_id, int(r.chunk_index), str(r.text), str(r.filename), score)
        )
    scored.sort(key=lambda h: (-h.score, str(h.document_id), h.index))
    return scored[:limit]
