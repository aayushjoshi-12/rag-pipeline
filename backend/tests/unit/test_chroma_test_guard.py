"""The conftest guard that keeps Chroma-using tests on the throwaway test server (ticket 50). No Chroma needed."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import chromadb
import pytest

HOST, PORT = os.environ["CHROMA_TEST_HOST"], int(os.environ["CHROMA_TEST_PORT"])


@pytest.fixture
def unreachable() -> Iterator[None]:
    probe: list[str | None] = chromadb.HttpClient.probe  # type: ignore[attr-defined]  # set by the conftest guard
    saved = list(probe)
    probe[:] = ["not reachable (test)"]
    yield
    probe[:] = saved


def test_dev_chroma_is_refused() -> None:
    with pytest.raises(pytest.fail.Exception, match="tests may only use"):
        chromadb.HttpClient(host="localhost", port=8001)


def test_unreachable_test_chroma_fails_loudly(
    unreachable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAG_TEST_ALLOW_NO_CHROMA", raising=False)
    with pytest.raises(pytest.fail.Exception, match="not reachable"):
        chromadb.HttpClient(host=HOST, port=PORT)


def test_unreachable_test_chroma_skips_only_when_the_flag_allows_it(
    unreachable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_TEST_ALLOW_NO_CHROMA", "1")
    with pytest.raises(pytest.skip.Exception, match="not reachable"):
        chromadb.HttpClient(host=HOST, port=PORT)


def test_settings_point_at_the_test_chroma_not_the_dev_default() -> None:
    from app.config import Settings

    cfg: Any = Settings()
    assert (cfg.chroma_host, cfg.chroma_port) == (HOST, PORT) and cfg.chroma_port != 8001
