"""Grant the new ``search:use`` permission to the built-in ``user`` and ``viewer`` roles.

New installs get it from ``accounts.seed()``; this one-time migration covers existing installs. It is
a migration (not a re-sync in ``seed()``) so that an admin who later removes ``search:use`` from a
built-in role is not overridden on the next start. Roles that do not exist are skipped; nothing is removed.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO role_permissions (id, role_id, permission)
        SELECT gen_random_uuid(), r.id, 'search:use'
        FROM roles r
        WHERE r.name IN ('user', 'viewer') AND r.is_builtin
          AND NOT EXISTS (
            SELECT 1 FROM role_permissions rp WHERE rp.role_id = r.id AND rp.permission = 'search:use'
          )
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission = 'search:use' "
        "AND role_id IN (SELECT id FROM roles WHERE name IN ('user', 'viewer') AND is_builtin)"
    )
