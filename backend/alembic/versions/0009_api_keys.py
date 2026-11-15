"""API keys (service accounts) for machine clients, and the ``apikeys:manage`` permission.

Only the SHA-256 of the key secret is stored. ``apikeys:manage`` is granted to the built-in ``admin`` role
here (``accounts.seed()`` also keeps admin synced to the full catalogue); no other role gets it.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"], unique=True)
    op.create_index("ix_api_keys_owner_user_id", "api_keys", ["owner_user_id"])
    op.create_index("ix_api_keys_role_id", "api_keys", ["role_id"])
    op.execute(
        """
        INSERT INTO role_permissions (id, role_id, permission)
        SELECT gen_random_uuid(), r.id, 'apikeys:manage'
        FROM roles r
        WHERE r.name = 'admin' AND r.is_builtin
          AND NOT EXISTS (
            SELECT 1 FROM role_permissions rp WHERE rp.role_id = r.id AND rp.permission = 'apikeys:manage'
          )
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission = 'apikeys:manage'")
    op.drop_table("api_keys")
