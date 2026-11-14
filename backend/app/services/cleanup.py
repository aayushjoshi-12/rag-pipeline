"""Housekeeping: remove upload directories that no document references."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.services.storage import Storage, key_dir

logger = structlog.get_logger(__name__)


async def cleanup_orphan_uploads(
    session: AsyncSession,
    data_dir: Path,
    *,
    min_age_hours: float,
    now: float | None = None,
) -> list[str]:
    """Delete ``data_dir/uploads/<id>/`` dirs not referenced by any Document and older than
    ``min_age_hours`` (the age guard avoids racing an upload still being recorded).

    Only real directories directly under ``uploads/`` are considered; symlinks and files are
    ignored, so nothing outside the uploads root can be removed. Returns removed dir names.
    """
    uploads = data_dir / "uploads"
    if not uploads.is_dir():
        return []
    paths = (await session.execute(select(Document.source_path))).scalars().all()
    referenced = {Path(p).parts[1] for p in paths if p and len(Path(p).parts) >= 2 and Path(p).parts[0] == "uploads"}
    # absolute source_paths (older rows / tests) reference by directory name too
    referenced |= {Path(p).parent.name for p in paths if p and Path(p).is_absolute()}

    cutoff = (now if now is not None else time.time()) - min_age_hours * 3600
    removed: list[str] = []
    for entry in uploads.iterdir():
        if entry.is_symlink() or not entry.is_dir() or entry.name in referenced:
            continue
        if entry.stat().st_mtime > cutoff:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name)
        logger.info("orphan_upload_removed", dir=entry.name)
    return removed


async def cleanup_orphan_objects(
    session: AsyncSession,
    storage: Storage,
    *,
    backend: str,
    min_age_hours: float,
    now: float | None = None,
) -> list[str]:
    """Remote-storage twin of ``cleanup_orphan_uploads``.

    Removes objects that (a) ``storage.iter_objects`` reports - i.e. only keys under this app's
    configured prefix and of the exact shape ``uploads/<32 hex>/<name>`` - (b) no Document of this
    ``backend`` references, and (c) are older than ``min_age_hours``. Anything else in the bucket
    (other prefixes, other key shapes, other tenants' data) is never listed, let alone deleted.
    """
    rows = (
        await session.execute(
            select(Document.source_path).where(Document.storage_backend == backend)
        )
    ).scalars().all()
    referenced = {key_dir(p) for p in rows if p}
    cutoff = (now if now is not None else time.time()) - min_age_hours * 3600
    removed: list[str] = []
    for obj in storage.iter_objects():
        if key_dir(obj.key) in referenced or obj.modified > cutoff:
            continue
        storage.delete_object(obj.key)
        removed.append(obj.key)
        logger.info("orphan_object_removed", key=obj.key)
    return removed
