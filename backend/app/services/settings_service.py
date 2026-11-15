"""Reading and changing runtime settings (ticket 43, ADR-0008). No HTTP knowledge here."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, config_file_keys, db_overrides_as, env_pin
from app.db.models import AppSetting, SettingsAudit, utcnow
from app.services import runtime_settings as rt
from app.services.settings_registry import (
    GROUPS,
    REGISTRY,
    ValueError_,
    coerce,
    coerce_secret,
    default_of,
    field_kind,
    scrub,
)

logger = logging.getLogger(__name__)

_AUDIT_VALUE_LIMIT = 2000


class SettingsOpError(Exception):
    """A rejected change. ``code`` is the stable machine code; ``errors`` is per setting key."""

    def __init__(
        self, status: int, code: str, message: str, errors: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.errors = errors or {}


@dataclass(frozen=True)
class Actor:
    name: str
    user_id: uuid.UUID | None = None


@dataclass
class ApplyResult:
    changed: list[str] = field(default_factory=list)
    reset: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    restart_required: list[str] = field(default_factory=list)
    reindex_keys: list[str] = field(default_factory=list)
    reindex_actions: list[str] = field(default_factory=list)
    dry_run: bool = False


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value


def _audit_value(value: Any) -> Any:
    value = scrub(_json_safe(value))
    if isinstance(value, str) and len(value) > _AUDIT_VALUE_LIMIT:
        return value[:_AUDIT_VALUE_LIMIT] + "…"
    return value


def _flat_errors(problems: dict[str | None, str], keys: list[str]) -> dict[str, str]:
    """Attribute whole-model validation errors to the keys being changed when they name no key."""
    out: dict[str, str] = {}
    for key, message in problems.items():
        if key is not None:
            out[key] = message
        else:
            for k in keys or ["settings"]:
                out.setdefault(k, message)
    return out


async def _current_values(session: AsyncSession) -> tuple[list[AppSetting], dict[str, Any]]:
    rows = await rt.load_rows(session)
    values, _, _ = rt.decode_rows(rows, rt.cipher_for())
    return rows, values


async def apply_changes(
    session: AsyncSession,
    *,
    actor: Actor,
    set_values: dict[str, Any],
    reset: list[str],
    confirm_reindex: bool = False,
    dry_run: bool = False,
) -> ApplyResult:
    """All-or-nothing: validate everything first, then write rows + audit in one transaction."""
    touched = [*set_values, *reset]
    if not touched:
        raise SettingsOpError(422, "validation_error", "no changes given")
    if len(set(touched)) != len(touched):
        raise SettingsOpError(422, "validation_error", "a setting appears twice in one request")
    unknown = {k: "unknown setting" for k in touched if k not in REGISTRY}
    if unknown:
        raise SettingsOpError(404, "unknown_setting", "unknown setting", unknown)
    fixed = {
        k: REGISTRY[k].reason or "not editable" for k in touched if REGISTRY[k].apply == "bootstrap"
    }
    if fixed:
        raise SettingsOpError(409, "setting_not_editable", "setting cannot be changed here", fixed)
    pinned = {k: f"set by environment variable {pin}" for k in touched if (pin := env_pin(k))}
    if pinned:
        raise SettingsOpError(409, "setting_locked", "setting is pinned by the environment", pinned)

    cipher = rt.cipher_for()
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for key, raw in set_values.items():
        spec = REGISTRY[key]
        if spec.secret and not cipher.enabled:
            raise SettingsOpError(
                409,
                "secrets_disabled",
                "SETTINGS_ENCRYPTION_KEY is not set, so secrets cannot be stored",
                {key: "secrets disabled"},
            )
        try:
            clean[key] = coerce_secret(raw) if spec.secret else coerce(key, raw)
        except ValueError_ as exc:
            errors[key] = str(exc)
    if errors:
        raise SettingsOpError(422, "validation_error", "invalid value", errors)

    rows, current = await _current_values(session)
    by_key = {r.key: r for r in rows}
    candidate = dict(current)
    candidate.update(clean)
    for key in reset:
        candidate.pop(key, None)
    problems = rt.validate_candidate(candidate)
    if problems:
        raise SettingsOpError(
            422, "validation_error", "invalid combination", _flat_errors(problems, touched)
        )

    before = Settings()
    with db_overrides_as(candidate):
        after = Settings()

    result = ApplyResult(dry_run=dry_run)
    for key in clean:
        existing = by_key.get(key)
        same_row = existing is not None and key in current and current[key] == clean[key]
        (result.unchanged if same_row else result.changed).append(key)
    for key in reset:
        (result.reset if key in by_key else result.unchanged).append(key)
    for key in [*result.changed, *result.reset]:
        spec = REGISTRY[key]
        differs = getattr(before, key) != getattr(after, key)
        if spec.apply == "reindex" and differs:
            result.reindex_keys.append(key)
            if spec.reindex_action not in result.reindex_actions:
                result.reindex_actions.append(spec.reindex_action)
        if spec.apply == "restart" and differs:
            result.restart_required.append(key)
    if result.reindex_keys and not confirm_reindex:
        raise SettingsOpError(
            409,
            "reindex_confirmation_required",
            "this change requires a reindex; resend with confirm_reindex=true",
            {k: REGISTRY[k].note or "needs a reindex" for k in result.reindex_keys},
        )
    if dry_run:
        return result

    now = utcnow()
    for key in result.changed:
        spec = REGISTRY[key]
        existing = by_key.get(key)
        if spec.secret:
            blob = cipher.encrypt(key, clean[key])
            await _upsert(session, existing, key, now, actor, is_secret=True, blob=blob)
            session.add(_audit(actor, "set_secret", key, secret=True))
        else:
            await _upsert(session, existing, key, now, actor, is_secret=False, value=clean[key])
            session.add(
                _audit(
                    actor,
                    "set",
                    key,
                    old=_audit_value(getattr(before, key)),
                    new=_audit_value(clean[key]),
                )
            )
    for key in result.reset:
        spec = REGISTRY[key]
        old = current.get(key)
        await session.execute(delete(AppSetting).where(AppSetting.key == key))
        if spec.secret:
            session.add(_audit(actor, "clear_secret", key, secret=True))
        else:
            session.add(
                _audit(
                    actor,
                    "reset",
                    key,
                    old=_audit_value(old),
                    new=_audit_value(getattr(after, key)),
                )
            )
    await session.commit()
    await rt.refresh(session, force=True)
    return result


async def _upsert(
    session: AsyncSession,
    existing: AppSetting | None,
    key: str,
    now: datetime,
    actor: Actor,
    *,
    is_secret: bool,
    value: Any = None,
    blob: str | None = None,
) -> None:
    if existing is None:
        session.add(
            AppSetting(
                key=key,
                value=value,
                secret_blob=blob,
                is_secret=is_secret,
                updated_at=now,
                updated_by=actor.name,
            )
        )
        return
    existing.value = value
    existing.secret_blob = blob
    existing.is_secret = is_secret
    existing.updated_at = now
    existing.updated_by = actor.name


def _audit(
    actor: Actor,
    action: str,
    key: str,
    *,
    secret: bool = False,
    old: Any = None,
    new: Any = None,
    detail: dict[str, object] | None = None,
) -> SettingsAudit:
    return SettingsAudit(
        id=uuid.uuid4(),
        at=utcnow(),
        actor=actor.name,
        actor_id=actor.user_id,
        action=action,
        key=key,
        old_value=None if secret else old,
        new_value=None if secret else new,
        secret=secret,
        detail=detail,
    )


async def record_reindex(
    session: AsyncSession, actor: Actor, *, kind: str, task_id: str, keys: list[str]
) -> None:
    session.add(
        _audit(
            actor, "reindex_queued", kind, detail={"task_id": task_id, "kind": kind, "keys": keys}
        )
    )
    await session.commit()


async def list_audit(
    session: AsyncSession,
    *,
    limit: int = 50,
    before: datetime | None = None,
    key: str | None = None,
) -> list[SettingsAudit]:
    stmt = select(SettingsAudit).order_by(SettingsAudit.at.desc(), SettingsAudit.id)
    if before is not None:
        stmt = stmt.where(SettingsAudit.at < before)
    if key:
        stmt = stmt.where(SettingsAudit.key == key)
    return list((await session.execute(stmt.limit(limit))).scalars())


async def latest_reindex(session: AsyncSession) -> SettingsAudit | None:
    stmt = (
        select(SettingsAudit)
        .where(SettingsAudit.action == "reindex_queued")
        .order_by(SettingsAudit.at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ── Read model ────────────────────────────────────────────────────────────────────────────────


def build_view(cfg: Settings | None = None) -> dict[str, Any]:
    """The GET /settings payload. Secret values are never read into it: only ``is_set``."""
    cfg = cfg or Settings()
    snap = rt.snapshot()
    overrides = rt.installed_overrides()
    config_keys = config_file_keys()
    cipher_ok = rt.cipher_for(cfg).enabled
    pending = set(rt.restart_pending_keys(cfg))
    items: list[dict[str, Any]] = []
    order = {name: i for i, (name, _) in enumerate(GROUPS)}
    for key, spec in sorted(REGISTRY.items(), key=lambda kv: order[kv[1].group]):
        kind, nullable = field_kind(key)
        pin = env_pin(key)
        value = getattr(cfg, key)
        if pin:
            source = "env"
        elif key in overrides and spec.apply != "bootstrap":
            source = "ui"
        elif key in config_keys:
            source = "config"
        else:
            source = "default"
        meta = snap.meta.get(key)
        locked = pin is not None or spec.apply == "bootstrap"
        reason = (
            f"set by environment variable {pin}"
            if pin
            else (spec.reason if spec.apply == "bootstrap" else None)
        )
        disabled = None
        if spec.secret and not locked and not cipher_ok:
            disabled = "SETTINGS_ENCRYPTION_KEY is not set on the server, so secrets cannot be stored here."
        shadowed = bool(pin and key in overrides and spec.apply != "bootstrap")
        items.append(
            {
                "key": key,
                "group": spec.group,
                "description": spec.description,
                "kind": kind,
                "nullable": nullable,
                "choices": list(spec.choices) if spec.choices else None,
                "unit": spec.unit,
                "note": spec.note,
                "apply": spec.apply,
                "secret": spec.secret,
                "reindex_action": spec.reindex_action if spec.apply == "reindex" else None,
                "value": None if spec.secret else _audit_value(value),
                "is_set": bool(value) if spec.secret else None,
                "default": None if spec.secret else _audit_value(default_of(key)),
                "source": source,
                "locked": locked,
                "locked_reason": reason,
                "disabled_reason": disabled,
                "shadowed": shadowed,
                "shadowed_value": (
                    None if spec.secret or not shadowed else _audit_value(overrides.get(key))
                ),
                "updated_at": meta.updated_at if meta and (source == "ui" or shadowed) else None,
                "updated_by": meta.updated_by if meta and (source == "ui" or shadowed) else None,
                "restart_pending": key in pending,
                "undecryptable": key in snap.undecryptable,
            }
        )
    return {
        "groups": [{"id": name, "description": desc} for name, desc in GROUPS],
        "settings": items,
        "summary": {
            "secrets_enabled": cipher_ok,
            "restart_pending": sorted(pending),
            "overrides_error": snap.error,
            "undecryptable": snap.undecryptable,
            "refresh_seconds": cfg.settings_refresh_seconds,
        },
    }


def missing_setup(cfg: Settings) -> list[dict[str, str]]:
    """First-run hints: what is not configured yet (no values, only names)."""
    out: list[dict[str, str]] = []
    llm_key = {
        "openrouter": cfg.openrouter_api_key,
        "openai": cfg.openai_api_key,
        "anthropic": cfg.anthropic_api_key,
    }.get(cfg.llm_provider, "")
    if not llm_key:
        out.append(
            {
                "area": "llm",
                "message": f"No API key for the LLM provider '{cfg.llm_provider}': chat answers will fail until one is set.",
            }
        )
    if cfg.embedding_provider == "openai" and not cfg.openai_api_key:
        out.append({"area": "embeddings", "message": "Embedding provider 'openai' has no API key."})
    if (
        cfg.parser_provider in ("openrouter", "openai", "anthropic")
        and not {
            "openrouter": cfg.openrouter_api_key,
            "openai": cfg.openai_api_key,
            "anthropic": cfg.anthropic_api_key,
        }[cfg.parser_provider]
    ):
        out.append(
            {
                "area": "parsing",
                "message": f"Parser provider '{cfg.parser_provider}' has no API key (PDF parsing will fail).",
            }
        )
    return out
