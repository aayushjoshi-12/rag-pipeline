"""Trace log service against a real, isolated Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.models import Trace
from app.services import traces
from sqlalchemy.ext.asyncio import AsyncSession


async def _add(s: AsyncSession, *, age_days: float = 0, **kw: object) -> None:
    fields: dict[str, object] = {"kind": "chat", "status": "ok", "duration_ms": 100}
    fields.update(kw)
    s.add(
        Trace(
            created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days),
            meta={},
            **fields,  # type: ignore[arg-type]
        )
    )
    await s.commit()


@pytest.fixture
async def clean(isolated_session: AsyncSession) -> AsyncSession:
    from sqlalchemy import delete

    await isolated_session.execute(delete(Trace))
    await isolated_session.commit()
    return isolated_session


async def test_record_persists_and_truncates_long_errors(clean: AsyncSession) -> None:
    await traces.record(clean, kind="ingest", status="error", duration_ms=-5, error="x" * 5000,
                        document_id=uuid.uuid4(), meta={"mode": "full"})
    rows, total = await traces.list_traces(clean)
    assert total == 1
    t = rows[0]
    assert t.duration_ms == 0  # negative clamped
    assert t.error is not None and len(t.error) == 2000
    assert t.meta == {"mode": "full"}
    assert await traces.get_trace(clean, t.id) is not None
    assert await traces.get_trace(clean, uuid.uuid4()) is None


async def test_record_never_raises_even_when_the_database_write_fails(clean: AsyncSession) -> None:
    # a kind longer than the column makes the INSERT fail; the caller must not see an exception
    await traces.record(clean, kind="k" * 40, status="ok", duration_ms=1)
    rows, total = await traces.list_traces(clean)  # session still usable after the rollback
    assert total == 0 and rows == []


async def test_list_filters_orders_newest_first_and_paginates(clean: AsyncSession) -> None:
    for i in range(5):
        await _add(clean, age_days=i, kind="chat", duration_ms=i)
    await _add(clean, kind="ingest", status="error")
    rows, total = await traces.list_traces(clean, kind="chat", limit=2, offset=0)
    assert total == 5 and [r.duration_ms for r in rows] == [0, 1]  # newest first
    rows, _ = await traces.list_traces(clean, kind="chat", limit=2, offset=4)
    assert [r.duration_ms for r in rows] == [4]
    only_err, n = await traces.list_traces(clean, status="error")
    assert n == 1 and only_err[0].kind == "ingest"
    assert (await traces.list_traces(clean, kind="ingest", status="ok"))[1] == 0


async def test_usage_aggregates_per_day_with_zero_filled_gaps(clean: AsyncSession) -> None:
    await _add(clean, duration_ms=100, prompt_tokens=10, completion_tokens=2)
    await _add(clean, duration_ms=300, prompt_tokens=30, completion_tokens=4, status="error")
    await _add(clean, age_days=2, duration_ms=50, kind="ingest")
    days = await traces.usage(clean, days=4)
    assert len(days) == 4 and days[-1].day == datetime.now(UTC).date()
    today, two_ago, empty = days[-1], days[-3], days[-2]
    assert (today.requests, today.errors) == (2, 1)
    assert today.avg_ms == 200.0
    assert today.p95_ms is not None and 100 <= today.p95_ms <= 300
    assert (today.prompt_tokens, today.completion_tokens) == (40, 6)
    assert (two_ago.requests, two_ago.prompt_tokens) == (1, 0)
    assert (empty.requests, empty.avg_ms, empty.p95_ms) == (0, None, None)  # gap day present, empty
    only_chat = await traces.usage(clean, days=4, kind="chat")
    assert sum(d.requests for d in only_chat) == 2


async def test_usage_excludes_rows_older_than_the_window(clean: AsyncSession) -> None:
    await _add(clean, age_days=30)
    assert sum(d.requests for d in await traces.usage(clean, days=7)) == 0


async def test_purge_deletes_only_rows_past_retention(clean: AsyncSession) -> None:
    await _add(clean, age_days=40)
    await _add(clean, age_days=31)
    await _add(clean, age_days=29)
    await _add(clean)
    assert await traces.purge_older_than(clean, 30) == 2
    assert (await traces.list_traces(clean))[1] == 2
