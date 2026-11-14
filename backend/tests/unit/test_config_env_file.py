"""Where Settings looks for ``.env`` files."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import _env_files


def test_default_reads_repo_root_then_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAG_ENV_FILE", raising=False)
    files = _env_files()
    assert isinstance(files, tuple)
    repo_env, cwd_env = files
    assert cwd_env == ".env"  # last wins, so a local .env overrides the repo-root one
    assert Path(repo_env).name == ".env"
    assert (Path(repo_env).parent / "backend" / "pyproject.toml").exists()


def test_explicit_env_file_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_ENV_FILE", "/tmp/custom.env")
    assert _env_files() == "/tmp/custom.env"
