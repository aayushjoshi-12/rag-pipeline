from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"email": "you@example.com", "password": "correct horse"}]}
    )

    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    is_builtin: bool
    permissions: list[str]


class RoleCreate(BaseModel):
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    permissions: list[str] | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    is_active: bool
    roles: list[str]
    permissions: list[str]


class UserCreate(BaseModel):
    email: str
    password: str
    role_ids: list[uuid.UUID] = Field(default_factory=list)
    is_active: bool = True


class UserUpdate(BaseModel):
    password: str | None = None
    is_active: bool | None = None
    role_ids: list[uuid.UUID] | None = None


class PermissionOut(BaseModel):
    name: str
    description: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="A label for the client, e.g. `billing-service`.")
    role_id: uuid.UUID = Field(description="The role whose permissions the key gets (read on every request).")
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650, description="Omit for a key that never expires."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "billing-service",
                    "role_id": "0d1c0a2e-5f67-4b0c-9a3e-7a1d5c1f2b10",
                    "expires_in_days": 90,
                }
            ]
        }
    )


class ApiKeyOut(BaseModel):
    """A key as listed: never contains the secret."""

    id: uuid.UUID
    name: str
    prefix: str = Field(description="Public part of the key, safe to show; identifies it in lists and logs.")
    role_id: uuid.UUID
    role: str
    owner_email: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None = Field(description="Updated at most once per minute.")
    revoked_at: datetime | None
    status: str = Field(description="`active`, `expired` or `revoked`.")


class ApiKeyCreated(ApiKeyOut):
    """Returned once, by POST /api-keys. ``key`` is the only time the secret is available."""

    key: str = Field(description="The full API key. Store it now: it cannot be retrieved again.")
    warnings: list[str] = Field(default_factory=list)


class MeOut(BaseModel):
    id: uuid.UUID | None
    email: str
    permissions: list[str]
    auth_enabled: bool
