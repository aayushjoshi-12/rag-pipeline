from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import Settings
from app.db.engine import async_session, engine
from app.http_common import (
    ErrorResponse,
    RequestContextMiddleware,
    error_response,
    install_error_handlers,
)
from app.observability import configure_for_server
from app.routers import (
    api_keys,
    auth,
    capabilities,
    chat,
    documents,
    health,
    search,
    traces,
)
from app.routers import cache as cache_router
from app.routers import settings as settings_router
from app.services import keyword, observability, retriever, runtime_settings
from app.services.auth import accounts
from app.services.langfuse import configure_from_settings
from app.services.vectorstore import VectorStoreMismatchError, log_effective_settings
from app.version import API_PREFIX, __version__

logger = logging.getLogger(__name__)
settings = Settings()


async def _seed_auth() -> None:
    """Create built-in roles and (first run only) the bootstrap admin. Never fatal."""
    if not settings.auth_enabled:
        logger.warning("AUTH_ENABLED=false: every request is treated as an admin. Dev/demo only.")
        return
    if not settings.secret_key:
        logger.warning(
            "SECRET_KEY is empty: using an ephemeral signing key; logins reset on restart."
        )
    try:
        async with async_session() as session:
            created = await accounts.seed(
                session, admin_email=settings.admin_email, admin_password=settings.admin_password
            )
        if created:
            logger.info("bootstrap admin created: %s", settings.admin_email)
        else:
            async with async_session() as session:
                if not await accounts.list_users(session):
                    logger.error(
                        "AUTH_ENABLED but there are no users and ADMIN_EMAIL/ADMIN_PASSWORD are "
                        "not set: nobody can sign in. Set them, or run "
                        "`python -m app.manage create-admin --email you@example.com`."
                    )
    except Exception:
        logger.exception("auth seeding failed (is the database migrated?)")


async def _check_keyword_index() -> None:
    """Log the effective retrieval settings; when keyword search is on, check its index matches the config."""
    retriever.log_effective_settings(settings)
    if settings.retrieval_mode == "semantic":
        return
    try:
        async with async_session() as session:
            await keyword.ensure_fts(session, settings.retrieval_fts_config)
    except keyword.KeywordIndexMismatchError as exc:
        logger.error("%s", exc)
    except Exception as exc:
        logger.warning("keyword index not verified at startup: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_for_server(level=logging.getLevelName(settings.log_level))
    configure_from_settings(settings)
    logger.info("rag-pipeline starting (env=%s)", settings.rag_env)
    # Warm the async engine pool; failures are non-fatal here (health endpoint reports).
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("DB not reachable at startup: %s", exc)
    await _seed_auth()
    await runtime_settings.start(async_session)  # the admin-UI settings layer (ticket 43)
    await asyncio.to_thread(log_effective_settings, settings=settings)
    await _check_keyword_index()
    yield
    observability.flush()
    await engine.dispose()


_DESCRIPTION = """\
Ready-to-go Retrieval-Augmented Generation service: ingest documents, search them, ask questions.

**Versioning.** This document describes `/v1`. The same routes without the prefix still work as
**deprecated aliases** (responses carry `Deprecation: true`); build new clients on `/v1`.

**Authentication.** `Authorization: Bearer <access token>` (from `POST /v1/auth/login`) or an API key in
`X-API-Key` (service accounts, see `POST /v1/api-keys`). Disabled only when `AUTH_ENABLED=false`.

**Errors.** Every error is `{"detail", "code", "request_id"}`; `code` is stable and machine-readable.
Every response carries `X-Request-ID` (send your own to correlate).
"""

_TAGS = [
    {"name": "meta", "description": "Server capabilities and limits."},
    {"name": "auth", "description": "Login, tokens, users, roles and permissions."},
    {"name": "api-keys", "description": "Long-lived credentials for services."},
    {"name": "documents", "description": "Upload / ingest documents, poll jobs, read chunks, delete."},
    {"name": "search", "description": "Ranked passages without an LLM."},
    {"name": "chat", "description": "Retrieval-augmented answers with sources."},
    {"name": "cache", "description": "Query cache statistics and purge."},
    {"name": "settings", "description": "Runtime settings: view, change, test connections, audit."},
    {"name": "observability", "description": "Traces, usage and cost."},
]

_COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or expired credentials."},
    403: {"model": ErrorResponse, "description": "The caller lacks the required permission."},
    500: {"model": ErrorResponse, "description": "Unexpected server error."},
}


def build_app(cfg: Settings) -> FastAPI:
    application = FastAPI(
        title="rag-pipeline",
        version=__version__,
        description=_DESCRIPTION,
        openapi_tags=_TAGS,
        lifespan=lifespan,
    )
    # (router, tag or None when the router tags its own routes)
    routers: list[tuple[Any, str | None]] = [
        (capabilities.router, "meta"),
        (auth.router, "auth"),
        (api_keys.router, None),
        (documents.router, "documents"),
        (chat.router, "chat"),
        (search.router, None),
        (cache_router.router, None),
        (settings_router.router, None),
        (traces.router, "observability"),
    ]
    for router, tag in routers:
        tags: list[str | Enum] | None = [tag] if tag else None
        application.include_router(
            router, prefix=API_PREFIX, tags=tags, responses=_COMMON_RESPONSES
        )
        application.include_router(router, tags=tags, include_in_schema=False)
    application.include_router(
        documents.v1_router, prefix=API_PREFIX, tags=["documents"], responses=_COMMON_RESPONSES
    )
    application.include_router(documents.legacy_router, tags=["documents"], include_in_schema=False)
    application.include_router(health.router, tags=["meta"], include_in_schema=False)

    install_error_handlers(application)

    @application.exception_handler(VectorStoreMismatchError)
    async def _vector_store_mismatch(request: Request, exc: VectorStoreMismatchError) -> JSONResponse:
        return error_response(request.scope, 503, str(exc))

    # Middleware order: the LAST added is outermost. The request-context layer is outermost so even a
    # CORS preflight carries X-Request-ID and is logged; CORS sits right inside it, so a preflight is
    # still answered before auth or any route runs.
    if cfg.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID", "Cache-Control"],
            expose_headers=[
                "X-Request-ID",
                "X-Cache",
                "X-Cache-Layers",
                "Deprecation",
                "Link",
                "WWW-Authenticate",
            ],
            allow_credentials=False,  # bearer / API-key auth needs no cookies; avoids '*' + credentials
            max_age=600,
        )
    application.add_middleware(RequestContextMiddleware)
    return application


app = build_app(settings)
