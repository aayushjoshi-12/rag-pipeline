"""Management commands.

``python -m app.manage create-admin --email you@example.com`` creates an admin, or resets the
password / re-activates / re-grants admin to an existing user. This is the recovery path when
nobody can sign in. The password is read from the ``ADMIN_PASSWORD`` env var or prompted for; it
is never accepted as a command-line argument.

``python -m app.manage vector-status`` prints the configured vs stored vector settings.
``python -m app.manage reindex [--force] [--queue]`` rebuilds the vector store from the stored chunk
text after changing VECTOR_* / embedding settings (``--queue`` hands it to a Celery worker).
``python -m app.manage fts-status`` / ``rebuild-fts`` show / recreate the keyword-search columns with
RETRIEVAL_FTS_CONFIG (needed after changing that setting).
``python -m app.manage rebuild-index`` retrains the pgvector ANN index on the current rows
(IVFFlat after bulk loads) without re-embedding.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
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


async def vector_status() -> dict[str, object]:
    from app.services.reindex import store_status
    from app.services.vectorstore import get_vector_store

    async with async_session() as session:
        return await store_status(session, get_vector_store(verify=False))


async def reindex(force: bool) -> dict[str, object]:
    from app.services.embeddings import get_embedder
    from app.services.reindex import reindex_all
    from app.services.vectorstore import get_vector_store

    async with async_session() as session:
        res = await reindex_all(
            session, embedder=get_embedder(), store=get_vector_store(verify=False), force=force
        )
    return {"documents": res.documents, "vectors": res.vectors, "seconds": round(res.seconds, 1)}


async def fts_status() -> dict[str, object]:
    from app.config import Settings
    from app.services.keyword import stored_fts_config

    async with async_session() as session:
        stored = await stored_fts_config(session)
    configured = Settings().retrieval_fts_config
    return {"configured": configured, "stored": stored, "ok": stored == configured}


async def rebuild_fts_cmd() -> str:
    from app.config import Settings
    from app.services.keyword import rebuild_fts

    config = Settings().retrieval_fts_config
    async with async_session() as session:
        await rebuild_fts(session, config)
    return f"keyword index rebuilt with text-search configuration '{config}'"


def rebuild_index() -> str:
    from app.services.vectorstore import get_vector_store

    store = get_vector_store(verify=False)
    rebuild = getattr(store, "rebuild_index", None)
    if rebuild is None:
        return "nothing to do: this store has no separately built index"
    store.verify()  # creates the table if missing
    rebuild()
    from app.services.cache import bump_index_version

    bump_index_version()
    return "index rebuilt"


async def _load_settings_layer() -> None:
    """CLI commands see the same effective settings as the server (the admin-UI layer included)."""
    from app.services import runtime_settings

    async with async_session() as session:
        await runtime_settings.refresh_quietly(session)


async def rotate_settings_key() -> dict[str, int]:
    """Re-encrypt every stored secret under the primary SETTINGS_ENCRYPTION_KEY."""
    from app.config import Settings
    from app.services import runtime_settings as rt

    cipher = rt.cipher_for(Settings())
    if not cipher.enabled:
        raise SystemExit("error: SETTINGS_ENCRYPTION_KEY is not set")
    done = failed = 0
    async with async_session() as session:
        for row in await rt.load_rows(session):
            if not row.is_secret or not row.secret_blob:
                continue
            try:
                row.secret_blob = cipher.rotate(row.key, row.secret_blob)
                done += 1
            except Exception:
                failed += 1
                print(f"error: cannot re-encrypt '{row.key}' (is the old key in SETTINGS_ENCRYPTION_KEY_PREVIOUS?)", file=sys.stderr)
        await session.commit()
    return {"rotated": done, "failed": failed}


def _ensure_bucket(wait: int) -> int:
    import time

    from app.config import Settings
    from app.services.storage import ensure_bucket

    cfg = Settings()
    deadline = time.monotonic() + wait
    while True:
        try:
            print(f"bucket {cfg.s3_bucket}: {ensure_bucket(cfg)}")
            return 0
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except (
            Exception
        ) as exc:  # emulator still starting, or a real failure once the deadline passes
            if time.monotonic() >= deadline:
                print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            time.sleep(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="create or reset an admin user")
    admin.add_argument("--email", required=True)
    sub.add_parser("vector-status", help="show configured vs stored vector-store settings")
    rx = sub.add_parser("reindex", help="re-embed all stored chunks into a fresh vector store")
    rx.add_argument("--force", action="store_true", help="run even if ingest jobs are running")
    rx.add_argument(
        "--queue", action="store_true", help="enqueue the Celery task instead of running inline"
    )
    sub.add_parser("fts-status", help="show configured vs stored keyword-search configuration")
    sub.add_parser(
        "rebuild-fts", help="recreate the keyword-search columns/index with RETRIEVAL_FTS_CONFIG"
    )
    sub.add_parser(
        "rotate-settings-key",
        help="re-encrypt stored secret settings under SETTINGS_ENCRYPTION_KEY (old key in ..._PREVIOUS)",
    )
    sub.add_parser("rebuild-index", help="rebuild the pgvector ANN index on the current rows")
    eb = sub.add_parser(
        "ensure-bucket", help="create S3_BUCKET if it does not exist (local S3 emulators)"
    )
    eb.add_argument(
        "--wait", type=int, default=0, help="retry for up to N seconds while the store starts"
    )
    args = parser.parse_args(argv)

    if args.command == "ensure-bucket":
        return _ensure_bucket(args.wait)
    if args.command == "rotate-settings-key":
        out = asyncio.run(rotate_settings_key())
        print(json.dumps(out))
        return 1 if out["failed"] else 0
    if args.command in ("vector-status", "reindex", "fts-status", "rebuild-fts", "rebuild-index"):
        asyncio.run(_load_settings_layer())
    if args.command == "vector-status":
        info = asyncio.run(vector_status())
        print(json.dumps(info, indent=2, default=str))
        return 1 if info.get("problems") else 0
    if args.command == "reindex":
        if args.queue:
            from app.tasks.reindex import reindex_vectors

            print(f"queued reindex task {reindex_vectors.delay(force=args.force).id}")
            return 0
        from app.services.reindex import ReindexError

        try:
            print(json.dumps(asyncio.run(reindex(args.force))))
        except ReindexError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "fts-status":
        info = asyncio.run(fts_status())
        print(json.dumps(info))
        return 0 if info["ok"] else 1
    if args.command == "rebuild-fts":
        print(asyncio.run(rebuild_fts_cmd()))
        return 0
    if args.command == "rebuild-index":
        print(rebuild_index())
        return 0

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
