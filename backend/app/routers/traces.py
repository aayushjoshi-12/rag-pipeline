from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.routers.auth_deps import require
from app.routers.deps import get_session, get_settings
from app.schema.traces import CreditsOut, DayUsageOut, InsightsOut, TraceOut, TracePage
from app.services import credits, traces, usage_insights
from app.services.auth.permissions import Permission

router = APIRouter(dependencies=[Depends(require(Permission.TRACES_READ))])
Session = Annotated[AsyncSession, Depends(get_session)]


def _check(value: str | None, allowed: tuple[str, ...], name: str) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name} must be one of {allowed}")


@router.get("/traces", response_model=TracePage, summary="List traces (paginated)")
async def list_traces(
    session: Session,
    kind: str | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TracePage:
    _check(kind, traces.KINDS, "kind")
    _check(status_, traces.STATUSES, "status")
    rows, total = await traces.list_traces(session, kind=kind, status=status_, limit=limit, offset=offset)
    return TracePage(items=[TraceOut.model_validate(r) for r in rows], total=total)


@router.get("/traces/{trace_id}", response_model=TraceOut)
async def get_trace(trace_id: uuid.UUID, session: Session) -> TraceOut:
    row = await traces.get_trace(session, trace_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace not found.")
    return TraceOut.model_validate(row)


@router.get("/usage", response_model=list[DayUsageOut])
async def get_usage(
    session: Session,
    days: Annotated[int, Query(ge=1, le=90)] = 14,
    kind: str | None = None,
) -> list[DayUsageOut]:
    _check(kind, traces.KINDS, "kind")
    return [DayUsageOut.model_validate(d, from_attributes=True) for d in await traces.usage(session, days=days, kind=kind)]


@router.get("/usage/insights", response_model=InsightsOut)
async def get_insights(
    session: Session,
    days: Annotated[int, Query(ge=1, le=90)] = 14,
    kind: str | None = None,
) -> InsightsOut:
    _check(kind, traces.KINDS, "kind")
    return InsightsOut.model_validate(await usage_insights.insights(session, days=days, kind=kind))


@router.get("/usage/credits", response_model=CreditsOut)
async def get_credits(cfg: Annotated[Settings, Depends(get_settings)]) -> CreditsOut:
    """The OpenRouter key balance (read server-side; the key is never returned)."""
    return CreditsOut.model_validate(await credits.get_credits(cfg))
