from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import anyio
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
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
from app.services.llm import (
    LLM,
    LLMConfigError,
    LLMError,
    LLMRequestError,
    LLMUnavailableError,
)
from app.services.qa import Answer, answer_question, prepare_stream, stream_answer
from app.services.retriever import ChunkResult, RetrievalOptions
from app.services.search import retrieve_ranked
from app.services.vectorstore import VectorStore

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/chat", response_model=ChatResponse, summary="Ask a question (retrieval-augmented answer with sources)")
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
            sources=[_source_out(c) for c in result.sources],
        )


def _source_out(c: ChunkResult) -> SourceOut:
    return SourceOut(
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


def sse(event: str, data: dict[str, Any]) -> bytes:
    """One Server-Sent Event frame (single-line JSON payload, so no data-line splitting is needed)."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def _llm_http_error(exc: LLMError) -> HTTPException:
    if isinstance(exc, LLMRequestError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE, see docs/api.md"}},
)
async def chat_stream(
    req: ChatRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require(Permission.CHAT_USE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    cfg: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder_dep)],
    store: Annotated[VectorStore, Depends(get_store)],
    llm: Annotated[LLM | None, Depends(get_llm_dep)],
) -> StreamingResponse:
    """Same inputs and permission as ``POST /chat``; the answer arrives as Server-Sent Events.

    Everything that can fail cheaply (auth, validation, retrieval, missing LLM key) happens BEFORE the
    response starts and is a normal HTTP error. After that the stream ends with exactly one ``done`` or
    ``error`` event, unless the client left, in which case the upstream LLM call is cancelled.
    """
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
        "ttft_ms": None,
        "cancelled": False,
    }

    async def record() -> uuid.UUID | None:
        return await traces.record(
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
                "cited_document_ids": outcome["cited"],
                "streamed": True,
                "ttft_ms": outcome["ttft_ms"],
                **({"cancelled": True} if outcome["cancelled"] else {}),
                **({"cache": call.trace_meta("answer")} if call.active else {}),
                **options.trace_meta(),
            },
        )

    try:
        with (
            log_context(chat_id=uuid.uuid4().hex, question_chars=len(req.question)),
            observability.span(
                "chat.retrieve", input=req.question, top_k=top_k, retrieval_mode=options.mode
            ),
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
            outcome["hits"] = len(retrieval.hits)
            prep = await prepare_stream(req.question, retrieval.passages, llm, cache=call)
    except LLMError as exc:
        logger.warning("llm_unavailable", error=str(exc))
        err = _llm_http_error(exc)
        outcome["error"] = str(err.detail)
        await record()
        raise err from exc
    except HTTPException as exc:
        outcome["error"] = str(exc.detail)
        await record()
        raise
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        await record()
        raise

    sources = [_source_out(c) for c in prep.sources]

    async def events() -> AsyncIterator[bytes]:
        recorded = False
        try:
            yield sse(
                "sources",
                {
                    "sources": [s.model_dump(mode="json") for s in sources],
                    "cache": call.result("answer"),
                },
            )
            result: Answer | None = None
            try:
                with observability.span("chat.answer", sources=len(sources), streamed=True):
                    async for item in stream_answer(prep):
                        if isinstance(item, str):
                            if outcome["ttft_ms"] is None:
                                outcome["ttft_ms"] = int((time.perf_counter() - started) * 1000)
                            yield sse("token", {"text": item})
                        else:
                            result = item
            except Exception as exc:
                if isinstance(exc, LLMError):
                    logger.warning("llm_stream_failed", error=str(exc))
                    code = 502 if isinstance(exc, LLMRequestError) else 503
                    detail = str(exc)
                else:
                    logger.exception("chat_stream_failed")
                    code, detail = 500, "internal error while streaming the answer"
                outcome["error"] = f"{type(exc).__name__}: {exc}"
                recorded = True
                trace_id = await record()
                yield sse(
                    "error",
                    {
                        "status": code,
                        "detail": detail,
                        "trace_id": str(trace_id) if trace_id else None,
                    },
                )
                return
            assert result is not None
            outcome.update(
                status="ok",
                prompt=result.prompt_tokens,
                completion=result.completion_tokens,
                cost=result.cost_usd,
                cited=list(dict.fromkeys(str(c.document_id) for c in result.sources if c.is_hit)),
            )
            recorded = True
            trace_id = await record()
            logger.info("chat_streamed", sources=len(sources), ttft_ms=outcome["ttft_ms"])
            yield sse(
                "done",
                {
                    "answer": result.answer,
                    "usage": {
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                    },
                    "cost_usd": result.cost_usd,
                    "cache": call.result("answer"),
                    "trace_id": str(trace_id) if trace_id else None,
                },
            )
        except (asyncio.CancelledError, GeneratorExit):
            # the client went away: aiter_stream closes the upstream call, nothing was cached
            outcome["error"] = "client_disconnected"
            outcome["cancelled"] = True
            raise
        finally:
            if not recorded:
                with anyio.CancelScope(shield=True):
                    await record()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **call.header("answer")},
    )
