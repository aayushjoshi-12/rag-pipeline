"""Integration tests — documents/jobs API over ASGI with real Postgres, faked broker + store."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.db import ops
from app.db.enums import JobStatus
from app.main import app
from app.routers import deps
from sqlalchemy.ext.asyncio import AsyncSession


class FakeStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def add_chunks(
        self, doc_id: str, chunks: list[dict[str, Any]], embeddings: list[list[float]]
    ) -> None:
        pass

    def similarity_search(
        self, query_embedding: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    def delete_doc(self, doc_id: str) -> None:
        self.deleted.append(doc_id)

    def reset(self) -> None:
        pass


class Harness:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.fail_enqueue = False
        self.store = FakeStore()


@pytest.fixture
async def client(
    db_session: AsyncSession, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, Harness, Path]]:
    h = Harness()
    cfg = Settings(data_dir=tmp_path, max_upload_size_bytes=2048)

    def enqueue(job_id: str) -> str:
        if h.fail_enqueue:
            raise ConnectionError("broker down")
        h.enqueued.append(job_id)
        return f"task-{len(h.enqueued)}"

    async def session_dep() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: cfg
    app.dependency_overrides[deps.get_enqueue] = lambda: enqueue
    app.dependency_overrides[deps.get_store] = lambda: h.store
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            yield c, h, tmp_path
    finally:
        app.dependency_overrides.clear()


async def _upload(
    c: httpx.AsyncClient,
    name: str = "note.md",
    data: bytes = b"# hi\n\nsynthetic",
    ctype: str = "text/markdown",
) -> httpx.Response:
    return await c.post("/documents", files={"file": (name, data, ctype)})


async def test_upload_stores_file_creates_rows_and_enqueues(client: Any) -> None:
    c, h, root = client
    r = await _upload(c)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["document"]["status"] == "pending"
    assert body["job"]["status"] == "pending"
    assert body["job"]["task_id"] == "task-1"
    assert h.enqueued == [body["job"]["id"]]
    stored = list((root / "uploads").rglob("note.md"))
    assert len(stored) == 1 and stored[0].read_bytes() == b"# hi\n\nsynthetic"


async def test_rejects_unsupported_extension(client: Any) -> None:
    c, h, _ = client
    r = await _upload(c, "evil.exe", b"MZ", "application/octet-stream")
    assert r.status_code == 415
    assert h.enqueued == []


async def test_rejects_implausible_content_type(client: Any) -> None:
    c, _, _ = client
    assert (await _upload(c, "a.pdf", b"%PDF", "image/png")).status_code == 415


async def test_rejects_oversize_and_cleans_up(client: Any) -> None:
    c, h, root = client
    r = await _upload(c, "big.txt", b"x" * 4096, "text/plain")
    assert r.status_code == 413
    assert h.enqueued == []
    assert not list((root / "uploads").rglob("big.txt"))


async def test_rejects_empty_file(client: Any) -> None:
    c, _, _ = client
    assert (await _upload(c, "e.txt", b"", "text/plain")).status_code == 400


async def test_path_traversal_filename_is_neutralised(client: Any) -> None:
    c, _, root = client
    r = await _upload(c, "../../etc/passwd.txt", b"hello", "text/plain")
    assert r.status_code == 201
    assert r.json()["document"]["filename"] == "passwd.txt"
    assert not (root.parent / "etc").exists()
    assert list((root / "uploads").rglob("passwd.txt"))


async def test_enqueue_failure_returns_503_and_marks_failed(
    client: Any, db_session: AsyncSession
) -> None:
    c, h, _ = client
    h.fail_enqueue = True
    r = await _upload(c, "q.txt", b"data", "text/plain")
    assert r.status_code == 503
    docs = (await c.get("/documents")).json()
    mine = next(d for d in docs if d["filename"] == "q.txt")
    assert mine["status"] == "failed"


async def test_list_get_job_and_chunks(client: Any, db_session: AsyncSession) -> None:
    c, _, _ = client
    up = (await _upload(c, "list-me.txt", b"data", "text/plain")).json()
    doc_id, job_id = up["document"]["id"], up["job"]["id"]

    assert doc_id in {d["id"] for d in (await c.get("/documents")).json()}
    detail = (await c.get(f"/documents/{doc_id}")).json()
    assert [j["id"] for j in detail["jobs"]] == [job_id]
    assert (await c.get(f"/jobs/{job_id}")).json()["status"] == "pending"

    await ops.create_chunks(
        document_id=uuid.UUID(doc_id),
        chunks=[{"index": 0, "text": "alpha", "metadata": {"k": 1}}],
        session=db_session,
    )
    chunks = (await c.get(f"/documents/{doc_id}/chunks")).json()
    assert chunks[0]["text"] == "alpha" and chunks[0]["metadata"] == {"k": 1}

    await ops.update_job_status(uuid.UUID(job_id), JobStatus.DONE, session=db_session)
    assert (await c.get(f"/jobs/{job_id}")).json()["status"] == "done"


async def test_not_found_and_bad_ids(client: Any) -> None:
    c, _, _ = client
    missing = uuid.uuid4()
    for path in (f"/documents/{missing}", f"/documents/{missing}/chunks", f"/jobs/{missing}"):
        assert (await c.get(path)).status_code == 404
    assert (await c.delete(f"/documents/{missing}")).status_code == 404
    assert (await c.get("/documents/not-a-uuid")).status_code == 422


async def test_delete_removes_row_vectors_and_file(client: Any) -> None:
    c, h, root = client
    up = (await _upload(c, "gone.txt", b"bye", "text/plain")).json()
    doc_id = up["document"]["id"]
    assert list((root / "uploads").rglob("gone.txt"))

    assert (await c.delete(f"/documents/{doc_id}")).status_code == 204
    assert h.store.deleted == [doc_id]
    assert (await c.get(f"/documents/{doc_id}")).status_code == 404
    assert (await c.get(f"/jobs/{up['job']['id']}")).status_code == 404  # cascade
    assert not list((root / "uploads").rglob("gone.txt"))


async def test_api_never_parses(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.parser as parser_mod

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("API must not parse")

    monkeypatch.setattr(parser_mod, "parse", boom)
    c, _, _ = client
    assert (await _upload(c)).status_code == 201


async def _put(
    c: httpx.AsyncClient,
    doc_id: str,
    name: str = "note.md",
    data: bytes = b"# v2\n\nchanged",
    ctype: str = "text/markdown",
) -> httpx.Response:
    return await c.put(f"/documents/{doc_id}", files={"file": (name, data, ctype)})


async def test_put_replaces_file_creates_new_job_and_removes_old_file(client: Any) -> None:
    c, h, root = client
    up = (await _upload(c, "v.md", b"version one")).json()
    doc_id = up["document"]["id"]
    old = next(iter((root / "uploads").rglob("v.md")))

    r = await _put(c, doc_id, "v.md", b"version two")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document"]["id"] == doc_id and body["document"]["status"] == "pending"
    assert body["job"]["id"] != up["job"]["id"] and len(h.enqueued) == 2
    assert h.enqueued[-1] == body["job"]["id"]
    files = list((root / "uploads").rglob("v.md"))
    assert len(files) == 1 and files[0].read_bytes() == b"version two" and files[0] != old
    jobs = (await c.get(f"/documents/{doc_id}")).json()["jobs"]
    assert len(jobs) == 2


async def test_put_can_change_the_file_type(client: Any) -> None:
    c, _, _ = client
    doc_id = (await _upload(c, "a.md", b"x")).json()["document"]["id"]
    r = await _put(c, doc_id, "a.txt", b"plain", "text/plain")
    assert (
        r.json()["document"]["filename"] == "a.txt"
        and r.json()["document"]["content_type"] == "text/plain"
    )


async def test_put_unknown_document_is_404_and_stores_nothing(client: Any) -> None:
    c, h, root = client
    r = await _put(c, str(uuid.uuid4()))
    assert r.status_code == 404 and h.enqueued == []
    assert not list((root / "uploads").rglob("note.md"))


async def test_put_validation_keeps_the_existing_version(client: Any) -> None:
    c, h, root = client
    doc_id = (await _upload(c, "keep.md", b"original")).json()["document"]["id"]
    assert (await _put(c, doc_id, "bad.exe", b"MZ", "application/octet-stream")).status_code == 415
    assert (await _put(c, doc_id, "big.txt", b"x" * 4096, "text/plain")).status_code == 413
    assert (await _put(c, doc_id, "empty.txt", b"", "text/plain")).status_code == 400
    assert len(h.enqueued) == 1
    kept = list((root / "uploads").rglob("keep.md"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"original"
    assert (await c.get(f"/documents/{doc_id}")).json()["status"] == "pending"


async def test_put_enqueue_failure_is_503_and_marks_failed(client: Any) -> None:
    c, h, _ = client
    doc_id = (await _upload(c, "q.md", b"one")).json()["document"]["id"]
    h.fail_enqueue = True
    assert (await _put(c, doc_id)).status_code == 503
    assert (await c.get(f"/documents/{doc_id}")).json()["status"] == "failed"
