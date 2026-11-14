"""Dashboard aggregates beyond the per-day totals: cost by model, latency distribution, error
reasons, request heatmap, RAG health and ingest efficiency. All grouping is done in Postgres
(bucketing, heatmap cells, cited-document counts); only error-text normalisation is in Python."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Integer, String, and_, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Document, Trace

MAX_LATENCY_BUCKETS = 12
TOP_DOCUMENTS = 10
TOP_REASONS = 8

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_PATH = re.compile(r"(?:/[\w.\-@+]+){2,}")
_NUMBER = re.compile(r"\b\d{5,}\b")
_SPACE = re.compile(r"\s+")


def normalize_error(error: str | None) -> str:
    """Group key for an error: first line, ids/paths/long numbers masked, capped at 80 chars."""
    if not error or not error.strip():
        return "Unknown error"
    line = error.strip().splitlines()[0]
    line = _PATH.sub("<path>", line)
    line = _UUID.sub("<id>", line)
    line = _NUMBER.sub("<n>", line)
    line = _SPACE.sub(" ", line).strip()
    return line if len(line) <= 80 else line[:77] + "..."


def nice_step(max_value: int, max_buckets: int = MAX_LATENCY_BUCKETS) -> int:
    """Smallest 1/2/5 x 10^k bucket width that covers ``0..max_value`` in <= ``max_buckets`` buckets."""
    if max_value < 1:
        return 1
    raw = (max_value + 1) / max_buckets
    pow10 = 10 ** math.floor(math.log10(raw)) if raw >= 1 else 1
    for m in (1, 2, 5, 10):
        if m * pow10 >= raw:
            return int(m * pow10)
    return int(10 * pow10)  # pragma: no cover - the loop always returns


def since_date(days: int) -> date:
    return datetime.now(UTC).date() - timedelta(days=days - 1)


def _since_dt(days: int) -> datetime:
    return datetime.combine(since_date(days), datetime.min.time())


@dataclass(frozen=True)
class ModelCost:
    model: str | None
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    unpriced_requests: int


@dataclass(frozen=True)
class Bucket:
    lo: int
    hi: int  # exclusive
    count: int


@dataclass(frozen=True)
class Reason:
    reason: str
    count: int


@dataclass(frozen=True)
class HeatCell:
    weekday: int  # 0 = Monday ... 6 = Sunday (UTC)
    hour: int  # 0..23 (UTC)
    count: int


@dataclass(frozen=True)
class DayRag:
    day: date
    queries: int = 0
    no_hit: int = 0


@dataclass(frozen=True)
class HitsBin:
    hits: int
    count: int


@dataclass(frozen=True)
class CitedDocument:
    document_id: str
    filename: str | None  # None = the document no longer exists
    citations: int


@dataclass(frozen=True)
class DayReuse:
    day: date
    ingests: int = 0
    reused: int = 0  # chunks kept as-is (no embedding call)
    embedded: int = 0


@dataclass(frozen=True)
class ModeStat:
    mode: str
    runs: int
    avg_ms: float
    p95_ms: float


@dataclass
class Insights:
    days: int
    kind: str | None
    # cost
    cost_usd: float | None = None
    priced_chats: int = 0
    unpriced_chats: int = 0
    avg_cost_per_chat: float | None = None
    by_model: list[ModelCost] = field(default_factory=list)
    # performance
    latency_bucket_ms: int = 1
    latency_histogram: list[Bucket] = field(default_factory=list)
    errors_by_reason: list[Reason] = field(default_factory=list)
    heatmap: list[HeatCell] = field(default_factory=list)
    # rag
    rag_days: list[DayRag] = field(default_factory=list)
    hits_histogram: list[HitsBin] = field(default_factory=list)
    top_documents: list[CitedDocument] = field(default_factory=list)
    cited_documents: int = 0
    total_documents: int = 0
    never_cited: int = 0
    reuse_days: list[DayReuse] = field(default_factory=list)
    # ingest
    ingest_modes: list[ModeStat] = field(default_factory=list)
    ingest_failures: list[Reason] = field(default_factory=list)


def _window(days: int, kind: str | None) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = [Trace.created_at >= _since_dt(days)]
    if kind:
        conds.append(Trace.kind == kind)
    return conds


async def _cost(session: AsyncSession, out: Insights) -> None:
    unpriced = and_(Trace.kind == "chat", Trace.status == "ok", Trace.cost_usd.is_(None))
    priced = and_(Trace.kind == "chat", Trace.status == "ok", Trace.cost_usd.is_not(None))
    total = (
        await session.execute(
            select(
                func.sum(Trace.cost_usd),
                func.count().filter(priced),
                func.count().filter(unpriced),
            ).where(*_window(out.days, out.kind))
        )
    ).one()
    out.cost_usd = float(total[0]) if total[0] is not None else None
    out.priced_chats, out.unpriced_chats = int(total[1]), int(total[2])
    chat_cost = (
        await session.execute(select(func.avg(Trace.cost_usd)).where(*_window(out.days, "chat"), priced))
    ).scalar_one()
    out.avg_cost_per_chat = float(chat_cost) if chat_cost is not None else None
    if out.kind == "ingest":
        return
    rows = await session.execute(
        select(
            Trace.model,
            func.count().label("n"),
            func.coalesce(func.sum(Trace.prompt_tokens), 0),
            func.coalesce(func.sum(Trace.completion_tokens), 0),
            func.sum(Trace.cost_usd),
            func.count().filter(unpriced),
        )
        .where(*_window(out.days, "chat"))
        .group_by(Trace.model)
        .order_by(func.coalesce(func.sum(Trace.cost_usd), 0).desc(), func.count().desc())
    )
    out.by_model = [
        ModelCost(r[0], int(r[1]), int(r[2]), int(r[3]), float(r[4]) if r[4] is not None else None, int(r[5]))
        for r in rows.all()
    ]


async def _performance(session: AsyncSession, out: Insights) -> None:
    where = _window(out.days, out.kind)
    max_ms = (await session.execute(select(func.max(Trace.duration_ms)).where(*where))).scalar_one()
    if max_ms is None:
        return
    step = nice_step(int(max_ms))
    out.latency_bucket_ms = step
    idx = cast(func.floor(Trace.duration_ms / step), Integer).label("b")
    counts = {
        int(b): int(n)
        for b, n in (await session.execute(select(idx, func.count()).where(*where).group_by(idx))).all()
    }
    last = max(counts)
    out.latency_histogram = [Bucket(i * step, (i + 1) * step, counts.get(i, 0)) for i in range(last + 1)]

    err_rows = await session.execute(
        select(Trace.error).where(*where, Trace.status == "error").order_by(Trace.created_at.desc()).limit(5000)
    )
    out.errors_by_reason = _group_reasons(r[0] for r in err_rows.all())

    wd = (func.extract("isodow", Trace.created_at) - 1).label("wd")
    hr = func.extract("hour", Trace.created_at).label("hr")
    cells = await session.execute(select(wd, hr, func.count()).where(*where).group_by(wd, hr))
    out.heatmap = sorted(
        (HeatCell(int(w), int(h), int(n)) for w, h, n in cells.all()), key=lambda c: (c.weekday, c.hour)
    )


def _group_reasons(errors: Iterable[str | None]) -> list[Reason]:
    tally: dict[str, int] = {}
    for e in errors:
        key = normalize_error(e)
        tally[key] = tally.get(key, 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    top = [Reason(k, n) for k, n in ranked[:TOP_REASONS]]
    rest = sum(n for _, n in ranked[TOP_REASONS:])
    return top + ([Reason("Other", rest)] if rest else [])


_CITED_SQL = text(
    """
    SELECT e.val AS doc_id, count(*) AS n
    FROM traces t,
         json_array_elements_text(
            CASE WHEN json_typeof(t.meta -> 'cited_document_ids') = 'array'
                 THEN t.meta -> 'cited_document_ids' ELSE '[]'::json END
         ) AS e(val)
    WHERE t.kind = 'chat' AND t.created_at >= :since
    GROUP BY e.val
    ORDER BY n DESC, e.val
    """
)

_REUSE_SQL = text(
    """
    SELECT date_trunc('day', created_at) AS day,
           count(*) AS ingests,
           coalesce(sum(CASE WHEN (meta ->> 'kept') ~ '^[0-9]+$' THEN (meta ->> 'kept')::int END), 0) AS reused,
           coalesce(sum(CASE WHEN (meta ->> 'embedded') ~ '^[0-9]+$' THEN (meta ->> 'embedded')::int END), 0) AS embedded
    FROM traces
    WHERE kind = 'ingest' AND status = 'ok' AND created_at >= :since
    GROUP BY 1
    """
)


async def _rag(session: AsyncSession, out: Insights) -> None:
    since = _since_dt(out.days)
    chat_ok = [Trace.created_at >= since, Trace.kind == "chat", Trace.status == "ok", Trace.hits.is_not(None)]
    day = func.date_trunc("day", Trace.created_at)
    rows = await session.execute(
        select(day, func.count(), func.count().filter(Trace.hits == 0)).where(*chat_ok).group_by(day)
    )
    found = {r[0].date(): DayRag(r[0].date(), int(r[1]), int(r[2])) for r in rows.all()}
    start = since.date()
    out.rag_days = [found.get(start + timedelta(days=i), DayRag(start + timedelta(days=i))) for i in range(out.days)]

    hist = await session.execute(
        select(Trace.hits, func.count()).where(*chat_ok).group_by(Trace.hits).order_by(Trace.hits)
    )
    out.hits_histogram = [HitsBin(int(h), int(n)) for h, n in hist.all()]

    cited = (await session.execute(_CITED_SQL, {"since": since})).all()
    out.cited_documents = len(cited)
    ids = [r[0] for r in cited[:TOP_DOCUMENTS]]
    names: dict[str, str] = {}
    if ids:
        docs = await session.execute(
            select(Document.id, Document.filename).where(cast(Document.id, String).in_(ids))
        )
        names = {str(i): f for i, f in docs.all()}
    out.top_documents = [CitedDocument(r[0], names.get(r[0]), int(r[1])) for r in cited[:TOP_DOCUMENTS]]
    existing = {str(i) for (i,) in (await session.execute(select(Document.id))).all()}
    out.total_documents = len(existing)
    out.never_cited = len(existing - {r[0] for r in cited})

    reuse = {
        r.day.date(): DayReuse(r.day.date(), int(r.ingests), int(r.reused), int(r.embedded))
        for r in (await session.execute(_REUSE_SQL, {"since": since})).all()
    }
    out.reuse_days = [reuse.get(start + timedelta(days=i), DayReuse(start + timedelta(days=i))) for i in range(out.days)]


async def _ingest(session: AsyncSession, out: Insights) -> None:
    since = _since_dt(out.days)
    mode = Trace.meta["mode"].as_string()
    rows = await session.execute(
        select(
            mode,
            func.count(),
            func.avg(Trace.duration_ms),
            func.percentile_cont(0.95).within_group(Trace.duration_ms),
        )
        .where(Trace.created_at >= since, Trace.kind == "ingest", Trace.status == "ok", mode.is_not(None))
        .group_by(mode)
        .order_by(mode)
    )
    out.ingest_modes = [ModeStat(str(m), int(n), float(a), float(p)) for m, n, a, p in rows.all()]
    errs = await session.execute(
        select(Trace.error)
        .where(Trace.created_at >= since, Trace.kind == "ingest", Trace.status == "error")
        .order_by(Trace.created_at.desc())
        .limit(5000)
    )
    out.ingest_failures = _group_reasons(r[0] for r in errs.all())


async def insights(session: AsyncSession, *, days: int, kind: str | None = None) -> Insights:
    out = Insights(days=days, kind=kind)
    await _cost(session, out)
    await _performance(session, out)
    await _rag(session, out)
    await _ingest(session, out)
    return out
