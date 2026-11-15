"""POST /documents/from-url end to end: API -> job -> (worker-side) fetch -> ingest, with a REAL HTTP
server on loopback. The SSRF guard must block that server by default; URL_INGEST_ALLOW_PRIVATE=true
(tests/smoke only) is the only thing that lets these tests reach it."""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from app.config import Settings
from app.db.enums import JobStatus
from app.main import app
from app.routers import deps
from app.services.fetcher import FetchError, fetch_to_storage
from app.services.ingestion import ingest_document
from app.services.parser import ParseResult
from app.services.storage import LocalStorage
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fakes import FakeEmbedder, FakeStore, api_client, make_settings

TEXT = (" ".join(f"loopback{i}" for i in range(300))).encode()


class _Handler(BaseHTTPRequestHandler):
    sent_bytes = 0
    hits: ClassVar[list[str]] = []

    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        path = self.path.split("?")[0]
        if path == "/doc.txt":
            self._ok(TEXT, "text/plain")
        elif path == "/page":
            self._ok(b"<html>hi</html>", "text/html")
        elif path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/doc.txt?token=SECRET")
            self.end_headers()
        elif path == "/to-metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif path == "/endless.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()  # no Content-Length: the size cap must be enforced while streaming
            try:
                for _ in range(5000):
                    self.wfile.write(b"a" * 65536)
                    type(self).sent_bytes += 65536
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)

    def _ok(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class Harness:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []
        self.fail_enqueue = False


def _cfg(tmp_path: Path, **kw: Any) -> Settings:
    kw.setdefault("max_upload_size_bytes", 100_000)
    return make_settings(data_dir=tmp_path, **kw)


@pytest.fixture
async def api(
    db_session: AsyncSession, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, Harness, Path]]:
    h = Harness()

    def enqueue_url(job_id: str, url: str) -> str:
        if h.fail_enqueue:
            raise ConnectionError("broker down")
        h.enqueued.append((job_id, url))
        return f"task-{len(h.enqueued)}"

    async with api_client(
        db_session, enqueue_url=lambda: enqueue_url, store=lambda: FakeStore()
    ) as c:
        yield c, h, tmp_path


def _use(cfg: Settings) -> None:
    app.dependency_overrides[deps.get_settings] = lambda: cfg


def _parse(path: Path, filetype: str, settings: Settings) -> ParseResult:
    return ParseResult(text=path.read_text(), format="text", filetype=filetype, provider="fake")


async def test_loopback_server_is_blocked_by_default(api: Any, server: str) -> None:
    c, h, tmp = api
    _use(_cfg(tmp))
    before = len(_Handler.hits)
    n_docs = len((await c.get("/documents")).json())
    r = await c.post("/documents/from-url", json={"url": f"{server}/doc.txt"})
    assert r.status_code == 400 and "private or internal" in r.json()["detail"]
    assert len((await c.get("/documents")).json()) == n_docs  # nothing was created
    assert h.enqueued == [] and len(_Handler.hits) == before  # and the server was never contacted


