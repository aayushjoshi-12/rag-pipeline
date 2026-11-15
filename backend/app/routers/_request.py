"""Plumbing shared by ``POST /chat``, ``POST /chat/stream`` and ``POST /search``.

One ``RequestTrace`` per request: the resolved retrieval inputs, the cache call, the timer and the outcome
that ends up on the trace row. Handlers fill it in as they go and always record it exactly once.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.routers.auth_deps import Principal
from app.services import traces
from app.services.cache import CacheCall, get_cache, wants_bypass
from app.services.embeddings import Embedder, measure_embeddings
from app.services.llm import LLMError, LLMRequestError
from app.services.qa import Answer
from app.services.retriever import RetrievalOptions
from app.services.search import Retrieval, embedding_usage, retrieve_ranked
from app.services.vectorstore import VectorStore


@dataclass
class RequestTrace:
    top_k: int
    options: RetrievalOptions
    call: CacheCall
    started: float = field(default_factory=time.perf_counter)
    status: str = "error"
    error: str | None = None
    hits: int | None = None
    prompt: int | None = None
    completion: int | None = None
    cost: float | None = None
    #: ids only (never content): which documents the answer was grounded on
    cited: list[str] = field(default_factory=list)
    embedding: dict[str, Any] | None = None
    ttft_ms: int | None = None
    cancelled: bool = False

    @classmethod
    def start(
        cls,
        cfg: Settings,
        request: Request,
        *,
        top_k: int | None,
        mode: str | None,
        fusion: str | None,
        cache: bool | None,
    ) -> RequestTrace:
        return cls(
            top_k=top_k or cfg.default_top_k,
            options=RetrievalOptions.from_settings(cfg, mode=mode, fusion=fusion),
            call=CacheCall(
                get_cache(cfg), bypass=wants_bypass(cache, request.headers.get("cache-control"))
            ),
        )

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def fail(self, exc: Exception) -> None:
        self.error = (
            str(exc.detail) if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
        )

    def keep_usage(self, exc: Exception) -> None:
        """A failed LLM call may still have been billed: keep what the provider reported on the trace."""
        usage = exc.usage if isinstance(exc, LLMError) else None
        if usage is not None:
            self.prompt, self.completion, self.cost = (
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.cost_usd,
            )

    def succeed(self, result: Answer) -> None:
        self.status = "ok"
        self.prompt, self.completion, self.cost = (
            result.prompt_tokens,
            result.completion_tokens,
            result.cost_usd,
        )
        self.cited = list(dict.fromkeys(str(c.document_id) for c in result.sources if c.is_hit))

    def meta(
        self,
        *,
        neighbors: int,
        document_ids: Sequence[uuid.UUID] | None,
        layer: str,
        lead: dict[str, Any],
        mid: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The trace meta. Key order is part of the contract (``GET /traces`` returns it as stored)."""
        return {
            "top_k": self.top_k,
            "neighbors": neighbors,
            "document_filter": len(document_ids or []),
            **lead,
            **({"embedding": self.embedding} if self.embedding else {}),
            **(mid or {}),
            **({"cache": self.call.trace_meta(layer)} if self.call.active else {}),
            **self.options.trace_meta(),
        }

    async def record(
        self,
        session: AsyncSession,
        cfg: Settings,
        principal: Principal,
        *,
        kind: str,
        question: str,
        model: str | None,
        meta: dict[str, Any],
    ) -> uuid.UUID | None:
        return await traces.record(
            session,
            kind=kind,
            status=self.status,
            duration_ms=self.elapsed_ms(),
            user_email=None if principal.anonymous else principal.email,
            model=model,
            prompt_tokens=self.prompt,
            completion_tokens=self.completion,
            hits=self.hits,
            cost_usd=self.cost,
            error=self.error,
            question=question if cfg.trace_capture_content else None,
            meta=meta,
        )


async def retrieve_for_request(
    rt: RequestTrace,
    query: str,
    *,
    neighbors: int,
    document_ids: Sequence[uuid.UUID] | None,
    embedder: Embedder,
    store: VectorStore,
    session: AsyncSession,
    span_prefix: str,
) -> Retrieval:
    """Retrieve + rank for a request and note the hit count and query-embedding usage on its trace."""
    with measure_embeddings() as meter:
        retrieval = await retrieve_ranked(
            query,
            top_k=rt.top_k,
            neighbors=neighbors,
            options=rt.options,
            document_ids=document_ids,
            embedder=embedder,
            store=store,
            session=session,
            span_prefix=span_prefix,
            cache=rt.call,
        )
    rt.hits = len(retrieval.hits)
    rt.embedding = embedding_usage(meter, rt.call, rt.options.mode)
    return retrieval


def llm_http_error(exc: LLMError) -> HTTPException:
    """The HTTP error for a failed LLM call: 502 when the provider rejected it, else 503."""
    code = (
        status.HTTP_502_BAD_GATEWAY
        if isinstance(exc, LLMRequestError)
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return HTTPException(code, str(exc))
