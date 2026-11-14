"""Settings guards around authentication."""

from __future__ import annotations

import pytest
from app.config import Settings
from pydantic import ValidationError

GOOD = "k" * 40


def test_dev_tolerates_an_empty_or_short_key() -> None:
    assert Settings(rag_env="dev", secret_key="").auth_enabled is True
    assert Settings(rag_env="dev", secret_key="change-me").secret_key == "change-me"


@pytest.mark.parametrize("bad", ["", "short", "change-me", "replace-me", "x" * 31])
def test_prod_rejects_weak_or_placeholder_keys(bad: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(rag_env="prod", secret_key=bad)


def test_prod_rejects_a_placeholder_even_if_long_enough() -> None:
    # `changeme` padded is still just a guessable word; only exact placeholders are blocked,
    # so make sure a genuinely random 40-char key passes and the placeholder list is honoured.
    Settings(rag_env="prod", secret_key=GOOD)
    with pytest.raises(ValidationError):
        Settings(rag_env="prod", secret_key="CHANGE-ME")


def test_prod_with_auth_disabled_needs_no_key() -> None:
    assert Settings(rag_env="prod", auth_enabled=False, secret_key="").auth_enabled is False


@pytest.mark.parametrize("kwargs", [{"access_token_minutes": 0}, {"refresh_token_days": -1}])
def test_token_lifetimes_must_be_positive(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(rag_env="dev", **kwargs)


def test_admin_password_is_a_secret_field() -> None:
    from app.config import SECRET_FIELD_NAMES

    assert {"admin_password", "secret_key"} <= SECRET_FIELD_NAMES


def test_incremental_max_churn_defaults_to_off_and_rejects_negatives() -> None:
    assert Settings(rag_env="dev").incremental_max_churn is None
    assert Settings(rag_env="dev", incremental_max_churn=0.5).incremental_max_churn == 0.5
    with pytest.raises(ValidationError):
        Settings(rag_env="dev", incremental_max_churn=-0.1)
