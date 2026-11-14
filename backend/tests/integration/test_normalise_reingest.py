
"""An existing document ingested WITH ligatures re-ingests once, incrementally, when the text is normalised."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.db import ops
from app.db.enums import JobStatus
from app.services.ingestion import ingest_document
from app.services.parser import ParseResult
from app.services.textnorm import normalise_text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeEmbedder, FakeStore

PARAS = [
    (f"Section {i}. The lender must conﬁrm the oﬀer terms and the oﬃcial ﬁnal figure number {i} before closing. "
     "Borrowers receive the notice within ten business days and may appeal the decision in writing. ") * 2
    if i % 5 == 0
    else f"Section {i}. A plain paragraph without any special characters, number {i}, describing routine servicing steps in detail. " * 3
    for i in range(60)
]
RAW = "\n\n".join(PARAS)


def _parse(text: str):
    def fn(path: Path, filetype: str, settings: Settings) -> ParseResult:
        return ParseResult(text=text, format="text", filetype=filetype, provider="fake")

    return fn


async def test_normalising_an_existing_document_reingests_incrementally(db_session: AsyncSession, tmp_path: Path) -> None:
    src = tmp_path / "d.txt"
    src.write_text("x")
    doc = await ops.create_document(filename="d.txt", content_type="text/plain", source_path=str(src), session=db_session)
    job1 = await ops.create_job(document_id=doc.id, session=db_session)
    job2 = await ops.create_job(document_id=doc.id, session=db_session)
    job1_id, job2_id, doc_id = job1.id, job2.id, doc.id
    store, emb = FakeStore(), FakeEmbedder()
    kw = dict(session=db_session, settings=Settings(), embedder=emb, store=store)
    assert await ingest_document(job1_id, parse_fn=_parse(RAW), **kw) == JobStatus.DONE
    first = {r.metadata_["chunker_fingerprint"] for r in await ops.list_chunks_by_document(doc_id, session=db_session)}
    embedded_first = len(emb.embedded_texts)
    assert any("ﬁ" in r.text for r in await ops.list_chunks_by_document(doc_id, session=db_session))

    assert await ingest_document(job2_id, parse_fn=_parse(normalise_text(RAW)), **kw) == JobStatus.DONE
    job = await ops.get_job(job2_id, session=db_session)
    rows = await ops.list_chunks_by_document(doc_id, session=db_session)
    assert {r.metadata_["chunker_fingerprint"] for r in rows} == first  # same chunker: no forced full rebuild
    assert job.stats["mode"] == "incremental"  # type: ignore[index,union-attr]
    assert 0 < job.stats["embedded"] < embedded_first  # type: ignore[index,union-attr]  # only chunks that held a ligature
    assert job.stats["kept"] > 0  # type: ignore[index,union-attr]
    assert not any(c in r.text for r in rows for c in "ﬀﬁﬂﬃﬄ")
    assert all(c not in v["text"] for v in store.vectors.values() for c in "ﬀﬁﬃ")
