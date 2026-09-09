from __future__ import annotations

import json

import app.services.embeddings as emb
import pytest
import respx
from httpx import Response


def test_get_embedder_ollama_default() -> None:
    embedder = emb.get_embedder(provider="ollama", model="mxbai-embed-large")
    assert isinstance(embedder, emb.OllamaEmbedder)
    assert embedder.model == "mxbai-embed-large"
    assert embedder.base_url == "http://localhost:11434"


def test_get_embedder_ollama_uses_settings_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emb._settings, "ollama_base_url", "http://test:9999")
    embedder = emb.get_embedder(provider="ollama")
    assert embedder.base_url == "http://test:9999"


def test_get_embedder_openai_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emb._settings, "openai_api_key", "")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        emb.get_embedder(provider="openai", model="text-embedding-3-small")


def test_get_embedder_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emb._settings, "openai_api_key", "sk-test")
    embedder = emb.get_embedder(provider="openai", model="text-embedding-3-small")
    assert isinstance(embedder, emb.OpenAIEmbedder)
    assert embedder.model == "text-embedding-3-small"


def test_get_embedder_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        emb.get_embedder(provider="bogus")


@respx.mock
def test_ollama_embedder_posts_to_api_embed() -> None:
    route = respx.post("http://localhost:11434/api/embed").mock(
        return_value=Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    )

    embedder = emb.OllamaEmbedder(model="mxbai-embed-large", base_url="http://localhost:11434")
    vectors = embedder.embed_documents(["a", "b"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert route.called
    assert json.loads(route.calls.last.request.content) == {
        "model": "mxbai-embed-large",
        "input": ["a", "b"],
    }


@respx.mock
def test_ollama_embedder_embed_query_returns_single_vector() -> None:
    respx.post("http://localhost:11434/api/embed").mock(
        return_value=Response(200, json={"embeddings": [[0.5, 0.6]]})
    )

    embedder = emb.OllamaEmbedder(model="m", base_url="http://localhost:11434")
    assert embedder.embed_query("q") == [0.5, 0.6]


def test_embed_texts_returns_empty_for_empty_input() -> None:
    assert emb.embed_texts([]) == []


def test_embed_texts_uses_provided_embedder() -> None:
    class FakeEmbedder:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, text: str) -> list[float]:
            return [1.0, 0.0]

    out = emb.embed_texts(["a", "b"], embedder=FakeEmbedder())  # type: ignore[arg-type]
    assert out == [[1.0, 0.0], [1.0, 0.0]]


def test_embed_texts_default_embedder_calls_embed_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEmbedder:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(texts))] for _ in texts]

        def embed_query(self, text: str) -> list[float]:
            return [0.0]

    monkeypatch.setattr(emb, "get_embedder", lambda: FakeEmbedder())  # type: ignore[assignment]
    out = emb.embed_texts(["x", "y", "z"])
    assert out == [[3.0], [3.0], [3.0]]


def test_config_rejects_unknown_embedding_provider() -> None:
    from app.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Unknown embedding provider"):
        Settings(embedding_provider="bogus")