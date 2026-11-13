from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.routers.deps import (
    get_embedder_dep,
    get_llm_dep,
    get_session,
    get_settings,
    get_store,
)
from app.schema.chat import ChatRequest, ChatResponse, SourceOut
from app.services.embeddings import Embedder
from app.services.llm import LLM, LLMConfigError, LLMRequestError, LLMUnavailableError
from app.services.qa import answer_question
from app.services.retriever import retrieve
from app.services.vectorstore import VectorStore

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    cfg: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder_dep)],
    store: Annotated[VectorStore, Depends(get_store)],
    llm: Annotated[LLM | None, Depends(get_llm_dep)],
) -> ChatResponse:
    try:
        chunks = await retrieve(
            req.question,
            top_k=req.top_k or cfg.default_top_k,
            embedder=embedder,
            store=store,
            session=session,
            document_ids=req.document_ids,
        )
    except Exception as exc:
        logger.exception("retrieval failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Retrieval backend unavailable."
        ) from exc

    try:
        result = await answer_question(req.question, chunks, llm)
    except (LLMUnavailableError, LLMConfigError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except LLMRequestError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return ChatResponse(
        answer=result.answer,
        sources=[
            SourceOut(
                text=c.text,
                document_id=c.document_id,
                chunk_index=c.chunk_index,
                filename=c.filename,
                score=c.score,
            )
            for c in result.sources
        ],
    )
