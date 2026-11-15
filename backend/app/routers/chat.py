from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import anyio
import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.observability import log_context
from app.routers._request import RequestTrace, llm_http_error, retrieve_for_request
from app.routers.auth_deps import Principal, require
from app.routers.deps import (
    get_embedder_dep,
    get_llm_dep,
    get_session,
    get_settings,
    get_store,
)
from app.schema.chat import ChatRequest, ChatResponse, ChunkRef, SourceOut
from app.services import observability
from app.services.auth.permissions import Permission
from app.services.embeddings import Embedder
from app.services.llm import LLM, LLMError, LLMRequestError
from app.services.qa import Answer, answer_question, prepare_stream, stream_answer
from app.services.retriever import ChunkResult
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
    rt = RequestTrace.start(
        cfg, request, top_k=req.top_k, mode=req.mode, fusion=req.fusion, cache=req.cache
    )
    neighbors = cfg.retrieval_neighbors if req.neighbors is None else req.neighbors
    try:
        out = await _chat(req, rt, neighbors, session, embedder, store, llm)
        response.headers.update(rt.call.header("answer"))
        return out
    except Exception as exc:
        rt.fail(exc)
        raise
    finally:
        await rt.record(
            session,
            cfg,
            principal,
            kind="chat",
            question=req.question,
            model=getattr(llm, "model", None),
            meta=rt.meta(
                neighbors=neighbors,
                document_ids=req.document_ids,
                layer="answer",
                lead={"cited_document_ids": rt.cited},
            ),
        )


async def _chat(
    req: ChatRequest,
    rt: RequestTrace,
    neighbors: int,
    session: AsyncSession,
    embedder: Embedder,
    store: VectorStore,
    llm: LLM | None,
) -> ChatResponse:
    with (
        log_context(chat_id=uuid.uuid4().hex, question_chars=len(req.question)),
        observability.span(
            "chat.request",
            input=req.question,
            top_k=rt.top_k,
            document_filter=len(req.document_ids or []),
            retrieval_mode=rt.options.mode,
        ) as root,
    ):
        retrieval = await retrieve_for_request(
            rt,
            req.question,
            neighbors=neighbors,
            document_ids=req.document_ids,
            embedder=embedder,
            store=store,
            session=session,
            span_prefix="chat",
        )
        chunks = retrieval.passages

        try:
            with observability.span("chat.answer", sources=len(chunks)):
                result = await answer_question(req.question, chunks, llm, cache=rt.call)
        except LLMError as exc:
            rt.keep_usage(exc)
            logger.warning(
                "llm_rejected" if isinstance(exc, LLMRequestError) else "llm_unavailable",
                error=str(exc),
            )
            raise llm_http_error(exc) from exc

        rt.succeed(result)
        root.update(
            output=observability.maybe_content(result.answer),
            metadata={"sources": len(result.sources)},
        )
        logger.info("chat_answered", sources=len(result.sources))
        return ChatResponse(
            cache=rt.call.result("answer"),
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
    rt = RequestTrace.start(
        cfg, request, top_k=req.top_k, mode=req.mode, fusion=req.fusion, cache=req.cache
    )
    neighbors = cfg.retrieval_neighbors if req.neighbors is None else req.neighbors
    call = rt.call

    async def record() -> uuid.UUID | None:
        return await rt.record(
            session,
            cfg,
            principal,
            kind="chat",
            question=req.question,
            model=getattr(llm, "model", None),
            meta=rt.meta(
                neighbors=neighbors,
                document_ids=req.document_ids,
                layer="answer",
                lead={"cited_document_ids": rt.cited},
                mid={
                    "streamed": True,
                    "ttft_ms": rt.ttft_ms,
                    **({"cancelled": True} if rt.cancelled else {}),
                },
            ),
        )

    try:
        with (
            log_context(chat_id=uuid.uuid4().hex, question_chars=len(req.question)),
            observability.span(
                "chat.retrieve", input=req.question, top_k=rt.top_k, retrieval_mode=rt.options.mode
            ),
        ):
            retrieval = await retrieve_for_request(
                rt,
                req.question,
                neighbors=neighbors,
                document_ids=req.document_ids,
                embedder=embedder,
                store=store,
                session=session,
                span_prefix="chat",
            )
            prep = await prepare_stream(req.question, retrieval.passages, llm, cache=call)
    except LLMError as exc:
        logger.warning("llm_unavailable", error=str(exc))
        err = llm_http_error(exc)
        rt.fail(err)
        await record()
        raise err from exc
    except Exception as exc:
        rt.fail(exc)
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
                            if rt.ttft_ms is None:
                                rt.ttft_ms = rt.elapsed_ms()
                            yield sse("token", {"text": item})
                        else:
                            result = item
            except Exception as exc:
                if isinstance(exc, LLMError):
                    logger.warning("llm_stream_failed", error=str(exc))
                    err = llm_http_error(exc)
                    code, detail = err.status_code, str(err.detail)
                else:
                    logger.exception("chat_stream_failed")
                    code, detail = 500, "internal error while streaming the answer"
                rt.keep_usage(exc)
                rt.fail(exc)
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
            rt.succeed(result)
            recorded = True
            trace_id = await record()
            logger.info("chat_streamed", sources=len(sources), ttft_ms=rt.ttft_ms)
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
            rt.error = "client_disconnected"
            rt.cancelled = True
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
