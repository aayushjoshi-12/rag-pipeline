"""Pluggable file storage for uploaded / downloaded source documents (ticket 22).

Keys are relative and always have the shape ``uploads/<32 hex>/<file name>``; a document's
``source_path`` holds its key and ``documents.storage_backend`` says which backend holds it, so
changing ``STORAGE_BACKEND`` never strands existing documents.

* ``LocalStorage`` - ``DATA_DIR/<key>`` (default; unchanged on-disk layout).
* ``S3Storage``    - ``s3://<S3_BUCKET>/<S3_PREFIX><key>`` through boto3 (AWS or any S3-compatible
  store such as Floci, MinIO or R2). The app only ever lists, reads or deletes keys under its own prefix.

All methods are blocking; async callers wrap them in ``asyncio.to_thread``.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)

UPLOADS = "uploads"
#: ``uploads/<32 hex>/<name>`` - the only key shape this app creates, so the only one it ever deletes.
_KEY_RE = re.compile(r"uploads/[0-9a-f]{32}/[^/]+")
_DIR_RE = re.compile(r"uploads/[0-9a-f]{32}")


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredObject:
    key: str  # relative to the storage root / prefix
    size: int
    modified: float  # unix seconds


def new_key(filename: str) -> str:
    return f"{UPLOADS}/{uuid.uuid4().hex}/{filename}"


def key_dir(key: str) -> str | None:
    """``uploads/<id>`` of a key, or None when the key is not one of ours (never delete those)."""
    parts = key.split("/")
    if len(parts) >= 3 and _DIR_RE.fullmatch("/".join(parts[:2])):
        return "/".join(parts[:2])
    return None


class Storage(Protocol):
    name: str

    def put_file(self, key: str, path: Path, content_type: str | None = None) -> None: ...
    def local_path(self, key: str) -> Any:
        """Context manager yielding a local ``Path`` of the stored file (temp copy for remote stores)."""
    def exists(self, key: str) -> bool: ...
    def delete_dir(self, key: str) -> int:
        """Remove everything stored under the ``uploads/<id>/`` directory of ``key``."""
    def iter_objects(self) -> Iterator[StoredObject]:
        """Every object under ``uploads/`` (relative keys), for the orphan sweep."""
    def delete_object(self, key: str) -> None: ...


class LocalStorage:
    name = "local"

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir

    def _path(self, key: str) -> Path:
        p = Path(key)
        return p if p.is_absolute() else self.root / p  # absolute: rows written before ticket 22

    def put_file(self, key: str, path: Path, content_type: str | None = None) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), target)

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        yield self._path(key)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete_dir(self, key: str) -> int:
        """Only ever removes our own ``<root>/uploads/<id>`` directories."""
        parent = self._path(key).parent
        if parent.parent == self.root / UPLOADS and parent.is_dir() and not parent.is_symlink():
            shutil.rmtree(parent, ignore_errors=True)
            return 1
        return 0

    def iter_objects(self) -> Iterator[StoredObject]:
        uploads = self.root / UPLOADS
        if not uploads.is_dir():
            return
        for entry in uploads.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            yield StoredObject(f"{UPLOADS}/{entry.name}", 0, entry.stat().st_mtime)

    def delete_object(self, key: str) -> None:
        self.delete_dir(f"{key}/x")


class S3Storage:
    name = "s3"

    def __init__(self, settings: Settings, bucket: str | None = None, client: Any = None) -> None:
        self.bucket = bucket or settings.s3_bucket
        self.prefix = settings.s3_key_prefix
        if not self.bucket:
            raise StorageError("S3 storage needs S3_BUCKET.")
        self._settings = settings
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = make_s3_client(self._settings)
        return self._client

    def _full(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in key.split("/") or "\x00" in key:
            raise StorageError("invalid storage key")
        return self.prefix + key

    def put_file(self, key: str, path: Path, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.upload_file(str(path), self.bucket, self._full(key), ExtraArgs=extra or None)
        path.unlink(missing_ok=True)

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        tmp = Path(tempfile.mkdtemp(prefix="rag-s3-"))
        try:
            target = tmp / Path(key).name
            self.client.download_file(self.bucket, self._full(key), str(target))
            yield target
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._full(key))
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def _list(self, prefix: str) -> Iterator[dict[str, Any]]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            yield from page.get("Contents", [])

    def delete_dir(self, key: str) -> int:
        directory = key_dir(key)
        if directory is None:
            return 0
        keys = [o["Key"] for o in self._list(directory + "/")]
        for i in range(0, len(keys), 1000):
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]], "Quiet": True},
            )
        return len(keys)

    def iter_objects(self) -> Iterator[StoredObject]:
        for obj in self._list(UPLOADS + "/"):
            rel = obj["Key"][len(self.prefix) :]
            if _KEY_RE.fullmatch(rel):
                yield StoredObject(rel, int(obj["Size"]), obj["LastModified"].timestamp())

    def delete_object(self, key: str) -> None:
        if not _KEY_RE.fullmatch(key):
            raise StorageError("refusing to delete a key this app does not own")
        self.client.delete_object(Bucket=self.bucket, Key=self._full(key))


def make_s3_client(settings: Settings) -> Any:
    import boto3
    from botocore.config import Config

    cfg = Config(
        s3={"addressing_style": "path" if settings.s3_endpoint_url else "auto"},
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=10,
        read_timeout=30,
        signature_version="s3v4",
    )
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id or None,
        aws_secret_access_key=settings.s3_secret_access_key or None,
        config=cfg,
    )


def ensure_bucket(settings: Settings) -> str:
    """Create ``S3_BUCKET`` when it does not exist. Idempotent; returns ``"exists"`` or ``"created"``.

    Meant for local S3 emulators (the Compose ``s3`` profile) that start empty; on AWS buckets are
    normally created by infrastructure code, but this works there too given the permission.
    """
    from botocore.exceptions import ClientError

    if not settings.s3_bucket:
        raise ValueError("S3_BUCKET is not set")
    client = make_s3_client(settings)
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
        return "exists"
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
    params: dict[str, Any] = {"Bucket": settings.s3_bucket}
    if settings.s3_region and settings.s3_region != "us-east-1":  # AWS rejects a LocationConstraint for us-east-1
        params["CreateBucketConfiguration"] = {"LocationConstraint": settings.s3_region}
    client.create_bucket(**params)
    return "created"


def get_storage(settings: Settings, backend: str | None = None) -> Storage:
    """The storage for ``backend`` (default: the configured ``STORAGE_BACKEND``)."""
    backend = backend or settings.storage_backend
    if backend == "local":
        return LocalStorage(settings.data_dir)
    if backend == "s3":
        return S3Storage(settings)
    raise StorageError(f"unknown storage backend '{backend}'")


def sweep_age_cutoff(min_age_hours: float, now: float | None = None) -> float:
    return (now if now is not None else time.time()) - min_age_hours * 3600
