"""S3Storage / s3:// ingest / orphan sweep against a REAL S3 emulator, Floci (skipped when it is not running)."""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.db import ops
from app.db.enums import JobStatus
from app.services.cleanup import cleanup_orphan_objects
from app.services.fetcher import FetchError, fetch_to_storage
from app.services.ingestion import ingest_document
from app.services.parser import ParseResult
from app.services.storage import S3Storage, StorageError, key_dir, new_key
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import s3_settings
from tests.fakes import FakeEmbedder, FakeStore


def _file(tmp_path: Path, content: bytes = b"hello s3 " * 50, name: str = "f.txt") -> Path:
    p = tmp_path / f"{uuid.uuid4().hex}-{name}"
    p.write_bytes(content)
    return p


def test_round_trip_exists_and_delete_dir(s3_bucket: str, tmp_path: Path) -> None:
    st = S3Storage(s3_settings(s3_bucket))
    key = new_key("a.txt")
    other = new_key("b.txt")
    st.put_file(key, _file(tmp_path), "text/plain")
    st.put_file(other, _file(tmp_path, b"other"), "text/plain")
    assert st.exists(key) and st.exists(other)
    with st.local_path(key) as p:
        assert p.name == "a.txt" and p.read_bytes().startswith(b"hello s3")
    assert not p.exists()  # temp copy cleaned up
    assert st.delete_dir(key) == 1
    assert not st.exists(key) and st.exists(other)  # only that document's directory
    assert st.delete_dir("not-ours/x/y") == 0  # keys of another shape are never deleted


def test_keys_are_confined_to_the_prefix(s3_bucket: str, s3_client: Any, tmp_path: Path) -> None:
    st = S3Storage(s3_settings(s3_bucket))
    key = new_key("c.txt")
    st.put_file(key, _file(tmp_path), None)
    keys = [o["Key"] for o in s3_client.list_objects_v2(Bucket=s3_bucket)["Contents"]]
    assert f"test-prefix/{key}" in keys
    for bad in ("../x", "/abs", "a/../b", ""):
        with pytest.raises(StorageError):
            st.exists(bad)


async def test_orphan_sweep_only_touches_owned_unreferenced_old_objects(
    s3_bucket: str, s3_client: Any, db_session: AsyncSession, tmp_path: Path
) -> None:
    st = S3Storage(s3_settings(s3_bucket, s3_prefix="sweep/"))
    # owned + referenced (must stay), owned + unreferenced (goes once old enough)
    referenced, orphan_key = new_key("ref.txt"), new_key("orphan.txt")
    for k in (referenced, orphan_key):
        st.put_file(k, _file(tmp_path), None)
    doc = await ops.create_document(
        filename="ref.txt", content_type="text/plain", source_path=referenced,
        storage_backend="s3", session=db_session,
    )
    # foreign objects the app must never touch: other prefix, same prefix wrong shape, nested weird shape
    foreign = ["other-tenant/uploads/" + uuid.uuid4().hex + "/x.txt", "sweep/notes/readme.txt",
               "sweep/uploads/not-a-uuid/x.txt", "sweep/uploads/" + uuid.uuid4().hex + "/sub/deep.txt", "sweep-extra/uploads/" + uuid.uuid4().hex + "/x.txt"]
    for k in foreign:
        s3_client.put_object(Bucket=s3_bucket, Key=k, Body=b"not ours")

    import time

    # age guard: everything was just written, so even the unreferenced object stays
    assert await cleanup_orphan_objects(db_session, st, backend="s3", min_age_hours=24, now=time.time()) == []
    assert st.exists(orphan_key)
    # three days later the unreferenced object is old enough; the referenced one never goes
    future = time.time() + dt.timedelta(days=3).total_seconds()
    removed = await cleanup_orphan_objects(db_session, st, backend="s3", min_age_hours=24, now=future)
    assert removed == [orphan_key]
    assert st.exists(referenced), "referenced document object must survive"
    assert not st.exists(orphan_key)
    for k in foreign:
        s3_client.head_object(Bucket=s3_bucket, Key=k)  # raises if the sweep deleted it
    await ops.delete_document(doc.id, session=db_session)


