"""Runtime settings: view, change, reset, test connections, reindex, audit (ticket 43, ADR-0008)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.routers.auth_deps import Principal, require, require_any
from app.routers.deps import (
    EnqueueReindex,
    TaskStatus,
    get_enqueue_reindex,
    get_session,
    get_task_status,
)
from app.schema.settings import (
    AuditOut,
    PatchResult,
    ReindexQueued,
    ReindexRequest,
    ReindexStatus,
    SettingOut,
    SettingsOut,
    SettingsPatch,
    TestOut,
)
from app.services import connection_test
from app.services import runtime_settings as rt
from app.services import settings_service as svc
from app.services.auth.permissions import Permission
from app.services.settings_registry import REGISTRY

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])

Reader = Annotated[
    Principal, Depends(require_any(Permission.SETTINGS_READ, Permission.SETTINGS_MANAGE))
]
Manager = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]
Session = Annotated[AsyncSession, Depends(get_session)]

_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {
        "description": "Locked by the environment, not editable, secrets disabled, or reindex not confirmed."
    },
    422: {"description": "A value is invalid (per-setting messages in `detail`)."},
}


def _actor(principal: Principal) -> svc.Actor:
    return svc.Actor(principal.email, principal.user_id)


def _http(exc: svc.SettingsOpError) -> HTTPException:
    if exc.status == 422:
        detail: Any = [
            {"loc": ["settings", k], "msg": m, "type": "value_error"} for k, m in exc.errors.items()
        ] or exc.message
    elif exc.errors:
        detail = {"message": exc.message, "code": exc.code, "errors": exc.errors}
    else:
        detail = exc.message
    return HTTPException(exc.status, detail)


async def _refresh(session: AsyncSession) -> None:
    try:
        await rt.refresh(session)
    except Exception:
        await session.rollback()
        logger.warning("settings layer could not be refreshed for this request")


@router.get(
    "/settings",
    response_model=SettingsOut,
    summary="Effective settings with their source (never secret values)",
    description=(
        "Every setting with its effective value, where it comes from (`env` > `ui` > `config` > `default`), "
        "whether it is locked, and when a change takes effect (`apply`). Secrets are write-only: `value` is "
        "always null and only `is_set` says whether one is stored. Needs `settings:read` or `settings:manage`."
    ),
)
async def get_settings_view(_: Reader, session: Session) -> SettingsOut:
    await _refresh(session)
    cfg = Settings()
    view = svc.build_view(cfg)
    view["summary"]["missing"] = svc.missing_setup(cfg)
    return SettingsOut(**view)


@router.patch(
    "/settings",
    response_model=PatchResult,
    summary="Change settings (all or nothing)",
    description=(
        "`set` maps keys to new values (a secret is sent as its plain value and never returned); `reset` lists "
        "keys to return to config.json / the built-in default (for a secret: clear it). Everything is validated "
        "first. Keys pinned by an environment variable and bootstrap keys are rejected with 409. A change that "
        "needs a reindex is rejected with 409 `reindex_confirmation_required` unless `confirm_reindex` is true; "
        "with it, the value is applied and the reindex is queued. `dry_run` validates and reports only."
    ),
    responses=_ERRORS,
)
async def patch_settings(
    body: SettingsPatch,
    principal: Manager,
    session: Session,
    enqueue: Annotated[EnqueueReindex, Depends(get_enqueue_reindex)],
) -> PatchResult:
    try:
        result = await svc.apply_changes(
            session,
            actor=_actor(principal),
            set_values=body.set,
            reset=body.reset,
            confirm_reindex=body.confirm_reindex,
            dry_run=body.dry_run,
        )
    except svc.SettingsOpError as exc:
        raise _http(exc) from exc
    queued = None
    if result.reindex_keys and not result.dry_run:
        queued = await _queue(
            session, principal, enqueue, result.reindex_actions, result.reindex_keys
        )
    touched = {*result.changed, *result.reset, *result.unchanged}
    view = svc.build_view()
    return PatchResult(
        changed=result.changed,
        reset=result.reset,
        unchanged=result.unchanged,
        restart_required=result.restart_required,
        reindex_required=result.reindex_keys,
        dry_run=result.dry_run,
        reindex=queued,
        settings=[SettingOut(**s) for s in view["settings"] if s["key"] in touched],
    )


async def _queue(
    session: AsyncSession,
    principal: Principal,
    enqueue: EnqueueReindex,
    kinds: list[str],
    keys: list[str],
) -> ReindexQueued:
    task_ids: list[str] = []
    error: str | None = None
    for kind in kinds:
        try:
            task_id = await asyncio.to_thread(enqueue, kind)
        except Exception as exc:  # broker down: the change is saved; the UI offers a retry
            logger.warning("could not queue %s: %s", kind, type(exc).__name__)
            error = f"could not queue the {kind} task ({type(exc).__name__}); retry from the Settings page"
            continue
        task_ids.append(task_id)
        await svc.record_reindex(session, _actor(principal), kind=kind, task_id=task_id, keys=keys)
    return ReindexQueued(queued=bool(task_ids), kinds=kinds, task_ids=task_ids, error=error)


@router.post(
    "/settings/reindex",
    response_model=ReindexQueued,
    summary="Queue a reindex (retry, or run one on demand)",
    responses=_ERRORS,
)
async def queue_reindex(
    body: ReindexRequest,
    principal: Manager,
    session: Session,
    enqueue: Annotated[EnqueueReindex, Depends(get_enqueue_reindex)],
) -> ReindexQueued:
    if not body.confirm_reindex:
        raise HTTPException(
            409,
            {
                "message": "a reindex rebuilds the vector index; search is unavailable or partial while it runs",
                "code": "reindex_confirmation_required",
                "errors": {},
            },
        )
    return await _queue(session, principal, enqueue, [body.kind], [])


@router.get(
    "/settings/reindex", response_model=ReindexStatus, summary="Reindex progress and index drift"
)
async def reindex_status(
    _: Reader, session: Session, status_of: Annotated[TaskStatus, Depends(get_task_status)]
) -> ReindexStatus:
    from app.services.vectorstore import get_vector_store

    cfg = Settings()
    problems: list[str] = []
    try:
        from app.services.reindex import store_status

        store = await asyncio.to_thread(get_vector_store, None, settings=cfg, verify=False)
        info = await store_status(session, store)
        problems.extend(str(p) for p in info.get("problems", []))
    except Exception as exc:
        problems.append(f"vector store not reachable ({type(exc).__name__})")
    try:
        from app.services.keyword import stored_fts_config

        stored = await stored_fts_config(session)
        if stored is not None and stored != cfg.retrieval_fts_config:
            problems.append(
                f"keyword index uses '{stored}' but RETRIEVAL_FTS_CONFIG is '{cfg.retrieval_fts_config}'"
            )
    except Exception:
        await session.rollback()
    last = await svc.latest_reindex(session)
    state: Literal["idle", "pending", "running", "done", "failed", "unknown"] = "idle"
    result: dict[str, Any] | None = None
    error: str | None = None
    task_id = None
    if last is not None and last.detail:
        task_id = str(last.detail.get("task_id") or "") or None
        if task_id:
            try:
                raw, result, error = await asyncio.to_thread(status_of, task_id)
                state = cast(Any, raw)
            except Exception:
                state = "unknown"
    return ReindexStatus(
        state=state,
        needed=bool(problems),
        problems=problems,
        queued_at=last.at if last else None,
        queued_by=last.actor if last else None,
        kind=str(last.detail.get("kind")) if last and last.detail else None,
        task_id=task_id,
        result=result,
        error=error,
    )


@router.post(
    "/settings/test/{service}",
    response_model=TestOut,
    summary="Test a service connection with the saved settings",
    description=(
        "Cheap and safe: LLM/parser = list models with the key (no tokens spent), embeddings = embed one short "
        "string, vector store = heartbeat, storage = head bucket / write test. Never returns secrets."
    ),
)
async def test_connection(
    service: Literal["llm", "embeddings", "vector_store", "storage", "parser"],
    _: Manager,
    session: Session,
) -> TestOut:
    await _refresh(session)
    out = await connection_test.run(service)
    return TestOut(**out.__dict__)


@router.get(
    "/settings/audit",
    response_model=list[AuditOut],
    summary="Who changed which setting (no secret values)",
)
async def settings_audit(
    _: Manager,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
    key: str | None = None,
) -> list[AuditOut]:
    rows = await svc.list_audit(session, limit=limit, before=before, key=key)
    return [
        AuditOut(
            id=str(r.id),
            at=r.at,
            actor=r.actor,
            action=r.action,
            key=r.key,
            old_value=r.old_value,
            new_value=r.new_value,
            secret=r.secret,
            detail=r.detail,
        )
        for r in rows
    ]


@router.post(
    "/settings/{key}/reset",
    response_model=PatchResult,
    summary="Return one setting to config.json / the built-in default",
    responses=_ERRORS,
)
async def reset_setting(
    key: str,
    principal: Manager,
    session: Session,
    enqueue: Annotated[EnqueueReindex, Depends(get_enqueue_reindex)],
    confirm_reindex: bool = False,
) -> PatchResult:
    if key not in REGISTRY:
        raise HTTPException(404, "unknown setting")
    return await patch_settings(
        SettingsPatch(set={}, reset=[key], confirm_reindex=confirm_reindex, dry_run=False),
        principal,
        session,
        enqueue,
    )
