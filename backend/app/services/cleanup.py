"""Housekeeping: remove upload directories that no document references."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document

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
