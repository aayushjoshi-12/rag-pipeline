"""Cleanup of orphaned upload dirs — real Postgres for the 'referenced' check."""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.config import Settings
from app.db import ops
from app.services.cleanup import cleanup_orphan_uploads
from sqlalchemy.ext.asyncio import AsyncSession

DAY = 24 * 3600


def _mk(root: Path, name: str, age_hours: float) -> Path:
    d = root / "uploads" / name
    d.mkdir(parents=True)
    (d / "f.txt").write_text("x")
    t = time.time() - age_hours * 3600
    os.utime(d, (t, t))
    return d


async def test_removes_only_old_unreferenced_dirs(db_session: AsyncSession, tmp_path: Path) -> None:
    kept_ref = _mk(tmp_path, "aaa", 100)
    old_orphan = _mk(tmp_path, "bbb", 100)
    young_orphan = _mk(tmp_path, "ccc", 1)
    await ops.create_document(
        filename="f.txt", content_type="text/plain", source_path="uploads/aaa/f.txt", session=db_session
    )
    removed = await cleanup_orphan_uploads(db_session, tmp_path, min_age_hours=24)
    assert removed == ["bbb"]
    assert kept_ref.exists() and young_orphan.exists() and not old_orphan.exists()


async def test_absolute_source_paths_count_as_referenced(db_session: AsyncSession, tmp_path: Path) -> None:
    d = _mk(tmp_path, "abs1", 100)
    await ops.create_document(
        filename="f.txt", content_type="text/plain", source_path=str(d / "f.txt"), session=db_session
    )
    assert await cleanup_orphan_uploads(db_session, tmp_path, min_age_hours=0) == []


async def test_ignores_files_symlinks_and_outside_paths(db_session: AsyncSession, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "stray.txt").write_text("x")
    (uploads / "link").symlink_to(outside)
    old = time.time() - 100 * 3600
    os.utime(outside, (old, old))
    assert await cleanup_orphan_uploads(db_session, tmp_path, min_age_hours=0) == []
    assert (outside / "keep.txt").exists() and (uploads / "stray.txt").exists()


async def test_missing_uploads_dir_is_fine(db_session: AsyncSession, tmp_path: Path) -> None:
    assert await cleanup_orphan_uploads(db_session, tmp_path, min_age_hours=0) == []


def test_negative_age_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="cleanup_orphan_age_hours"):
        Settings(_env_file=None, cleanup_orphan_age_hours=-1)


def test_task_registered_and_swallows_errors(monkeypatch) -> None:
    from app.tasks import cleanup

    async def boom() -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr(cleanup, "_run", boom)
    assert cleanup.cleanup_temp() == 0
