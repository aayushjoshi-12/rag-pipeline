from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
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
from app.schema.chat import ChatRequest, ChatResponse, ChunkRef, SourceOut
from app.services import observability, traces
from app.services.auth.permissions import Permission
from app.services.cache import CacheCall, get_cache, wants_bypass
from app.services.embeddings import Embedder
from app.services.llm import LLM, LLMConfigError, LLMRequestError, LLMUnavailableError
from app.services.qa import answer_question
from app.services.retriever import RetrievalOptions
from app.services.search import retrieve_ranked
from app.services.vectorstore import VectorStore

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require(Permission.CHAT_USE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    cfg: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder_dep)],
    store: Annotated[VectorStore, Depends(get_store)],
    llm: Annotated[LLM | None, Depends(get_llm_dep)],
) -> ChatResponse:
    top_k = req.top_k or cfg.default_top_k
    neighbors = cfg.retrieval_neighbors if req.neighbors is None else req.neighbors
    options = RetrievalOptions.from_settings(cfg, mode=req.mode, fusion=req.fusion)
    started = time.perf_counter()
    call = CacheCall(
        get_cache(cfg), bypass=wants_bypass(req.cache, request.headers.get("cache-control"))
    )
    outcome: dict[str, Any] = {
        "status": "error",
        "error": None,
        "hits": None,
        "prompt": None,
        "completion": None,
        "cost": None,
        "cited": [],
    }
    try:
        out = await _chat(
            req, top_k, neighbors, options, session, cfg, embedder, store, llm, outcome, call
        )
        response.headers.update(call.header("answer"))
        return out
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
            cost_usd=outcome["cost"],
            error=outcome["error"],
            question=req.question if cfg.trace_capture_content else None,
            meta={
                "top_k": top_k,
                "neighbors": neighbors,
                "document_filter": len(req.document_ids or []),
                # ids only (never content): which documents the answer was grounded on
                "cited_document_ids": outcome["cited"],
                **({"cache": call.trace_meta("answer")} if call.active else {}),
                **options.trace_meta(),
            },
        )


async def _chat(
    req: ChatRequest,
    top_k: int,
    neighbors: int,
    options: RetrievalOptions,
    session: AsyncSession,
    cfg: Settings,
    embedder: Embedder,
    store: VectorStore,
    llm: LLM | None,
    outcome: dict[str, Any],
    call: CacheCall,
) -> ChatResponse:
    with (
        log_context(chat_id=uuid.uuid4().hex, question_chars=len(req.question)),
        observability.span(
            "chat.request",
            input=req.question,
            top_k=top_k,
            document_filter=len(req.document_ids or []),
            retrieval_mode=options.mode,
        ) as root,
    ):
        retrieval = await retrieve_ranked(
            req.question,
            top_k=top_k,
            neighbors=neighbors,
            options=options,
            document_ids=req.document_ids,
            embedder=embedder,
            store=store,
            session=session,
            span_prefix="chat",
            cache=call,
        )
        chunks = retrieval.passages
        outcome["hits"] = len(retrieval.hits)

        try:
            with observability.span("chat.answer", sources=len(chunks)):
                result = await answer_question(req.question, chunks, llm, cache=call)
        except (LLMUnavailableError, LLMConfigError) as exc:
            logger.warning("llm_unavailable", error=str(exc))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except LLMRequestError as exc:
            logger.warning("llm_rejected", error=str(exc))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

        outcome.update(
            status="ok",
            prompt=result.prompt_tokens,
            completion=result.completion_tokens,
            cost=result.cost_usd,
            cited=list(dict.fromkeys(str(c.document_id) for c in result.sources if c.is_hit)),
        )
        root.update(
            output=observability.maybe_content(result.answer),
            metadata={"sources": len(result.sources)},
        )
        logger.info("chat_answered", sources=len(result.sources))
        return ChatResponse(
            cache=call.result("answer"),
            answer=result.answer,
            sources=[
                SourceOut(
                    text=c.text,
                    document_id=c.document_id,
                    chunk_index=c.chunk_index,
                    filename=c.filename,
                    score=c.score,
                    score_kind=c.score_kind,
                    semantic_rank=c.semantic_rank,
                    keyword_rank=c.keyword_rank,
                    is_hit=c.is_hit,
                    rank=c.rank,
                    context_for=(
                        ChunkRef(document_id=c.context_for[0], chunk_index=c.context_for[1])
                        if c.context_for
                        else None
                    ),
                )
                for c in result.sources
            ],
        )
