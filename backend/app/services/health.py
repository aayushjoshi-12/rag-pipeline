from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config import Settings


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

        r = aioredis.from_url(Settings().redis_url, socket_connect_timeout=3)
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
            host=Settings().chroma_host,
            port=Settings().chroma_port,
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

        store = await asyncio.to_thread(PgVectorStore.from_settings, Settings())
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

        store = await asyncio.to_thread(get_vector_store, None, settings=Settings(), verify=False)
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
            resp = await client.get(f"{Settings().ollama_base_url}/api/tags")
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
        if Settings().vector_store == "pgvector"
        else [("chroma", _check_chroma, True)]
    ),
    ("vector_settings", _check_vector_settings, False),
    ("ollama", _check_ollama, False),
]


def _active_checks() -> list[tuple[str, Probe, bool]]:
    """``CHECKS`` with the stock vector-store probe swapped for the store configured NOW (admin-UI changes)."""
    pg = Settings().vector_store == "pgvector"
    out: list[tuple[str, Probe, bool]] = []
    for name, probe, critical in CHECKS:
        if probe is _check_chroma and pg:
            out.append(("pgvector", _check_pgvector, critical))
        elif probe is _check_pgvector and not pg:
            out.append(("chroma", _check_chroma, critical))
        else:
            out.append((name, probe, critical))
    return out


async def run_health_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    checks = _active_checks()
    for name, _probe, critical in checks:
        results.append(CheckResult(name=name, ok=False, critical=critical))
    # Bind each probe and result as positional args inside a helper coroutine so
    # the generator does not late-bind the loop variable `probe` to its last value.
    await asyncio.gather(
        *(_run_probe(result, probe) for result, (_n, probe, _c) in zip(results, checks, strict=True)),
        return_exceptions=True,
    )
    return results


async def _run_probe(result: CheckResult, probe: Probe) -> None:
    await probe(result)
