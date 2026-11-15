"""Shared test fixtures — async DB session against compose Postgres."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

# Tests must never read a developer's real .env (API keys, provider choices): that would make
# "no real API calls in tests" depend on whoever runs them. Set before app.config is imported,
# because Settings captures its env-file list when the class is defined.
os.environ["RAG_ENV_FILE"] = os.devnull
# The query cache lives in a shared Redis: suites that do not test it must not read or write it.
# Cache tests opt in with an explicit ``Settings(cache_enabled=True, cache_redis_url=<db 13>)``.
os.environ["CACHE_ENABLED"] = "false"

from app.config import Settings
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


@contextmanager
def _noop_generation(*args: Any, **kwargs: Any) -> Any:
    yield type("NullGen", (), {"update": lambda self, **_: None})()


@pytest.fixture(autouse=True)
def _no_langfuse_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit/integration tests fully offline wrt Langfuse.

    Strips creds so the lazy `get_client()` initialises as a disabled no-op, and swaps the
    observed `generation` seam for a no-op so nothing can attempt a real export regardless
    of how a test constructs Settings.
    """
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    from app.services import observability

    monkeypatch.setattr(observability, "generation", _noop_generation)


@pytest.fixture
def synthetic_docx(tmp_path: Path) -> Path:
    """A self-contained synthetic .docx (handcrafted OOXML zip — no python-docx dep)."""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:r><w:t>Hello RAG Parser</w:t></w:r></w:p>
<w:p><w:r><w:t>Second paragraph with numbers 12345.</w:t></w:r></w:p>
</w:body>
</w:document>"""
    path = tmp_path / "sample.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document)
    return path


"""Throwaway migrated database so tests never touch the developer's real ``rag`` database."""

_BACKEND = Path(__file__).resolve().parents[1]


def _admin_dsn(url: str) -> tuple[str, str]:
    parsed = make_url(url)
    dsn = parsed.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    return dsn, parsed.database or "rag"


@pytest.fixture(scope="module")
def isolated_db_url() -> Iterator[str]:
    base = Settings().database_url
    admin_dsn, _ = _admin_dsn(base)
    name = f"rag_test_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable for isolated DB tests: {exc}")
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": url, "RAG_ENV_FILE": os.devnull}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"
        yield url
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def db_session(isolated_db_url: str) -> AsyncIterator[AsyncSession]:
    """Yield an async session on the throwaway database (see ``isolated_db_url``).

    Creates a fresh engine with NullPool per test to avoid event-loop
    conflicts across pytest-asyncio test functions.

    Prereq: compose Postgres reachable (make db-up); the DB is migrated automatically.
    """
    engine = create_async_engine(
        isolated_db_url,
        echo=False,
        poolclass=NullPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


# ── Real S3 emulator: Floci (Compose profile `s3`: `docker compose --profile s3 up -d floci`) ─────────

S3_EMULATOR_ENDPOINT = os.environ.get("TEST_S3_ENDPOINT_URL", "http://localhost:4566")
S3_EMULATOR_KEY = os.environ.get("TEST_S3_ACCESS_KEY_ID", "test")
S3_EMULATOR_SECRET = os.environ.get("TEST_S3_SECRET_ACCESS_KEY", "test")


@pytest.fixture(scope="module")
def s3_bucket() -> Iterator[str]:
    """A throwaway bucket on the real S3 emulator (Floci); skipped with a clear message when it is not running."""
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=S3_EMULATOR_ENDPOINT,
        aws_access_key_id=S3_EMULATOR_KEY,
        aws_secret_access_key=S3_EMULATOR_SECRET,
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"}, connect_timeout=2, retries={"max_attempts": 1}
        ),
    )
    name = f"rag-test-{uuid.uuid4().hex[:12]}"
    try:
        client.create_bucket(Bucket=name)
    except Exception as exc:
        pytest.skip(
            f"S3 emulator (Floci) not reachable at {S3_EMULATOR_ENDPOINT} ({type(exc).__name__}); "
            "start it with `docker compose --profile s3 up -d floci`"
        )
    try:
        yield name
    finally:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=name):
            for obj in page.get("Contents", []):
                client.delete_object(Bucket=name, Key=obj["Key"])
        client.delete_bucket(Bucket=name)


@pytest.fixture
def s3_client(s3_bucket: str) -> Any:
    from app.services.storage import make_s3_client

    return make_s3_client(s3_settings(s3_bucket))


def s3_settings(bucket: str, **kw: Any) -> Settings:
    kw.setdefault("s3_prefix", "test-prefix/")
    return Settings(
        storage_backend="s3",
        s3_endpoint_url=S3_EMULATOR_ENDPOINT,
        s3_bucket=bucket,
        s3_access_key_id=S3_EMULATOR_KEY,
        s3_secret_access_key=S3_EMULATOR_SECRET,
        auth_enabled=False,
        **kw,
    )


@pytest.fixture(autouse=True)
def _fresh_chroma_clients() -> Iterator[None]:
    """The process-wide Chroma client cache must not carry a client (or a test's fake) across tests."""
    from app.services import vectorstore

    vectorstore._chroma_clients.clear()
    yield
    vectorstore._chroma_clients.clear()
