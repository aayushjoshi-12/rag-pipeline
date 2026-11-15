from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.observability import log_context
from app.routers._request import RequestTrace, retrieve_for_request
from app.routers.auth_deps import Principal, require
from app.routers.deps import get_embedder_dep, get_session, get_settings, get_store
from app.schema.search import ContextChunk, SearchRequest, SearchResponse, SearchResult
from app.services.auth.permissions import Permission
from app.services.embeddings import Embedder, embedding_cost
from app.services.vectorstore import VectorStore

logger = structlog.get_logger(__name__)
router = APIRouter()

_DESCRIPTION = """\
Ranked passages for a query, **without calling an LLM** (no LLM key needed; works when the LLM is down).

Uses exactly the retrieval path of `POST /chat` (same modes, filters, de-duplication and ranking), so for the
same query and options the results are the passages `/chat` would have cited, in the same order.

**Ordering guarantee.** `results` are hits only, sorted best first; `rank` is 1..n with no gaps. Neighbouring
chunks (`neighbors` > 0) are never mixed into `results`: they are listed under the hit they surround in
`results[].context` (document order, unscored, no ranks). `score` is mode-specific (see `score_kind`).

Errors: `422` invalid request, `503` retrieval backend unavailable or the keyword/vector index does not match
the configuration (the message names the fix), `401`/`403` missing credentials / the `search:use` permission.
"""


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Search documents (ranked passages, no LLM)",
    description=_DESCRIPTION,
    tags=["search"],
    responses={
        503: {"description": "Retrieval backend unavailable, or keyword/vector index mismatch."},
    },
)
async def search(
    req: SearchRequest,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require(Permission.SEARCH_USE))],
    session: Annotated[AsyncSession, Depends(get_session)],
    cfg: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder_dep)],
    store: Annotated[VectorStore, Depends(get_store)],
) -> SearchResponse:
    rt = RequestTrace.start(
        cfg, request, top_k=req.top_k, mode=req.mode, fusion=req.fusion, cache=req.cache
    )
    options, call = rt.options, rt.call
    try:
        with log_context(question_chars=len(req.query)):
            retrieval = await retrieve_for_request(
                rt,
                req.query,
                neighbors=req.neighbors,
                document_ids=req.document_ids,
                embedder=embedder,
                store=store,
                session=session,
                span_prefix="search",
            )
        rt.status = "ok"
        results = [
            SearchResult(
                rank=h.rank or 0,
                document_id=h.document_id,
                chunk_index=h.chunk_index,
                filename=h.filename,
                text=h.text if req.include_text else None,
                score=h.score if h.score is not None else 0.0,
                score_kind=h.score_kind,
                semantic_rank=h.semantic_rank,
                keyword_rank=h.keyword_rank,
                context=[
                    ContextChunk(
                        chunk_index=c.chunk_index, text=c.text if req.include_text else None
                    )
                    for c in retrieval.context_for(h)
                ],
            )
            for h in retrieval.hits
        ]
        took_ms = rt.elapsed_ms()
        logger.info(
            "search_done", hits=len(results), took_ms=took_ms, cache=call.result("retrieval")
        )
        response.headers.update(call.header("retrieval"))
        return SearchResponse(
            query=req.query,
            mode=options.mode,
            fusion=options.fusion if options.mode == "hybrid" else None,
            top_k=rt.top_k,
            neighbors=req.neighbors,
            returned=len(results),
            took_ms=took_ms,
            cache=call.result("retrieval"),
            results=results,
        )
    except Exception as exc:
        rt.fail(exc)
        raise
    finally:
        emb = rt.embedding
        if emb:
            rt.prompt, rt.completion, rt.cost = emb["tokens"], 0, embedding_cost(cfg, emb["tokens"])
        await rt.record(
            session,
            cfg,
            principal,
            kind="search",
            question=req.query,
            model=None,
            meta=rt.meta(
                neighbors=req.neighbors,
                document_ids=req.document_ids,
                layer="retrieval",
                lead={"include_text": req.include_text},
            ),
        )
