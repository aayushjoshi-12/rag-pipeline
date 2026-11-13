from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.config import Settings
from app.db.engine import engine
from app.observability import configure_for_server
from app.routers import chat, documents, health

logger = logging.getLogger(__name__)
settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_for_server(level=logging.getLevelName(settings.log_level))
    logger.info("rag-pipeline starting (env=%s)", settings.rag_env)
    # Warm the async engine pool; failures are non-fatal here (health endpoint reports).
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("DB not reachable at startup: %s", exc)
    yield
    await engine.dispose()


app = FastAPI(title="rag-pipeline", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(documents.router)
app.include_router(chat.router)
