"""The settings registry (ticket 43, ADR-0008): how every ``Settings`` field behaves in the admin UI.

Type, default and nullability come from the pydantic field itself and validation is the real ``Settings``
validation (field types + model validators), so there is exactly one set of rules. This module adds what the
model cannot say: the UI group, a description, and ``apply`` -- *when* a change takes effect:

* ``live``      -- the next request / next Celery task uses it (no restart).
* ``restart``   -- stored now, used after the process restarts (captured at startup).
* ``reindex``   -- changes what the vector index must look like; needs a confirmed reindex.
* ``bootstrap`` -- never editable from the UI (needed before the database is reachable, or too dangerous
                   to change from a web page); shown read-only with the reason.

A test fails when a ``Settings`` field is missing here, so a new setting cannot silently skip classification.
"""

from __future__ import annotations

import re
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import TypeAdapter

from app.config import (
    FUSION_METHODS,
    KEYWORD_METHODS,
    RETRIEVAL_MODES,
    STORAGE_BACKENDS,
    VECTOR_INDEXES,
    VECTOR_METRICS,
    VECTOR_STORES,
    Settings,
)

Apply = Literal["live", "restart", "reindex", "bootstrap"]
Kind = Literal["string", "text", "integer", "number", "boolean", "enum", "path"]
ReindexAction = Literal["reindex", "rebuild_fts"]


@dataclass(frozen=True)
class Spec:
    group: str
    description: str
    apply: Apply = "live"
    secret: bool = False
    choices: tuple[str, ...] | None = None
    unit: str | None = None
    note: str | None = None
    #: Why a ``bootstrap`` setting is not editable here.
    reason: str | None = None
    multiline: bool = False
    reindex_action: ReindexAction = "reindex"


GROUPS: list[tuple[str, str]] = [
    ("Retrieval", "How passages are found and ranked for a question."),
    ("LLM", "The model that writes answers, its limits and cost."),
    ("Embeddings", "How text becomes vectors."),
    ("Vector store", "Where vectors live and how the index is built."),
    ("Ingestion", "Chunking of newly ingested documents."),
    ("Parsing", "Turning uploads into text (OCR / vision / native)."),
    ("Cache", "The Redis query cache."),
    ("Storage", "Where uploaded files are kept; URL ingestion."),
    ("Auth & CORS", "Sessions and browser access."),
    ("Observability", "Tracing and retention."),
    ("Limits", "Upload sizes and housekeeping."),
    ("System", "Deployment-level values, read-only here."),
]

_DB = "Needed to reach the database or broker before any setting can be read from it."
_LOCKOUT = "Changing it from a web page could lock every user out or re-key sessions."


def _s(group: str, description: str, **kw: Any) -> Spec:
    return Spec(group, description, **kw)


