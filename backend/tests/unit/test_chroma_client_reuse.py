"""One Chroma HTTP client per server for the process (a client per request leaked ~1.5 MB each, ticket 37)."""

from __future__ import annotations

from typing import Any

import chromadb
import pytest
from app.services import vectorstore as vs


class _FakeClient:
    created = 0

    def __init__(self, host: str, port: int) -> None:
        type(self).created += 1

    def get_or_create_collection(self, name: str, configuration: Any) -> object:
        return object()


def test_stores_share_one_client_per_server(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.created = 0
    monkeypatch.setattr(chromadb, "HttpClient", _FakeClient)
    monkeypatch.setattr(vs, "_chroma_clients", {})
    a = vs.ChromaStore(host="h1", port=1, collection_name="c")
    b = vs.ChromaStore(host="h1", port=1, collection_name="c")
    c = vs.ChromaStore(host="h2", port=1, collection_name="c")
    assert _FakeClient.created == 2  # h1 once, h2 once
    assert a._client is b._client and a._client is not c._client
    assert a._collection is not b._collection  # the collection handle is still fetched per store
