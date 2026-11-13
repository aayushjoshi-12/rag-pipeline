"""Shared test fixtures — async DB session against compose Postgres."""

from __future__ import annotations

import zipfile
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


@contextmanager
def _noop_generation(*args: Any, **kwargs: Any) -> Any:
    yield type("NullGen", (), {"update": lambda self, **_: None})()


@pytest.fixture(autouse=True)
def _no_langfuse_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit/integration tests fully offline wrt Langfuse.

    Strips creds so the lazy `get_client()` initialises as a disabled no-op, and swaps the
    observed `generation` seam for a no-op so nothing can attempt a real export regardless
    of how a test constructs Settings.
    """
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    from app.services import observability

    monkeypatch.setattr(observability, "generation", _noop_generation)


@pytest.fixture
def synthetic_docx(tmp_path: Path) -> Path:
    """A self-contained synthetic .docx (handcrafted OOXML zip — no python-docx dep)."""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:r><w:t>Hello RAG Parser</w:t></w:r></w:p>
<w:p><w:r><w:t>Second paragraph with numbers 12345.</w:t></w:r></w:p>
</w:body>
</w:document>"""
    path = tmp_path / "sample.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document)
    return path


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
