"""Integration — re-ingesting an edited document only re-embeds changed chunks (real Postgres)."""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.db import ops
from app.db.enums import DocumentStatus, JobStatus
from app.services.chunking import chunker_fingerprint
from app.services.ingestion import ingest_document
from app.services.parser import ParseResult
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeEmbedder, FakeStore
from tests.vector_helpers import InspectableStore, reader_for, real_store


def make_text(n_words: int = 3000, seed: int = 3) -> str:
    rng = random.Random(seed)
    return " ".join(f"w{rng.randrange(5000)}k" for _ in range(n_words))


class Env:
    """One document whose source text can be edited between ingests."""

    def __init__(self, session: AsyncSession, tmp_path: Path, doc_id: uuid.UUID, store: Any) -> None:
        self.session, self.doc_id = session, doc_id
        self.embedder, self.store = FakeEmbedder(), store
        self.text = ""
        self.settings = Settings()

    def parse(self, path: Path, ft: str, cfg: Settings) -> ParseResult:
        return ParseResult(text=self.text, format="text", filetype=ft, provider="fake")

    async def run(self) -> dict[str, Any]:
        job = await ops.create_job(document_id=self.doc_id, session=self.session)
        job_id = job.id
        before = len(self.embedder.embedded_texts)
        status = await ingest_document(
            job_id,
            session=self.session,
            settings=self.settings,
            parse_fn=self.parse,
            embedder=self.embedder,
            store=self.store,
        )
        self.session.expire_all()
        row = await ops.get_job(job_id, session=self.session)
        assert row is not None
        return {
            "status": status,
            "stats": row.stats,
            "embedded_now": len(self.embedder.embedded_texts) - before,
            "error": row.error,
        }


@pytest.fixture(params=["fake", "chroma", "pgvector"])
async def env(
    request: pytest.FixtureRequest, db_session: AsyncSession, tmp_path: Path, isolated_db_url: str
) -> AsyncIterator[Env]:
    """The whole incremental-update suite runs against the in-memory fake AND both real stores."""
    src = tmp_path / "d.txt"
    src.write_text("x")
    doc = await ops.create_document(
        filename="d.txt", content_type="text/plain", source_path=str(src), session=db_session
    )
    if request.param == "fake":
        yield Env(db_session, tmp_path, doc.id, FakeStore())
        return
    with real_store(request.param, isolated_db_url, metric="cosine", dimension=3) as real:
        yield Env(db_session, tmp_path, doc.id, InspectableStore(real, reader_for(request.param, real, isolated_db_url)))


async def test_first_ingest_is_full(env: Env) -> None:
    env.text = make_text()
    r = await env.run()
    assert r["status"] == JobStatus.DONE and r["stats"]["mode"] == "full"
    assert r["stats"]["embedded"] == r["stats"]["added"] > 10


async def test_unchanged_reingest_embeds_nothing(env: Env) -> None:
    env.text = make_text()
    await env.run()
    r = await env.run()
    assert r["stats"]["mode"] == "incremental"
    assert r["embedded_now"] == 0 and r["stats"]["added"] == 0 and r["stats"]["removed"] == 0
    assert r["stats"]["kept"] == r["stats"]["chunks_total"]


async def test_local_edit_embeds_only_the_neighbourhood(env: Env) -> None:
    env.text = make_text()
    first = await env.run()
    total = first["stats"]["chunks_total"]
    mid = len(env.text) // 2
    env.text = env.text[:mid] + " INSERTED SYNTHETIC SENTENCE ABOUT NOTHING " + env.text[mid:]
    r = await env.run()
    assert r["stats"]["mode"] == "incremental", r["stats"]
    assert 1 <= r["embedded_now"] <= 4 < total / 4
    assert r["stats"]["kept"] >= total - 4
    # the store now matches the new document exactly
    rows = await ops.list_chunks_by_document(env.doc_id, session=env.session)
    assert env.store.texts_for(str(env.doc_id)) == {c.text for c in rows}
    assert any("INSERTED SYNTHETIC" in t for t in env.store.texts_for(str(env.doc_id)))


async def test_kept_vectors_get_their_position_refreshed(env: Env) -> None:
    env.text = make_text()
    await env.run()
    ids_before = set(env.store.vectors)
    # a block big enough to add several chunks at the top shifts every later chunk's position
    env.text = make_text(600, seed=99) + " " + env.text
    r = await env.run()
    assert r["stats"]["mode"] == "incremental", r["stats"]
    assert (
        len(ids_before & set(env.store.vectors)) >= len(ids_before) - 2
    )  # same content => same ids
    assert "update_metadata" in env.store.calls
    rows = await ops.list_chunks_by_document(env.doc_id, session=env.session)
    first_pos: dict[str, int] = {}
    for c in sorted(rows, key=lambda c: c.index):
        first_pos.setdefault(c.metadata_["vector_id"], c.index)
    assert set(first_pos) == set(env.store.vectors)
    for vid, v in env.store.vectors.items():
        assert v["metadata"]["index"] == first_pos[vid], vid


