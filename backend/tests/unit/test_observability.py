"""Unit tests — tracing spans, capture-content flag, structlog context, Langfuse config warnings."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import pytest
import structlog
from app.config import Settings
from app.observability import configure_for_server, log_context
from app.services import observability


class _FakeObs:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kw: Any) -> None:
        self.updates.append(kw)


class _FakeClient:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.obs = _FakeObs()

    @contextmanager
    def start_as_current_observation(self, **kw: Any):
        self.started.append(kw)
        yield self.obs


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    import langfuse

    client = _FakeClient()
    monkeypatch.setattr(langfuse, "get_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _reset_capture() -> Any:
    before = observability._capture_content
    yield
    observability._capture_content = before


def test_span_opens_a_span_observation(fake_client: _FakeClient) -> None:
    with observability.span("stage", input=None, chunks=3):
        pass
    started = fake_client.started[0]
    assert started["as_type"] == "span" and started["name"] == "stage"
    assert started["metadata"] == {"chunks": 3}


def test_span_input_is_redacted_unless_capture_enabled(fake_client: _FakeClient) -> None:
    observability._capture_content = False
    with observability.span("chat", input="secret question"):
        pass
    assert fake_client.started[-1]["input"] != "secret question"

    observability._capture_content = True
    with observability.span("chat", input="secret question"):
        pass
    assert fake_client.started[-1]["input"] == "secret question"


def test_span_marks_error_and_reraises(fake_client: _FakeClient) -> None:
    with pytest.raises(ValueError, match="boom"), observability.span("stage"):
        raise ValueError("boom")
    err = fake_client.obs.updates[-1]
    assert err["level"] == "ERROR" and "ValueError: boom" in err["status_message"]


def test_span_fails_open_when_langfuse_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    import langfuse

    def boom() -> None:
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(langfuse, "get_client", boom)
    with observability.span("stage") as sp:
        sp.update(anything=1)  # null object accepts updates
    # and the body's own exceptions still propagate
    with pytest.raises(KeyError), observability.span("stage"):
        raise KeyError("x")


def test_span_is_a_noop_without_credentials() -> None:
    # conftest strips LANGFUSE_* env; the real client is then fully disabled and must not raise
    with observability.span("stage", input="q") as sp:
        sp.update(output="o")


def test_log_context_binds_and_unbinds() -> None:
    structlog.contextvars.clear_contextvars()
    with log_context(job_id="j1", doc_id="d1"):
        assert structlog.contextvars.get_contextvars() == {"job_id": "j1", "doc_id": "d1"}
        with log_context(extra=1):
            assert structlog.contextvars.get_contextvars()["extra"] == 1
    assert structlog.contextvars.get_contextvars() == {}


def test_server_logging_renders_bound_context_as_json(caplog: pytest.LogCaptureFixture) -> None:
    structlog.reset_defaults()
    configure_for_server(logging.INFO)
    with caplog.at_level(logging.INFO), log_context(job_id="job-123"):
        structlog.get_logger("t").info("hello_event", chunks=2)
    err = caplog.text
    assert '"job_id": "job-123"' in err and '"event": "hello_event"' in err and '"chunks": 2' in err


def test_langfuse_warnings_never_fail(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        s = Settings(
            _env_file=None, langfuse_public_key="wrong", langfuse_secret_key="", langfuse_host="not-a-url"
        )
    assert s.langfuse_public_key == "wrong"
    text = caplog.text
    assert "half-configured" in text and "not an http(s) URL" in text and "pk-" in text


def test_langfuse_no_warnings_when_unset_or_valid(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        Settings(_env_file=None)
        Settings(
            _env_file=None,
            langfuse_public_key="pk-lf-1",
            langfuse_secret_key="sk-lf-1",
            langfuse_host="https://cloud.langfuse.com",
        )
    assert "Langfuse" not in caplog.text and "LANGFUSE" not in caplog.text


def test_flush_calls_client_and_swallows_errors(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import langfuse

    flushed: list[int] = []
    fake_client.flush = lambda: flushed.append(1)  # type: ignore[attr-defined]
    observability.flush()
    assert flushed == [1]

    def boom() -> None:
        raise RuntimeError("down")

    monkeypatch.setattr(langfuse, "get_client", boom)
    observability.flush()  # must not raise
