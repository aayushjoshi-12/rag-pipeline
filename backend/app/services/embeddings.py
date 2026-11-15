from __future__ import annotations

import contextvars
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, cast, runtime_checkable

from app.config import Settings

_VALID_PROVIDERS = ("ollama", "openai")

_EMBED_TIMEOUT_SECONDS = 60.0

#: Output vector length of well-known embedding models, so the pgvector column can be created
#: without a network call. Unknown models: set EMBEDDING_DIMENSION or let the store probe once.
KNOWN_DIMENSIONS: dict[str, int] = {
    "mxbai-embed-large": 1024,
    "nomic-embed-text": 768,
    "all-minilm": 384,
    "bge-m3": 1024,
    "snowflake-arctic-embed": 1024,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


def configured_dimension(settings: Settings) -> int | None:
    """EMBEDDING_DIMENSION, else the known length of EMBEDDING_MODEL, else ``None`` (unknown)."""
    if settings.embedding_dimension is not None:
        return settings.embedding_dimension
    model = settings.embedding_model.split(":", 1)[0]  # "mxbai-embed-large:latest"
    return KNOWN_DIMENSIONS.get(model)


def probe_dimension(embedder: Embedder) -> int:
    """Embed one throw-away string to learn the model's vector length (a real provider call)."""
    return len(embedder.embed_query("dimension probe"))


class EmbeddingMeter:
    """Sums the token counts the embedding provider reports during one request/job.

    ``tokens`` stays ``None`` until a provider reports a count (fakes and providers without usage never do),
    which is how "unknown" is told apart from a real 0. Thread-safe: embedding runs in worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tokens: int | None = None

    def add(self, tokens: int | None) -> None:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            return
        with self._lock:
            self.tokens = (self.tokens or 0) + tokens


_meter: contextvars.ContextVar[EmbeddingMeter | None] = contextvars.ContextVar(
    "embedding_meter", default=None
)


@contextmanager
def measure_embeddings() -> Iterator[EmbeddingMeter]:
    """Collect the provider-reported usage of every embedding call made inside the block (also in
    ``asyncio.to_thread`` workers, which inherit the context). The ``Embedder`` protocol is unchanged."""
    meter = EmbeddingMeter()
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


def _report(tokens: object) -> None:
    meter = _meter.get()
    if meter is not None and isinstance(tokens, int):
        meter.add(tokens)


def embedding_cost(settings: Settings, tokens: int | None) -> float | None:
    """USD for ``tokens`` embedded: always 0.0 for local Ollama, priced by ``EMBEDDING_PRICE_PER_MTOK`` for a
    hosted provider, else ``None`` (unknown, never 0)."""
    if tokens is None:
        return None
    if settings.embedding_provider == "ollama":
        return 0.0
    if settings.embedding_price_per_mtok is None:
        return None
    return tokens * settings.embedding_price_per_mtok / 1_000_000


@runtime_checkable
class Embedder(Protocol):
    """Embedding provider interface — text in, vectors out."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents, one vector per input text."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string."""
        ...


class OllamaEmbedder:
    """Embeddings via the Ollama HTTP API (``POST /api/embed``)."""

    def __init__(self, model: str, base_url: str) -> None:
        self.model = model
        self.base_url = base_url

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        import httpx

        with httpx.Client(timeout=_EMBED_TIMEOUT_SECONDS) as client:
            resp = client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            body = resp.json()
            _report(body.get("prompt_eval_count"))
            return cast(list[list[float]], body["embeddings"])

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class OpenAIEmbedder:
    """Embeddings via the OpenAI SDK (``client.embeddings.create``)."""

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._api_key = api_key

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key)
        resp = client.embeddings.create(model=self.model, input=texts)
        _report(getattr(resp.usage, "total_tokens", None))
        return [item.embedding for item in resp.data]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def get_embedder(
    provider: str | None = None,
    model: str | None = None,
    settings: Settings | None = None,
) -> Embedder:
    """Return an ``Embedder`` for the configured provider.

    Parameters default to the values in ``Settings`` when omitted.
    """
    cfg = settings or Settings()  # fresh: picks up admin-UI changes (ticket 43)
    provider = provider or cfg.embedding_provider
    model = model or cfg.embedding_model

    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"Unknown embedding provider '{provider}'. "
            f"Supported: {', '.join(_VALID_PROVIDERS)}."
        )

    if provider == "ollama":
        return OllamaEmbedder(model=model, base_url=cfg.ollama_base_url)

    if provider == "openai":
        if not cfg.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required for the 'openai' embedding provider."
            )
        return OpenAIEmbedder(model=model, api_key=cfg.openai_api_key)

    # Unreachable — _VALID_PROVIDERS guard above.
    raise ValueError(f"Unhandled embedding provider '{provider}'.")  # pragma: no cover

