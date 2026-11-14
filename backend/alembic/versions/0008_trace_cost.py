"""Per-trace LLM cost (USD, NULL = unknown).

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("traces", sa.Column("cost_usd", sa.Numeric(14, 8), nullable=True))


def downgrade() -> None:
    op.drop_column("traces", "cost_usd")
