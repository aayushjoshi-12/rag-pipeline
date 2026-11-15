"""Trace log: record chat/ingest runs and aggregate them for the dashboard."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Trace, utcnow

logger = logging.getLogger(__name__)

KINDS = ("chat", "search", "ingest")
STATUSES = ("ok", "error")


async def record(
    session: AsyncSession,
    *,
    kind: str,
    status: str,
    duration_ms: int,
    user_email: str | None = None,
    document_id: uuid.UUID | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    hits: int | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
    question: str | None = None,
    meta: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """Persist a trace and return its id (``None`` if it could not be saved).

    Never raises: observability must not break the request it describes."""
    trace_id = uuid.uuid4()
    try:
        session.add(
            Trace(
                id=trace_id,
                kind=kind,
                status=status,
                duration_ms=max(0, duration_ms),
                user_email=user_email,
                document_id=document_id,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                hits=hits,
                cost_usd=cost_usd,
                error=(error or None) and error[:2000],
                question=question,
                meta=meta or {},
            )
        )
        await session.commit()
        return trace_id
    except Exception:
        logger.warning("could not record trace", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.debug("rollback after trace failure also failed", exc_info=True)
        return None


async def list_traces(
    session: AsyncSession,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Trace], int]:
    stmt = select(Trace)
    count = select(func.count()).select_from(Trace)
    for col, val in ((Trace.kind, kind), (Trace.status, status)):
        if val:
            stmt, count = stmt.where(col == val), count.where(col == val)
    rows = await session.execute(stmt.order_by(Trace.created_at.desc()).limit(limit).offset(offset))
    return list(rows.scalars()), int((await session.execute(count)).scalar_one())


async def get_trace(session: AsyncSession, trace_id: uuid.UUID) -> Trace | None:
    return await session.get(Trace, trace_id)


@dataclass(frozen=True)
class DayUsage:
    day: date
    requests: int = 0
    errors: int = 0
    avg_ms: float | None = None
    p95_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Sum of the known costs of the day; ``None`` when no request that day had a known cost.
    cost_usd: float | None = None
    #: Successful chat requests whose cost is unknown (so ``cost_usd`` is a lower bound).
    unpriced_requests: int = 0


async def usage(session: AsyncSession, *, days: int, kind: str | None = None) -> list[DayUsage]:
    """Per-day totals for the last ``days`` days (UTC), zero-filled so charts have no gaps."""
    today = datetime.now(UTC).date()
    since = today - timedelta(days=days - 1)
    day = func.date_trunc("day", Trace.created_at)
    stmt = (
        select(
            day.label("day"),
            func.count().label("requests"),
            func.count().filter(Trace.status == "error").label("errors"),
            func.avg(Trace.duration_ms).label("avg_ms"),
            func.percentile_cont(0.95).within_group(Trace.duration_ms).label("p95_ms"),
            func.coalesce(func.sum(Trace.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(Trace.completion_tokens), 0).label("completion_tokens"),
            func.sum(Trace.cost_usd).label("cost_usd"),
            func.count()
            .filter(and_(Trace.kind == "chat", Trace.status == "ok", Trace.cost_usd.is_(None)))
            .label("unpriced"),
        )
        .where(Trace.created_at >= datetime.combine(since, datetime.min.time()))
        .group_by(day)
    )
    if kind:
        stmt = stmt.where(Trace.kind == kind)
    found = {
        r.day.date(): DayUsage(
            r.day.date(),
            int(r.requests),
            int(r.errors),
            float(r.avg_ms) if r.avg_ms is not None else None,
            float(r.p95_ms) if r.p95_ms is not None else None,
            int(r.prompt_tokens),
            int(r.completion_tokens),
            float(r.cost_usd) if r.cost_usd is not None else None,
            int(r.unpriced),
        )
        for r in (await session.execute(stmt)).all()
    }
    return [found.get(since + timedelta(days=i), DayUsage(since + timedelta(days=i))) for i in range(days)]


async def purge_older_than(session: AsyncSession, days: int) -> int:
    cutoff = utcnow() - timedelta(days=days)
    result = await session.execute(delete(Trace).where(Trace.created_at < cutoff))
    await session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]  # CursorResult has rowcount
