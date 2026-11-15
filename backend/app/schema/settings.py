"""Request/response models for the runtime-settings API (ticket 43, ADR-0008)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SettingOut(BaseModel):
    key: str
    group: str
    description: str
    kind: Literal["string", "text", "integer", "number", "boolean", "enum", "path"]
    nullable: bool
    choices: list[str] | None = None
    unit: str | None = None
    note: str | None = Field(None, description="Consequence / caveat shown next to the field.")
    apply: Literal["live", "restart", "reindex", "bootstrap"] = Field(
        description="live = next request/task; restart = after a process restart; reindex = needs a confirmed "
        "reindex; bootstrap = not editable here."
    )
    secret: bool = Field(
        description="Write-only: `value` and `default` are always null; see `is_set`."
    )
    reindex_action: Literal["reindex", "rebuild_fts"] | None = None
    value: Any = Field(None, description="Effective value (null for secrets).")
    is_set: bool | None = Field(None, description="Secrets only: whether a value is in force.")
    default: Any = None
    source: Literal["env", "ui", "config", "default"] = Field(
        description="Which layer wins: env > ui (database) > config (config.<env>.json) > default."
    )
    locked: bool = Field(
        description="Cannot be changed here (pinned by the environment, or a bootstrap setting)."
    )
    locked_reason: str | None = None
    disabled_reason: str | None = Field(
        None, description="Editable in principle, but not possible right now."
    )
    shadowed: bool = Field(
        False, description="A UI value is stored but the environment overrides it."
    )
    shadowed_value: Any = None
    updated_at: datetime | None = None
    updated_by: str | None = None
    restart_pending: bool = Field(
        False, description="Changed since this process started; needs a restart."
    )
    undecryptable: bool = False


class GroupOut(BaseModel):
    id: str
    description: str


class MissingOut(BaseModel):
    area: str
    message: str


class SettingsSummary(BaseModel):
    secrets_enabled: bool
    restart_pending: list[str]
    overrides_error: str | None
    undecryptable: list[str]
    refresh_seconds: float
    missing: list[MissingOut] = Field(
        default_factory=list, description="First-run hints: what is not configured."
    )


class SettingsOut(BaseModel):
    groups: list[GroupOut]
    settings: list[SettingOut]
    summary: SettingsSummary


class SettingsPatch(BaseModel):
    set: dict[str, Any] = Field(
        default_factory=dict,
        description="key -> new value (secrets: the plain value, never echoed).",
    )
    reset: list[str] = Field(
        default_factory=list,
        description="Keys to return to config.json / default (clears secrets).",
    )
    confirm_reindex: bool = Field(False, description="Required when a change needs a reindex.")
    dry_run: bool = Field(False, description="Validate and report the consequences without saving.")


class ReindexQueued(BaseModel):
    queued: bool
    kinds: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class PatchResult(BaseModel):
    changed: list[str]
    reset: list[str]
    unchanged: list[str]
    restart_required: list[str]
    reindex_required: list[str]
    dry_run: bool
    reindex: ReindexQueued | None = None
    settings: list[SettingOut] = Field(description="The touched settings after the change.")


class AuditOut(BaseModel):
    id: str
    at: datetime
    actor: str
    action: str
    key: str
    old_value: Any = None
    new_value: Any = None
    secret: bool
    detail: dict[str, Any] | None = None


class ReindexStatus(BaseModel):
    state: Literal["idle", "pending", "running", "done", "failed", "unknown"]
    needed: bool = Field(
        description="The stored index differs from the settings (searches may 503 until fixed)."
    )
    problems: list[str]
    queued_at: datetime | None = None
    queued_by: str | None = None
    kind: str | None = None
    task_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class ReindexRequest(BaseModel):
    confirm_reindex: bool = False
    kind: Literal["reindex", "rebuild_fts"] = "reindex"


class TestOut(BaseModel):
    service: str
    ok: bool
    detail: str
    provider: str
    latency_ms: int