def test_worker_side_fetch_also_blocks_loopback_by_default(server: str, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    before = len(_Handler.hits)
    with pytest.raises(FetchError, match="refused"):
        fetch_to_storage(f"{server}/doc.txt", cfg, LocalStorage(tmp_path))
    assert len(_Handler.hits) == before


async def test_full_flow_with_explicit_allow_private(api: Any, server: str, db_session: AsyncSession) -> None:
    c, h, tmp = api
    cfg = _cfg(tmp, url_ingest_allow_private=True)
    _use(cfg)
    url = f"{server}/redir?sig=TOPSECRET"
    r = await c.post("/documents/from-url", json={"url": url})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["document"]["source_url"] == f"{server}/redir?…" and "TOPSECRET" not in r.text
    assert body["job"]["status"] == "pending" and h.enqueued == [(body["job"]["id"], url)]
    # the worker half
    import uuid

    job_id = uuid.UUID(body["job"]["id"])
    store = FakeStore()
    status = await ingest_document(job_id, session=db_session, settings=cfg, parse_fn=_parse,
                                   embedder=FakeEmbedder(), store=store, fetch_url=url)
    assert status == JobStatus.DONE
    job = (await c.get(f"/jobs/{job_id}")).json()
    assert job["stats"]["bytes_downloaded"] == len(TEXT) and job["stats"]["storage"] == "local"
    assert job["stats"]["source_url"] == f"{server}/redir?…"
    doc = (await c.get(f"/documents/{body['document']['id']}")).json()
    assert doc["filename"] == "doc.txt" and doc["status"] == "ready" and doc["content_type"] == "text/plain"
    assert "TOPSECRET" not in str(doc) and "TOPSECRET" not in str(job) and "SECRET" not in str(job)
    assert list((tmp / "uploads").rglob("doc.txt"))
    # delete removes the stored file
    assert (await c.delete(f"/documents/{doc['id']}")).status_code == 204
    assert not list((tmp / "uploads").rglob("doc.txt"))


async def test_same_url_twice_makes_two_documents(api: Any, server: str) -> None:
    c, h, tmp = api
    _use(_cfg(tmp, url_ingest_allow_private=True))
    a = (await c.post("/documents/from-url", json={"url": f"{server}/doc.txt"})).json()
    b = (await c.post("/documents/from-url", json={"url": f"{server}/doc.txt"})).json()
    assert a["document"]["id"] != b["document"]["id"] and len(h.enqueued) == 2


async def test_non_document_and_endless_responses_fail_the_job(api: Any, server: str, db_session: AsyncSession) -> None:
    c, _h, tmp = api
    cfg = _cfg(tmp, url_ingest_allow_private=True, url_ingest_max_bytes=200_000)
    _use(cfg)
    import uuid

    async def run(path: str) -> dict[str, Any]:
        url = f"{server}{path}"
        r = (await c.post("/documents/from-url", json={"url": url})).json()
        await ingest_document(uuid.UUID(r["job"]["id"]), session=db_session, settings=cfg, parse_fn=_parse,
                              embedder=FakeEmbedder(), store=FakeStore(), fetch_url=url)
        return (await c.get(f"/jobs/{r['job']['id']}")).json()  # type: ignore[no-any-return]

    page = await run("/page")
    assert page["status"] == "failed" and "web page" in page["error"]
    _Handler.sent_bytes = 0
    big = await run("/endless.txt")
    assert big["status"] == "failed" and "larger than" in big["error"]
    time.sleep(0.2)
    assert _Handler.sent_bytes < 5_000_000  # the server was cut off, not drained
    assert not list((tmp / "uploads").rglob("*"))  # nothing half-downloaded was kept
    missing = await run("/nope.txt")
    assert "HTTP 404" in missing["error"]


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/a.pdf", "file:///etc/passwd", "http://user:pw@example.com/a.pdf", "http://169.254.169.254/",
     "http://[::ffff:127.0.0.1]/", "http://2130706433/", "http://localhost:8000/health", "s3://not-configured/x.pdf", "not a url"],
)
async def test_api_rejects_hostile_urls_with_400(api: Any, url: str) -> None:
    c, h, tmp = api
    _use(_cfg(tmp))
    n_docs = len((await c.get("/documents")).json())
    r = await c.post("/documents/from-url", json={"url": url})
    assert r.status_code == 400, r.text
    assert isinstance(r.json()["detail"], str) and "pw" not in r.json()["detail"]
    assert len((await c.get("/documents")).json()) == n_docs and h.enqueued == []


async def test_validation_and_broker_failure(api: Any, server: str) -> None:
    c, h, tmp = api
    _use(_cfg(tmp, url_ingest_allow_private=True))
    assert (await c.post("/documents/from-url", json={})).status_code == 422
    assert (await c.post("/documents/from-url", json={"url": "x" * 3000})).status_code == 422
    h.fail_enqueue = True
    r = await c.post("/documents/from-url", json={"url": f"{server}/doc.txt"})
    assert r.status_code == 503
    docs = (await c.get("/documents")).json()
    assert docs[0]["status"] == "failed"


def test_openapi_documents_the_endpoint() -> None:
    op = app.openapi()["paths"]["/v1/documents/from-url"]["post"]
    assert ("worker" in op["description"] and "NEW" in op["description"]) or "new" in op["description"]
    assert {"201", "400", "401", "403", "422", "503"} <= set(op["responses"])
    schema = app.openapi()["components"]["schemas"]["FromUrlRequest"]["properties"]["url"]
    assert schema["examples"] and "NEW document" in schema["description"]
