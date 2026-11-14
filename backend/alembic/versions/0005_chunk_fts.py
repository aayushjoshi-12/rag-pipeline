"""Keyword search: generated tsvector + token-length columns on chunks, GIN index.

``chunks.fts``      = to_tsvector('english', text)         (generated, stored)
``chunks.fts_len``  = token count of fts (BM25 length)     (generated, stored)
The configuration baked in here must equal RETRIEVAL_FTS_CONFIG (default ``english``); change it
afterwards with ``python -m app.manage rebuild-fts``. See services/keyword.py.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rag_fts_len(v tsvector) RETURNS integer
        LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
        $$ SELECT coalesce(sum(coalesce(cardinality(positions), 1)), 0)::integer FROM unnest(v) $$
        """
    )
    op.execute(
        "ALTER TABLE chunks ADD COLUMN fts tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english'::regconfig, text)) STORED"
    )
    op.execute(
        "ALTER TABLE chunks ADD COLUMN fts_len integer "
        "GENERATED ALWAYS AS (rag_fts_len(to_tsvector('english'::regconfig, text))) STORED"
    )
    op.execute("CREATE INDEX ix_chunks_fts ON chunks USING gin (fts)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_fts")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS fts_len, DROP COLUMN IF EXISTS fts")
    op.execute("DROP FUNCTION IF EXISTS rag_fts_len(tsvector)")
