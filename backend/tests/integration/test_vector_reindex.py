"""Reindex: rebuild a vector store from the chunk text in Postgres (no parsing), on real services."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.db import ops
from app.db.enums import JobStatus
from app.services.ingestion import ingest_document
from app.services.parser import ParseResult
from app.services.pgvector_store import PgVectorStore, dsn_from_database_url
from app.services.reindex import ReindexError, reindex_all, store_status
from app.services.vectorstore import VectorStoreMismatchError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.test_incremental_ingest import make_text
from tests.vector_helpers import real_store, settings_for


@pytest.fixture(autouse=True)
async def _empty_database(db_session: AsyncSession) -> None:
    """Reindex is global (every chunk row), so each test starts from an empty isolated database."""
    from sqlalchemy import text

    await db_session.execute(text("TRUNCATE documents CASCADE"))
    await db_session.commit()


class HashEmbedder:
    """Deterministic 3-d embedding of the text, and a record of everything it was asked to embed."""

    def __init__(self) -> None:
        self.embedded: list[str] = []

    @staticmethod
    def vec(text: str) -> list[float]:
        d = hashlib.sha256(text.encode()).digest()
        return [d[0] / 255 + 0.01, d[1] / 255 + 0.01, d[2] / 255 + 0.01]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [self.vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.vec(text)


async def ingest(session: AsyncSession, tmp_path: Path, store: Any, text: str, name: str) -> uuid.UUID:
    src = tmp_path / name
    src.write_text("x")
    doc = await ops.create_document(filename=name, content_type="text/plain", source_path=str(src), session=session)
    job = await ops.create_job(document_id=doc.id, session=session)
    status = await ingest_document(
        job.id,
        session=session,
        settings=Settings(),
        parse_fn=lambda p, ft, cfg: ParseResult(text=text, format="text", filetype=ft, provider="fake"),
        embedder=HashEmbedder(),
        store=store,
    )
    assert status == JobStatus.DONE
    return doc.id


async def test_switching_store_leaves_it_empty_until_reindex_which_rebuilds_without_parsing(
    db_session: AsyncSession, tmp_path: Path, isolated_db_url: str
) -> None:
    with real_store("chroma", isolated_db_url) as chroma, real_store("pgvector", isolated_db_url) as pg:
        d1 = await ingest(db_session, tmp_path, chroma, make_text(1500, seed=1), "a.txt")
        d2 = await ingest(db_session, tmp_path, chroma, make_text(1500, seed=2), "b.txt")
        rows = [c for d in (d1, d2) for c in await ops.list_chunks_by_document(d, session=db_session)]
        expected_ids = {str(c.metadata_["vector_id"]) for c in rows}
        assert len(expected_ids) > 10

        # the "I switched VECTOR_STORE" situation: the new store is empty while the DB has chunks
        status = await store_status(db_session, pg)
        assert status["expected_vectors"] == len(expected_ids) and status["count"] == 0
        assert any("empty but the database holds" in p for p in status["problems"])

        embedder = HashEmbedder()
        result = await reindex_all(db_session, embedder=embedder, store=pg)
        assert result.documents == 2 and result.vectors == len(expected_ids)
        assert set(embedder.embedded) == {c.text for c in rows}  # only stored chunk text was embedded

        after = await store_status(db_session, pg)
        assert after["problems"] == [] and after["count"] == len(expected_ids)
        got = pg.similarity_search(HashEmbedder.vec(rows[0].text), 1)[0]
        assert got["text"] == rows[0].text and got["score"] == pytest.approx(0.0, abs=1e-5)
        # same ids + metadata shape as an ingest would produce -> incremental updates keep working
        assert {h["id"] for h in pg.similarity_search([1.0, 1.0, 1.0], 500)} == expected_ids
        meta = got["metadata"]
        assert {"doc_id", "index", "content_hash", "vector_id", "start", "end", "filename"} <= set(meta)

        # a later unchanged re-ingest into the REBUILT store embeds nothing (kept == all)
        job_id = (await ops.create_job(document_id=d1, session=db_session)).id
        second = HashEmbedder()
        await ingest_document(
            job_id, session=db_session, settings=Settings(), embedder=second, store=pg,
            parse_fn=lambda p, ft, cfg: ParseResult(text=make_text(1500, seed=1), format="text", filetype=ft, provider="fake"),
        )
        db_session.expire_all()
        j = await ops.get_job(job_id, session=db_session)
        assert j is not None and j.stats is not None and j.stats["mode"] == "incremental" and second.embedded == []


async def test_changed_metric_is_detected_then_fixed_by_reindex(
    db_session: AsyncSession, tmp_path: Path, isolated_db_url: str
) -> None:
    with real_store("pgvector", isolated_db_url, metric="cosine") as cos:
        await ingest(db_session, tmp_path, cos, make_text(1200, seed=4), "c.txt")
        l2 = PgVectorStore(dsn_from_database_url(isolated_db_url), settings_for("pgvector", "l2"), table=cos._table)  # type: ignore[attr-defined]
        with pytest.raises(VectorStoreMismatchError, match="VECTOR_METRIC"):
            l2.verify()
        await reindex_all(db_session, embedder=HashEmbedder(), store=l2)
        l2.verify()
        assert l2.describe()["stored"]["metric"] == "l2"
        text = (await ops.list_chunks_by_document((await _only_doc(db_session)), session=db_session))[0].text
        top = l2.similarity_search(HashEmbedder.vec(text), 1)[0]
        assert top["text"] == text and top["score"] == pytest.approx(0.0, abs=1e-5)  # squared l2 of identical vectors


async def _only_doc(session: AsyncSession) -> uuid.UUID:
    from app.db.models import Document
    from sqlalchemy import select

    return (await session.execute(select(Document.id).limit(1))).scalar_one()


async def test_changed_dimension_is_fixed_by_reindex(db_session: AsyncSession, tmp_path: Path, isolated_db_url: str) -> None:
    class Wide(HashEmbedder):
        @staticmethod
        def vec(text: str) -> list[float]:
            return [*HashEmbedder.vec(text), 0.5, 0.25]

    with real_store("pgvector", isolated_db_url, dimension=3) as small:
        await ingest(db_session, tmp_path, small, make_text(900, seed=6), "d.txt")
        wide = PgVectorStore(dsn_from_database_url(isolated_db_url), settings_for("pgvector", dimension=5), table=small._table)  # type: ignore[attr-defined]
        with pytest.raises(VectorStoreMismatchError, match="dimension"):
            wide.verify()
        with pytest.raises(VectorStoreMismatchError, match="reindex"):  # writes with the new model fail clearly too
            wide.add_chunks("x", [{"text": "t"}], [Wide.vec("t")])
        await reindex_all(db_session, embedder=Wide(), store=wide)
        assert wide.stored_settings()["dimension"] == 5  # type: ignore[index]
        assert wide.describe()["problems"] == [] and wide.describe()["count"] > 5


async def test_reindex_refuses_while_an_ingest_job_is_running_unless_forced(
    db_session: AsyncSession, tmp_path: Path, isolated_db_url: str
) -> None:
    with real_store("pgvector", isolated_db_url) as pg:
        await ingest(db_session, tmp_path, pg, make_text(600, seed=8), "e.txt")
        doc = await _only_doc(db_session)
        job = await ops.create_job(document_id=doc, session=db_session)
        await ops.update_job_status(job.id, JobStatus.RUNNING, session=db_session)
        with pytest.raises(ReindexError, match="running"):
            await reindex_all(db_session, embedder=HashEmbedder(), store=pg)
        assert (await reindex_all(db_session, embedder=HashEmbedder(), store=pg, force=True)).documents == 1


async def test_reindex_with_no_documents_just_recreates_the_empty_store(db_session: AsyncSession, isolated_db_url: str) -> None:
    with real_store("chroma", isolated_db_url) as chroma:
        result = await reindex_all(db_session, embedder=HashEmbedder(), store=chroma)
        assert (result.documents, result.vectors) == (0, 0)
        chroma.verify()


async def test_ivfflat_is_trained_once_after_the_bulk_load(db_session: AsyncSession, tmp_path: Path, isolated_db_url: str) -> None:
    with real_store("pgvector", isolated_db_url, index="ivfflat", ivfflat_lists=5, ivfflat_probes=5) as pg:
        await ingest(db_session, tmp_path, pg, make_text(1500, seed=9), "f.txt")
        await reindex_all(db_session, embedder=HashEmbedder(), store=pg)
        import psycopg

        with psycopg.connect(dsn_from_database_url(isolated_db_url)) as c:
            defs = [r[0] for r in c.execute("SELECT indexdef FROM pg_indexes WHERE tablename = %s", (pg._table,)).fetchall()]  # type: ignore[attr-defined]
        assert len([d for d in defs if "ivfflat" in d]) == 1 and "lists='5'" in "".join(defs)
        assert pg.describe()["index_state"] == "ok"


# ── entry points ──────────────────────────────────────────────────────────────────────────
def test_celery_task_is_registered_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.celery_app import celery_app
    from app.tasks import reindex as task_mod

    assert "app.tasks.reindex.reindex_vectors" in celery_app.tasks
    seen: list[bool] = []

    async def fake_run(force: bool) -> dict[str, Any]:
        seen.append(force)
        return {"documents": 0, "vectors": 0, "seconds": 0.0}

    monkeypatch.setattr(task_mod, "_run", fake_run)
    assert task_mod.reindex_vectors.run(force=True) == {"documents": 0, "vectors": 0, "seconds": 0.0}
    assert seen == [True]


async def test_manage_cli_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    import asyncio

    import app.manage as manage
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    monkeypatch.setattr(manage, "async_session", async_sessionmaker(engine, expire_on_commit=False))
    import app.services.embeddings as emb
    import app.services.vectorstore as vsmod

    with real_store("pgvector", isolated_db_url) as pg:
        await ingest(db_session, tmp_path, pg, make_text(600, seed=12), "g.txt")
        monkeypatch.setattr(vsmod, "get_vector_store", lambda *a, **k: pg)
        monkeypatch.setattr(emb, "get_embedder", lambda *a, **k: HashEmbedder())
        # main() uses asyncio.run, which cannot nest in the running test loop: run it in a thread
        capsys.readouterr()  # drop the ingest log lines
        code = await asyncio.to_thread(manage.main, ["vector-status"])
        status = json.loads(capsys.readouterr().out)
        assert code == 0 and status["store"] == "pgvector" and status["count"] > 0

        code = await asyncio.to_thread(manage.main, ["reindex"])
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])  # last line; structlog logs precede it
        assert code == 0 and out["documents"] == 1 and out["vectors"] > 0

        assert await asyncio.to_thread(manage.main, ["rebuild-index"]) == 0
        assert "index rebuilt" in capsys.readouterr().out
    await engine.dispose()
