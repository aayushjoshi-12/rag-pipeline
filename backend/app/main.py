from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import Settings
from app.db.engine import async_session, engine
from app.observability import configure_for_server
from app.routers import api_keys, auth, chat, documents, health, search, traces
from app.services import keyword, observability, retriever
from app.services.auth import accounts
from app.services.langfuse import configure_from_settings
from app.services.vectorstore import VectorStoreMismatchError, log_effective_settings

logger = logging.getLogger(__name__)
settings = Settings()


async def _seed_auth() -> None:
    """Create built-in roles and (first run only) the bootstrap admin. Never fatal."""
    if not settings.auth_enabled:
        logger.warning("AUTH_ENABLED=false: every request is treated as an admin. Dev/demo only.")
        return
    if not settings.secret_key:
        logger.warning("SECRET_KEY is empty: using an ephemeral signing key; logins reset on restart.")
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
    await asyncio.to_thread(log_effective_settings, settings=settings)
    await _check_keyword_index()
    yield
    observability.flush()
    await engine.dispose()


app = FastAPI(title="rag-pipeline", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(api_keys.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(search.router)
app.include_router(traces.router)


@app.exception_handler(VectorStoreMismatchError)
async def _vector_store_mismatch(_: Request, exc: VectorStoreMismatchError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})
