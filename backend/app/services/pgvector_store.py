"""pgvector backend for the ``VectorStore`` protocol (``VECTOR_STORE=pgvector``).

Storage: one table (``vector_items`` by default)::

    id text PRIMARY KEY            -- content-addressed vector id (services.incremental)
    doc_id text NOT NULL           -- indexed; used by delete_doc and document filters
    text text NOT NULL
    metadata jsonb NOT NULL        -- {"doc_id", "index", ...}; same shape Chroma stores
    embedding vector(N) NOT NULL   -- N from EMBEDDING_DIMENSION / the model / a one-off probe

This is deliberately *not* the legacy ``chunks.vector`` column: chunk rows are deleted and
recreated on every ingest (so kept vectors would be lost) and that column is a hard-coded
``vector(1024)``. The table and index are created idempotently by ``verify()`` (advisory-locked so
API and worker can start together). ``text`` is stored so later retrieval modes can do keyword
search in the same table.

Score conventions (lower = closer, identical numbers to Chroma): the index operator is used in
``ORDER BY`` (so the planner can use the index) and the *selected* score is converted:

======  ============  ==========================  ===============
metric  operator      native value                returned score
======  ============  ==========================  ===============
cosine  ``<=>``       1 - cos                     same
l2      ``<->``       euclidean distance          squared (``^2``)
ip      ``<#>``       negative dot product        ``1 + native`` = 1 - dot
======  ============  ==========================  ===============

Connections are opened per operation (no pool dependency); the cost is a few ms locally.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from app.config import Settings
from app.services.vectorstore import (
    VectorSettings,
    VectorStoreMismatchError,
    _filter_clauses,
    _prepare_rows,
    compare_settings,
    resolve_vector_settings,
)

logger = logging.getLogger(__name__)

#: What psycopg's ``execute`` accepts as a composed statement.
_Q = sql.SQL | sql.Composed

_OPS = {"cosine": "vector_cosine_ops", "l2": "vector_l2_ops", "ip": "vector_ip_ops"}
_OPERATORS = {"cosine": "<=>", "l2": "<->", "ip": "<#>"}
_OPCLASS_TO_METRIC = {v: k for k, v in _OPS.items()}
#: pgvector cannot index ``vector`` columns wider than this (hnsw and ivfflat).
MAX_INDEXED_DIMENSION = 2000
_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
_LOCK_KEY = 0x5241_4756  # "RAGV": serialises concurrent DDL from api + worker start-up


def dsn_from_database_url(url: str) -> str:
    """The app's SQLAlchemy URL (``postgresql+asyncpg://``) as a plain libpq/psycopg DSN."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _vec(values: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


