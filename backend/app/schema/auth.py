from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
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


class MeOut(BaseModel):
    id: uuid.UUID | None
    email: str
    permissions: list[str]
    auth_enabled: bool
