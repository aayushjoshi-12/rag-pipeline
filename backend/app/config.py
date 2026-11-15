from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

logger = logging.getLogger(__name__)


class SettingsError(ValueError):
    """Raised when runtime configuration is invalid."""


_VALID_ENVIRONMENTS = ("dev", "prod")
#: Config JSON files resolve relative to the repo root, e.g. backend/config/config.dev.json
#: when running from backend/ (uv), and /app/config/config.prod.json inside the image.
_CONFIG_DIR = Path(os.environ.get("RAG_CONFIG_DIR", "config"))


def _selected_environment() -> str | None:
    """Resolve the config environment from RAG_ENV.

    Returns None when RAG_ENV is absent or not a valid config environment (e.g.
    RAG_ENV unset on the laptop defers to env vars + defaults).
    """
    rag_env = os.environ.get("RAG_ENV", "").strip()
    if not rag_env:
        return None
    return rag_env if rag_env in _VALID_ENVIRONMENTS else None


#: Upper bound for ``retrieval_neighbors`` (each hit expands to at most 2N+1 chunks).
MAX_NEIGHBORS = 5

VECTOR_STORES = ("chroma", "pgvector")
VECTOR_METRICS = ("cosine", "l2", "ip")
VECTOR_INDEXES = ("hnsw", "ivfflat", "none")

RETRIEVAL_MODES = ("semantic", "keyword", "hybrid")
KEYWORD_METHODS = ("bm25", "postgres_fts")
FUSION_METHODS = ("rrf", "weighted")

STORAGE_BACKENDS = ("local", "s3")
URL_SCHEMES = ("http", "https", "s3")

_PLACEHOLDER_KEYS: frozenset[str] = frozenset({"change-me", "replace-me", "changeme", "secret"})

#: Fields that are secrets and must NEVER appear in config/config.<env>.json.
SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "openrouter_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "database_url",
        "secret_key",
        "admin_password",
        "langfuse_public_key",
        "langfuse_secret_key",
        "s3_access_key_id",
        "s3_secret_access_key",
    }
)


def _load_environment_json(settings_cls: type[BaseSettings]) -> dict[str, Any]:
    """Load config/config.<env>.json, keyed by field name. {} when RAG_ENV is absent,
    maps to no config environment, or the file does not exist (graceful degradation).
    """
    env = _selected_environment()
    if env is None:
        return {}

    path = _CONFIG_DIR / f"config.{env}.json"
    if not path.is_file():
        return {}

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"Environment config file {path} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SettingsError(f"Environment config file {path} must contain a JSON object.")

    secret_upper = {name.upper() for name in SECRET_FIELD_NAMES}
    offending = sorted(key for key in parsed if key.upper() in secret_upper)
    if offending:
        raise SettingsError(
            f"Secret settings must not appear in {path}: {', '.join(offending)}. "
            "Secrets belong in environment variables / .env (dev) or K8s Secrets (prod)."
        )

    field_upper_to_name = {name.upper(): name for name in settings_cls.model_fields}
    values: dict[str, Any] = {}
    for key, value in parsed.items():
        field_name = field_upper_to_name.get(key.upper())
        if field_name is None:
            logger.warning("Ignoring unknown key %r in environment config file %s", key, path)
            continue
        values[field_name] = value
    return values


