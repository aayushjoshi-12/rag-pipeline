from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.observability import log_context
from app.routers.auth_deps import Principal, require
from app.routers.deps import (
    get_embedder_dep,
    get_llm_dep,
    get_session,
    get_settings,
    get_store,
)
from app.schema.chat import ChatRequest, ChatResponse, SourceOut
from app.services import observability, traces
from app.services.auth.permissions import Permission
from app.services.context import add_neighbor_context
from app.services.embeddings import Embedder
from app.services.llm import LLM, LLMConfigError, LLMRequestError, LLMUnavailableError
from app.services.qa import answer_question
from app.services.retriever import retrieve
from app.services.vectorstore import VectorStore

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    principal: Annotated[Principal, Depends(require(Permission.CHAT_USE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    cfg: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder_dep)],
    store: Annotated[VectorStore, Depends(get_store)],
    llm: Annotated[LLM | None, Depends(get_llm_dep)],
) -> ChatResponse:
    top_k = req.top_k or cfg.default_top_k
    neighbors = cfg.retrieval_neighbors if req.neighbors is None else req.neighbors
    started = time.perf_counter()
    outcome: dict[str, Any] = {"status": "error", "error": None, "hits": None, "prompt": None, "completion": None}
    try:
        return await _chat(req, top_k, neighbors, session, cfg, embedder, store, llm, outcome)
    except HTTPException as exc:
        outcome["error"] = str(exc.detail)
        raise
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await traces.record(
            session,
            kind="chat",
            status=outcome["status"],
            duration_ms=int((time.perf_counter() - started) * 1000),
            user_email=None if principal.anonymous else principal.email,
            model=getattr(llm, "model", None),
            prompt_tokens=outcome["prompt"],
            completion_tokens=outcome["completion"],
            hits=outcome["hits"],
            error=outcome["error"],
            question=req.question if cfg.trace_capture_content else None,
            meta={"top_k": top_k, "neighbors": neighbors, "document_filter": len(req.document_ids or [])},
        )


async def _chat(
    req: ChatRequest,
    top_k: int,
    neighbors: int,
    session: AsyncSession,
    cfg: Settings,
    embedder: Embedder,
    store: VectorStore,
    llm: LLM | None,
    outcome: dict[str, Any],
) -> ChatResponse:
    with (
        log_context(chat_id=uuid.uuid4().hex, question_chars=len(req.question)),
        observability.span(
            "chat.request",
            input=req.question,
            top_k=top_k,
            document_filter=len(req.document_ids or []),
        ) as root,
    ):
        try:
            with observability.span("chat.retrieve", top_k=top_k) as sp:
                chunks = await retrieve(
                    req.question,
                    top_k=top_k,
                    embedder=embedder,
                    store=store,
                    session=session,
                    document_ids=req.document_ids,
                )
                sp.update(metadata={"hits": len(chunks)})
            outcome["hits"] = len(chunks)
            if neighbors:
                with observability.span("chat.context", neighbors=neighbors):
                    chunks = await add_neighbor_context(chunks, neighbors, session=session)
        except Exception as exc:
            logger.exception("retrieval_failed")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Retrieval backend unavailable."
            ) from exc

        try:
            with observability.span("chat.answer", sources=len(chunks)):
                result = await answer_question(req.question, chunks, llm)
        except (LLMUnavailableError, LLMConfigError) as exc:
            logger.warning("llm_unavailable", error=str(exc))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except LLMRequestError as exc:
            logger.warning("llm_rejected", error=str(exc))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

        outcome.update(status="ok", prompt=result.prompt_tokens, completion=result.completion_tokens)
        root.update(output=observability.maybe_content(result.answer), metadata={"sources": len(result.sources)})
        logger.info("chat_answered", sources=len(result.sources))
        return ChatResponse(
            answer=result.answer,
            sources=[
                SourceOut(
                    text=c.text,
                    document_id=c.document_id,
                    chunk_index=c.chunk_index,
                    filename=c.filename,
                    score=c.score,
                    is_hit=c.is_hit,
                )
                for c in result.sources
            ],
        )
