"""Download a document from an http(s) URL or an s3:// object into the configured storage (worker side).

Runs in the Celery worker, never in the API request. Every hop of an http(s) download is
validated by ``urlguard`` and the connection is made to the validated IP (Host header and TLS SNI
keep the original name), so DNS rebinding between "check" and "connect" cannot redirect it.
Nothing here logs or raises with the raw URL: only ``redact_url`` output (no userinfo / query).
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from app.config import Settings
from app.services.filenames import ALLOWED_EXTENSIONS, name_from_url_path, safe_name
from app.services.storage import S3Storage, Storage, new_key
from app.services.urlguard import (
    Resolver,
    Target,
    UrlRejected,
    check_http_target,
    parse_s3_link,
    redact_url,
)

logger = structlog.get_logger(__name__)

_REDIRECTS = {301, 302, 303, 307, 308}
_CHUNK = 64 * 1024
_TYPE_TO_EXT = {
    "application/pdf": "pdf",
    "application/x-pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/x-markdown": "md",
}
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_USER_AGENT = "rag-pipeline-fetcher/1.0"

ClientFactory = Callable[[Settings], httpx.Client]


class FetchError(Exception):
    """A download failed; the message is safe to store on the job and show to the user."""


@dataclass(frozen=True)
class Fetched:
    key: str
    filename: str
    content_type: str
    size: int
    storage: str
    source_url: str  # redacted


def _default_client(settings: Settings) -> httpx.Client:
    t = settings.url_ingest_timeout_seconds
    return httpx.Client(
        timeout=httpx.Timeout(t, connect=t),
        follow_redirects=False,  # we follow, re-validating every hop
        trust_env=False,  # never honour HTTP(S)_PROXY / .netrc: they would bypass the guard
        verify=True,
        headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"},
    )


def _content_disposition_name(value: str | None) -> str:
    if not value:
        return ""
    msg = Message()
    msg["content-disposition"] = value
    name = msg.get_filename()
    return safe_name(name) if name else ""


def _open_hop(client: httpx.Client, target: Target, headers: dict[str, str]) -> httpx.Response:
    """Connect to the validated addresses in order; returns the open (streaming) response."""
    last: Exception | None = None
    for ip in target.ips:
        host = f"[{ip}]" if ":" in ip else ip
        url = f"{target.scheme}://{host}:{target.port}{target.path_query}"
        request = client.build_request(
            "GET",
            url,
            headers={**headers, "Host": target.host_header},
            extensions={"sni_hostname": target.host},
        )
        try:
            return client.send(request, stream=True)
        except httpx.ConnectError as exc:
            last = exc
        except httpx.TimeoutException as exc:
            raise FetchError("The server did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"The download failed ({type(exc).__name__}).") from exc
    raise FetchError(f"Could not connect to the server ({type(last).__name__}).")


def download_http(
    url: str,
    dest: Path,
    settings: Settings,
    *,
    resolver: Resolver | None = None,
    client_factory: ClientFactory | None = None,
) -> tuple[int, str, str, str]:
    """Stream ``url`` into ``dest``.

    Returns (bytes, content-type, Content-Disposition name hint, path of the final URL after redirects).
    """
    max_bytes = settings.url_max_bytes
    deadline = time.monotonic() + settings.url_ingest_total_timeout_seconds
    client = (client_factory or _default_client)(settings)
    current = url
    try:
        for hop in range(settings.url_ingest_max_redirects + 1):
            try:
                target = check_http_target(
                    current,
                    schemes=settings.url_schemes,
                    allow_private=settings.url_ingest_allow_private,
                    resolver=resolver,
                )
            except UrlRejected as exc:
                where = "The link" if hop == 0 else "A redirect target"
                raise FetchError(f"{where} was refused: {exc}") from exc
            response = _open_hop(client, target, {"Accept": "application/pdf, text/*;q=0.9, */*;q=0.5"})
            with closing(response):
                if response.status_code in _REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(f"HTTP {response.status_code} redirect without a Location.")
                    current = urljoin(current, location)
                    continue
                if not 200 <= response.status_code < 300:
                    raise FetchError(f"The server answered HTTP {response.status_code}.")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(f"The file is larger than the {max_bytes} byte limit.")
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                hint = _content_disposition_name(response.headers.get("content-disposition"))
                total = 0
                with dest.open("wb") as out:
                    for chunk in response.iter_bytes(_CHUNK):
                        total += len(chunk)
                        if total > max_bytes:  # Content-Length can lie or be absent: count what arrives
                            raise FetchError(f"The file is larger than the {max_bytes} byte limit.")
                        if time.monotonic() > deadline:
                            raise FetchError("The download took too long.")
                        out.write(chunk)
                return total, ctype, hint, urlsplit(current).path
        raise FetchError(f"Too many redirects (more than {settings.url_ingest_max_redirects}).")
    except httpx.HTTPError as exc:  # mid-stream failures
        raise FetchError(f"The download failed ({type(exc).__name__}).") from exc
    finally:
        client.close()


def download_s3(url: str, dest: Path, settings: Settings, *, client: Any = None) -> tuple[int, str, str, str]:
    """Copy an ``s3://bucket/key`` object (allowed buckets only) into ``dest``, size-capped."""
    if not settings.s3_configured and not settings.url_ingest_s3_buckets:
        raise FetchError("s3:// links need S3 to be configured (S3_BUCKET, credentials).")
    try:
        link = parse_s3_link(url, allowed_buckets=settings.url_s3_buckets)
    except UrlRejected as exc:
        raise FetchError(f"The link was refused: {exc}") from exc
    max_bytes = settings.url_max_bytes
    source = S3Storage(settings, bucket=link.bucket, client=client)
    try:
        obj = source.client.get_object(Bucket=link.bucket, Key=link.key)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        raise FetchError(f"Could not read the s3:// object ({code}).") from None
    body = obj["Body"]
    try:
        if int(obj.get("ContentLength", 0)) > max_bytes:
            raise FetchError(f"The file is larger than the {max_bytes} byte limit.")
        total = 0
        with dest.open("wb") as out:
            for chunk in body.iter_chunks(_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(f"The file is larger than the {max_bytes} byte limit.")
                out.write(chunk)
    finally:
        body.close()
    return total, str(obj.get("ContentType", "")).split(";")[0].strip().lower(), "", urlsplit(url).path


def resolve_type(
    *, url_path: str, hint: str, content_type: str, head: bytes
) -> tuple[str, str, str]:
    """Decide (filename, extension, canonical content type) from name, header and file content."""
    if content_type in _HTML_TYPES:
        raise FetchError("The link returned a web page (HTML), not a document file.")
    name = hint or name_from_url_path(url_path)
    ext = Path(name).suffix.lstrip(".").lower() if name else ""
    if ext not in ALLOWED_EXTENSIONS:
        ext = _TYPE_TO_EXT.get(content_type, "")
        if not ext:
            raise FetchError(
                "Cannot tell the file type: the link has no .pdf/.docx/.txt/.md name and the server "
                f"sent '{content_type or 'no content type'}'."
            )
        stem = Path(name).stem if name and Path(name).suffix else (name or "download")
        name = safe_name(f"{stem}.{ext}")
    if ext == "pdf" and b"%PDF-" not in head[:1024]:
        raise FetchError("The file does not look like a PDF (no %PDF header).")
    if ext == "docx" and not head.startswith(b"PK\x03\x04"):
        raise FetchError("The file does not look like a DOCX (not a zip container).")
    if ext in ("txt", "md") and b"\x00" in head:
        raise FetchError("The file looks binary, not text.")
    return name, ext, ALLOWED_EXTENSIONS[ext]


def fetch_to_storage(
    url: str,
    settings: Settings,
    storage: Storage,
    *,
    resolver: Resolver | None = None,
    client_factory: ClientFactory | None = None,
    s3_client: Any = None,
) -> Fetched:
    """Download ``url`` (http, https or s3://) and store it. Raises ``FetchError`` with a safe message."""
    redacted = redact_url(url)
    scheme = urlsplit(url.strip()).scheme.lower()
    if scheme not in settings.url_schemes:
        raise FetchError(f"The link was refused: URL scheme '{scheme}' is not allowed.")
    logger.info("url_fetch_start", url=redacted)
    with tempfile.TemporaryDirectory(prefix="rag-fetch-") as tmp:
        dest = Path(tmp) / "download"
        if scheme == "s3":
            size, ctype, hint, path_for_name = download_s3(url, dest, settings, client=s3_client)
        else:
            size, ctype, hint, path_for_name = download_http(
                url, dest, settings, resolver=resolver, client_factory=client_factory
            )
        if size == 0:
            raise FetchError("The downloaded file is empty.")
        with dest.open("rb") as fh:
            head = fh.read(8192)
        name, _, canonical = resolve_type(
            url_path=path_for_name, hint=hint, content_type=ctype, head=head
        )
        key = new_key(name)
        storage.put_file(key, dest, canonical)
    logger.info("url_fetch_done", url=redacted, bytes=size, storage=storage.name)
    return Fetched(key, name, canonical, size, storage.name, redacted)
