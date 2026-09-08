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


#: (name, probe, critical)
CHECKS: list[tuple[str, Probe, bool]] = [
    ("db", _check_db, True),
    ("redis", _check_redis, True),
    ("chroma", _check_chroma, True),
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
