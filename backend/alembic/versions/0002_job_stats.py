"""Add jobs.stats (ingest summary).

Revision ID: 0002
Revises: 5359cfdc659d
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "5359cfdc659d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("stats", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "stats")
