"""Integration tests — ingest orchestration against real Postgres, faked parse/embed/store."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.db import ops
from app.db.enums import DocumentStatus, JobStatus
from app.services.ingestion import ingest_document
from app.services.parser import ParseResult
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeEmbedder, FakeStore

LONG_TEXT = " ".join(f"synthetic{i}" for i in range(600))



def _parse_ok(text: str = LONG_TEXT):
    def fn(path: Path, filetype: str, settings: Settings) -> ParseResult:
        return ParseResult(text=text, format="text", filetype=filetype, provider="fake")

    return fn


def _parse_boom(path: Path, filetype: str, settings: Settings) -> ParseResult:
    raise RuntimeError("parser exploded")


async def _setup(session: AsyncSession, tmp_path: Path, *, create_file: bool = True):
    src = tmp_path / "doc.pdf"
    if create_file:
        src.write_bytes(b"%PDF-synthetic")
    doc = await ops.create_document(
        filename="doc.pdf", content_type="application/pdf", source_path=str(src), session=session
    )
    job = await ops.create_job(document_id=doc.id, session=session)
    return doc, job


async def test_happy_path(db_session: AsyncSession, tmp_path: Path) -> None:
    doc, job = await _setup(db_session, tmp_path)
    doc_id, job_id = doc.id, job.id
    store = FakeStore()
    status = await ingest_document(
        job.id,
        session=db_session,
        settings=Settings(),
        parse_fn=_parse_ok(),
        embedder=FakeEmbedder(),
        store=store,
    )
    assert status == JobStatus.DONE
    db_session.expire_all()
    assert (await ops.get_job(job_id, session=db_session)).status == JobStatus.DONE  # type: ignore[union-attr]
    assert (await ops.get_document(doc_id, session=db_session)).status == DocumentStatus.READY  # type: ignore[union-attr]
    rows = await ops.list_chunks_by_document(doc_id, session=db_session)
    assert len(rows) > 1
    assert [r.index for r in rows] == list(range(len(rows)))
    assert len(store.vectors) == len({r.metadata_['content_hash'] for r in rows})
    assert "content_hash" in rows[0].metadata_ and "chunker_fingerprint" in rows[0].metadata_


async def test_rerun_replaces_chunks_without_duplicating(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    doc, job = await _setup(db_session, tmp_path)
    store = FakeStore()
    kwargs: dict[str, Any] = dict(
        session=db_session, settings=Settings(), embedder=FakeEmbedder(), store=store
    )
    await ingest_document(job.id, parse_fn=_parse_ok(), **kwargs)
    first = len(await ops.list_chunks_by_document(doc.id, session=db_session))
    await ingest_document(job.id, parse_fn=_parse_ok(), **kwargs)
    second = len(await ops.list_chunks_by_document(doc.id, session=db_session))
    assert first == second > 0
    assert store.texts_for(str(doc.id))


async def test_parse_failure_marks_failed_and_records_error(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    doc, job = await _setup(db_session, tmp_path)
    doc_id, job_id = doc.id, job.id
    status = await ingest_document(
        job.id,
        session=db_session,
        settings=Settings(),
        parse_fn=_parse_boom,
        embedder=FakeEmbedder(),
        store=FakeStore(),
    )
    assert status == JobStatus.FAILED
    db_session.expire_all()
    failed = await ops.get_job(job_id, session=db_session)
    assert failed is not None and "parser exploded" in (failed.error or "")
    assert (await ops.get_document(doc_id, session=db_session)).status == DocumentStatus.FAILED  # type: ignore[union-attr]
    assert await ops.list_chunks_by_document(doc_id, session=db_session) == []


async def test_embed_failure_leaves_no_vectors(db_session: AsyncSession, tmp_path: Path) -> None:
    doc, job = await _setup(db_session, tmp_path)
    store = FakeStore()
    status = await ingest_document(
        job.id,
        session=db_session,
        settings=Settings(),
        parse_fn=_parse_ok(),
        embedder=FakeEmbedder(fail=True),
        store=store,
    )
    assert status == JobStatus.FAILED
    assert not store.texts_for(str(doc.id))


async def test_missing_source_file_fails_cleanly(db_session: AsyncSession, tmp_path: Path) -> None:
    _, job = await _setup(db_session, tmp_path, create_file=False)
    job_id = job.id
    status = await ingest_document(
        job.id,
        session=db_session,
        settings=Settings(),
        parse_fn=_parse_ok(),
        embedder=FakeEmbedder(),
        store=FakeStore(),
    )
    assert status == JobStatus.FAILED
    db_session.expire_all()
    job_row = await ops.get_job(job_id, session=db_session)
    assert job_row is not None and "source file not found" in (job_row.error or "")


async def test_empty_text_fails(db_session: AsyncSession, tmp_path: Path) -> None:
    _, job = await _setup(db_session, tmp_path)
    status = await ingest_document(
        job.id,
        session=db_session,
        settings=Settings(),
        parse_fn=_parse_ok("   "),
        embedder=FakeEmbedder(),
        store=FakeStore(),
    )
    assert status == JobStatus.FAILED


async def test_unknown_job_returns_failed_without_raising(db_session: AsyncSession) -> None:
    status = await ingest_document(
        uuid.uuid4(),
        session=db_session,
        settings=Settings(),
        parse_fn=_parse_ok(),
        embedder=FakeEmbedder(),
        store=FakeStore(),
    )
    assert status == JobStatus.FAILED


async def test_relative_source_path_resolves_under_data_dir(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "r.pdf").write_bytes(b"%PDF")
    doc = await ops.create_document(
        filename="r.pdf",
        content_type="application/pdf",
        source_path="sub/r.pdf",
        session=db_session,
    )
    job = await ops.create_job(document_id=doc.id, session=db_session)
    status = await ingest_document(
        job.id,
        session=db_session,
        settings=Settings(data_dir=tmp_path),
        parse_fn=_parse_ok(),
        embedder=FakeEmbedder(),
        store=FakeStore(),
    )
    assert status == JobStatus.DONE


@pytest.mark.parametrize("name", ["process_document"])
def test_task_is_registered_with_stable_name(name: str) -> None:
    from app.celery_app import celery_app
    from app.tasks import ingest

    assert getattr(ingest, name).name == "app.tasks.ingest.process_document"
    assert "app.tasks.ingest.process_document" in celery_app.tasks


async def test_successful_ingest_records_a_trace_with_stats(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    from app.services import traces

    doc, job = await _setup(db_session, tmp_path)
    doc_id = doc.id
    await ingest_document(
        job.id, session=db_session, settings=Settings(), parse_fn=_parse_ok(),
        embedder=FakeEmbedder(), store=FakeStore(),
    )
    rows, _ = await traces.list_traces(db_session, kind="ingest", limit=200)
    mine = [t for t in rows if t.document_id == doc_id]
    assert len(mine) == 1
    t = mine[0]
    assert (t.status, t.error, t.model) == ("ok", None, Settings().embedding_model)
    assert t.meta["mode"] == "full" and t.meta["chunks_total"] > 1 and t.meta["embedded"] > 1
    assert t.duration_ms >= 0


async def test_failed_ingest_records_an_error_trace(db_session: AsyncSession, tmp_path: Path) -> None:
    from app.services import traces

    doc, job = await _setup(db_session, tmp_path)
    doc_id = doc.id
    await ingest_document(
        job.id, session=db_session, settings=Settings(), parse_fn=_parse_boom,
        embedder=FakeEmbedder(), store=FakeStore(),
    )
    rows, _ = await traces.list_traces(db_session, kind="ingest", status="error", limit=200)
    mine = [t for t in rows if t.document_id == doc_id]
    assert len(mine) == 1 and "parser exploded" in (mine[0].error or "")
