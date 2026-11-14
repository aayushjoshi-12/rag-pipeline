from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config import Settings

_settings = Settings()


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True


Probe = Callable[["CheckResult"], Awaitable[None]]


async def _check_db(result: CheckResult) -> None:
    try:
        from sqlalchemy import text

        from app.db.engine import engine

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        result.ok = True
        result.detail = "connected"
    except Exception as exc:
        result.ok = False
        result.detail = str(exc)[:300]


async def _check_redis(result: CheckResult) -> None:
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(_settings.redis_url, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        result.ok = True
        result.detail = "connected"
    except Exception as exc:
        result.ok = False
        result.detail = str(exc)[:300]


async def _check_chroma(result: CheckResult) -> None:
    try:
        import chromadb

        client = chromadb.HttpClient(
            host=_settings.chroma_host,
            port=_settings.chroma_port,
        )
        client.heartbeat()
        result.ok = True
        result.detail = "connected"
    except Exception as exc:
        result.ok = False
        result.detail = str(exc)[:300]


async def _check_pgvector(result: CheckResult) -> None:
    try:
        from app.services.pgvector_store import PgVectorStore

        store = await asyncio.to_thread(PgVectorStore.from_settings, _settings)
        info = await asyncio.to_thread(store.describe)
        result.ok = info["stored"] is not None
        result.detail = f"{info['count']} vectors" if result.ok else "table missing"
    except Exception as exc:
        result.ok = False
        result.detail = str(exc)[:300]


async def _check_vector_settings(result: CheckResult) -> None:
    """Stored index vs configured settings (and empty-store-with-data). Informational: a mismatch
    degrades search, it should not take the API (login, documents) out of rotation."""
    try:
        from app.db.engine import async_session
        from app.services.reindex import store_status
        from app.services.vectorstore import get_vector_store

        store = await asyncio.to_thread(get_vector_store, None, settings=_settings, verify=False)
        async with async_session() as session:
            info = await store_status(session, store)
        result.ok = not info["problems"]
        result.detail = (
            f"{info['store']} {info['configured']['metric']}/{info['configured']['index']}"
            if result.ok
            else "; ".join(info["problems"])[:300]
        )
    except Exception as exc:
        result.ok = False
        result.detail = str(exc)[:300]


async def _check_ollama(result: CheckResult) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{_settings.ollama_base_url}/api/tags")
        if resp.status_code == 200:
            result.ok = True
            result.detail = "connected"
        else:
            result.ok = False
            result.detail = f"HTTP {resp.status_code}"
    except Exception as exc:
        result.ok = False
        result.detail = str(exc)[:300]


#: (name, probe, critical). The active vector store is probed (critical); the other one is not.
CHECKS: list[tuple[str, Probe, bool]] = [
    ("db", _check_db, True),
    ("redis", _check_redis, True),
    *(
        [("pgvector", _check_pgvector, True)]
        if _settings.vector_store == "pgvector"
        else [("chroma", _check_chroma, True)]
    ),
    ("vector_settings", _check_vector_settings, False),
    ("ollama", _check_ollama, False),
]


async def run_health_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, _probe, critical in CHECKS:
        results.append(CheckResult(name=name, ok=False, critical=critical))
    # Bind each probe and result as positional args inside a helper coroutine so
    # the generator does not late-bind the loop variable `probe` to its last value.
    await asyncio.gather(
        *(_run_probe(result, probe) for result, (_n, probe, _c) in zip(results, CHECKS, strict=True)),
        return_exceptions=True,
    )
    return results


async def _run_probe(result: CheckResult, probe: Probe) -> None:
    await probe(result)
