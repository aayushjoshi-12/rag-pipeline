from __future__ import annotations

import statistics
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Trace, utcnow
from app.routers.auth_deps import require
from app.routers.deps import get_session, get_settings
from app.services.auth.permissions import Permission
from app.services.cache import LAYERS, abump_index_version, get_cache

router = APIRouter(prefix="/cache", tags=["cache"])


class CacheStatsOut(BaseModel):
    """Live process counters, Redis keyspace and what recent traces say the cache saved."""

    config: dict[str, Any]
    process: dict[str, Any]
    redis: dict[str, Any]
    traces: dict[str, Any]


class PurgeOut(BaseModel):
    purged_keys: int | None
    index_version_bumped: bool


def trace_summary(rows: list[tuple[str, int, dict[str, Any]]]) -> dict[str, Any]:
    """Aggregate ``(kind, duration_ms, meta)`` rows of chat/search traces into per-kind cache stats."""
    out: dict[str, Any] = {}
    for kind in ("search", "chat"):
        mine = [(d, (m or {}).get("cache") or {}) for k, d, m in rows if k == kind]
        verdicts = [c.get("result") for _, c in mine if c]
        hit_ms = [d for d, c in mine if c.get("result") == "hit"]
        miss_ms = [d for d, c in mine if c.get("result") == "miss"]
        layers: dict[str, dict[str, int]] = {}
        for layer in LAYERS:
            states = [c[layer] for _, c in mine if layer in c]
            layers[layer] = {
                "hit": states.count("hit") + states.count("coalesced"),
                "miss": states.count("miss"),
            }
        saved = [float(c["saved_usd"]) for _, c in mine if c.get("saved_usd") is not None]
        out[kind] = {
            "requests": len(mine),
            "with_cache_info": len(verdicts),
            "hits": verdicts.count("hit"),
            "misses": verdicts.count("miss"),
            "bypassed": verdicts.count("bypass"),
            "hit_rate": (
                verdicts.count("hit") / max(1, verdicts.count("hit") + verdicts.count("miss"))
            )
            if verdicts
            else None,
            "avg_hit_ms": statistics.fmean(hit_ms) if hit_ms else None,
            "avg_miss_ms": statistics.fmean(miss_ms) if miss_ms else None,
            "layers": layers,
            "llm_calls_avoided": sum(int(c.get("llm_calls_avoided", 0)) for _, c in mine),
            "embeddings_avoided": sum(int(c.get("embeddings_avoided", 0)) for _, c in mine),
            "saved_usd": sum(saved) if saved else None,
        }
    return out


@router.get(
    "/stats", response_model=CacheStatsOut, dependencies=[Depends(require(Permission.TRACES_READ))]
)
async def cache_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
    cfg: Annotated[Settings, Depends(get_settings)],
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> CacheStatsOut:
    qc = get_cache(cfg)
    since = utcnow() - timedelta(days=days)
    res = await session.execute(
        select(Trace.kind, Trace.duration_ms, Trace.meta).where(
            Trace.created_at >= since, Trace.kind.in_(("search", "chat"))
        )
    )
    rows = [(k, d, m) for k, d, m in res.all()]
    redis_info = await qc.redis_stats() if qc.enabled else {"reachable": False, "keys": {}}
    return CacheStatsOut(
        config=qc.describe(), process=qc.local_stats(), redis=redis_info, traces=trace_summary(rows)
    )


@router.post(
    "/purge", response_model=PurgeOut, dependencies=[Depends(require(Permission.CACHE_MANAGE))]
)
async def cache_purge(cfg: Annotated[Settings, Depends(get_settings)]) -> PurgeOut:
    """Delete every cached entry (all layers) and bump the index version. Never touches Celery's Redis dbs."""
    qc = get_cache(cfg)
    removed = await qc.purge()
    bumped = await abump_index_version()
    return PurgeOut(purged_keys=removed, index_version_bumped=bumped)
