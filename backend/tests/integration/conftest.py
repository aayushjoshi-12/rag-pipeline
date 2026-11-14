"""Fixtures for tests that need a real, isolated Postgres database.

``isolated_db_url`` creates a throwaway database on the compose Postgres, runs the real Alembic
migrations on it (so the migrations are tested too) and drops it afterwards. Nothing touches
the developer's actual ``rag`` database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from app.config import Settings
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_BACKEND = Path(__file__).resolve().parents[2]


def _admin_dsn(url: str) -> tuple[str, str]:
    parsed = make_url(url)
    dsn = parsed.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    return dsn, parsed.database or "rag"


@pytest.fixture(scope="module")
def isolated_db_url() -> Iterator[str]:
    base = Settings().database_url
    admin_dsn, _ = _admin_dsn(base)
    name = f"rag_test_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable for isolated DB tests: {exc}")
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": url, "RAG_ENV_FILE": os.devnull}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"
        yield url
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def isolated_session(isolated_db_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()
