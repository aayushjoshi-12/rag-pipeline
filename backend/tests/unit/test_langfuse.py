"""Unit tests — app-level Langfuse wiring (`app.services.langfuse`)."""

from __future__ import annotations

import pytest
from app.config import Settings
from app.services import langfuse


def test_configure_with_creds_exports_env_and_enables_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    langfuse.configure_from_settings(
        Settings(
            _env_file=None,
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            langfuse_host="https://us.cloud.langfuse.com",
            langfuse_capture_content=True,
        )
    )

    import os

    from app.services import observability

    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-test"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-test"
    assert os.environ["LANGFUSE_HOST"] == "https://us.cloud.langfuse.com"
    assert observability.capture_enabled() is True


def test_configure_without_creds_clears_env_and_disables_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    langfuse.configure_from_settings(Settings(_env_file=None))

    # Pre-pollute the environment AFTER construction — Settings would otherwise re-read
    # the leftovers straight back into its fields.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-leftover")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-leftover")
    monkeypatch.setenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com")
    from app.services import observability

    observability._capture_content = True

    langfuse.configure_from_settings(
        Settings(
            _env_file=None,
            langfuse_public_key="",
            langfuse_secret_key="",
            langfuse_host="",
            langfuse_capture_content=False,
        )
    )

    import os

    assert "LANGFUSE_PUBLIC_KEY" not in os.environ
    assert "LANGFUSE_SECRET_KEY" not in os.environ
    assert "LANGFUSE_HOST" not in os.environ
    assert observability.capture_enabled() is False
