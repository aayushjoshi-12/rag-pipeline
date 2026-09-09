from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

from app.config import Settings

_settings = Settings()

_VALID_PROVIDERS = ("ollama", "openai")

_EMBED_TIMEOUT_SECONDS = 60.0


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
            return cast(list[list[float]], resp.json()["embeddings"])

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
        return [item.embedding for item in resp.data]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def get_embedder(
    provider: str | None = None,
    model: str | None = None,
) -> Embedder:
    """Return an ``Embedder`` for the configured provider.

    Parameters default to the values in ``Settings`` when omitted.
    """
    provider = provider or _settings.embedding_provider
    model = model or _settings.embedding_model

    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"Unknown embedding provider '{provider}'. "
            f"Supported: {', '.join(_VALID_PROVIDERS)}."
        )

    if provider == "ollama":
        return OllamaEmbedder(model=model, base_url=_settings.ollama_base_url)

    if provider == "openai":
        if not _settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required for the 'openai' embedding provider."
            )
        return OpenAIEmbedder(model=model, api_key=_settings.openai_api_key)

    # Unreachable — _VALID_PROVIDERS guard above.
    raise ValueError(f"Unhandled embedding provider '{provider}'.")  # pragma: no cover


def embed_texts(
    texts: list[str],
    embedder: Embedder | None = None,
) -> list[list[float]]:
    """Embed a list of texts, returning one vector per input text."""
    if not texts:
        return []
    _embedder = embedder or get_embedder()
    return _embedder.embed_documents(texts)