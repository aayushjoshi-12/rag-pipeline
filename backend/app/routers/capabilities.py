"""``GET /v1/capabilities``: what this deployment supports, so a client can adapt without guessing."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.config import (
    FUSION_METHODS,
    KEYWORD_METHODS,
    RETRIEVAL_MODES,
    URL_SCHEMES,
    Settings,
)
from app.routers.auth_deps import get_principal
from app.routers.deps import get_settings
from app.services.filenames import ALLOWED_EXTENSIONS
from app.services.vectorstore import resolve_vector_settings
from app.version import API_PREFIX, __version__

router = APIRouter()


class RetrievalCaps(BaseModel):
    modes: list[str] = Field(examples=[["semantic", "keyword", "hybrid"]])
    default_mode: str
    fusions: list[str] = Field(description="Hybrid fusion methods.")
    default_fusion: str
    keyword_methods: list[str]
    default_keyword_method: str
    default_top_k: int
    max_top_k: int = Field(description="Largest `top_k` accepted by `/search` and `/chat`.")
    max_neighbors: int


class VectorCaps(BaseModel):
    store: str = Field(examples=["chroma"])
    metric: str = Field(examples=["l2"])
    index: str


class UploadCaps(BaseModel):
    max_bytes: int
    extensions: list[str] = Field(examples=[["docx", "md", "pdf", "txt"]])
    content_types: dict[str, str]


class PaginationCaps(BaseModel):
    default_limit: int
    max_limit: int


class FeatureCaps(BaseModel):
    url_ingest: bool = Field(description="`POST /v1/documents/from-url` is available.")
    url_ingest_schemes: list[str]
    storage_backend: str = Field(
        description="Where uploaded source files are kept: `local` or `s3`."
    )
    query_cache: bool
    api_keys: bool = Field(description="API keys can be created (needs auth enabled).")
    cors_enabled: bool


class CapabilitiesOut(BaseModel):
    """No secrets: only switches and limits."""

    version: str = Field(examples=["0.1.0"])
    api_versions: list[str] = Field(examples=[["v1"]])
    auth_enabled: bool
    retrieval: RetrievalCaps
    vector_store: VectorCaps
    embedding: dict[str, str] = Field(description="`provider` and `model` of the embedder.")
    uploads: UploadCaps
    pagination: PaginationCaps
    features: FeatureCaps


def build_capabilities(cfg: Settings) -> CapabilitiesOut:
    vec = resolve_vector_settings(cfg)
    return CapabilitiesOut(
        version=__version__,
        api_versions=[API_PREFIX.lstrip("/")],
        auth_enabled=cfg.auth_enabled,
        retrieval=RetrievalCaps(
            modes=list(RETRIEVAL_MODES),
            default_mode=cfg.retrieval_mode,
            fusions=list(FUSION_METHODS),
            default_fusion=cfg.retrieval_fusion,
            keyword_methods=list(KEYWORD_METHODS),
            default_keyword_method=cfg.retrieval_keyword_method,
            default_top_k=cfg.default_top_k,
            max_top_k=50,
            max_neighbors=5,
        ),
        vector_store=VectorCaps(store=vec.store, metric=vec.metric, index=vec.index),
        embedding={"provider": cfg.embedding_provider, "model": cfg.embedding_model},
        uploads=UploadCaps(
            max_bytes=cfg.max_upload_size_bytes,
            extensions=sorted(ALLOWED_EXTENSIONS),
            content_types=dict(sorted(ALLOWED_EXTENSIONS.items())),
        ),
        pagination=PaginationCaps(default_limit=50, max_limit=500),
        features=FeatureCaps(
            url_ingest=bool(cfg.url_schemes),
            url_ingest_schemes=[s for s in cfg.url_schemes if s in URL_SCHEMES],
            storage_backend=cfg.storage_backend,
            query_cache=cfg.cache_enabled,
            api_keys=cfg.auth_enabled,
            cors_enabled=bool(cfg.cors_origins),
        ),
    )


@router.get(
    "/capabilities",
    response_model=CapabilitiesOut,
    summary="Server capabilities and limits",
    dependencies=[Depends(get_principal)],
)
async def capabilities(cfg: Annotated[Settings, Depends(get_settings)]) -> CapabilitiesOut:
    """What this deployment supports: retrieval modes/fusions/keyword methods, the vector store and its
    metric, upload limits and file types, pagination bounds and feature switches.

    Requires authentication (any valid user token or API key; no particular permission). Contains no
    secrets, hostnames or credentials. Intended for clients to adapt (e.g. hide `hybrid` when unsupported)
    instead of hard-coding assumptions.
    """
    return build_capabilities(cfg)
