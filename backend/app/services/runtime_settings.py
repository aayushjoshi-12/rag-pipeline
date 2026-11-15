"""The "UI" settings layer at runtime (ticket 43, ADR-0008): load, validate and serve admin overrides.

The overrides live in the ``app_settings`` table. Each process keeps an in-memory snapshot of them
(``app.config.set_db_overrides``) that every ``Settings()`` reads through ``DbOverridesSource``. A snapshot is
only installed after a full ``Settings()`` has been built from it, so a bad row can never take the app down.

Freshness: the API refreshes lazily (``ensure_fresh``, at most every ``SETTINGS_REFRESH_SECONDS``) and
immediately after its own writes; Celery tasks call ``refresh`` when they start. There is no push channel on
purpose: the database is the single source of truth and replicas converge within the interval.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, db_overrides_as, get_db_overrides, set_db_overrides
from app.db.models import AppSetting
from app.services.settings_crypto import CryptoError, SettingsCipher
from app.services.settings_registry import EDITABLE_KEYS, REGISTRY

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RowMeta:
    updated_at: datetime | None
    updated_by: str
    is_secret: bool


@dataclass
class Snapshot:
    fingerprint: tuple[Any, ...] = ()
    values: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, RowMeta] = field(default_factory=dict)
    #: Secret rows that exist but cannot be decrypted with the configured key(s): not applied.
    undecryptable: list[str] = field(default_factory=list)
    #: Why the latest stored overrides were NOT installed (the previous snapshot stays in force).
    error: str | None = None


_snapshot = Snapshot()
_factory: async_sessionmaker[AsyncSession] | None = None
_last_check = 0.0
_last_warned = 0.0
_boot_values: dict[str, Any] = {}


def snapshot() -> Snapshot:
    return _snapshot


def reset_for_tests() -> None:
    global _snapshot, _factory, _last_check, _last_warned
    _snapshot = Snapshot()
    _factory = None
    _last_check = 0.0
    _last_warned = 0.0
    _boot_values.clear()
    set_db_overrides({})


def cipher_for(cfg: Settings | None = None) -> SettingsCipher:
    cfg = cfg or Settings()
    return SettingsCipher(cfg.settings_encryption_key, cfg.settings_encryption_key_previous)


def describe_validation_error(exc: Exception) -> dict[str | None, str]:
    """``{setting key or None: message}`` without ever echoing an input value (pydantic's str() does)."""
    out: dict[str | None, str] = {}
    if isinstance(exc, ValidationError):
        for err in exc.errors(include_input=False, include_url=False):
            loc = err.get("loc") or ()
            key = str(loc[0]) if loc and str(loc[0]) in REGISTRY else None
            msg = str(err.get("msg", "invalid value"))
            msg = msg.removeprefix("Value error, ")
            out.setdefault(key, msg)
        return out or {None: "invalid settings"}
    return {None: str(exc) or type(exc).__name__}


def validate_candidate(values: dict[str, Any]) -> dict[str | None, str]:
    """Build a full ``Settings`` as if ``values`` were the stored layer. ``{}`` = valid."""
    try:
        with db_overrides_as(values):
            Settings()
    except Exception as exc:
        return describe_validation_error(exc)
    return {}


def decode_rows(
    rows: list[AppSetting], cipher: SettingsCipher
) -> tuple[dict[str, Any], dict[str, RowMeta], list[str]]:
    values: dict[str, Any] = {}
    meta: dict[str, RowMeta] = {}
    bad: list[str] = []
    for row in rows:
        spec = REGISTRY.get(row.key)
        if spec is None or row.key not in EDITABLE_KEYS or spec.secret != row.is_secret:
            logger.warning("ignoring stored setting '%s': not an editable setting", row.key)
            continue
        if row.is_secret:
            if not row.secret_blob:
                continue
            try:
                values[row.key] = cipher.decrypt(row.key, row.secret_blob)
            except CryptoError:
                bad.append(row.key)
                logger.error("stored secret '%s' cannot be decrypted; it is not applied", row.key)
                continue
        else:
            values[row.key] = row.value
        meta[row.key] = RowMeta(row.updated_at, row.updated_by, row.is_secret)
    return values, meta, sorted(bad)


async def load_rows(session: AsyncSession) -> list[AppSetting]:
    return list((await session.execute(select(AppSetting).order_by(AppSetting.key))).scalars())


async def refresh(session: AsyncSession, *, force: bool = False) -> bool:
    """Re-read the stored overrides; install them when changed and valid. Returns True when installed."""
    global _snapshot
    rows = await load_rows(session)
    fingerprint = tuple((r.key, r.updated_at, r.is_secret) for r in rows)
    if not force and fingerprint == _snapshot.fingerprint:
        return False
    values, meta, bad = decode_rows(rows, cipher_for())
    problems = validate_candidate(values)
    if problems:
        message = "; ".join(f"{k or 'settings'}: {m}" for k, m in problems.items())
        logger.error("stored settings are invalid and were NOT applied: %s", message)
        _snapshot = Snapshot(
            fingerprint=fingerprint,
            values=_snapshot.values,
            meta=_snapshot.meta,
            undecryptable=bad,
            error=message,
        )
        return False
    _snapshot = Snapshot(fingerprint=fingerprint, values=values, meta=meta, undecryptable=bad)
    set_db_overrides(values)
    return True


async def refresh_quietly(session: AsyncSession) -> None:
    """``refresh`` for Celery tasks: a missing table / unreachable layer must never fail a job."""
    try:
        await refresh(session)
    except Exception as exc:
        await session.rollback()  # a failed statement poisons the transaction
        logger.warning(
            "settings layer not loaded (%s); using env/config/defaults", type(exc).__name__
        )


def configure(factory: async_sessionmaker[AsyncSession]) -> None:
    global _factory
    _factory = factory


async def start(factory: async_sessionmaker[AsyncSession]) -> None:
    """API start-up: remember the session factory, load the layer, record this process's boot values."""
    configure(factory)
    await ensure_fresh(force=True)
    record_boot_values()


def record_boot_values() -> None:
    cfg = Settings()
    _boot_values.clear()
    _boot_values.update({k: getattr(cfg, k) for k, s in REGISTRY.items() if s.apply == "restart"})


def restart_pending_keys(cfg: Settings | None = None) -> list[str]:
    """Restart-only settings whose effective value differs from what this process started with."""
    if not _boot_values:
        return []
    cfg = cfg or Settings()
    return sorted(k for k, v in _boot_values.items() if getattr(cfg, k) != v)


async def ensure_fresh(*, force: bool = False) -> None:
    """Refresh at most every ``SETTINGS_REFRESH_SECONDS``; never raises (the app keeps the last good layer)."""
    global _last_check, _last_warned
    if _factory is None:
        return
    now = time.monotonic()
    interval = Settings().settings_refresh_seconds
    if not force and now - _last_check < interval:
        return
    _last_check = now  # set first: concurrent requests skip instead of piling onto the database
    try:
        async with _factory() as session:
            await refresh(session, force=force)
    except Exception as exc:
        if now - _last_warned > 60:
            _last_warned = now
            logger.warning(
                "settings layer not refreshed (%s); keeping the last known values",
                type(exc).__name__,
            )


def installed_overrides() -> dict[str, Any]:
    return get_db_overrides()
