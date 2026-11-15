from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ApiKey, utcnow
from app.routers.auth_deps import Principal, require
from app.routers.deps import get_session, get_settings
from app.schema.auth import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from app.services.auth import accounts, apikeys
from app.services.auth.accounts import AccountError, ConflictError, NotFoundError
from app.services.auth.permissions import Permission

router = APIRouter(tags=["api-keys"])

Admin = Annotated[Principal, Depends(require(Permission.APIKEYS_MANAGE))]
Session = Annotated[AsyncSession, Depends(get_session)]


def _http(exc: AccountError) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _out(key: ApiKey) -> ApiKeyOut:
    now = utcnow()
    if key.revoked_at is not None:
        state = "revoked"
    elif key.expires_at is not None and now >= key.expires_at:
        state = "expired"
    else:
        state = "active"
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        role_id=key.role_id,
        role=key.role.name,
        owner_email=key.owner.email,
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        status=state,
    )


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key (the secret is returned once)",
    description=(
        "Creates a non-interactive credential bound to an existing role. **The `key` in the response is the only "
        "time the secret is ever available**; only its SHA-256 is stored. Send it as `X-API-Key: <key>` or "
        "`Authorization: Bearer <key>`.\n\n"
        "The key's permissions are the role's permissions, read on every request, minus the management "
        "permissions (`users:manage`, `roles:manage`, `apikeys:manage`), which keys never hold. You can only bind a "
        "role whose permissions you hold yourself. Binding the `admin` role is allowed but returns a warning.\n\n"
        'Example response: `{"id": "...", "name": "billing-service", "prefix": "k3j9x0aa", '
        '"key": "rag_k3j9x0aa_<43 random characters>", "status": "active", ...}`'
    ),
    responses={
        409: {"description": "Auth is disabled, or the role holds permissions the creator does not."},
        404: {"description": "Unknown role."},
    },
)
async def create_api_key(
    body: ApiKeyCreate, principal: Admin, session: Session, cfg: Annotated[Settings, Depends(get_settings)]
) -> ApiKeyCreated:
    if not cfg.auth_enabled or principal.user_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "API keys need authentication enabled (AUTH_ENABLED=true)")
    owner = await accounts.get_user(session, principal.user_id)
    role = await accounts.get_role(session, body.role_id)
    if owner is None or role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "role not found")
    warnings = apikeys.warnings_for(role)  # before create(): a prefix-retry rollback expires `role`
    try:
        key, full = await apikeys.create(
            session,
            name=body.name,
            owner=owner,
            role=role,
            creator_permissions=principal.permissions,
            expires_in_days=body.expires_in_days,
        )
    except AccountError as exc:
        raise _http(exc) from exc
    return ApiKeyCreated(**_out(key).model_dump(), key=full, warnings=warnings)


@router.get(
    "/api-keys",
    response_model=list[ApiKeyOut],
    summary="List API keys (never includes secrets)",
)
async def list_api_keys(_: Admin, session: Session) -> list[ApiKeyOut]:
    return [_out(k) for k in await apikeys.list_keys(session)]


@router.post(
    "/api-keys/{key_id}/revoke",
    response_model=ApiKeyOut,
    summary="Revoke an API key (immediate, idempotent)",
    description="Takes effect on the very next request. The row is kept (audit); revoking twice keeps the first time.",
    responses={404: {"description": "Unknown key."}},
)
async def revoke_api_key(key_id: uuid.UUID, _: Admin, session: Session) -> ApiKeyOut:
    key = await apikeys.get(session, key_id)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    return _out(await apikeys.revoke(session, key))
