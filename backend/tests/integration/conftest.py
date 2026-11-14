"""Integration-test fixtures. ``isolated_db_url`` (tests/conftest.py) provides the throwaway DB."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


@pytest.fixture
async def isolated_session(isolated_db_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()