REGISTRY: dict[str, Spec] = {
    # ── Retrieval ───────────────────────────────────────────────────────────────
    "default_top_k": _s(
        "Retrieval", "Passages returned per question when the request does not say."
    ),
    "retrieval_neighbors": _s(
        "Retrieval", "Also give the model the N chunks before and after each hit (0 = hits only)."
    ),
    "retrieval_mode": _s(
        "Retrieval",
        "semantic (vectors), keyword, or hybrid (both, fused).",
        choices=RETRIEVAL_MODES,
    ),
    "retrieval_keyword_method": _s(
        "Retrieval",
        "Keyword scorer: in-process BM25 or Postgres full text.",
        choices=KEYWORD_METHODS,
    ),
    "retrieval_fusion": _s("Retrieval", "How hybrid results are merged.", choices=FUSION_METHODS),
    "retrieval_rrf_k": _s("Retrieval", "Reciprocal-rank-fusion constant."),
    "retrieval_weight_semantic": _s(
        "Retrieval", "Weight of semantic rank for weighted fusion (normalised)."
    ),
    "retrieval_weight_keyword": _s(
        "Retrieval", "Weight of keyword rank for weighted fusion (normalised)."
    ),
    "retrieval_candidates": _s("Retrieval", "Per-retriever pool size before fusion."),
    "retrieval_keyword_pool": _s(
        "Retrieval", "Postgres FTS matches pulled in to be re-scored with BM25."
    ),
    "bm25_k1": _s("Retrieval", "BM25 term-frequency saturation."),
    "bm25_b": _s("Retrieval", "BM25 length normalisation."),
    "retrieval_dedupe": _s(
        "Retrieval", "Drop chunks whose text is identical to a better-ranked one."
    ),
    "retrieval_fts_config": _s(
        "Retrieval",
        "Postgres text-search configuration baked into the keyword index (english stems, simple does not).",
        apply="reindex",
        reindex_action="rebuild_fts",
        note="Rebuilds the keyword index of every chunk. Keyword and hybrid search return an error until it finishes.",
    ),
    # ── LLM ─────────────────────────────────────────────────────────────────────
    "llm_provider": _s("LLM", "Chat provider.", choices=("openrouter", "openai", "anthropic")),
    "default_llm_model": _s("LLM", "Model that writes answers."),
    "llm_max_tokens": _s(
        "LLM",
        "Output-token budget of one answer (reasoning models spend it thinking).",
        unit="tokens",
    ),
    "llm_price_input_per_mtok": _s(
        "LLM",
        "USD per million input tokens, when the provider does not report cost.",
        unit="USD / Mtok",
    ),
    "llm_price_output_per_mtok": _s(
        "LLM",
        "USD per million output tokens, when the provider does not report cost.",
        unit="USD / Mtok",
    ),
    "openrouter_base_url": _s(
        "LLM", "OpenRouter-compatible endpoint. Whoever controls it receives the API key."
    ),
    "openrouter_api_key": _s("LLM", "OpenRouter API key.", secret=True),
    "openai_api_key": _s("LLM", "OpenAI API key.", secret=True),
    "anthropic_api_key": _s("LLM", "Anthropic API key.", secret=True),
    # ── Embeddings ──────────────────────────────────────────────────────────────
    "embedding_provider": _s(
        "Embeddings",
        "Embedding provider.",
        apply="reindex",
        choices=("ollama", "openai"),
        note="Vectors of a different model are not comparable: every stored chunk is re-embedded.",
    ),
    "embedding_model": _s(
        "Embeddings",
        "Embedding model.",
        apply="reindex",
        note="Vectors of a different model are not comparable: every stored chunk is re-embedded.",
    ),
    "ollama_base_url": _s("Embeddings", "Ollama server URL."),
    "embedding_price_per_mtok": _s(
        "Embeddings", "USD per million embedded tokens (hosted providers).", unit="USD / Mtok"
    ),
    # ── Vector store ────────────────────────────────────────────────────────────
    "vector_store": _s(
        "Vector store",
        "Vector store backend.",
        apply="reindex",
        choices=VECTOR_STORES,
        note="The new store starts empty and is filled from the chunks in Postgres.",
    ),
    "chroma_host": _s(
        "Vector store",
        "Chroma server host.",
        apply="reindex",
        note="Points at a different Chroma server: reindex into it.",
    ),
    "chroma_port": _s(
        "Vector store",
        "Chroma server port.",
        apply="reindex",
        note="Points at a different Chroma server: reindex into it.",
    ),
    "chroma_collection": _s(
        "Vector store",
        "Chroma collection name.",
        apply="reindex",
        note="A new collection starts empty: reindex fills it.",
    ),
    "vector_metric": _s(
        "Vector store",
        "Distance metric (empty = store default).",
        apply="reindex",
        choices=VECTOR_METRICS,
        note="The metric is built into the index: it is recreated from the stored chunks (they are re-embedded).",
    ),
    "vector_index": _s(
        "Vector store",
        "ANN index type (empty = hnsw).",
        apply="reindex",
        choices=VECTOR_INDEXES,
        note="The index is rebuilt from the stored chunks (they are re-embedded).",
    ),
    "hnsw_m": _s(
        "Vector store",
        "HNSW graph degree (index build). Higher = better recall, more memory and slower builds.",
        apply="reindex",
        note="Index build parameter: the index is rebuilt from the stored chunks.",
    ),
    "hnsw_ef_construction": _s(
        "Vector store",
        "HNSW build-time candidate list size. Higher = better graph, slower builds.",
        apply="reindex",
        note="Index build parameter: the index is rebuilt from the stored chunks.",
    ),
    "hnsw_ef_search": _s(
        "Vector store",
        "HNSW query-time candidate list size.",
        note="Query-time knob: applies to new queries on pgvector; Chroma applies it on the next collection open.",
    ),
    "ivfflat_lists": _s(
        "Vector store",
        "IVFFlat list count (index build).",
        apply="reindex",
        note="Index build parameter: the index is rebuilt from the stored chunks.",
    ),
    "ivfflat_probes": _s("Vector store", "IVFFlat probes per query (query time)."),
    "embedding_dimension": _s(
        "Vector store",
        "Vector length for pgvector (empty = detect).",
        apply="reindex",
        note="The vector column is recreated: every chunk is re-embedded.",
    ),
    # ── Ingestion ───────────────────────────────────────────────────────────────
    "chunk_size": _s(
        "Ingestion",
        "Characters per chunk.",
        unit="chars",
        note="Applies to documents ingested afterwards; already stored chunks keep their size (re-upload to re-chunk).",
    ),
    "chunk_overlap": _s(
        "Ingestion",
        "Characters shared by neighbouring chunks.",
        unit="chars",
        note="Applies to documents ingested afterwards; already stored chunks keep their size (re-upload to re-chunk).",
    ),
    "incremental_max_churn": _s(
        "Ingestion",
        "Rebuild a document from scratch when more than this share of its chunks changed (empty = always update incrementally).",
    ),
    "parse_normalize_text": _s(
        "Ingestion",
        "Fold ligatures, odd spaces and curly quotes in extracted text.",
        note="Applies to documents ingested afterwards.",
    ),
    # ── Parsing ─────────────────────────────────────────────────────────────────
    "parser_provider": _s(
        "Parsing",
        "Text extraction backend for PDFs.",
        choices=("openrouter", "openai", "anthropic", "ocr"),
    ),
    "parser_llm_model": _s("Parsing", "Vision model used by the parser."),
    "parser_system_prompt": _s(
        "Parsing", "Instruction given to the vision model for every page.", multiline=True
    ),
    "parser_timeout_seconds": _s("Parsing", "Timeout of one parser call.", unit="s"),
    "parser_max_completion_tokens": _s(
        "Parsing", "Output-token budget of one parsed page.", unit="tokens"
    ),
    "pdf_dpi": _s("Parsing", "Render resolution for scanned pages.", unit="dpi"),
    "pdf_text_source": _s(
        "Parsing",
        "auto = text layer when present, else OCR; native; image.",
        choices=("auto", "native", "image"),
    ),
    "pdf_native_min_chars": _s(
        "Parsing", "A page with a shorter text layer counts as scanned.", unit="chars"
    ),
    "ocr_lang": _s("Parsing", "Tesseract language(s)."),
    "docx_reader": _s("Parsing", "DOCX reader.", choices=("docling",)),
    "tesseract_cmd": _s(
        "Parsing",
        "Path of the tesseract executable.",
        apply="bootstrap",
        reason="It names a program the server runs: not changeable from a web page.",
    ),
    # ── Cache ───────────────────────────────────────────────────────────────────
    "cache_enabled": _s("Cache", "Master switch of the query cache (fail-open)."),
    "cache_redis_url": _s(
        "Cache",
        "Redis URL of the cache (may contain a password).",
        secret=True,
        note="Must not be the Celery broker/result database.",
    ),
    "cache_embedding_ttl_seconds": _s("Cache", "Embedding cache lifetime (0 = off).", unit="s"),
    "cache_retrieval_ttl_seconds": _s("Cache", "Retrieval cache lifetime (0 = off).", unit="s"),
    "cache_answer_ttl_seconds": _s("Cache", "Answer cache lifetime (0 = off).", unit="s"),
    "cache_max_value_bytes": _s("Cache", "Largest value stored.", unit="bytes"),
    "cache_timeout_ms": _s("Cache", "Per-operation Redis timeout.", unit="ms"),
    "cache_breaker_seconds": _s(
        "Cache", "How long the cache is skipped after a Redis failure.", unit="s"
    ),
    # ── Storage ─────────────────────────────────────────────────────────────────
    "storage_backend": _s(
        "Storage",
        "Where new uploads are kept.",
        choices=STORAGE_BACKENDS,
        note="Existing documents stay where they were uploaded.",
    ),
    "s3_endpoint_url": _s(
        "Storage", "S3-compatible endpoint (empty = AWS). Whoever controls it receives the S3 keys."
    ),
    "s3_bucket": _s("Storage", "S3 bucket."),
    "s3_region": _s("Storage", "S3 region."),
    "s3_prefix": _s("Storage", "Key prefix under which this app keeps (and deletes) its objects."),
    "s3_access_key_id": _s("Storage", "S3 access key id.", secret=True),
    "s3_secret_access_key": _s("Storage", "S3 secret access key.", secret=True),
    "url_ingest_schemes": _s(
        "Storage", "URL schemes POST /documents/from-url accepts (comma separated)."
    ),
    "url_ingest_s3_buckets": _s(
        "Storage", "s3:// links may read only these buckets (comma separated; empty = S3_BUCKET)."
    ),
    "url_ingest_timeout_seconds": _s("Storage", "Connect/read timeout of one download.", unit="s"),
    "url_ingest_total_timeout_seconds": _s(
        "Storage", "Overall deadline of one download.", unit="s"
    ),
    "url_ingest_max_redirects": _s("Storage", "Redirects followed per download."),
    "url_ingest_max_bytes": _s(
        "Storage", "Download size cap (empty = the upload limit).", unit="bytes"
    ),
    "url_ingest_allow_private": _s(
        "Storage",
        "Allow downloads from private/loopback addresses.",
        apply="bootstrap",
        reason="It disables the SSRF protection: tests and smoke runs only, set it in the environment.",
    ),
    # ── Auth & CORS ─────────────────────────────────────────────────────────────
    "access_token_minutes": _s(
        "Auth & CORS",
        "Access-token lifetime.",
        unit="min",
        note="Applies to tokens issued afterwards.",
    ),
    "refresh_token_days": _s(
        "Auth & CORS",
        "Refresh-token lifetime.",
        unit="days",
        note="Applies to tokens issued afterwards.",
    ),
    "cors_allow_origins": _s(
        "Auth & CORS",
        "Browser origins allowed to call the API (comma separated, or *). Empty = none.",
        apply="restart",
    ),
    "auth_enabled": _s("Auth & CORS", "Require sign-in.", apply="bootstrap", reason=_LOCKOUT),
    "admin_email": _s(
        "Auth & CORS",
        "Bootstrap admin e-mail (first start only).",
        apply="bootstrap",
        reason="Used once at first start.",
    ),
    "admin_password": _s(
        "Auth & CORS",
        "Bootstrap admin password (first start only).",
        apply="bootstrap",
        secret=True,
        reason="Used once at first start.",
    ),
    # ── Observability ───────────────────────────────────────────────────────────
    "trace_capture_content": _s(
        "Observability", "Store question text in the trace log (questions can be sensitive)."
    ),
    "trace_retention_days": _s(
        "Observability", "Traces older than this are purged by the scheduled cleanup.", unit="days"
    ),
    "langfuse_public_key": _s(
        "Observability", "Langfuse public key.", apply="restart", secret=True
    ),
    "langfuse_secret_key": _s(
        "Observability", "Langfuse secret key.", apply="restart", secret=True
    ),
    "langfuse_host": _s("Observability", "Langfuse host.", apply="restart"),
    "langfuse_capture_content": _s(
        "Observability", "Attach prompts/pages/answers to Langfuse traces.", apply="restart"
    ),
    "log_level": _s(
        "Observability",
        "Log level.",
        apply="restart",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
    # ── Limits ──────────────────────────────────────────────────────────────────
    "max_upload_size_bytes": _s("Limits", "Largest accepted upload.", unit="bytes"),
    "cleanup_orphan_age_hours": _s(
        "Limits", "Orphaned upload files older than this are swept.", unit="h"
    ),
    # ── System (bootstrap, read-only) ───────────────────────────────────────────
    "rag_env": _s(
        "System",
        "Config environment.",
        apply="bootstrap",
        reason="Selects which config file is read, before anything else.",
    ),
    "port": _s("System", "API listen port.", apply="bootstrap", reason="Bound at process start."),
    "data_dir": _s(
        "System",
        "Directory of local uploads.",
        apply="bootstrap",
        reason="A volume mount: moving it orphans every stored file.",
    ),
    "database_url": _s(
        "System", "Postgres connection URL.", apply="bootstrap", secret=True, reason=_DB
    ),
    "redis_url": _s("System", "Redis URL.", apply="bootstrap", secret=True, reason=_DB),
    "celery_broker_url": _s(
        "System", "Celery broker URL.", apply="bootstrap", secret=True, reason=_DB
    ),
    "celery_result_backend": _s(
        "System", "Celery result backend URL.", apply="bootstrap", secret=True, reason=_DB
    ),
    "secret_key": _s(
        "System", "Token signing key.", apply="bootstrap", secret=True, reason=_LOCKOUT
    ),
    "settings_encryption_key": _s(
        "System",
        "Key that encrypts secrets stored from this page.",
        apply="bootstrap",
        secret=True,
        reason="It must live outside the database it protects.",
    ),
    "settings_encryption_key_previous": _s(
        "System",
        "Previous encryption key(s) kept for rotation.",
        apply="bootstrap",
        secret=True,
        reason="It must live outside the database it protects.",
    ),
    "settings_refresh_seconds": _s(
        "System",
        "How often each process re-reads these settings.",
        apply="bootstrap",
        unit="s",
        reason="Read at process start.",
    ),
}

#: Setting keys whose value must be re-embedded/reindexed when changed.
REINDEX_KEYS: frozenset[str] = frozenset(k for k, v in REGISTRY.items() if v.apply == "reindex")
EDITABLE_KEYS: frozenset[str] = frozenset(k for k, v in REGISTRY.items() if v.apply != "bootstrap")


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    """``X | None`` -> (X, True)."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], len(args) != len(get_args(annotation))
    return annotation, False


def field_kind(key: str) -> tuple[Kind, bool]:
    """The UI kind and nullability of a setting, from its pydantic field and its registry entry."""
    base, nullable = _unwrap(Settings.model_fields[key].annotation)
    spec = REGISTRY[key]
    if base is bool:
        return "boolean", nullable
    if base is int:
        return "integer", nullable
    if base is float:
        return "number", nullable
    if base is Path:
        return "path", nullable
    if spec.choices:
        return "enum", nullable
    return ("text" if spec.multiline else "string"), nullable


def default_of(key: str) -> Any:
    default = Settings.model_fields[key].default
    return str(default) if isinstance(default, Path) else default


class ValueError_(ValueError):
    """A submitted value is not valid for its setting."""


_URL_CREDENTIALS = re.compile(r"://[^/\s@]*:[^/\s@]*@")


def scrub(value: Any) -> Any:
    """Strip ``user:password@`` from URL-ish strings before they are displayed or audited."""
    if isinstance(value, str):
        return _URL_CREDENTIALS.sub("://***@", value)
    return value


def coerce(key: str, raw: Any) -> Any:
    """Validate ``raw`` against the field's type and return its JSON-safe form. Raises ``ValueError_``."""
    spec = REGISTRY[key]
    kind, nullable = field_kind(key)
    if raw is None:
        if nullable:
            return None
        raise ValueError_("a value is required")
    if kind in ("integer", "number") and isinstance(raw, bool):
        raise ValueError_("must be a number")
    if kind == "boolean" and not isinstance(raw, bool):
        raise ValueError_("must be true or false")
    if kind in ("string", "text", "enum", "path") and not isinstance(raw, str):
        raise ValueError_("must be text")
    try:
        value: Any = TypeAdapter(Settings.model_fields[key].annotation).validate_python(raw)
    except Exception as exc:
        raise ValueError_("is not a valid value") from exc
    if spec.choices and value not in spec.choices and not (nullable and value is None):
        raise ValueError_(f"must be one of: {', '.join(spec.choices)}")
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError_("must not contain NUL characters")
        if len(value) > (8000 if spec.multiline else 2000):
            raise ValueError_("is too long")
        if not spec.secret and _URL_CREDENTIALS.search(value):
            raise ValueError_(
                "must not embed credentials in a URL; store credentials in a secret setting"
            )
    return value


def coerce_secret(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError_("a non-empty value is required (use reset to clear the secret)")
    value = raw.strip()
    if len(value) > 4096 or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError_("is not a valid secret")
    return value
