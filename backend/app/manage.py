"""Management commands: ``python -m app.manage create-admin --email you@example.com``.

Creates an admin, or resets the password / re-activates / re-grants admin to an existing user.
This is the recovery path when nobody can sign in. The password is read from the
``ADMIN_PASSWORD`` env var or prompted for; it is never accepted as a command-line argument.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.db.engine import async_session
from app.services.auth import accounts
from app.services.auth.permissions import ADMIN_ROLE


async def create_admin(email: str, password: str) -> str:
    async with async_session() as session:
        await accounts.seed(session)
        admin_role = await accounts.get_role_by_name(session, ADMIN_ROLE)
        assert admin_role is not None
        existing = await accounts.get_user_by_email(session, email)
        if existing is None:
            await accounts.create_user(
                session, email=email, password=password, role_ids=[admin_role.id]
            )
            return "created"
        role_ids = {r.id for r in existing.roles} | {admin_role.id}
        await accounts.update_user(
            session, existing, password=password, is_active=True, role_ids=role_ids
        )
        return "updated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="create or reset an admin user")
    admin.add_argument("--email", required=True)
    args = parser.parse_args(argv)

    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    try:
        result = asyncio.run(create_admin(args.email, password))
    except accounts.AccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"admin {args.email}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
