"""Cheap, safe "Test connection" checks for the admin Settings flows (ticket 43).

Each test uses the settings in force NOW (or a candidate ``Settings``), makes at most one tiny request and returns
``TestResult``. Results never contain a secret or a response body: only a short, sanitised description.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from app.config import Settings

Service = Literal["llm", "embeddings", "vector_store", "storage", "parser"]
SERVICES: tuple[Service, ...] = ("llm", "embeddings", "vector_store", "storage", "parser")

_TIMEOUT = httpx.Timeout(10.0)


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a pytest class

    service: str
    ok: bool
    detail: str
    provider: str
    latency_ms: int


def _status_detail(status: int) -> str:
    if status in (401, 403):
        return f"the provider rejected the API key (HTTP {status})"
    if status == 404:
        return "endpoint not found (HTTP 404): check the base URL"
    if status == 429:
        return "reachable, but rate limited (HTTP 429)"
    return f"HTTP {status}"


async def _llm_key_check(provider: str, cfg: Settings) -> tuple[bool, str]:
    """Key/models check: lists models, which costs nothing and spends no tokens."""
    if provider == "openrouter":
        key, url, headers = cfg.openrouter_api_key, f"{cfg.openrouter_base_url.rstrip('/')}/key", {}
        headers = {"Authorization": f"Bearer {key}"}
    elif provider == "openai":
        key, url = cfg.openai_api_key, "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {key}"}
    elif provider == "anthropic":
        key, url = cfg.anthropic_api_key, "https://api.anthropic.com/v1/models"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        return False, f"unknown provider '{provider}'"
    if not key:
        return False, f"no API key is set for '{provider}'"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"cannot reach the provider ({type(exc).__name__})"
    if resp.status_code == 200:
        return True, "API key accepted"
    return False, _status_detail(resp.status_code)


async def _test_llm(cfg: Settings) -> tuple[bool, str, str]:
    ok, detail = await _llm_key_check(cfg.llm_provider, cfg)
    return ok, detail, cfg.llm_provider


async def _test_parser(cfg: Settings) -> tuple[bool, str, str]:
    provider = cfg.parser_provider
    if provider == "ocr":
        try:
            import pytesseract

            if cfg.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = cfg.tesseract_cmd
            version = await asyncio.to_thread(pytesseract.get_tesseract_version)
        except Exception as exc:
            return False, f"tesseract is not available ({type(exc).__name__})", provider
        return True, f"tesseract {version} found", provider
    ok, detail = await _llm_key_check(provider, cfg)
    return ok, detail, provider


async def _test_embeddings(cfg: Settings) -> tuple[bool, str, str]:
    from app.services.embeddings import get_embedder

    try:
        embedder = get_embedder(settings=cfg)
        vector = await asyncio.wait_for(
            asyncio.to_thread(embedder.embed_query, "connection test"), 20
        )
    except Exception as exc:
        return False, f"embedding failed ({type(exc).__name__})", cfg.embedding_provider
    return True, f"embedded one short string ({len(vector)} dimensions)", cfg.embedding_provider


async def _test_vector_store(cfg: Settings) -> tuple[bool, str, str]:
    from app.services.vectorstore import get_vector_store

    def probe() -> str:
        store = get_vector_store(settings=cfg, verify=False)
        info = store.describe()
        problems = info.get("problems") or []
        if problems:
            return (
                "reachable, but the stored index differs from the settings (reindex needed): "
                + "; ".join(str(p) for p in problems)
            )
        return f"reachable ({info.get('count', 0)} vectors)"

    try:
        detail = await asyncio.wait_for(asyncio.to_thread(probe), 20)
    except Exception as exc:
        return False, f"cannot reach the vector store ({type(exc).__name__})", cfg.vector_store
    return "reindex needed" not in detail, detail, cfg.vector_store


async def _test_storage(cfg: Settings) -> tuple[bool, str, str]:
    if cfg.storage_backend == "local":

        def writable() -> str:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            probe = cfg.data_dir / ".settings-test"
            probe.write_bytes(b"x")
            probe.unlink()
            return "the data directory is writable"

        try:
            return True, await asyncio.to_thread(writable), "local"
        except OSError as exc:
            return False, f"the data directory is not writable ({type(exc).__name__})", "local"

    def head() -> str:
        from app.services.storage import make_s3_client

        make_s3_client(cfg).head_bucket(Bucket=cfg.s3_bucket)
        return f"bucket '{cfg.s3_bucket}' is reachable"

    if not cfg.s3_bucket:
        return False, "S3_BUCKET is not set", "s3"
    try:
        return True, await asyncio.wait_for(asyncio.to_thread(head), 20), "s3"
    except Exception as exc:
        code = (
            getattr(exc, "response", {}).get("Error", {}).get("Code")
            if hasattr(exc, "response")
            else None
        )
        return False, f"cannot open the bucket ({code or type(exc).__name__})", "s3"


_TESTS = {
    "llm": _test_llm,
    "embeddings": _test_embeddings,
    "vector_store": _test_vector_store,
    "storage": _test_storage,
    "parser": _test_parser,
}


async def run(service: Service, cfg: Settings | None = None) -> TestResult:
    cfg = cfg or Settings()
    started = time.perf_counter()
    try:
        ok, detail, provider = await _TESTS[service](cfg)
    except Exception as exc:  # a test must report, never raise
        ok, detail, provider = False, f"test failed ({type(exc).__name__})", ""
    return TestResult(service, ok, detail, provider, int((time.perf_counter() - started) * 1000))