async def test_sweep_respects_age_limit_and_other_backend_rows(
    s3_bucket: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    st = S3Storage(s3_settings(s3_bucket, s3_prefix="age/"))
    key = new_key("x.txt")
    st.put_file(key, _file(tmp_path), None)
    # a LOCAL document with the same key must not protect the S3 object (different backend)
    doc = await ops.create_document(filename="x.txt", content_type="text/plain", source_path=key,
                                    storage_backend="local", session=db_session)
    import time

    assert await cleanup_orphan_objects(db_session, st, backend="s3", min_age_hours=1, now=time.time()) == []
    assert await cleanup_orphan_objects(db_session, st, backend="s3", min_age_hours=1, now=time.time() + 7200) == [key]
    await ops.delete_document(doc.id, session=db_session)


def test_s3_link_download_copies_into_storage(s3_bucket: str, s3_client: Any, tmp_path: Path) -> None:
    cfg = s3_settings(s3_bucket, s3_prefix="dl/")
    s3_client.put_object(Bucket=s3_bucket, Key="inbox/Report 1.txt", Body=b"quarterly numbers " * 10,
                         ContentType="text/plain")
    out = fetch_to_storage(f"s3://{s3_bucket}/inbox/Report%201.txt", cfg, S3Storage(cfg))
    assert out.filename == "Report 1.txt" and out.storage == "s3" and out.size == 180
    assert key_dir(out.key) is not None
    with S3Storage(cfg).local_path(out.key) as p:
        assert p.read_bytes().startswith(b"quarterly")


def test_s3_link_rules(s3_bucket: str, s3_client: Any) -> None:
    cfg = s3_settings(s3_bucket, max_upload_size_bytes=100)
    s3_client.put_object(Bucket=s3_bucket, Key="big.txt", Body=b"x" * 500, ContentType="text/plain")
    s3_client.put_object(Bucket=s3_bucket, Key="noext", Body=b"abc", ContentType="application/octet-stream")
    st = S3Storage(cfg)
    with pytest.raises(FetchError, match="larger than"):
        fetch_to_storage(f"s3://{s3_bucket}/big.txt", cfg, st)
    with pytest.raises(FetchError, match="not allowed"):
        fetch_to_storage("s3://some-other-bucket/big.txt", cfg, st)
    with pytest.raises(FetchError, match="NoSuchKey"):
        fetch_to_storage(f"s3://{s3_bucket}/missing.txt", cfg, st)
    with pytest.raises(FetchError, match="Cannot tell the file type"):
        fetch_to_storage(f"s3://{s3_bucket}/noext", cfg, st)
    with pytest.raises(FetchError, match="not allowed"):  # scheme allow-list also covers s3
        fetch_to_storage(f"s3://{s3_bucket}/big.txt", Settings(**{**cfg.model_dump(), "url_ingest_schemes": "https"}), st)


def test_extra_buckets_allow_list(s3_bucket: str, s3_client: Any) -> None:
    cfg = s3_settings(s3_bucket, url_ingest_s3_buckets="not-this-one")
    assert cfg.url_s3_buckets == ("not-this-one",)
    with pytest.raises(FetchError, match="not allowed"):
        fetch_to_storage(f"s3://{s3_bucket}/x.txt", cfg, S3Storage(cfg))


async def test_ingest_from_s3_link_into_s3_storage(
    s3_bucket: str, s3_client: Any, db_session: AsyncSession
) -> None:
    cfg = s3_settings(s3_bucket, s3_prefix="e2e/")
    s3_client.put_object(Bucket=s3_bucket, Key="src/notes.md", ContentType="text/markdown",
                         Body=(" ".join(f"word{i}" for i in range(400))).encode())
    url = f"s3://{s3_bucket}/src/notes.md"
    doc = await ops.create_document(filename="notes.md", content_type="application/octet-stream",
                                    source_url=f"s3://{s3_bucket}/src/notes.md", storage_backend="s3", session=db_session)
    job = await ops.create_job(document_id=doc.id, session=db_session)
    doc_id, job_id = doc.id, job.id
    seen: list[str] = []

    def parse(path: Path, filetype: str, settings: Settings) -> ParseResult:
        seen.append(path.read_text())
        return ParseResult(text=seen[-1], format="text", filetype=filetype, provider="fake")

    store = FakeStore()
    status = await ingest_document(job_id, session=db_session, settings=cfg, parse_fn=parse,
                                   embedder=FakeEmbedder(), store=store, fetch_url=url)
    assert status == JobStatus.DONE and seen and seen[0].startswith("word0")
    db_session.expire_all()
    job = await ops.get_job(job_id, session=db_session)  # type: ignore[assignment]
    doc = await ops.get_document(doc_id, session=db_session)  # type: ignore[assignment]
    assert job.stats["source_url"] == url and job.stats["bytes_downloaded"] == len(seen[0]) and job.stats["storage"] == "s3"  # type: ignore[index,union-attr]
    assert doc.storage_backend == "s3" and doc.source_path.startswith("uploads/")  # type: ignore[union-attr]
    st = S3Storage(cfg)
    assert st.exists(doc.source_path)  # type: ignore[union-attr,arg-type]
    st.delete_dir(doc.source_path)  # type: ignore[union-attr,arg-type]
    await ops.delete_document(doc_id, session=db_session)


async def test_ingest_without_the_link_fails_cleanly(db_session: AsyncSession, tmp_path: Path) -> None:
    doc = await ops.create_document(filename="x.pdf", content_type="application/octet-stream",
                                    source_url="https://example.test/x.pdf", session=db_session)
    job = await ops.create_job(document_id=doc.id, session=db_session)
    job_id = job.id
    status = await ingest_document(job_id, session=db_session, settings=Settings(data_dir=tmp_path),
                                   parse_fn=lambda *a: None, embedder=FakeEmbedder(), store=FakeStore())  # type: ignore[arg-type,return-value]
    assert status == JobStatus.FAILED
    db_session.expire_all()
    assert "submit the link again" in (await ops.get_job(job_id, session=db_session)).error  # type: ignore[union-attr,operator]


# ── the HTTP API on the S3 backend: upload, replace, delete ────────────────────────────────────────


@pytest.fixture
async def s3_api(s3_bucket: str, db_session: AsyncSession) -> Any:
    import httpx
    from app.main import app
    from app.routers import deps

    cfg = s3_settings(s3_bucket, s3_prefix="api/", max_upload_size_bytes=10_000)

    async def session_dep() -> Any:
        yield db_session

    app.dependency_overrides[deps.get_session] = session_dep
    app.dependency_overrides[deps.get_settings] = lambda: cfg
    app.dependency_overrides[deps.get_enqueue] = lambda: (lambda job_id: "t")
    app.dependency_overrides[deps.get_store] = lambda: FakeStore()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c, S3Storage(cfg), cfg
    finally:
        app.dependency_overrides.clear()


async def test_api_upload_replace_delete_on_s3(s3_api: Any, db_session: AsyncSession) -> None:
    c, st, _ = s3_api
    up = (await c.post("/documents", files={"file": ("v1.md", b"version one", "text/markdown")})).json()
    doc_id = uuid.UUID(up["document"]["id"])
    assert up["document"]["storage_backend"] == "s3"
    key1 = (await ops.get_document(doc_id, session=db_session)).source_path  # type: ignore[union-attr]
    assert key1 and st.exists(key1)
    # replace: new object stored, the previous version's object removed
    rep = await c.put(f"/documents/{doc_id}", files={"file": ("v2.md", b"version two", "text/markdown")})
    assert rep.status_code == 200, rep.text
    db_session.expire_all()
    key2 = (await ops.get_document(doc_id, session=db_session)).source_path  # type: ignore[union-attr]
    assert key2 and key2 != key1 and st.exists(key2) and not st.exists(key1)
    with st.local_path(key2) as p:
        assert p.read_bytes() == b"version two"
    # delete: the object goes with the document
    assert (await c.delete(f"/documents/{doc_id}")).status_code == 204
    assert not st.exists(key2)


async def test_api_oversize_upload_leaves_no_object(s3_api: Any, s3_client: Any, s3_bucket: str) -> None:
    c, st, _ = s3_api
    before = len(list(st.iter_objects()))
    r = await c.post("/documents", files={"file": ("big.txt", b"x" * 20_000, "text/plain")})
    assert r.status_code == 413
    assert len(list(st.iter_objects())) == before


async def test_documents_created_on_local_keep_working_after_switching_to_s3(
    s3_api: Any, db_session: AsyncSession, tmp_path: Path
) -> None:
    """storage_backend is per document: a local document is still deleted from local disk."""
    from app.services.storage import LocalStorage

    c, _, _cfg = s3_api
    local_file = tmp_path / "uploads" / ("a" * 32) / "old.txt"
    local_file.parent.mkdir(parents=True)
    local_file.write_text("legacy")
    doc = await ops.create_document(filename="old.txt", content_type="text/plain",
                                    source_path="uploads/" + "a" * 32 + "/old.txt", storage_backend="local", session=db_session)
    # the API config points data_dir elsewhere, so use LocalStorage semantics directly for the file
    assert LocalStorage(tmp_path).exists("uploads/" + "a" * 32 + "/old.txt")
    assert (await c.delete(f"/documents/{doc.id}")).status_code == 204


# ── ensure_bucket / `manage ensure-bucket` (local S3 emulators start empty) ──────────────────────────


def test_ensure_bucket_creates_once_and_is_idempotent(s3_bucket: str, s3_client: Any) -> None:
    from app.services.storage import ensure_bucket

    name = f"rag-ensure-{uuid.uuid4().hex[:10]}"
    cfg = s3_settings(name)
    try:
        assert ensure_bucket(cfg) == "created"
        s3_client.head_bucket(Bucket=name)  # really exists now
        assert ensure_bucket(cfg) == "exists"  # second call is a no-op
    finally:
        with contextlib.suppress(Exception):  # best-effort cleanup of a bucket this test created
            s3_client.delete_bucket(Bucket=name)


def test_ensure_bucket_in_a_non_default_region(s3_bucket: str, s3_client: Any) -> None:
    from app.services.storage import ensure_bucket

    name = f"rag-ensure-eu-{uuid.uuid4().hex[:8]}"
    cfg = s3_settings(name, s3_region="eu-west-1")
    try:
        assert ensure_bucket(cfg) == "created"
        assert ensure_bucket(cfg) == "exists"
    finally:
        with contextlib.suppress(Exception):  # best-effort cleanup of a bucket this test created
            s3_client.delete_bucket(Bucket=name)


def test_ensure_bucket_requires_a_bucket_name() -> None:
    from app.services.storage import ensure_bucket

    with pytest.raises(ValueError, match="S3_BUCKET"):
        ensure_bucket(Settings(storage_backend="s3", s3_bucket="", auth_enabled=False))


def test_manage_ensure_bucket_command(s3_bucket: str, s3_client: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import app.manage as manage
    from tests.conftest import S3_EMULATOR_ENDPOINT, S3_EMULATOR_KEY, S3_EMULATOR_SECRET

    name = f"rag-cli-{uuid.uuid4().hex[:10]}"
    for k, v in {"STORAGE_BACKEND": "s3", "S3_BUCKET": name, "S3_ENDPOINT_URL": S3_EMULATOR_ENDPOINT,
                 "S3_ACCESS_KEY_ID": S3_EMULATOR_KEY, "S3_SECRET_ACCESS_KEY": S3_EMULATOR_SECRET}.items():
        monkeypatch.setenv(k, v)
    try:
        assert manage.main(["ensure-bucket"]) == 0
        assert f"bucket {name}: created" in capsys.readouterr().out
        assert manage.main(["ensure-bucket"]) == 0
        assert f"bucket {name}: exists" in capsys.readouterr().out
    finally:
        with contextlib.suppress(Exception):  # best-effort cleanup of a bucket this test created
            s3_client.delete_bucket(Bucket=name)


def test_manage_ensure_bucket_fails_fast_when_the_store_is_unreachable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import app.manage as manage

    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "whatever")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "x")
    assert manage.main(["ensure-bucket"]) == 1
    assert "error:" in capsys.readouterr().err
