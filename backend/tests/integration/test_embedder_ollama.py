"""Integration tests for the Ollama embedder against local Ollama.

Prereq: Ollama running with `mxbai-embed-large` pulled
(`ollama pull mxbai-embed-large`). Skipped automatically when Ollama is down.
These hit the real model (no mocks) — the CLAUDE.md "no real API calls" rule
targets external paid APIs; local compose services (Postgres/Chroma/Ollama) are
already exercised for real in the integration tier.
"""

from __future__ import annotations

import pytest
from app.config import Settings


def _ollama_up() -> bool:
    try:
        import httpx

        base = Settings().ollama_base_url
        return httpx.get(f"{base}/api/tags", timeout=3).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _ollama_up(), reason="Ollama is not reachable on localhost")
def test_ollama_mxbai_embed_returns_1024_dim() -> None:
    from app.services.embeddings import get_embedder

    embedder = get_embedder()
    vectors = embedder.embed_documents(
        ["Retrieval-Augmented Generation combines retrieval with generation."]
    )
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024