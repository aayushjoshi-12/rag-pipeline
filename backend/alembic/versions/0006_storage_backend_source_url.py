"""Pluggable storage + URL ingest: documents.storage_backend and documents.source_url.

``storage_backend`` says where ``source_path`` (a storage key) lives (``local`` | ``s3``); existing
rows are all ``local``. ``source_url`` holds the redacted link a document was ingested from.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("storage_backend", sa.String(16), nullable=False, server_default="local"),
    )
    op.add_column("documents", sa.Column("source_url", sa.String(2048), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "source_url")
    op.drop_column("documents", "storage_backend")
