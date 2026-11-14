"""URL download: streaming size cap, redirect re-validation, pinned connections, type/filename rules."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.services.fetcher import FetchError, download_http, fetch_to_storage, resolve_type
from app.services.filenames import safe_name
from app.services.storage import LocalStorage
from app.services.urlguard import UrlRejected

PUBLIC = "93.184.216.34"
Handler = Callable[[httpx.Request], httpx.Response]


def _cfg(tmp_path: Path, **kw: Any) -> Settings:
    kw.setdefault("max_upload_size_bytes", 1000)
    return Settings(data_dir=tmp_path / "data", auth_enabled=False, **kw)


def _factory(handler: Handler) -> Callable[[Settings], httpx.Client]:
    return lambda _s: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _resolver(mapping: dict[str, list[str]]) -> Callable[[str, int], list[str]]:
    def resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise UrlRejected(f"Could not resolve host '{host}'.")
        return mapping[host]

    return resolve


RES = _resolver({"example.test": [PUBLIC], "cdn.test": ["93.184.216.35"], "evil.test": ["10.1.2.3"]})


def _dl(tmp_path: Path, url: str, handler: Handler, **kw: Any) -> tuple[int, str, str, str]:
    return download_http(url, tmp_path / "out", _cfg(tmp_path, **kw), resolver=RES, client_factory=_factory(handler))


def test_downloads_and_connects_to_the_validated_ip(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain; charset=utf-8"})

    size, ctype, _, _ = _dl(tmp_path, "https://example.test/a.txt?q=1", handler)
    assert (size, ctype) == (5, "text/plain")
    assert (tmp_path / "out").read_bytes() == b"hello"
    req = seen[0]
    # DNS-rebinding defence: the socket target is the IP we validated, the name only goes in Host/SNI
    assert req.url.host == PUBLIC and req.headers["host"] == "example.test"
    assert req.url.query == b"q=1"


def test_size_cap_enforced_while_streaming_without_content_length(tmp_path: Path) -> None:
    pulled = 0

    def body() -> Iterator[bytes]:
        nonlocal pulled
        for _ in range(1000):  # an endless-ish stream, no Content-Length
            pulled += 1
            yield b"x" * 70_000

    with pytest.raises(FetchError, match="larger than"):
        _dl(tmp_path, "https://example.test/big.txt", lambda r: httpx.Response(200, content=body()))
    assert pulled < 5  # stopped at the first chunk over the cap, never read the 70 MB stream


def test_size_cap_ignores_a_lying_content_length(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"y" * 5000, headers={"content-length": "10"})

    # httpx itself notices the mismatch or we cap by counting: either way it must not succeed
    with pytest.raises((FetchError, httpx.HTTPError)):
        _dl(tmp_path, "https://example.test/lie.txt", handler)


def test_declared_oversize_is_refused_early(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"content-length": "99999"})

    with pytest.raises(FetchError, match="larger than"):
        _dl(tmp_path, "https://example.test/a.txt", handler)


def test_url_ingest_max_bytes_overrides_upload_limit(tmp_path: Path) -> None:
    h: Handler = lambda r: httpx.Response(200, content=b"z" * 500)  # noqa: E731
    with pytest.raises(FetchError):
        _dl(tmp_path, "https://example.test/a.txt", h, url_ingest_max_bytes=100)
    assert _dl(tmp_path, "https://example.test/a.txt", h)[0] == 500


def test_gzip_bomb_is_capped_on_decoded_bytes(tmp_path: Path) -> None:
    import gzip

    bomb = gzip.compress(b"0" * 200_000)
    assert len(bomb) < 1000

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"})

    with pytest.raises(FetchError, match="larger than"):
        _dl(tmp_path, "https://example.test/bomb.txt", handler)


def test_redirect_to_private_address_is_refused(tmp_path: Path) -> None:
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.host)
        return httpx.Response(302, headers={"location": "http://evil.test/secret"})

    with pytest.raises(FetchError, match="redirect target was refused"):
        _dl(tmp_path, "https://example.test/a.txt", handler)
    assert hits == [PUBLIC]  # the private hop was never connected to


@pytest.mark.parametrize(
    "location",
    ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://2130706433/",
     "http://localhost/", "file:///etc/passwd", "ftp://example.test/x", "s3://bucket/key", "gopher://example.test/"],
)
def test_redirect_targets_are_each_validated(tmp_path: Path, location: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": location})

    with pytest.raises(FetchError, match="redirect target was refused"):
        _dl(tmp_path, "https://example.test/a.txt", handler)


def test_follows_redirects_across_hosts_and_relative_locations(tmp_path: Path) -> None:
    chain: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        chain.append((req.url.host, req.url.path))
        if req.url.path == "/a":
            return httpx.Response(301, headers={"location": "https://cdn.test/b"})
        if req.url.path == "/b":
            return httpx.Response(302, headers={"location": "/c.txt"})
        return httpx.Response(200, content=b"final", headers={"content-type": "text/plain"})

    assert _dl(tmp_path, "https://example.test/a", handler)[0] == 5
    assert chain == [(PUBLIC, "/a"), ("93.184.216.35", "/b"), ("93.184.216.35", "/c.txt")]


def test_redirect_loop_and_limit(tmp_path: Path) -> None:
    h: Handler = lambda r: httpx.Response(302, headers={"location": "https://example.test/again"})  # noqa: E731
    with pytest.raises(FetchError, match="Too many redirects"):
        _dl(tmp_path, "https://example.test/a", h, url_ingest_max_redirects=3)


def test_rebinding_between_hops_cannot_reach_private_address(tmp_path: Path) -> None:
    """The name resolves publicly for the first hop and privately afterwards (rebinding)."""
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    def resolver(host: str, port: int) -> list[str]:
        return next(answers)

    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.host)
        return httpx.Response(302, headers={"location": "https://rebind.test/next"})

    with pytest.raises(FetchError, match="refused"):
        download_http("https://rebind.test/a", tmp_path / "o", _cfg(tmp_path), resolver=resolver, client_factory=_factory(handler))
    assert hits == [PUBLIC]


def test_private_first_hop_and_http_errors(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="link was refused"):
        _dl(tmp_path, "http://evil.test/a.txt", lambda r: httpx.Response(200, content=b"x"))
    with pytest.raises(FetchError, match="HTTP 404"):
        _dl(tmp_path, "https://example.test/a.txt", lambda r: httpx.Response(404))
    with pytest.raises(FetchError, match="Location"):
        _dl(tmp_path, "https://example.test/a.txt", lambda r: httpx.Response(302))


def test_timeouts_and_connect_errors_become_fetch_errors(tmp_path: Path) -> None:
    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("t")

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-host-detail")

    with pytest.raises(FetchError, match="did not respond"):
        _dl(tmp_path, "https://example.test/a.txt", slow)
    with pytest.raises(FetchError, match="Could not connect") as ei:
        _dl(tmp_path, "https://example.test/a.txt", down)
    assert "secret-host-detail" not in str(ei.value)


def test_total_deadline(tmp_path: Path) -> None:
    import time

    def body() -> Iterator[bytes]:
        for _ in range(50):
            time.sleep(0.02)
            yield b"x"

    with pytest.raises(FetchError, match="too long"):
        _dl(tmp_path, "https://example.test/slow.txt", lambda r: httpx.Response(200, content=body()),
            url_ingest_timeout_seconds=0.05, url_ingest_total_timeout_seconds=0.2)


def test_no_credentials_or_query_in_errors(tmp_path: Path) -> None:
    url = "https://example.test/a.txt?X-Amz-Signature=TOPSECRET"
    with pytest.raises(FetchError) as ei:
        _dl(tmp_path, url, lambda r: httpx.Response(500))
    assert "TOPSECRET" not in str(ei.value)


# ── type / filename rules ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "ctype", "head", "expected"),
    [
        ("/docs/report.pdf", "application/pdf", b"%PDF-1.7", "report.pdf"),
        ("/docs/report.pdf", "application/octet-stream", b"%PDF-1.4", "report.pdf"),
        ("/download", "application/pdf", b"%PDF-1.4", "download.pdf"),
        ("/dl.php", "text/plain", b"hello", "dl.txt"),
        ("/x", "text/markdown", b"# hi", "x.md"),
        ("/notes.MD", "", b"# hi", "notes.MD"),
        ("/%2e%2e%2f%2e%2e%2fetc%2fpasswd.txt", "text/plain", b"hi", "passwd.txt"),
    ],
)
def test_resolve_type_accepts(name: str, ctype: str, head: bytes, expected: str) -> None:
    assert resolve_type(url_path=name, hint="", content_type=ctype, head=head)[0] == expected


@pytest.mark.parametrize(
    ("name", "ctype", "head"),
    [
        ("/page.html", "text/html", b"<html>"),
        ("/a.txt", "text/html", b"<html>"),
        ("/x", "text/html", b"<html>"),
        ("/x", "image/png", b"\x89PNG"),
        ("/x", "", b"data"),
        ("/fake.pdf", "application/pdf", b"<html>not a pdf"),
        ("/fake.docx", "application/octet-stream", b"not a zip"),
        ("/bin.txt", "text/plain", b"ab\x00cd"),
        ("/run.exe", "application/octet-stream", b"MZ"),
    ],
)
def test_resolve_type_rejects(name: str, ctype: str, head: bytes) -> None:
    with pytest.raises(FetchError):
        resolve_type(url_path=name, hint="", content_type=ctype, head=head)


def test_content_disposition_filename_wins_and_is_sanitised() -> None:
    name, ext, _ = resolve_type(url_path="/dl", hint=safe_name("../../evil name.pdf"), content_type="application/pdf", head=b"%PDF-")
    assert name == "evil name.pdf" and ext == "pdf"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("/abs/path/file.pdf", "file.pdf"),
        ("..", "upload"),
        (".", "upload"),
        ("", "upload"),
        ("a\x00b.pdf", "ab.pdf"),
        ("na me;$(rm -rf).pdf", "na me___rm -rf_.pdf"),
        ("ünï.pdf", "_n_.pdf"),
        ("a" * 400 + ".pdf", "a" * 200),
        ("  .hidden  ", "hidden"),
    ],
)
def test_safe_name(raw: str, expected: str) -> None:
    out = safe_name(raw)
    assert out == expected
    assert ("/" not in out and "\\" not in out and ".." not in out.replace("...", "")) or out == expected


def test_fetch_to_storage_end_to_end_local(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    storage = LocalStorage(cfg.data_dir)
    out = fetch_to_storage(
        "https://example.test/dir/My%20Doc.txt?token=SECRET",
        cfg,
        storage,
        resolver=RES,
        client_factory=_factory(lambda r: httpx.Response(200, content=b"body text", headers={"content-type": "text/plain"})),
    )
    assert out.filename == "My Doc.txt" and out.size == 9 and out.storage == "local"
    assert "SECRET" not in out.source_url and out.source_url.endswith("My%20Doc.txt?…")
    assert (cfg.data_dir / out.key).read_bytes() == b"body text"
    assert out.key.startswith("uploads/") and out.key.count("/") == 2


def test_fetch_rejects_empty_and_disallowed_scheme(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, url_ingest_schemes="https")
    st = LocalStorage(cfg.data_dir)
    with pytest.raises(FetchError, match="not allowed"):
        fetch_to_storage("http://example.test/a.txt", cfg, st, resolver=RES)
    with pytest.raises(FetchError, match="empty"):
        fetch_to_storage("https://example.test/a.txt", cfg, st, resolver=RES,
                         client_factory=_factory(lambda r: httpx.Response(200, content=b"", headers={"content-type": "text/plain"})))


@pytest.mark.parametrize("location", ["s3://bucket/key", "file:///etc/passwd", "ftp://example.test/x", "gopher://example.test/"])
def test_redirect_scheme_is_checked_even_when_the_host_would_resolve_publicly(tmp_path: Path, location: str) -> None:
    everything_public = lambda host, port: [PUBLIC]  # noqa: E731

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(FetchError, match="redirect target was refused"):
        download_http("https://example.test/a", tmp_path / "o", _cfg(tmp_path), resolver=everything_public, client_factory=_factory(handler))


def test_redirect_limit_is_exact(tmp_path: Path) -> None:
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(302, headers={"location": "https://example.test/again"})

    with pytest.raises(FetchError, match="Too many redirects"):
        _dl(tmp_path, "https://example.test/a", handler, url_ingest_max_redirects=3)
    assert n == 4  # the first request plus exactly 3 followed redirects


def test_default_client_ignores_proxy_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP(S)_PROXY would route the request through a proxy that never sees our IP validation."""
    from app.services.fetcher import _default_client

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    client = _default_client(_cfg(tmp_path))
    try:
        assert not client._mounts  # no proxy transport mounted
        assert client.follow_redirects is False
    finally:
        client.close()