async def test_deleting_a_section_removes_its_vectors(env: Env) -> None:
    env.text = make_text()
    await env.run()
    n_before = len(env.store.vectors)
    third = len(env.text) // 3
    env.text = env.text[:third] + env.text[third + 600 :]
    r = await env.run()
    assert r["stats"]["mode"] == "incremental" and r["stats"]["removed"] >= 1
    rows = await ops.list_chunks_by_document(env.doc_id, session=env.session)
    assert env.store.texts_for(str(env.doc_id)) == {c.text for c in rows}
    assert len(env.store.vectors) < n_before + 2


async def test_rewriting_most_of_the_document_is_still_incremental_by_default(env: Env) -> None:
    """A total rewrite has nothing to reuse, but the update must still be correct and never embed
    more than a rebuild would (the old 50% rule forced rebuilds even when half could be reused)."""
    env.text = make_text(seed=1)
    await env.run()
    env.text = make_text(seed=2)
    r = await env.run()
    assert r["stats"]["mode"] == "incremental"
    rows = await ops.list_chunks_by_document(env.doc_id, session=env.session)
    assert env.store.texts_for(str(env.doc_id)) == {c.text for c in rows}


async def test_max_churn_setting_restores_the_full_rebuild_guard(env: Env) -> None:
    env.text = make_text(seed=1)
    await env.run()
    env.settings = Settings(incremental_max_churn=0.5)
    env.text = make_text(seed=2)
    r = await env.run()
    assert r["stats"]["mode"] == "full" and r["stats"]["reason"] == "most chunks changed"
    rows = await ops.list_chunks_by_document(env.doc_id, session=env.session)
    assert env.store.texts_for(str(env.doc_id)) == {c.text for c in rows}


async def test_changed_chunker_config_forces_full_rebuild(env: Env) -> None:
    env.text = make_text()
    await env.run()
    env.settings = Settings(chunk_size=300, chunk_overlap=40)
    r = await env.run()
    assert r["stats"]["mode"] == "full" and "config" in r["stats"]["reason"]


async def test_legacy_index_is_rebuilt(env: Env) -> None:
    """Chunks written before content-addressed vectors (no vector_id) must not be diffed."""
    env.text = make_text(1500)
    await ops.create_chunks(
        document_id=env.doc_id,
        chunks=[
            {
                "index": 0,
                "text": "old",
                "metadata": {
                    "content_hash": "h",
                    "chunker_fingerprint": chunker_fingerprint(
                        env.settings.chunk_size, env.settings.chunk_overlap
                    ),
                },
            }
        ],
        session=env.session,
    )
    r = await env.run()
    assert r["stats"]["mode"] == "full" and "legacy" in r["stats"]["reason"]


async def test_embed_failure_keeps_the_previous_version_searchable(env: Env) -> None:
    env.text = make_text()
    await env.run()
    snapshot = set(env.store.vectors)
    env.text = env.text + " brand new tail content that needs embedding " * 20
    env.embedder.fail = True
    r = await env.run()
    assert r["status"] == JobStatus.FAILED
    assert set(env.store.vectors) == snapshot  # nothing was mutated before the embed failed
    assert len(await ops.list_chunks_by_document(env.doc_id, session=env.session)) > 0


async def test_store_failure_after_mutation_leaves_a_clean_slate(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.text = make_text()
    await env.run()
    env.text = env.text[:5000] + " NEW MATERIAL " * 30 + env.text[5000:]

    def boom(*a: Any, **k: Any) -> None:
        raise ConnectionError("chroma went away")

    monkeypatch.setattr(env.store, "add_chunks", boom)
    r = await env.run()
    assert r["status"] == JobStatus.FAILED
    assert not env.store.texts_for(str(env.doc_id))  # no half-updated vectors
    assert await ops.list_chunks_by_document(env.doc_id, session=env.session) == []
    # ...so the next attempt is a clean full rebuild that succeeds
    monkeypatch.undo()
    r2 = await env.run()
    assert r2["status"] == JobStatus.DONE and r2["stats"]["mode"] == "full"
    doc = await ops.get_document(env.doc_id, session=env.session)
    assert doc is not None and doc.status == DocumentStatus.READY
