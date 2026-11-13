from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from app.config import (
    SECRET_FIELD_NAMES,
    EnvJsonSettingsSource,
    Settings,
    SettingsError,
)
from pydantic import ValidationError


@pytest.fixture()
def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("RAG_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_settings_defaults(clear_env: None) -> None:
    s = Settings()
    assert s.rag_env == "dev"
    assert s.log_level == "INFO"
    assert s.port == 8000
    assert s.max_upload_size_bytes == 25 * 1024 * 1024
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.embedding_provider == "ollama"
    assert s.embedding_model == "mxbai-embed-large"
    assert s.llm_provider == "openrouter"
    assert s.default_llm_model == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_settings_reject_secrets_in_json(tmp_path: Path) -> None:
    """Secret fields must never appear in config/config.<env>.json.

    The source raises SettingsError so a leaked key fails loudly at boot.
    """
    env_dir = tmp_path / "config"
    env_dir.mkdir()
    (env_dir / "config.prod.json").write_text(
        json.dumps({"OPENROUTER_API_KEY": "sk-or-leaked", "port": 9000})
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAG_ENV", "prod")
        mp.chdir(tmp_path)
        with pytest.raises(SettingsError):
            Settings()


def test_env_json_source_unknown_keys_ignored(tmp_path: Path) -> None:
    env_dir = tmp_path / "config"
    env_dir.mkdir()
    (env_dir / "config.dev.json").write_text(json.dumps({"bogus_key": 1, "port": 9000}))

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAG_ENV", "dev")
        mp.chdir(tmp_path)
        source = EnvJsonSettingsSource(Settings)
        values = source()
        assert "bogus_key" not in values
        assert values["port"] == 9000


def test_env_json_supports_lowercase_and_upper(tmp_path: Path) -> None:
    env_dir = tmp_path / "config"
    env_dir.mkdir()
    (env_dir / "config.dev.json").write_text(
        json.dumps({"llm_provider": "openrouter", "PORT": 9111})
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAG_ENV", "dev")
        mp.chdir(tmp_path)
        s = Settings(_env_file=None)
        assert s.llm_provider == "openrouter"
        assert s.port == 9111


def test_env_json_absent_when_no_rag_env() -> None:
    source = EnvJsonSettingsSource(Settings)
    assert source() == {}


def test_settings_unknown_provider_rejected() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LLM_PROVIDER", "bogus")
        with pytest.raises(ValidationError):
            Settings()


def test_settings_unknown_parser_provider_rejected() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PARSER_PROVIDER", "gemini")
        with pytest.raises(ValidationError, match="Unknown parser provider"):
            Settings()


def test_settings_parser_provider_accepts_ocr() -> None:
    assert Settings(parser_provider="ocr").parser_provider == "ocr"


def test_settings_parser_provider_accepts_openrouter() -> None:
    assert Settings(parser_provider="openrouter").parser_provider == "openrouter"


def test_secret_field_names_picklist_nonempty() -> None:
    assert "openrouter_api_key" in SECRET_FIELD_NAMES
    assert "database_url" in SECRET_FIELD_NAMES


def test_settings_customises_sources_includes_env_json(tmp_path: Path) -> None:
    # Behavioural check: when RAG_ENV is set and config/config.dev.json exists,
    # values flow into Settings (this proves EnvJsonSettingsSource is wired in).
    env_dir = tmp_path / "config"
    env_dir.mkdir()
    (env_dir / "config.dev.json").write_text(json.dumps({"port": 9443}))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAG_ENV", "dev")
        mp.chdir(tmp_path)
        assert Settings(_env_file=None).port == 9443


_ = ValidationError  # keep import used for tooling/type-awareness
