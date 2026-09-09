"""Shared test fixtures — async DB session against compose Postgres."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from app.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yield an async session for integration tests.

    Creates a fresh engine with NullPool per test to avoid event-loop
    conflicts across pytest-asyncio test functions.

    Prereq: compose Postgres must be up and migrated (make db-up && make migrate).
    """
    settings = Settings()
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        poolclass=NullPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()
