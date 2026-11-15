"""The admin-UI settings layer: precedence, registry completeness, crypto, validation (ticket 43)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from app import config
from app.config import SECRET_FIELD_NAMES, Settings, db_overrides_as, env_pin
from app.services import runtime_settings as rt
from app.services import settings_registry as reg
from app.services.settings_crypto import CryptoError, SettingsCipher
from app.services.settings_service import build_view

KEY_A = "a" * 40
KEY_B = "b" * 40


@pytest.fixture(autouse=True)
def _clean_layer() -> Any:
    rt.reset_for_tests()
    yield
    rt.reset_for_tests()


# ── precedence: env > ui > config.json > default ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "ui", "cfg_file"), list(itertools.product([None, 11], [None, 22], [None, 33]))
)
def test_precedence_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: int | None,
    ui: int | None,
    cfg_file: int | None,
) -> None:
    monkeypatch.setattr(config, "_CONFIG_DIR", tmp_path)
    monkeypatch.setenv("RAG_ENV", "dev")
    monkeypatch.delenv("DEFAULT_TOP_K", raising=False)
    if cfg_file is not None:
        (tmp_path / "config.dev.json").write_text(json.dumps({"DEFAULT_TOP_K": cfg_file}))
    if env is not None:
        monkeypatch.setenv("DEFAULT_TOP_K", str(env))
    values = {} if ui is None else {"default_top_k": ui}
    with db_overrides_as(values):
        got = Settings().default_top_k
    expected = next(v for v in (env, ui, cfg_file, 5) if v is not None)
    assert got == expected
    if env is not None:
        assert env_pin("default_top_k") == "DEFAULT_TOP_K"


def test_view_reports_the_winning_layer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "_CONFIG_DIR", tmp_path)
    monkeypatch.setenv("RAG_ENV", "dev")
    (tmp_path / "config.dev.json").write_text(json.dumps({"CHUNK_SIZE": 500}))
    monkeypatch.setenv("RETRIEVAL_NEIGHBORS", "2")
    config.set_db_overrides({"default_top_k": 9, "retrieval_neighbors": 4})
    by_key = {s["key"]: s for s in build_view()["settings"]}
    assert by_key["retrieval_neighbors"]["source"] == "env"
    assert by_key["retrieval_neighbors"]["locked"] is True
    assert "RETRIEVAL_NEIGHBORS" in by_key["retrieval_neighbors"]["locked_reason"]
    assert by_key["retrieval_neighbors"]["shadowed"] is True
    assert by_key["default_top_k"]["source"] == "ui"
    assert by_key["default_top_k"]["value"] == 9
    assert by_key["chunk_size"]["source"] == "config"
    assert by_key["chunk_size"]["value"] == 500
    assert by_key["retrieval_rrf_k"]["source"] == "default"
    assert by_key["retrieval_rrf_k"]["default"] == 60


def test_ui_layer_ignores_unknown_keys() -> None:
    with db_overrides_as({"not_a_setting": 1, "default_top_k": 7}):
        assert Settings().default_top_k == 7


# ── registry completeness ────────────────────────────────────────────────────────────────────────


def test_every_settings_field_is_classified() -> None:
    fields = set(Settings.model_fields)
    assert fields - set(reg.REGISTRY) == set(), (
        "classify new Settings fields in settings_registry.REGISTRY"
    )
    assert set(reg.REGISTRY) - fields == set(), "registry lists a field Settings does not have"


def test_registry_entries_are_coherent() -> None:
    groups = {name for name, _ in reg.GROUPS}
    for key, spec in reg.REGISTRY.items():
        assert spec.group in groups, key
        assert spec.description, key
        if spec.apply == "bootstrap":
            assert spec.reason, f"{key}: a bootstrap setting must say why"
        kind, _ = reg.field_kind(key)
        assert kind in ("string", "text", "integer", "number", "boolean", "enum", "path"), key
    assert {k for k in SECRET_FIELD_NAMES} <= {k for k, s in reg.REGISTRY.items() if s.secret}
    # the settings that cannot come from the database they would open stay out of the UI
    for key in (
        "database_url",
        "celery_broker_url",
        "celery_result_backend",
        "redis_url",
        "secret_key",
        "settings_encryption_key",
        "auth_enabled",
        "url_ingest_allow_private",
        "tesseract_cmd",
    ):
        assert reg.REGISTRY[key].apply == "bootstrap", key
    assert reg.REGISTRY["embedding_model"].apply == "reindex"
    assert reg.REGISTRY["retrieval_fts_config"].reindex_action == "rebuild_fts"
    assert reg.REGISTRY["default_top_k"].apply == "live"


def test_defaults_come_from_the_model() -> None:
    assert reg.default_of("default_top_k") == Settings.model_fields["default_top_k"].default
    assert reg.default_of("data_dir") == "data"


# ── value validation ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value", "ok"),
    [
        ("default_top_k", 8, True),
        ("default_top_k", "8", True),
        ("default_top_k", True, False),
        ("default_top_k", None, False),
        ("cache_enabled", "yes", False),
        ("cache_enabled", False, True),
        ("retrieval_mode", "hybrid", True),
        ("retrieval_mode", "wat", False),
        ("vector_metric", None, True),
        ("vector_metric", "euclid", False),
        ("default_llm_model", 5, False),
        ("ollama_base_url", "http://user:hunter2@host:1", False),
        ("ollama_base_url", "http://host:11434", True),
        ("llm_price_input_per_mtok", 0.5, True),
        ("llm_price_input_per_mtok", "abc", False),
    ],
)
def test_coerce(key: str, value: Any, ok: bool) -> None:
    if ok:
        reg.coerce(key, value)
    else:
        with pytest.raises(reg.ValueError_):
            reg.coerce(key, value)


def test_cross_field_validation_uses_the_real_model_validators() -> None:
    problems = rt.validate_candidate({"chunk_size": 100, "chunk_overlap": 500})
    assert problems
    assert (
        "chunk_size" in "".join(problems.values())
        or None in problems
        or "chunk_overlap" in problems
    )
    assert rt.validate_candidate({"chunk_size": 800, "chunk_overlap": 50}) == {}


def test_validation_messages_never_echo_input() -> None:
    problems = rt.validate_candidate({"default_top_k": "hunter2-not-a-number"})
    assert problems
    assert "hunter2" not in json.dumps(problems)


def test_secret_value_rules() -> None:
    assert reg.coerce_secret("  sk-abc  ") == "sk-abc"
    for bad in ("", "   ", "a\nb", None, 5):
        with pytest.raises(reg.ValueError_):
            reg.coerce_secret(bad)


def test_scrub_hides_url_credentials() -> None:
    assert reg.scrub("redis://u:pw@h:6379/1") == "redis://***@h:6379/1"
    assert reg.scrub("http://h:1") == "http://h:1"


# ── encryption ───────────────────────────────────────────────────────────────────────────────────


def test_encryption_round_trip_and_nothing_readable() -> None:
    cipher = SettingsCipher(KEY_A)
    blob = cipher.encrypt("openai_api_key", "sk-very-secret-canary")
    assert "sk-very-secret-canary" not in blob
    assert cipher.decrypt("openai_api_key", blob) == "sk-very-secret-canary"
    assert cipher.encrypt("openai_api_key", "x") != cipher.encrypt(
        "openai_api_key", "x"
    )  # random IV


def test_ciphertext_is_bound_to_its_row() -> None:
    cipher = SettingsCipher(KEY_A)
    blob = cipher.encrypt("openai_api_key", "sk-1")
    with pytest.raises(CryptoError):
        cipher.decrypt("anthropic_api_key", blob)


def test_wrong_key_and_tampering_fail_closed() -> None:
    blob = SettingsCipher(KEY_A).encrypt("openai_api_key", "sk-1")
    with pytest.raises(CryptoError):
        SettingsCipher(KEY_B).decrypt("openai_api_key", blob)
    with pytest.raises(CryptoError):
        SettingsCipher(KEY_A).decrypt("openai_api_key", blob[:-4] + "AAAA")


def test_missing_key_disables_encryption() -> None:
    cipher = SettingsCipher("")
    assert cipher.enabled is False
    with pytest.raises(CryptoError):
        cipher.encrypt("openai_api_key", "x")
    with pytest.raises(CryptoError):
        cipher.decrypt("openai_api_key", "x")


def test_short_key_rejected() -> None:
    with pytest.raises(CryptoError):
        SettingsCipher("too-short")


def test_rotation_with_previous_key() -> None:
    old = SettingsCipher(KEY_A)
    blob = old.encrypt("openai_api_key", "sk-1")
    new = SettingsCipher(KEY_B, previous=KEY_A)
    assert new.decrypt("openai_api_key", blob) == "sk-1"  # still readable during rotation
    rotated = new.rotate("openai_api_key", blob)
    assert (
        SettingsCipher(KEY_B).decrypt("openai_api_key", rotated) == "sk-1"
    )  # old key no longer needed
    with pytest.raises(CryptoError):
        old.decrypt("openai_api_key", rotated)


def test_empty_env_var_pins_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker-compose passes `${VAR:-}` as an empty string: that must not lock the key or beat a lower layer."""
    monkeypatch.setenv("RAG_ENV", "dev")
    monkeypatch.setenv("DEFAULT_TOP_K", "")
    assert env_pin("default_top_k") is None
    assert Settings().default_top_k == Settings.model_fields["default_top_k"].default