class EnvJsonSettingsSource(PydanticBaseSettingsSource):
    """pydantic-settings source for the per-environment JSON config file.

    Ranked lowest so env vars / .env always win. Effective precedence (highest first):
      init > env vars > .env > env JSON > defaults
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._values = _load_environment_json(settings_cls)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
    ) -> Any:
        return value

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            value = self._values.get(field_name)
            if value is not None:
                values[field_name] = value
        return values


def _env_files() -> str | tuple[str, ...]:
    """``.env`` locations, later entries overriding earlier ones.

    ``RAG_ENV_FILE`` wins outright. Otherwise the repo-root ``.env`` is read (so running from
    ``backend/`` works) and a ``.env`` in the current directory overrides it.
    """
    explicit = os.environ.get("RAG_ENV_FILE")
    if explicit:
        return explicit
    repo_root = Path(__file__).resolve().parents[2]
    return (str(repo_root / ".env"), ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            EnvJsonSettingsSource(settings_cls),
        )

    # ── Runtime / app ────────────────────────────────────────────────────────
    rag_env: str = "dev"
    log_level: str = "INFO"
    port: int = 8000
    secret_key: str = ""
    # ── Auth / RBAC ──────────────────────────────────────────────────────────
    auth_enabled: bool = True
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    #: First-admin bootstrap (only used when no users exist yet).
    admin_email: str = ""
    admin_password: str = ""
    #: Browser origins allowed to call the API cross-origin (comma separated, or ``*``). Empty (default)
    #: = no CORS headers at all: only same-origin and non-browser clients. ticket 36.
    cors_allow_origins: str = ""
    data_dir: Path = Path("data")
    max_upload_size_bytes: int = 25 * 1024 * 1024
    cleanup_orphan_age_hours: float = 24.0
    #: Optional guard for document updates: rebuild from scratch when (added + removed) chunks
    #: exceed this share of the old chunk count. None (default) = always update incrementally,
    #: which never embeds more than a rebuild.
    incremental_max_churn: float | None = None
    #: Store chat question text in the trace log (off by default: questions can be sensitive).
    trace_capture_content: bool = False
    #: Traces older than this many days are purged by the scheduled cleanup task.
    trace_retention_days: int = 30

    # ── File storage (local default; S3-compatible optional) + URL ingest (docs/configuration.md) ──
    storage_backend: str = "local"
    #: Custom endpoint for S3-compatible stores (Floci, MinIO, R2, Ceph, ...). Empty = AWS.
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    #: Key prefix under which this app keeps (and only ever deletes) its objects.
    s3_prefix: str = "rag-pipeline/"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    #: Which URL schemes POST /documents/from-url accepts (comma separated subset of http,https,s3).
    url_ingest_schemes: str = "http,https,s3"
    #: s3:// links may only read these buckets (comma separated). Empty = only S3_BUCKET.
    url_ingest_s3_buckets: str = ""
    #: Disable the private/loopback/link-local/metadata address block. Tests and smoke runs only.
    url_ingest_allow_private: bool = False
    #: Per-operation (connect / read) timeout and overall deadline for one download.
    url_ingest_timeout_seconds: float = 20.0
    url_ingest_total_timeout_seconds: float = 120.0
    url_ingest_max_redirects: int = 5
    #: Download size cap in bytes. None = MAX_UPLOAD_SIZE_BYTES.
    url_ingest_max_bytes: int | None = None

    # ── Persistence ──────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"

    # ── Redis / Celery ───────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ── Query cache (ticket 37; see docs/configuration.md "Query cache") ─────────────
    #: Master switch. The cache is fail-open: with Redis down requests are served uncached, never failed.
    cache_enabled: bool = True
    #: Dedicated Redis db (the Celery broker/results use 0/1); every key also carries the ``rag:cache:v1:`` prefix.
    cache_redis_url: str = "redis://localhost:6379/12"
    #: Per-layer lifetime in seconds; 0 switches that layer off.
    cache_embedding_ttl_seconds: int = 86400
    cache_retrieval_ttl_seconds: int = 600
    cache_answer_ttl_seconds: int = 3600
    #: Values larger than this are not stored (they would only evict useful entries).
    cache_max_value_bytes: int = 262144
    #: Per-operation Redis timeout; a slower Redis is treated as down.
    cache_timeout_ms: int = 100
    #: After a Redis failure the cache is skipped for this long (no per-request timeout penalty).
    cache_breaker_seconds: float = 5.0

    # ── Vector store (Chroma default, pgvector optional; see docs/configuration.md) ──
    vector_store: str = "chroma"
    chroma_host: str = "localhost"
    chroma_port: int = 8001
    #: Chroma collection holding the vectors (several deployments can share one Chroma server).
    chroma_collection: str = "documents"
    #: Distance metric. None = the store's own default: ``l2`` for Chroma (what an existing
    #: collection already uses), ``cosine`` for pgvector. Index-time (needs a reindex to change).
    vector_metric: str | None = None
    #: ANN index. None = ``hnsw`` (Chroma only ever has HNSW). pgvector also supports ivfflat/none.
    vector_index: str | None = None
    #: HNSW build/search knobs. None = the store default (pgvector 16/64/40; Chroma 16/100/100).
    hnsw_m: int | None = None
    hnsw_ef_construction: int | None = None
    hnsw_ef_search: int | None = None
    #: pgvector ivfflat knobs. None = 100 lists / 10 probes.
    ivfflat_lists: int | None = None
    ivfflat_probes: int | None = None
    #: Embedding vector length for pgvector's column. None = look up the model, else probe once.
    embedding_dimension: int | None = None

    # ── Embeddings (Ollama default) ──────────────────────────────────────────
    embedding_provider: str = "ollama"
    embedding_model: str = "mxbai-embed-large"
    ollama_base_url: str = "http://localhost:11434"

    # ── Chat/QA synthesis (OpenRouter default) ───────────────────────────────
    llm_provider: str = "openrouter"
    default_llm_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    #: Output-token budget of one answer (reasoning models spend it thinking, so too small = empty answer).
    llm_max_tokens: int = 2048
    # ── LLM cost (ticket 32) ─────────────────────────────────────────────────
    #: USD per million tokens, used when the provider does not report a cost itself (OpenAI,
    #: Anthropic; OpenRouter reports it). Both must be set to price a call; unset = cost unknown.
    llm_price_input_per_mtok: float | None = None
    llm_price_output_per_mtok: float | None = None
    #: USD per million embedded tokens for a hosted embedding provider (openai). Local ollama is always $0;
    #: unset on a hosted provider = embedding cost unknown.
    embedding_price_per_mtok: float | None = None

    # ── LLM provider keys (secrets) ──────────────────────────────────────────
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # ── Chunking ─────────────────────────────────────────────────────────────
    chunk_size: int = 1000
    chunk_overlap: int = 100
    default_top_k: int = 5
    #: Query-time: also give the LLM the N chunks before/after each hit (0 = hits only).
    retrieval_neighbors: int = 1
    #: Query-time retrieval strategy (per-request ``mode`` / ``fusion`` on POST /chat). See docs/configuration.md.
    retrieval_mode: str = "semantic"
    retrieval_keyword_method: str = "bm25"
    retrieval_fusion: str = "rrf"
    retrieval_rrf_k: int = 60
    #: Relative weights for ``weighted`` fusion; auto-normalised to sum 1 (only their ratio matters).
    retrieval_weight_semantic: float = 0.6
    retrieval_weight_keyword: float = 0.4
    #: Per-retriever pool size before fusion / the top_k cut (the pool is at least top_k).
    retrieval_candidates: int = 50
    #: Postgres FTS matches pulled in (ranked by ts_rank_cd) to be re-scored with BM25.
    retrieval_keyword_pool: int = 200
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    #: Postgres text-search configuration baked into chunks.fts (``english`` stems + drops stopwords,
    #: ``simple`` does neither). Index-time: change with ``python -m app.manage rebuild-fts``.
    retrieval_fts_config: str = "english"
    #: Drop chunks whose text is identical to a better-ranked one (duplicate uploads).
    retrieval_dedupe: bool = True

    # ── Document parsing (see docs/adr/0002-document-parsing.md) ─────────────
    parser_provider: str = "openrouter"
    parser_llm_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    parser_system_prompt: str = (
        "Extract all text from this document page verbatim, preserving layout as much as "
        "possible. Output plain text only — no commentary."
    )
    parser_timeout_seconds: float = 60.0
    parser_max_completion_tokens: int = 16032
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    pdf_dpi: int = 300
    #: Where PDF page text comes from: ``auto`` = the PDF's own text layer when a page has enough
    #: text, else OCR/vision for that page; ``native`` = text layer only; ``image`` = always OCR/vision.
    pdf_text_source: str = "auto"
    #: A page whose text layer is shorter than this (after stripping) is treated as scanned.
    pdf_native_min_chars: int = 40
    ocr_lang: str = "eng"
    tesseract_cmd: str | None = None
    docx_reader: str = "docling"
    #: Fold PDF ligatures (ﬁ -> fi), odd spaces, soft hyphens and curly quotes in extracted text before
    #: chunking (ticket 33, services/textnorm.py). Index-time: applies to documents ingested afterwards.
    parse_normalize_text: bool = True

    # ── Observability ────────────────────────────────────────────────────────
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://us.cloud.langfuse.com"
    langfuse_capture_content: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.llm_provider not in ("openrouter", "openai", "anthropic"):
            raise SettingsError(f"Unknown LLM provider '{self.llm_provider}'.")
        if self.embedding_provider not in ("ollama", "openai"):
            raise SettingsError(f"Unknown embedding provider '{self.embedding_provider}'.")
        self._validate_vector()
        if self.parser_provider not in ("openrouter", "openai", "anthropic", "ocr"):
            raise SettingsError(f"Unknown parser provider '{self.parser_provider}'.")
        if self.pdf_text_source not in ("auto", "native", "image"):
            raise SettingsError(f"Unknown pdf_text_source '{self.pdf_text_source}'.")
        if self.pdf_native_min_chars < 0:
            raise SettingsError("pdf_native_min_chars must be >= 0.")
        if self.docx_reader not in ("docling",):
            raise SettingsError(f"Unknown DOCX reader '{self.docx_reader}'.")
        if self.access_token_minutes <= 0 or self.refresh_token_days <= 0:
            raise SettingsError("token lifetimes must be positive.")
        weak_key = len(self.secret_key) < 32 or self.secret_key.lower() in _PLACEHOLDER_KEYS
        if self.auth_enabled and self.rag_env != "dev" and weak_key:
            raise SettingsError(
                "SECRET_KEY must be a real random value of at least 32 characters when "
                "AUTH_ENABLED=true outside dev (e.g. `python -c 'import secrets;"
                "print(secrets.token_urlsafe(48))'`)."
            )
        if self.incremental_max_churn is not None and self.incremental_max_churn < 0:
            raise SettingsError("incremental_max_churn must be >= 0 (or unset).")
        if self.chunk_size <= 0 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise SettingsError("chunk_size must be > 0 and 0 <= chunk_overlap < chunk_size.")
        if not 0 <= self.retrieval_neighbors <= MAX_NEIGHBORS:
            raise SettingsError(f"retrieval_neighbors must be within 0..{MAX_NEIGHBORS}.")
        self._validate_retrieval()
        self._validate_storage()
        self._validate_cache()
        self._validate_cors()
        for name in ("llm_price_input_per_mtok", "llm_price_output_per_mtok"):
            price = getattr(self, name)
            if price is not None and price < 0:
                raise SettingsError(f"{name} must be >= 0 (or unset).")
        if (self.llm_price_input_per_mtok is None) != (self.llm_price_output_per_mtok is None):
            raise SettingsError(
                "Set both llm_price_input_per_mtok and llm_price_output_per_mtok, or neither."
            )
        if not 1 <= self.llm_max_tokens <= 200_000:
            raise SettingsError("llm_max_tokens must be between 1 and 200000.")
        if self.embedding_price_per_mtok is not None and self.embedding_price_per_mtok < 0:
            raise SettingsError("embedding_price_per_mtok must be >= 0 (or unset).")
        if self.trace_retention_days < 1:
            raise SettingsError("trace_retention_days must be >= 1.")
        if self.cleanup_orphan_age_hours < 0:
            raise SettingsError("cleanup_orphan_age_hours must be >= 0.")
        self._warn_langfuse()
        if not (256 <= self.parser_max_completion_tokens <= 32000):
            raise SettingsError("parser_max_completion_tokens must be within 256..32000.")
        return self

    @property
    def cors_origins(self) -> tuple[str, ...]:
        return tuple(x.strip().rstrip("/") for x in self.cors_allow_origins.split(",") if x.strip())

    def _validate_cors(self) -> None:
        for origin in self.cors_origins:
            if origin != "*" and not re.fullmatch(r"https?://[^/\s]+", origin):
                raise SettingsError(
                    f"CORS_ALLOW_ORIGINS entry '{origin}' must be '*' or an origin like "
                    "https://app.example.com (scheme + host[:port], no path)."
                )

    @property
    def url_schemes(self) -> tuple[str, ...]:
        return tuple(x.strip().lower() for x in self.url_ingest_schemes.split(",") if x.strip())

    @property
    def url_s3_buckets(self) -> tuple[str, ...]:
        names = tuple(x.strip() for x in self.url_ingest_s3_buckets.split(",") if x.strip())
        return names or ((self.s3_bucket,) if self.s3_bucket else ())

    @property
    def url_max_bytes(self) -> int:
        return self.url_ingest_max_bytes or self.max_upload_size_bytes

    @property
    def s3_key_prefix(self) -> str:
        """``S3_PREFIX`` normalised: no leading slash, one trailing slash (or empty)."""
        prefix = self.s3_prefix.strip().lstrip("/")
        return prefix if not prefix or prefix.endswith("/") else prefix + "/"

    @property
    def s3_configured(self) -> bool:
        return bool(self.s3_bucket)

    def _validate_storage(self) -> None:
        if self.storage_backend not in STORAGE_BACKENDS:
            raise SettingsError(
                f"Unknown STORAGE_BACKEND '{self.storage_backend}'. Allowed: {', '.join(STORAGE_BACKENDS)}."
            )
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise SettingsError("STORAGE_BACKEND=s3 requires S3_BUCKET.")
        if bool(self.s3_access_key_id) != bool(self.s3_secret_access_key):
            raise SettingsError("S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set together.")
        if self.s3_endpoint_url and not self.s3_endpoint_url.startswith(("http://", "https://")):
            raise SettingsError("S3_ENDPOINT_URL must be an http(s) URL.")
        if ".." in self.s3_prefix.split("/"):
            raise SettingsError("S3_PREFIX must not contain '..'.")
        bad = [x for x in self.url_schemes if x not in URL_SCHEMES]
        if bad or not self.url_schemes:
            raise SettingsError(
                f"Unknown URL_INGEST_SCHEMES {bad or self.url_ingest_schemes!r}. "
                f"Allowed: {', '.join(URL_SCHEMES)} (comma separated, at least one)."
            )
        if not 0 < self.url_ingest_timeout_seconds <= 600:
            raise SettingsError("URL_INGEST_TIMEOUT_SECONDS must be within (0, 600].")
        if not self.url_ingest_timeout_seconds <= self.url_ingest_total_timeout_seconds <= 3600:
            raise SettingsError(
                "URL_INGEST_TOTAL_TIMEOUT_SECONDS must be >= URL_INGEST_TIMEOUT_SECONDS and <= 3600."
            )
        if not 0 <= self.url_ingest_max_redirects <= 20:
            raise SettingsError("URL_INGEST_MAX_REDIRECTS must be within 0..20.")
        if self.url_ingest_max_bytes is not None and self.url_ingest_max_bytes < 1:
            raise SettingsError("URL_INGEST_MAX_BYTES must be >= 1 (or unset).")

    def _validate_cache(self) -> None:
        if not self.cache_redis_url.startswith(("redis://", "rediss://", "unix://")):
            raise SettingsError("CACHE_REDIS_URL must start with redis://, rediss:// or unix://.")
        for name, value, low, high in (
            ("CACHE_EMBEDDING_TTL_SECONDS", self.cache_embedding_ttl_seconds, 0, 30 * 86400),
            ("CACHE_RETRIEVAL_TTL_SECONDS", self.cache_retrieval_ttl_seconds, 0, 30 * 86400),
            ("CACHE_ANSWER_TTL_SECONDS", self.cache_answer_ttl_seconds, 0, 30 * 86400),
            ("CACHE_MAX_VALUE_BYTES", self.cache_max_value_bytes, 1024, 16 * 1024 * 1024),
            ("CACHE_TIMEOUT_MS", self.cache_timeout_ms, 10, 5000),
        ):
            if not low <= value <= high:
                raise SettingsError(f"{name} must be within {low}..{high} (got {value}).")
        if not 0.5 <= self.cache_breaker_seconds <= 300:
            raise SettingsError(
                f"CACHE_BREAKER_SECONDS must be within 0.5..300 (got {self.cache_breaker_seconds})."
            )
        if self.cache_enabled and self.cache_redis_url in (
            self.celery_broker_url,
            self.celery_result_backend,
        ):
            raise SettingsError(
                "CACHE_REDIS_URL must not be the Celery broker/result Redis db (a cache purge must never touch "
                "Celery keys); use another db number, e.g. redis://localhost:6379/12."
            )

    def _validate_retrieval(self) -> None:
        for name, value, allowed in (
            ("RETRIEVAL_MODE", self.retrieval_mode, RETRIEVAL_MODES),
            ("RETRIEVAL_KEYWORD_METHOD", self.retrieval_keyword_method, KEYWORD_METHODS),
            ("RETRIEVAL_FUSION", self.retrieval_fusion, FUSION_METHODS),
        ):
            if value not in allowed:
                raise SettingsError(f"Unknown {name} '{value}'. Allowed: {', '.join(allowed)}.")
        int_ranges = {
            "retrieval_rrf_k": (self.retrieval_rrf_k, 1, 1000),
            "retrieval_candidates": (self.retrieval_candidates, 1, 500),
            "retrieval_keyword_pool": (self.retrieval_keyword_pool, 1, 5000),
        }
        for key, (number, low, high) in int_ranges.items():
            if not low <= number <= high:
                raise SettingsError(f"{key} must be within {low}..{high} (got {number}).")
        floats = (
            self.bm25_k1,
            self.bm25_b,
            self.retrieval_weight_semantic,
            self.retrieval_weight_keyword,
        )
        if not all(math.isfinite(f) for f in floats):
            raise SettingsError(
                "BM25_K1, BM25_B and the RETRIEVAL_WEIGHT_* values must be finite numbers."
            )
        if not 0 <= self.bm25_k1 <= 10:
            raise SettingsError(f"bm25_k1 must be within 0..10 (got {self.bm25_k1}).")
        if not 0 <= self.bm25_b <= 1:
            raise SettingsError(f"bm25_b must be within 0..1 (got {self.bm25_b}).")
        weights = (self.retrieval_weight_semantic, self.retrieval_weight_keyword)
        if any(w < 0 for w in weights) or sum(weights) <= 0:
            raise SettingsError(
                "retrieval_weight_semantic / retrieval_weight_keyword must be >= 0 and not both 0 "
                "(they are normalised to sum to 1)."
            )
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.retrieval_fts_config):
            raise SettingsError(
                f"retrieval_fts_config '{self.retrieval_fts_config}' must be a lowercase Postgres "
                "text-search configuration name such as 'english' or 'simple'."
            )

    def _validate_vector(self) -> None:
        if self.vector_store not in VECTOR_STORES:
            raise SettingsError(
                f"Unknown vector store '{self.vector_store}'. Allowed: {', '.join(VECTOR_STORES)}."
            )
        if self.vector_metric is not None and self.vector_metric not in VECTOR_METRICS:
            raise SettingsError(
                f"Unknown VECTOR_METRIC '{self.vector_metric}'. Allowed: {', '.join(VECTOR_METRICS)}."
            )
        if self.vector_index is not None and self.vector_index not in VECTOR_INDEXES:
            raise SettingsError(
                f"Unknown VECTOR_INDEX '{self.vector_index}'. Allowed: {', '.join(VECTOR_INDEXES)}."
            )
        if self.vector_store == "chroma" and self.vector_index not in (None, "hnsw"):
            raise SettingsError(
                f"VECTOR_INDEX={self.vector_index} is only available with VECTOR_STORE=pgvector "
                "(Chroma always builds an HNSW index)."
            )
        ranges = {
            "hnsw_m": (self.hnsw_m, 2, 100),
            "hnsw_ef_construction": (self.hnsw_ef_construction, 4, 1000),
            "hnsw_ef_search": (self.hnsw_ef_search, 1, 1000),
            "ivfflat_lists": (self.ivfflat_lists, 1, 32768),
            "ivfflat_probes": (self.ivfflat_probes, 1, 32768),
            "embedding_dimension": (self.embedding_dimension, 1, 16000),
        }
        for name, (value, low, high) in ranges.items():
            if value is not None and not low <= value <= high:
                raise SettingsError(f"{name} must be within {low}..{high} (got {value}).")
        if (
            self.hnsw_m is not None
            and self.hnsw_ef_construction is not None
            and self.hnsw_ef_construction < 2 * self.hnsw_m
        ):
            raise SettingsError("hnsw_ef_construction must be at least 2 * hnsw_m.")
        if (
            self.ivfflat_lists is not None
            and self.ivfflat_probes is not None
            and self.ivfflat_probes > self.ivfflat_lists
        ):
            raise SettingsError("ivfflat_probes must not exceed ivfflat_lists.")

    def _warn_langfuse(self) -> None:
        """Tracing is best-effort: a malformed Langfuse config warns, it never fails startup."""
        if not (self.langfuse_public_key or self.langfuse_secret_key):
            return
        log = logging.getLogger(__name__)
        if not (self.langfuse_public_key and self.langfuse_secret_key):
            log.warning(
                "Langfuse is half-configured (need both public and secret key); tracing off."
            )
        if not self.langfuse_host.startswith(("http://", "https://")):
            log.warning(
                "LANGFUSE_HOST %r is not an http(s) URL; tracing may fail.", self.langfuse_host
            )
        if self.langfuse_public_key and not self.langfuse_public_key.startswith("pk-"):
            log.warning("LANGFUSE_PUBLIC_KEY does not look like a Langfuse public key (pk-...).")
        if self.langfuse_secret_key and not self.langfuse_secret_key.startswith("sk-"):
            log.warning("LANGFUSE_SECRET_KEY does not look like a Langfuse secret key (sk-...).")
