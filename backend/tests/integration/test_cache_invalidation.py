"""Every path that changes what is searchable must bump the cache index version (ticket 37)."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import Settings
from app.db import ops
from app.services import cache as cache_mod
from app.services import keyword as kw
from app.services.ingestion import ingest_document
from app.services.parser import ParseResult
from app.services.reindex import reindex_all
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeEmbedder, FakeStore


@pytest.fixture
def bumps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(cache_mod, "bump_index_version", lambda: calls.append("bump") or True)
    return calls


def _parse(path: Path, filetype: str, settings: Settings) -> ParseResult:
    return ParseResult(
        text=" ".join(f"w{i}" for i in range(400)),
        format="text",
        filetype=filetype,
        provider="fake",
    )


async def _doc(session: AsyncSession, tmp_path: Path):
    src = tmp_path / "d.pdf"
    src.write_bytes(b"%PDF-synthetic")
    doc = await ops.create_document(
        filename="d.pdf", content_type="application/pdf", source_path=str(src), session=session
    )
    job = await ops.create_job(document_id=doc.id, session=session)
    return doc, job


async def test_successful_ingest_bumps(
    db_session: AsyncSession, tmp_path: Path, bumps: list[str]
) -> None:
    _, job = await _doc(db_session, tmp_path)
    await ingest_document(
        job.id,
        session=db_session,
        settings=Settings(),
        parse_fn=_parse,
        embedder=FakeEmbedder(),
        store=FakeStore(),
    )
    assert bumps  # the new chunks are searchable now


async def test_delete_replace_and_rebuild_fts_bump(
    db_session: AsyncSession, tmp_path: Path, bumps: list[str]
) -> None:
    doc, _ = await _doc(db_session, tmp_path)
    n = len(bumps)
    await ops.replace_document_source(
        doc.id,
        filename="e.pdf",
        content_type="application/pdf",
        source_path=str(tmp_path / "d.pdf"),
        session=db_session,
    )
    assert len(bumps) > n
    n = len(bumps)
    await kw.rebuild_fts(db_session, "simple")
    await kw.rebuild_fts(db_session, "english")
    assert len(bumps) >= n + 2
    n = len(bumps)
    assert await ops.delete_document(doc.id, session=db_session)
    assert len(bumps) > n


class _Store(FakeStore):
    def verify(self) -> None:
        return None

    def describe(self) -> dict[str, object]:
        return {}


async def test_reindex_bumps_even_when_nothing_to_do(
    db_session: AsyncSession, bumps: list[str]
) -> None:
    await reindex_all(db_session, embedder=FakeEmbedder(), store=_Store(), force=True)
    assert bumps
