"""Admin-UI settings layer: ``app_settings`` (overrides) and ``settings_audit``, plus the ``settings:*`` permissions.

Secrets are stored only as Fernet tokens (``secret_blob``); the audit log never holds a secret value.
``settings:read`` / ``settings:manage`` are granted to the built-in ``admin`` role only.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_PERMISSIONS = ("settings:read", "settings:manage")


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("secret_blob", sa.Text(), nullable=True),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.String(320), nullable=False, server_default=""),
    )
    op.create_table(
        "settings_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(320), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("old_value", sa.JSON(), nullable=True),
        sa.Column("new_value", sa.JSON(), nullable=True),
        sa.Column("secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    op.create_index("ix_settings_audit_at", "settings_audit", ["at"])
    op.create_index("ix_settings_audit_key_at", "settings_audit", ["key", "at"])
    for permission in _PERMISSIONS:
        op.execute(
            f"""
            INSERT INTO role_permissions (id, role_id, permission)
            SELECT gen_random_uuid(), r.id, '{permission}'
            FROM roles r
            WHERE r.name = 'admin' AND r.is_builtin
              AND NOT EXISTS (
                SELECT 1 FROM role_permissions rp WHERE rp.role_id = r.id AND rp.permission = '{permission}'
              )
            """
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission IN ('settings:read', 'settings:manage')"
    )
    op.drop_table("settings_audit")
    op.drop_table("app_settings")