class PgVectorStore:
    def __init__(
        self,
        dsn: str,
        vector_settings: VectorSettings,
        *,
        table: str = "vector_items",
        dimension_probe: Callable[[], int] | None = None,
    ) -> None:
        if not _TABLE_RE.match(table):
            raise ValueError(f"invalid table name {table!r}")
        self._dsn = dsn
        self.settings = vector_settings
        self._table = table
        self._probe = dimension_probe
        self._index_name = f"{table}_embedding_idx"
        self._index_built: bool | None = None
        #: ``reindex_all`` sets this while bulk-loading so ivfflat is trained once, at the end.
        self.defer_index_build = False

    @classmethod
    def from_settings(cls, settings: Settings) -> PgVectorStore:
        from app.services.embeddings import get_embedder, probe_dimension

        return cls(
            dsn_from_database_url(settings.database_url),
            resolve_vector_settings(settings, "pgvector"),
            dimension_probe=lambda: probe_dimension(get_embedder()),
        )

    # ── plumbing ────────────────────────────────────────────────────────────────
    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(self._dsn) as conn:  # commits on clean exit, rolls back on error
            yield conn

    @property
    def _t(self) -> sql.Identifier:
        return sql.Identifier(self._table)

    def _dimension(self) -> int:
        if self.settings.dimension is not None:
            return self.settings.dimension
        if self._probe is None:
            raise ValueError("Cannot create the pgvector table: set EMBEDDING_DIMENSION (unknown embedding model).")
        dim = self._probe()
        logger.info("pgvector: probed embedding dimension %d", dim)
        return dim

    # ── schema ──────────────────────────────────────────────────────────────────
    def _table_exists(self, conn: psycopg.Connection[Any]) -> bool:
        row = conn.execute("SELECT to_regclass(%s)", (self._table,)).fetchone()
        return bool(row and row[0])

    def _create_table(self, conn: psycopg.Connection[Any], dimension: int) -> None:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {t} (id text PRIMARY KEY, doc_id text NOT NULL, text text NOT NULL, "
                "metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb, embedding vector({d}) NOT NULL)"
            ).format(t=self._t, d=sql.Literal(dimension))
        )
        conn.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (doc_id)").format(
                i=sql.Identifier(f"{self._table}_doc_idx"), t=self._t
            )
        )

    def _ann_index(self, conn: psycopg.Connection[Any]) -> dict[str, Any] | None:
        """The ANN index on the table, parsed from ``pg_indexes`` (``None`` if there is none)."""
        rows = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = %s "
            "AND (indexdef LIKE %s OR indexdef LIKE %s)",
            (self._table, "% USING hnsw %", "% USING ivfflat %"),
        ).fetchall()
        if not rows:
            return None
        return self._parse_indexdef(rows[0][0])

    @staticmethod
    def _parse_indexdef(indexdef: str) -> dict[str, Any]:
        out: dict[str, Any] = {"index": "hnsw" if " USING hnsw " in indexdef else "ivfflat"}
        opclass = re.search(r"vector_(?:cosine|l2|ip)_ops", indexdef)
        out["metric"] = _OPCLASS_TO_METRIC.get(opclass.group(0)) if opclass else None
        opts = dict(re.findall(r"(\w+)='?(\d+)'?", indexdef.split(" WITH ", 1)[1])) if " WITH " in indexdef else {}
        if out["index"] == "hnsw":
            out["hnsw_m"] = int(opts.get("m", 16))
            out["hnsw_ef_construction"] = int(opts.get("ef_construction", 64))
        else:
            out["ivfflat_lists"] = int(opts.get("lists", 100))
        return out

    def _count(self, conn: psycopg.Connection[Any]) -> int:
        row = conn.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=self._t)).fetchone()
        return int(row[0]) if row else 0

    def _create_index(self, conn: psycopg.Connection[Any], dimension: int) -> bool:
        """Create the configured ANN index if missing. ``False`` = deliberately deferred/not wanted."""
        s = self.settings
        if s.index == "none":
            return False
        if dimension > MAX_INDEXED_DIMENSION:
            raise ValueError(
                f"pgvector cannot build a {s.index} index on {dimension}-dimensional vectors "
                f"(limit {MAX_INDEXED_DIMENSION}); set VECTOR_INDEX=none or use a smaller embedding model."
            )
        ops = sql.Identifier(_OPS[s.metric])
        if s.index == "hnsw":
            conn.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} USING hnsw (embedding {o}) WITH (m = {m}, ef_construction = {e})").format(
                    i=sql.Identifier(self._index_name), t=self._t, o=ops,
                    m=sql.Literal(s.hnsw_m), e=sql.Literal(s.hnsw_ef_construction),
                )
            )
            return True
        # ivfflat learns its list centroids from the rows present when it is built, so an index built
        # on an empty/tiny table is useless. Defer until there are at least `lists` rows.
        lists = s.ivfflat_lists or 100
        rows = self._count(conn)
        if rows < lists:
            logger.warning(
                "pgvector: ivfflat index deferred (%d rows < IVFFLAT_LISTS=%d); searches are exact (sequential "
                "scan) until enough rows exist. It is built automatically after an ingest crosses the "
                "threshold, or by `python -m app.manage reindex`.",
                rows, lists,
            )
            return False
        conn.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} USING ivfflat (embedding {o}) WITH (lists = {n})").format(
                i=sql.Identifier(self._index_name), t=self._t, o=ops, n=sql.Literal(lists)
            )
        )
        return True

    def _stored_dimension(self, conn: psycopg.Connection[Any]) -> int | None:
        row = conn.execute(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attname = 'embedding'",
            (self._table,),
        ).fetchone()
        return int(row[0]) if row and row[0] and row[0] > 0 else None

    def stored_settings(self) -> dict[str, Any] | None:
        """What the database really has (typmod + ``pg_indexes``); ``None`` when the table is missing."""
        with self._conn() as conn:
            return self._stored(conn)

    def _stored(self, conn: psycopg.Connection[Any]) -> dict[str, Any] | None:
        if not self._table_exists(conn):
            return None
        ann = self._ann_index(conn)
        stored: dict[str, Any] = {"dimension": self._stored_dimension(conn), "index": "none", "metric": None}
        if ann:
            stored.update(ann)
        return stored

    def verify(self) -> None:
        """Create the table/index when missing (idempotent), then compare with the configuration."""
        with self._conn() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            if not self._table_exists(conn):
                self._create_table(conn, self._dimension())
            stored = self._stored(conn)
            assert stored is not None
            dimension = stored["dimension"] or self._dimension()
            if stored["index"] == "none" and self.settings.index != "none":
                problems = compare_settings(self.settings, {"dimension": stored["dimension"]})
                if not problems:
                    self._create_index(conn, dimension)
                    stored = self._stored(conn)
                    assert stored is not None
            problems = compare_settings(self.settings, self._normalise(stored))
            if problems:
                raise VectorStoreMismatchError(problems)
            self._index_built = stored["index"] != "none"

    def _normalise(self, stored: dict[str, Any]) -> dict[str, Any]:
        """A deferred ivfflat index (none stored yet) is not a mismatch."""
        if stored["index"] == "none" and self.settings.index == "ivfflat":
            return {**stored, "index": None}
        return stored

    def describe(self) -> dict[str, Any]:
        with self._conn() as conn:
            stored = self._stored(conn)
            count = self._count(conn) if stored else 0
        state = "ok"
        if stored is None:
            problems = ["table missing (run verify)"]
        else:
            problems = compare_settings(self.settings, self._normalise(stored))
            if stored["index"] == "none" and self.settings.index == "ivfflat":
                state = f"ivfflat deferred ({count} rows < lists {self.settings.ivfflat_lists})"
        return {
            "store": "pgvector",
            "table": self._table,
            "configured": self.settings.as_dict(),
            "stored": stored,
            "count": count,
            "index_state": state,
            "problems": problems,
        }

    # ── writes ──────────────────────────────────────────────────────────────────
    def add_chunks(self, doc_id: str, chunks: list[dict[str, Any]], embeddings: list[list[float]]) -> None:
        rows = _prepare_rows(doc_id, chunks, embeddings)
        if not rows:
            return
        ids = [r[0] for r in rows]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ids in one add_chunks call")
        params = [(r[0], doc_id, r[1], Jsonb(r[2]), _vec(e)) for r, e in zip(rows, embeddings, strict=True)]
        stmt = sql.SQL(
            "INSERT INTO {t} (id, doc_id, text, metadata, embedding) VALUES (%s, %s, %s, %s, %s::vector) "
            "ON CONFLICT (id) DO UPDATE SET doc_id = EXCLUDED.doc_id, text = EXCLUDED.text, "
            "metadata = {t}.metadata || EXCLUDED.metadata, embedding = EXCLUDED.embedding"
        ).format(t=self._t)
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.executemany(stmt, params)
                self._maybe_build_ivfflat(conn)
        except psycopg.errors.DataException as exc:  # e.g. "expected 1024 dimensions, not 768"
            if "dimension" in str(exc).lower():
                raise VectorStoreMismatchError([f"embedding dimension differs from the stored column ({exc})"]) from exc
            raise

    def _maybe_build_ivfflat(self, conn: psycopg.Connection[Any]) -> None:
        if self.settings.index != "ivfflat" or self._index_built or self.defer_index_build:
            return
        if self._ann_index(conn) is not None:
            self._index_built = True
            return
        dim = self._stored_dimension(conn) or self._dimension()
        if self._create_index(conn, dim):
            self._index_built = True
            logger.info("pgvector: ivfflat index built after reaching IVFFLAT_LISTS rows")

    def delete_doc(self, doc_id: str) -> None:
        with self._conn() as conn:
            conn.execute(sql.SQL("DELETE FROM {t} WHERE doc_id = %s").format(t=self._t), (doc_id,))

    def delete_ids(self, ids: list[str]) -> None:
        if ids:
            with self._conn() as conn:
                conn.execute(sql.SQL("DELETE FROM {t} WHERE id = ANY(%s)").format(t=self._t), (list(ids),))

    def update_metadata(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        if len(ids) != len(metadatas):
            raise ValueError("ids and metadatas must have equal length.")
        if not ids:
            return
        stmt = sql.SQL("UPDATE {t} SET metadata = metadata || %s WHERE id = %s").format(t=self._t)
        with self._conn() as conn, conn.cursor() as cur:
            cur.executemany(stmt, [(Jsonb(m), i) for i, m in zip(ids, metadatas, strict=True)])

    def reset(self) -> None:
        """Drop and recreate the table (and index) with the configured settings."""
        with self._conn() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {t}").format(t=self._t))
            dim = self._dimension()
            self._create_table(conn, dim)
            self._index_built = self._create_index(conn, dim)

    # ── reads ───────────────────────────────────────────────────────────────────
    def _where(self, filters: dict[str, Any] | None) -> tuple[_Q, list[Any]]:
        if not filters:
            return sql.SQL(""), []
        parts: list[_Q] = []
        params: list[Any] = []
        for key, values in _filter_clauses(filters):
            if not values:
                parts.append(sql.SQL("FALSE"))
            elif key == "doc_id":
                parts.append(sql.SQL("doc_id = ANY(%s)"))
                params.append([str(v) for v in values])
            else:
                parts.append(sql.SQL("(") + sql.SQL(" OR ").join(sql.SQL("metadata @> %s") for _ in values) + sql.SQL(")"))
                params.extend(Jsonb({key: v}) for v in values)
        return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts), params

    def _search_plan(
        self, query_embedding: list[float], top_k: int, filters: dict[str, Any] | None
    ) -> tuple[list[_Q], _Q, list[Any]]:
        """``(SET LOCAL statements, SELECT, params)`` - shared by search and ``explain`` so the plan
        tests look at exactly the SQL that runs."""
        s = self.settings
        op = sql.SQL(_OPERATORS[s.metric])
        score = {
            "cosine": sql.SQL("embedding <=> %s::vector"),
            "l2": sql.SQL("power(embedding <-> %s::vector, 2)"),
            "ip": sql.SQL("1 + (embedding <#> %s::vector)"),
        }[s.metric]
        where, wparams = self._where(filters)
        stmt = (
            sql.SQL("SELECT id, text, metadata, ") + score + sql.SQL(" AS score FROM {t}").format(t=self._t)
            + where + sql.SQL(" ORDER BY embedding ") + op + sql.SQL(" %s::vector LIMIT %s")
        )
        vec = _vec(query_embedding)
        params = [vec, *wparams, vec, top_k]
        sets: list[_Q] = []
        if s.index == "hnsw":
            # hnsw returns at most ef_search candidates, so it must be >= top_k
            sets.append(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(min(1000, max(s.hnsw_ef_search or 40, top_k)))))
            if filters:
                # Without iterative scans a filtered query post-filters ef_search candidates and can return
                # fewer than top_k rows (pgvector >= 0.8).
                sets.append(sql.SQL("SET LOCAL hnsw.iterative_scan = strict_order"))
        elif s.index == "ivfflat":
            sets.append(sql.SQL("SET LOCAL ivfflat.probes = {}").format(sql.Literal(s.ivfflat_probes or 10)))
            if filters:
                sets.append(sql.SQL("SET LOCAL ivfflat.iterative_scan = relaxed_order"))
        return sets, stmt, params

    def similarity_search(
        self, query_embedding: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if top_k <= 0:
            return []
        sets, stmt, params = self._search_plan(query_embedding, top_k, filters)
        try:
            with self._conn() as conn:
                for st in sets:
                    conn.execute(st)
                rows = conn.execute(stmt, params).fetchall()
        except psycopg.errors.DataException as exc:
            if "dimension" in str(exc).lower():
                raise VectorStoreMismatchError([f"query embedding dimension differs from the stored column ({exc})"]) from exc
            raise
        out = [
            {"id": r[0], "text": r[1], "metadata": r[2] if isinstance(r[2], dict) else json.loads(r[2]), "score": float(r[3])}
            for r in rows
        ]
        out.sort(key=lambda h: h["score"])  # ivfflat relaxed_order may be slightly out of order
        return out

    def explain(
        self, query_embedding: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[str]:
        """The query plan of exactly the SQL ``similarity_search`` runs (diagnostics and tests)."""
        sets, stmt, params = self._search_plan(query_embedding, top_k, filters)
        with self._conn() as conn:
            for st in sets:
                conn.execute(st)
            rows = conn.execute(sql.SQL("EXPLAIN ") + stmt, params).fetchall()
        return [r[0] for r in rows]

    def analyze(self) -> None:
        """Refresh planner statistics (autovacuum does this eventually; tests and bulk loads call it)."""
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("ANALYZE {t}").format(t=self._t))

    def rebuild_index(self) -> None:
        """Drop and rebuild the ANN index (retrains ivfflat centroids on the current rows)."""
        with self._conn() as conn:
            conn.execute(sql.SQL("DROP INDEX IF EXISTS {i}").format(i=sql.Identifier(self._index_name)))
            dim = self._stored_dimension(conn) or self._dimension()
            self._index_built = self._create_index(conn, dim)
