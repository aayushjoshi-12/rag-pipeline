from __future__ import annotations

import json
import logging
import os
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
    data_dir: Path = Path("data")
    max_upload_size_bytes: int = 25 * 1024 * 1024
    cleanup_orphan_age_hours: float = 24.0
    #: Optional guard for document updates: rebuild from scratch when (added + removed) chunks
    #: exceed this share of the old chunk count. None (default) = always update incrementally,
    #: which never embeds more than a rebuild.
    incremental_max_churn: float | None = None

    # ── Persistence ──────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"

    # ── Redis / Celery ───────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ── Vector store (Chroma) ────────────────────────────────────────────────
    vector_store: str = "chroma"
    chroma_host: str = "localhost"
    chroma_port: int = 8001

    # ── Embeddings (Ollama default) ──────────────────────────────────────────
    embedding_provider: str = "ollama"
    embedding_model: str = "mxbai-embed-large"
    ollama_base_url: str = "http://localhost:11434"

    # ── Chat/QA synthesis (OpenRouter default) ───────────────────────────────
    llm_provider: str = "openrouter"
    default_llm_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"

    # ── LLM provider keys (secrets) ──────────────────────────────────────────
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # ── Chunking ─────────────────────────────────────────────────────────────
    chunk_size: int = 512
    chunk_overlap: int = 64
    default_top_k: int = 5

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
        if self.vector_store not in ("chroma",):
            raise SettingsError(f"Unknown vector store '{self.vector_store}'.")
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
        if self.cleanup_orphan_age_hours < 0:
            raise SettingsError("cleanup_orphan_age_hours must be >= 0.")
        self._warn_langfuse()
        if not (256 <= self.parser_max_completion_tokens <= 32000):
            raise SettingsError(
                "parser_max_completion_tokens must be within 256..32000."
            )
        return self

    def _warn_langfuse(self) -> None:
        """Tracing is best-effort: a malformed Langfuse config warns, it never fails startup."""
        if not (self.langfuse_public_key or self.langfuse_secret_key):
            return
        log = logging.getLogger(__name__)
        if not (self.langfuse_public_key and self.langfuse_secret_key):
            log.warning("Langfuse is half-configured (need both public and secret key); tracing off.")
        if not self.langfuse_host.startswith(("http://", "https://")):
            log.warning("LANGFUSE_HOST %r is not an http(s) URL; tracing may fail.", self.langfuse_host)
        if self.langfuse_public_key and not self.langfuse_public_key.startswith("pk-"):
            log.warning("LANGFUSE_PUBLIC_KEY does not look like a Langfuse public key (pk-...).")
        if self.langfuse_secret_key and not self.langfuse_secret_key.startswith("sk-"):
            log.warning("LANGFUSE_SECRET_KEY does not look like a Langfuse secret key (sk-...).")
